"""Expected-points (xP) baseline for FPL Draft.

For every player and every fixture in the horizon we estimate:
  * availability (injury news / suspension / loan rules)
  * P(start), P(60+ mins) and expected minutes from recent matches
  * team goal expectancy for both sides, from bookmaker match odds when published,
    otherwise from xG / goals strength ratings (recency weighted)
  * goals, assists, clean sheet, goals conceded, saves, bonus, defensive
    contribution and card points using the league's own scoring settings
  * rotation risk around European fixtures and extra uncertainty after a manager change
  * a Monte Carlo range (10th-90th percentile) for the horizon total

Every season figure is rebuilt from match-by-match data, so the model can be replayed
as of any past gameweek (``cutoff``) for backtesting. Scout intel nudges availability,
start probability, penalty duty or output.
"""
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
# per-90 priors used to regress small samples (xG, xA)
PRIOR90 = {1: (0.0, 0.01), 2: (0.04, 0.05), 3: (0.12, 0.12), 4: (0.30, 0.08)}
DC_PRIOR = {1: 0.0, 2: 0.30, 3: 0.15, 4: 0.05}
PEN_XG_PER_MATCH = 0.08   # league-average penalty xG per team per match

DEFAULT_PARAMS = {
    "home_adv": 1.10, "away_adv": 0.92,
    "prior_mins": 450,          # shrink per-90 rates towards the positional prior
    "strength_shrink": 4.0,     # games of "average team" added to team strength
    "xg_weight": 0.65,          # xG vs actual goals in team strength
    "recent_weight": 0.3,       # weight on last-6-match per-90 rates vs season
    "recent_team_weight": 0.3,  # weight on last-6-match team strength vs season
    "euro_rotation": 0.88,      # start-probability factor near a European match
    "new_manager_blend": 0.25,  # pull start probability towards 55% after a manager change
    "cs_scale": 1.0,            # <1 = more clean sheets than a plain Poisson model implies
}

FD_ALIASES = {
    "man united": "MUN", "man utd": "MUN", "man city": "MCI", "nott'm forest": "NFO",
    "nottingham forest": "NFO", "tottenham": "TOT", "spurs": "TOT", "coventry": "COV",
    "hull": "HUL", "ipswich": "IPS", "leeds": "LEE", "wolves": "WOL", "newcastle": "NEW",
    "brighton": "BHA", "west ham": "WHU", "sheffield united": "SHU", "sunderland": "SUN",
}


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def e_floor_div(lam, n, kmax=15):
    """E[floor(X/n)] for X ~ Poisson(lam)."""
    return sum(pois(k, lam) * (k // n) for k in range(kmax))


def fit_lambdas(ph, pd, pa, po25=None):
    """Find (home, away) goal expectancies matching odds-implied probabilities."""
    best, best_err = (1.4, 1.1), 9e9
    for i in range(4, 71):
        lh = i * 0.05
        ph_k = [pois(k, lh) for k in range(11)]
        for j in range(4, 71):
            la = j * 0.05
            pa_k = [pois(k, la) for k in range(11)]
            hw = dr = 0.0
            for x in range(11):
                for y in range(11):
                    p = ph_k[x] * pa_k[y]
                    if x > y:
                        hw += p
                    elif x == y:
                        dr += p
            aw = 1 - hw - dr
            err = (hw - ph) ** 2 + (dr - pd) ** 2 + (aw - pa) ** 2
            if po25 is not None:
                p_over = 1 - sum(pois(k, lh + la) for k in range(3))
                err += (p_over - po25) ** 2
            if err < best_err:
                best, best_err = (lh, la), err
    return best


class Model:
    def __init__(self, data, cfg, intel_effects=None, cutoff=None, params=None):
        """cutoff: replay as if gameweek `cutoff` were next (backtesting)."""
        self.d = data
        self.cfg = cfg
        self.intel = intel_effects or {}
        self.P = dict(DEFAULT_PARAMS)
        self.P.update(cfg.get("model_params", {}))
        if params:
            self.P.update(params)
        b = data["boot"]
        self.scoring = b["settings"]["scoring"]
        self.teams = {t["id"]: t for t in b["teams"]}
        self.team_by_short = {t["short_name"]: t["id"] for t in b["teams"]}
        self.elements = {e["id"]: e for e in b["elements"]}
        self.classic_by_code = {e["code"]: e for e in data["classic_boot"]["elements"]}
        self.events = {e["id"]: e for e in b["events"]["data"]}
        self.backtest = cutoff is not None
        self.next_gw = cutoff if cutoff else data["game"]["next_event"]
        self.last_done = self.next_gw - 1 if cutoff else data["last_finished_gw"]
        self.horizon = list(range(self.next_gw, min(self.next_gw + cfg["horizon_gameweeks"], 39)))
        if cutoff:
            self.horizon = [cutoff]
        self.fixtures = data["fixtures"]
        self.euro = defaultdict(list)
        for m in cfg.get("european_matches", []):
            t = self.team_by_short.get(m["club"])
            if t:
                self.euro[t].append(datetime.fromisoformat(m["date"]).replace(tzinfo=timezone.utc))
        self.new_mgr = {self.team_by_short[m["club"]]: m["since_gw"] for m in cfg.get("new_managers", [])
                        if m["club"] in self.team_by_short}
        self._team_history()
        self._season_totals()
        self._strengths()
        self._fixture_lambdas()

    # ------------------------------------------------------------------ teams
    def _team_history(self):
        self.team_gws = defaultdict(list)          # team -> [gw,...] finished before cutoff
        self.opp = {}                               # (gw, team) -> [(opp, home)]
        self.team_goals = defaultdict(dict)         # team -> gw -> (gf, ga)
        for f in self.fixtures:
            gw = f.get("event")
            if not f.get("finished") or not gw or gw > self.last_done:
                continue
            h, a = f["team_h"], f["team_a"]
            hs, as_ = f["team_h_score"] or 0, f["team_a_score"] or 0
            for t, o, home, gf, ga in ((h, a, True, hs, as_), (a, h, False, as_, hs)):
                self.team_gws[t].append(gw)
                self.opp.setdefault((gw, t), []).append((o, home))
                pg = self.team_goals[t].get(gw, (0, 0))
                self.team_goals[t][gw] = (pg[0] + gf, pg[1] + ga)

        self.pstats = defaultdict(dict)             # pid -> gw -> stats (finished, before cutoff)
        self.actual = defaultdict(dict)             # pid -> gw -> stats (all, for scoring)
        team_gw_xg = defaultdict(float)
        for gw, live in self.d["live"].items():
            gw = int(gw)
            for pid, rec in live["elements"].items():
                pid = int(pid)
                st = rec["stats"]
                self.actual[pid][gw] = st
                if gw > self.last_done:
                    continue
                self.pstats[pid][gw] = st
                el = self.elements.get(pid)
                if el:
                    team_gw_xg[(gw, el["team"])] += fnum(st.get("expected_goals"))
        self.team_xg = defaultdict(dict)            # team -> gw -> (xgf, xga)
        for (gw, t), opps in self.opp.items():
            xga = sum(team_gw_xg[(gw, o)] for o, _ in opps)
            self.team_xg[t][gw] = (team_gw_xg[(gw, t)], xga)

    def _season_totals(self):
        keys = ("minutes", "goals_scored", "assists", "bonus", "yellow_cards", "saves", "starts")
        self.tot = {}
        self.recent = {}
        for pid, el in self.elements.items():
            gws = self.pstats.get(pid, {})
            t = {k: sum(st.get(k, 0) for st in gws.values()) for k in keys}
            t["xg"] = sum(fnum(st.get("expected_goals")) for st in gws.values())
            t["xa"] = sum(fnum(st.get("expected_assists")) for st in gws.values())
            t["points"] = sum(st.get("total_points", 0) for st in gws.values())
            self.tot[pid] = t
            last = sorted(self.team_gws[el["team"]])[-6:]
            r = {"minutes": sum(gws.get(g, {}).get("minutes", 0) for g in last),
                 "xg": sum(fnum(gws.get(g, {}).get("expected_goals")) for g in last),
                 "xa": sum(fnum(gws.get(g, {}).get("expected_assists")) for g in last)}
            self.recent[pid] = r

    def _strengths(self):
        P = self.P
        tot_goals = sum(gf for t in self.team_goals.values() for gf, _ in t.values())
        tot_games = sum(len(v) for v in self.team_gws.values()) or 1
        self.avg = max(0.9, tot_goals / tot_games) if tot_goals else 1.35
        k, w = P["strength_shrink"], P["xg_weight"]
        self.att, self.dfn = {}, {}
        for t in self.teams:
            gws = sorted(self.team_gws[t])

            def rate(sel):
                n = len(sel)
                if not n:
                    return 1.0, 1.0, 0
                xf = sum(self.team_xg[t].get(g, (0, 0))[0] for g in sel)
                xa = sum(self.team_xg[t].get(g, (0, 0))[1] for g in sel)
                gf = sum(self.team_goals[t].get(g, (0, 0))[0] for g in sel)
                ga = sum(self.team_goals[t].get(g, (0, 0))[1] for g in sel)
                return (w * xf + (1 - w) * gf) / n / self.avg, (w * xa + (1 - w) * ga) / n / self.avg, n

            sa, sd, n = rate(gws)
            ra, rd, nr = rate(gws[-6:])
            rw = P["recent_team_weight"] if nr >= 4 else 0
            af, dg = (1 - rw) * sa + rw * ra, (1 - rw) * sd + rw * rd
            self.att[t] = (n * af + k) / (n + k)
            self.dfn[t] = (n * dg + k) / (n + k)   # >1 = leaky

    def _team_code(self, name):
        n = name.strip().lower()
        if n in FD_ALIASES:
            return self.team_by_short.get(FD_ALIASES[n])
        for t in self.teams.values():
            if t["name"].lower() == n or t["short_name"].lower() == n:
                return t["id"]
            if t["name"].lower().startswith(n) or n.startswith(t["name"].lower()):
                return t["id"]
        return None

    def _fixture_lambdas(self):
        odds_idx = {}
        if not self.backtest:
            for o in self.d.get("odds", []):
                h, a = self._team_code(o["home"]), self._team_code(o["away"])
                if h and a:
                    odds_idx[(h, a)] = o
        self.fix = {}
        self.team_fix = defaultdict(list)   # (gw, team) -> [fixture dicts]
        last_gw = self.next_gw + (0 if self.backtest else self.cfg.get("dgw_lookahead_gameweeks", 10))
        for f in self.fixtures:
            gw = f.get("event")
            if not gw or gw < self.next_gw or gw > last_gw:
                continue
            h, a = f["team_h"], f["team_a"]
            lh = self.avg * self.att[h] * self.dfn[a] * self.P["home_adv"]
            la = self.avg * self.att[a] * self.dfn[h] * self.P["away_adv"]
            src = "model"
            o = odds_idx.get((h, a))
            if o and gw in self.horizon:
                inv = [1 / o["h"], 1 / o["d"], 1 / o["a"]]
                s = sum(inv)
                po = None
                if o.get("o25") and o.get("u25"):
                    io, iu = 1 / o["o25"], 1 / o["u25"]
                    po = io / (io + iu)
                oh, oa = fit_lambdas(inv[0] / s, inv[1] / s, inv[2] / s, po)
                lh, la = 0.7 * oh + 0.3 * lh, 0.7 * oa + 0.3 * la
                src = "odds"
            ko = f.get("kickoff_time")
            fx = {"id": f["id"], "gw": gw, "h": h, "a": a, "lh": lh, "la": la, "src": src,
                  "kickoff": ko, "fdr_h": f.get("team_h_difficulty"), "fdr_a": f.get("team_a_difficulty")}
            self.fix[f["id"]] = fx
            self.team_fix[(gw, h)].append(fx)
            self.team_fix[(gw, a)].append(fx)

    def euro_clash(self, team, kickoff):
        if not kickoff or not self.euro.get(team):
            return False
        ko = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
        return any(abs((d - ko).total_seconds()) <= 3.5 * 86400 for d in self.euro[team])

    def team_outlook(self, t):
        out = []
        for gw in self.horizon:
            for fx in self.team_fix.get((gw, t), []):
                home = fx["h"] == t
                lf, la = (fx["lh"], fx["la"]) if home else (fx["la"], fx["lh"])
                out.append({"gw": gw, "opp": self.teams[fx["a"] if home else fx["h"]]["short_name"],
                            "home": home, "xg_for": round(lf, 2), "xg_against": round(la, 2),
                            "cs": round(math.exp(-la * self.P["cs_scale"]), 2), "src": fx["src"],
                            "fdr": fx["fdr_h"] if home else fx["fdr_a"],
                            "euro": self.euro_clash(t, fx["kickoff"])})
        return out

    def gw_flags(self):
        flags = []
        last_gw = self.next_gw + self.cfg.get("dgw_lookahead_gameweeks", 10)
        for gw in range(self.next_gw, min(last_gw, 38) + 1):
            dbl = [self.teams[t]["short_name"] for t in self.teams if len(self.team_fix.get((gw, t), [])) > 1]
            blank = [self.teams[t]["short_name"] for t in self.teams if len(self.team_fix.get((gw, t), [])) == 0]
            if dbl or blank:
                flags.append({"gw": gw, "double": dbl, "blank": blank})
        return flags

    # ---------------------------------------------------------------- players
    def availability(self, el, k, gw):
        if self.backtest:
            return 1.0      # historical injury news isn't available
        status = el.get("status", "a")
        cop = el.get("chance_of_playing_next_round")
        cop = (1.0 if status == "a" else (0.0 if status in "isun" else 0.5)) if cop is None else cop / 100
        if status == "u":
            a = 0.0
        elif status in "ns":
            a = 0.0 if k == 0 else 1.0
        elif status in "id":
            ret = self.return_date(el)
            if ret:
                dl = self.events.get(gw, {}).get("deadline_time")
                dl = datetime.fromisoformat(dl.replace("Z", "+00:00")) if dl else None
                a = 1.0 if (dl and dl >= ret) else cop
            else:
                rate = 0.12 if "unknown return" in (el.get("news") or "").lower() else 0.35
                a = min(1.0, cop + (1 - cop) * rate * k)
        else:
            a = cop
        ce = self.classic_by_code.get(el["code"], {})
        for r in ce.get("scout_risks") or []:
            if r.get("property") == "loan_ineligible" and r.get("gameweek") == gw:
                a = 0.0
        return a

    def return_date(self, el):
        m = re.search(r"Expected back (\d{1,2} \w{3})", el.get("news") or "")
        if not m:
            return None
        now = datetime.now(timezone.utc)
        try:
            d = datetime.strptime(f"{m.group(1)} {now.year}", "%d %b %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        if d < now - timedelta(days=60):
            d = d.replace(year=now.year + 1)
        return d

    def long_term_out(self, el):
        if el.get("status") not in ("i", "u"):
            return False
        news = (el.get("news") or "").lower()
        if "unknown return" in news or el.get("status") == "u":
            return True
        ret = self.return_date(el)
        if ret:
            return ret > datetime.now(timezone.utc) + timedelta(weeks=self.cfg.get("lock_release_if_out_gameweeks", 4))
        return False

    def minutes_profile(self, pid, team):
        gws = sorted(self.team_gws[team])[-6:]
        weights = [1, 1, 1.5, 1.5, 2, 2][-len(gws):] if gws else []
        sw = sum(weights) or 1
        start = sub = 0.0
        smins, sub_mins, n60, nst = [], [], 0, 0
        for w, gw in zip(weights, gws):
            st = self.pstats[pid].get(gw)
            if not st:
                continue
            m = st.get("minutes", 0)
            if st.get("starts", 0) >= 1:
                start += w
                nst += 1
                smins.append(min(90, m))
                n60 += m >= 60
            elif m > 0:
                sub += w
                sub_mins.append(m)
        return {"p_start": start / sw if gws else 0.0, "p_sub": sub / sw if gws else 0.0,
                "mins_start": sum(smins) / len(smins) if smins else 80.0,
                "p60_start": (n60 + 0.85) / (nst + 1),
                "mins_sub": sum(sub_mins) / len(sub_mins) if sub_mins else 20.0}

    def rates(self, pid, el):
        pos = el["element_type"]
        t, rc = self.tot[pid], self.recent[pid]
        pm = self.P["prior_mins"]
        pxg, pxa = PRIOR90[pos]
        denom = (t["minutes"] + pm) / 90
        xg_s = (0.8 * t["xg"] + 0.2 * t["goals_scored"] + pxg * pm / 90) / denom
        xa_s = (0.8 * t["xa"] + 0.2 * t["assists"] + pxa * pm / 90) / denom
        rw = self.P["recent_weight"] if rc["minutes"] >= 180 else 0
        rden = (rc["minutes"] + pm) / 90
        xg_r = (rc["xg"] + pxg * pm / 90) / rden
        xa_r = (rc["xa"] + pxa * pm / 90) / rden
        r = {
            "xg90": (1 - rw) * xg_s + rw * xg_r,
            "xa90": (1 - rw) * xa_s + rw * xa_r,
            "bonus90": (t["bonus"] + (0.3 if pos == 1 else 0.15) * pm / 90) / denom,
            "yc90": (t["yellow_cards"] + 0.1 * pm / 90) / denom,
            "saves90": (t["saves"] + 2.8 * pm / 90) / denom if pos == 1 else 0,
        }
        thr = self.scoring.get(f"defensive_contribution_limit_{POS[pos]}", 0)
        hits = games = 0
        if thr:
            for st in self.pstats[pid].values():
                if st.get("minutes", 0) >= 60:
                    games += 1
                    hits += st.get("defensive_contribution", 0) >= thr
        r["p_dc"] = (hits + DC_PRIOR[pos] * 3) / (games + 3) if thr else 0
        return r

    def fixture_components(self, pid, force_avail=False):
        """Per gameweek, per fixture inputs to the points model (shared by xP and simulation)."""
        el = self.elements[pid]
        pos, team = el["element_type"], el["team"]
        prof = self.minutes_profile(pid, team)
        r = self.rates(pid, el)
        ce = self.classic_by_code.get(el["code"], {})
        is_taker = ce.get("penalties_order") == 1
        eff = self.intel.get(pid, [])
        out = []
        for k, gw in enumerate(self.horizon):
            avail = 1.0 if force_avail else self.availability(el, k, gw)
            p_start_raw = prof["p_start"]
            mult, pen = 1.0, 0.0
            for e in eff:
                if e.get("gameweeks") and gw not in e["gameweeks"]:
                    continue
                w = e["weight"]
                if "avail" in e and not force_avail:
                    avail = avail + w * (e["avail"] - avail)
                if "start_prob" in e:
                    p_start_raw = p_start_raw + w * (e["start_prob"] - p_start_raw)
                if "xp_multiplier" in e:
                    mult *= 1 + w * (e["xp_multiplier"] - 1)
                if "penalty_taker" in e:
                    if e["penalty_taker"] and not is_taker:
                        pen += w * PEN_XG_PER_MATCH
                    elif not e["penalty_taker"] and is_taker:
                        pen -= w * PEN_XG_PER_MATCH
            if team in self.new_mgr and self.new_mgr[team] <= gw < self.new_mgr[team] + 3:
                b = self.P["new_manager_blend"]
                p_start_raw = (1 - b) * p_start_raw + b * 0.55
            fxs = []
            for fx in self.team_fix.get((gw, team), []):
                home = fx["h"] == team
                lf, la = (fx["lh"], fx["la"]) if home else (fx["la"], fx["lh"])
                ps = p_start_raw * (self.P["euro_rotation"] if self.euro_clash(team, fx["kickoff"]) else 1.0)
                p_start = avail * ps
                p_subp = avail * min(prof["p_sub"], 1 - ps)
                att_ratio = lf / (self.avg * self.att[team])
                fxs.append({
                    "opp": self.teams[fx["a"] if home else fx["h"]]["short_name"], "home": home,
                    "lf": lf, "la": la, "p_start": p_start, "p_subp": p_subp,
                    "mins_start": prof["mins_start"], "mins_sub": prof["mins_sub"], "p60_start": prof["p60_start"],
                    "g90": max(0.0, r["xg90"] * att_ratio + pen), "a90": r["xa90"] * att_ratio,
                    "att_ratio": att_ratio, "shots_ratio": la / (self.avg * self.dfn[team]),
                    "r": r, "mult": mult, "euro": self.euro_clash(team, fx["kickoff"]),
                })
            out.append({"gw": gw, "fixtures": fxs})
        return out

    def expected(self, pos, c):
        """Expected points and parts for one fixture's components."""
        P_ = POS[pos]
        sc = self.scoring
        r = c["r"]
        p_play = c["p_start"] + c["p_subp"]
        p60 = c["p_start"] * c["p60_start"]
        e_mins = c["p_start"] * c["mins_start"] + c["p_subp"] * c["mins_sub"]
        mfrac = e_mins / 90
        e_g, e_a = c["g90"] * mfrac, c["a90"] * mfrac
        p_cs = math.exp(-c["la"] * self.P["cs_scale"])
        xp = p60 * sc["long_play"] + (p_play - p60) * sc["short_play"]
        xp += e_g * sc[f"goals_scored_{P_}"] + e_a * sc["assists"]
        xp += p60 * p_cs * sc[f"clean_sheets_{P_}"]
        if sc[f"goals_conceded_{P_}"]:
            xp += sc[f"goals_conceded_{P_}"] * e_floor_div(c["la"] * min(1, c["mins_start"] / 90), sc["concede_limit"]) * c["p_start"]
        if pos == 1:
            xp += sc["saves"] * e_floor_div(r["saves90"] * c["shots_ratio"], sc["saves_limit"]) * c["p_start"]
        bonus_mod = (0.5 + 0.5 * c["att_ratio"]) if pos >= 3 else (0.5 + 0.5 * p_cs / 0.3)
        xp += r["bonus90"] * mfrac * min(1.8, bonus_mod)
        xp += r["p_dc"] * p60 * sc.get(f"defensive_contribution_{P_}", 0)
        xp += r["yc90"] * mfrac * sc["yellow_cards"]
        return xp * c["mult"], e_g, e_a, e_mins, p_cs

    def simulate(self, pid, comps, n=2000, seed=None):
        """Monte Carlo total points over the horizon; returns (p10, p50, p90)."""
        el = self.elements[pid]
        pos, P_ = el["element_type"], POS[el["element_type"]]
        sc = self.scoring
        rng = np.random.default_rng(seed if seed is not None else pid)
        total = np.zeros(n)
        for g in comps:
            for c in g["fixtures"]:
                r = c["r"]
                u = rng.random(n)
                start = u < c["p_start"]
                sub = (~start) & (u < c["p_start"] + c["p_subp"])
                full = start & (rng.random(n) < c["p60_start"])
                mins = np.where(full, np.maximum(c["mins_start"], 60), np.where(start, 50, np.where(sub, c["mins_sub"], 0)))
                mf = mins / 90
                goals = rng.poisson(c["g90"] * mf)
                assists = rng.poisson(c["a90"] * mf)
                conc = rng.poisson(c["la"] * mf * self.P["cs_scale"])
                pts = np.where(mins >= 60, sc["long_play"], np.where(mins > 0, sc["short_play"], 0)).astype(float)
                pts += goals * sc[f"goals_scored_{P_}"] + assists * sc["assists"]
                pts += np.where((mins >= 60) & (conc == 0), sc[f"clean_sheets_{P_}"], 0)
                if sc[f"goals_conceded_{P_}"]:
                    pts += sc[f"goals_conceded_{P_}"] * (conc // sc["concede_limit"])
                if pos == 1:
                    saves = rng.poisson(r["saves90"] * c["shots_ratio"] * mf)
                    pts += sc["saves"] * (saves // sc["saves_limit"])
                bonus_mod = (0.5 + 0.5 * c["att_ratio"]) if pos >= 3 else 1.0
                pts += np.minimum(3, rng.poisson(r["bonus90"] * mf * bonus_mod))
                pts += np.where((mins >= 60) & (rng.random(n) < r["p_dc"]), sc.get(f"defensive_contribution_{P_}", 0), 0)
                pts += np.where(rng.random(n) < r["yc90"] * mf, sc["yellow_cards"], 0)
                total += pts * c["mult"]
        return [float(round(x, 1)) for x in np.percentile(total, [10, 50, 90])]

    def player(self, pid, sims=0):
        el = self.elements[pid]
        pos, team = el["element_type"], el["team"]
        comps = self.fixture_components(pid)
        gws = []
        for g in comps:
            row = {"gw": g["gw"], "fixtures": [], "xp": 0.0, "e_g": 0.0, "e_a": 0.0, "e_mins": 0.0,
                   "p_start": round(g["fixtures"][0]["p_start"], 2) if g["fixtures"] else 0.0}
            for c in g["fixtures"]:
                xp, e_g, e_a, e_mins, p_cs = self.expected(pos, c)
                row["xp"] += xp
                row["e_g"] += e_g
                row["e_a"] += e_a
                row["e_mins"] += e_mins
                row["fixtures"].append({"opp": c["opp"], "home": c["home"], "cs": round(p_cs, 2),
                                        "xg_for": round(c["lf"], 2), "euro": c["euro"]})
            for key in ("xp", "e_g", "e_a", "e_mins"):
                row[key] = round(row[key], 2)
            gws.append(row)
        first = gws[0] if gws else {"xp": 0, "e_g": 0, "e_a": 0, "p_start": 0}
        egi = first["e_g"] + first["e_a"]
        if self.backtest:
            return {"id": pid, "xp_next": first["xp"], "e_mins": first.get("e_mins", 0),
                    "e_g": first["e_g"], "e_a": first["e_a"], "pos": POS[pos]}
        ce = self.classic_by_code.get(el["code"], {})
        t = self.tot[pid]
        recent = [self.pstats[pid].get(g, {}).get("total_points") for g in range(max(1, self.last_done - 4), self.last_done + 1)]
        out = {
            "id": pid, "name": el["web_name"], "full_name": f"{el['first_name']} {el['second_name']}",
            "team": self.teams[team]["short_name"], "team_id": team, "pos": POS[pos],
            "status": el.get("status"), "news": el.get("news") or "", "news_added": el.get("news_added"),
            "chance": el.get("chance_of_playing_next_round"),
            "set_pieces": {"penalties": ce.get("penalties_order"), "corners": ce.get("corners_and_indirect_freekicks_order"),
                           "free_kicks": ce.get("direct_freekicks_order")},
            "risks": [r_["notes"] for r_ in (ce.get("scout_risks") or [])],
            "gws": gws,
            "xp_next": first["xp"],
            "xp_total": round(sum(g["xp"] for g in gws), 2),
            "p_start_next": first["p_start"],
            "p_return_next": round(1 - math.exp(-egi), 2),
            "p_haul_next": round(1 - math.exp(-egi) * (1 + egi), 2),
            "ceiling": round(first["xp"] + 2.5 * egi, 2),
            "security": round(sum(g["p_start"] for g in gws) / max(1, len(gws)), 2),
            "season": {"pts": el.get("total_points", t["points"]), "mins": t["minutes"], "starts": t["starts"],
                       "g": t["goals_scored"], "a": t["assists"], "xg": round(t["xg"], 2), "xa": round(t["xa"], 2),
                       "form": fnum(el.get("form")), "ppg": fnum(el.get("points_per_game"))},
            "recent_pts": [p if p is not None else 0 for p in recent],
            "intel_applied": bool(self.intel.get(pid)),
            "long_term_out": self.long_term_out(el),
            "new_manager": team in self.new_mgr,
        }
        if sims:
            out["range"] = self.simulate(pid, comps, n=sims)
        return out

    def fit_xp(self, pid):
        """xP for the next gameweek if the player were fully available (buy-low watchlist)."""
        el = self.elements[pid]
        comps = self.fixture_components(pid, force_avail=True)
        if not comps or not comps[0]["fixtures"]:
            return 0.0
        c = dict(comps[0]["fixtures"][0])
        # use the season start rate when recent matches were missed through injury
        season_rate = self.tot[pid]["starts"] / max(1, len(self.team_gws[el["team"]]))
        c["p_start"] = max(c["p_start"], season_rate)
        c["p_subp"] = min(c["p_subp"], 1 - c["p_start"])
        return round(sum(self.expected(el["element_type"], c)[0] for _ in [0]), 2)
