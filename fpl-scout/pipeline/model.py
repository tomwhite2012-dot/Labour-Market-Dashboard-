"""Expected-points (xP) baseline for FPL Draft.

For every player and every fixture in the horizon we estimate:
  * availability (injury news / suspension / loan rules)
  * P(start), P(60+ mins) and expected minutes from the last six matches
  * team goal expectancy for both sides, from bookmaker match odds when published,
    otherwise from season xG / goals strength ratings
  * goals, assists, clean sheet, goals conceded, saves, bonus, defensive
    contribution and card points using the league's own scoring settings

Scout intel can then nudge availability / start probability / output.
"""
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
# per-90 priors used to regress small samples (xG, xA)
PRIOR90 = {1: (0.0, 0.01), 2: (0.04, 0.05), 3: (0.12, 0.12), 4: (0.30, 0.08)}
PRIOR_MINS = 450
DC_PRIOR = {1: 0.0, 2: 0.30, 3: 0.15, 4: 0.05}
HOME_ADV = 1.10
AWAY_ADV = 0.92

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


def p_at_least(lam, n):
    return 1 - sum(pois(k, lam) for k in range(n))


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
                tot = lh + la
                p_over = 1 - sum(pois(k, tot) for k in range(3))
                err += (p_over - po25) ** 2
            if err < best_err:
                best, best_err = (lh, la), err
    return best


class Model:
    def __init__(self, data, cfg, intel_effects=None):
        self.d = data
        self.cfg = cfg
        self.intel = intel_effects or {}
        b = data["boot"]
        self.scoring = b["settings"]["scoring"]
        self.teams = {t["id"]: t for t in b["teams"]}
        self.team_by_short = {t["short_name"]: t["id"] for t in b["teams"]}
        self.elements = {e["id"]: e for e in b["elements"]}
        self.classic_by_code = {e["code"]: e for e in data["classic_boot"]["elements"]}
        self.events = {e["id"]: e for e in b["events"]["data"]}
        self.next_gw = data["game"]["next_event"]
        self.last_done = data["last_finished_gw"]
        self.horizon = list(range(self.next_gw, min(self.next_gw + cfg["horizon_gameweeks"], 39)))
        self.fixtures = data["fixtures"]
        self._team_history()
        self._strengths()
        self._fixture_lambdas()

    # ------------------------------------------------------------------ teams
    def _team_history(self):
        self.team_gws = defaultdict(list)          # team -> [gw,...] finished
        self.opp = {}                               # (gw, team) -> [(opp, home)]
        self.gf = defaultdict(float)
        self.ga = defaultdict(float)
        for f in self.fixtures:
            if not f.get("finished") or not f.get("event"):
                continue
            gw, h, a = f["event"], f["team_h"], f["team_a"]
            self.team_gws[h].append(gw)
            self.team_gws[a].append(gw)
            self.opp.setdefault((gw, h), []).append((a, True))
            self.opp.setdefault((gw, a), []).append((h, False))
            self.gf[h] += f["team_h_score"] or 0
            self.ga[h] += f["team_a_score"] or 0
            self.gf[a] += f["team_a_score"] or 0
            self.ga[a] += f["team_h_score"] or 0

        # per-player per-gw stats
        self.pstats = defaultdict(dict)             # pid -> gw -> stats
        team_gw_xg = defaultdict(float)
        for gw, live in self.d["live"].items():
            gw = int(gw)
            for pid, rec in live["elements"].items():
                pid = int(pid)
                st = rec["stats"]
                self.pstats[pid][gw] = st
                el = self.elements.get(pid)
                if el:
                    team_gw_xg[(gw, el["team"])] += fnum(st.get("expected_goals"))
        self.xgf = defaultdict(float)
        self.xga = defaultdict(float)
        for (gw, t), opps in self.opp.items():
            self.xgf[t] += team_gw_xg[(gw, t)]
            for o, _ in opps:
                self.xga[t] += team_gw_xg[(gw, o)]

    def _strengths(self):
        games = {t: max(1, len(self.team_gws[t])) for t in self.teams}
        tot_goals = sum(self.gf.values())
        tot_games = sum(len(v) for v in self.team_gws.values()) or 1
        self.avg = max(0.9, tot_goals / tot_games) if tot_goals else 1.35
        k = 4.0
        self.att, self.dfn = {}, {}
        for t in self.teams:
            n = len(self.team_gws[t])
            af = (0.65 * self.xgf[t] + 0.35 * self.gf[t]) / games[t] / self.avg
            dg = (0.65 * self.xga[t] + 0.35 * self.ga[t]) / games[t] / self.avg
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
        for o in self.d.get("odds", []):
            h, a = self._team_code(o["home"]), self._team_code(o["away"])
            if h and a:
                odds_idx[(h, a)] = o
        self.fix = {}       # fixture id -> dict
        self.team_fix = defaultdict(list)   # (gw, team) -> [fixture dicts]
        last_gw = self.next_gw + self.cfg.get("dgw_lookahead_gameweeks", 10)
        for f in self.fixtures:
            gw = f.get("event")
            if not gw or gw < self.next_gw or gw > last_gw:
                continue
            h, a = f["team_h"], f["team_a"]
            lh = self.avg * self.att[h] * self.dfn[a] * HOME_ADV
            la = self.avg * self.att[a] * self.dfn[h] * AWAY_ADV
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
            fx = {"id": f["id"], "gw": gw, "h": h, "a": a, "lh": lh, "la": la, "src": src,
                  "kickoff": f.get("kickoff_time"), "fdr_h": f.get("team_h_difficulty"),
                  "fdr_a": f.get("team_a_difficulty")}
            self.fix[f["id"]] = fx
            self.team_fix[(gw, h)].append(fx)
            self.team_fix[(gw, a)].append(fx)

    def team_outlook(self, t):
        out = []
        for gw in self.horizon:
            for fx in self.team_fix.get((gw, t), []):
                home = fx["h"] == t
                lf, la = (fx["lh"], fx["la"]) if home else (fx["la"], fx["lh"])
                out.append({"gw": gw, "opp": self.teams[fx["a"] if home else fx["h"]]["short_name"],
                            "home": home, "xg_for": round(lf, 2), "xg_against": round(la, 2),
                            "cs": round(math.exp(-la), 2), "src": fx["src"],
                            "fdr": fx["fdr_h"] if home else fx["fdr_a"]})
        return out

    def gw_flags(self):
        """Double / blank gameweeks in the lookahead window."""
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
        status = el.get("status", "a")
        cop = el.get("chance_of_playing_next_round")
        cop = (1.0 if status == "a" else (0.0 if status in "isun" else 0.5)) if cop is None else cop / 100
        if status == "u":
            a = 0.0
        elif status == "n":
            a = 0.0 if k == 0 else 1.0
        elif status == "s":
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
        """True if the player looks out for longer than the lock-release window."""
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
        p_start = start / sw if gws else 0.0
        p_sub = sub / sw if gws else 0.0
        mins_start = sum(smins) / len(smins) if smins else 80.0
        p60_start = (n60 + 0.85) / (nst + 1)
        mins_sub = sum(sub_mins) / len(sub_mins) if sub_mins else 20.0
        return p_start, p_sub, mins_start, p60_start, mins_sub

    def rates(self, pid, el):
        pos = el["element_type"]
        mins = el.get("minutes", 0)
        pxg, pxa = PRIOR90[pos]
        denom = (mins + PRIOR_MINS) / 90
        xg = 0.8 * fnum(el.get("expected_goals")) + 0.2 * el.get("goals_scored", 0)
        xa = 0.8 * fnum(el.get("expected_assists")) + 0.2 * el.get("assists", 0)
        r = {
            "xg90": (xg + pxg * PRIOR_MINS / 90) / denom,
            "xa90": (xa + pxa * PRIOR_MINS / 90) / denom,
            "bonus90": (el.get("bonus", 0) + 0.15 * PRIOR_MINS / 90) / denom,
            "yc90": (el.get("yellow_cards", 0) + 0.1 * PRIOR_MINS / 90) / denom,
            "saves90": (el.get("saves", 0) + 2.8 * PRIOR_MINS / 90) / denom if pos == 1 else 0,
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

    def player(self, pid):
        el = self.elements[pid]
        pos, team = el["element_type"], el["team"]
        P = POS[pos]
        sc = self.scoring
        prof = self.minutes_profile(pid, team)
        r = self.rates(pid, el)
        eff = self.intel.get(pid, [])
        gws = []
        for k, gw in enumerate(self.horizon):
            avail = self.availability(el, k, gw)
            p_start_raw, p_sub, mins_start, p60_start, mins_sub = prof
            mult = 1.0
            for e in eff:
                if e.get("gameweeks") and gw not in e["gameweeks"]:
                    continue
                w = e["weight"]
                if "avail" in e:
                    avail = avail + w * (e["avail"] - avail)
                if "start_prob" in e:
                    p_start_raw = p_start_raw + w * (e["start_prob"] - p_start_raw)
                if "xp_multiplier" in e:
                    mult *= 1 + w * (e["xp_multiplier"] - 1)
            fx_list = self.team_fix.get((gw, team), [])
            row = {"gw": gw, "fixtures": [], "xp": 0.0, "e_g": 0.0, "e_a": 0.0, "e_mins": 0.0,
                   "p_start": round(avail * p_start_raw, 2)}
            for fx in fx_list:
                home = fx["h"] == team
                lf, la = (fx["lh"], fx["la"]) if home else (fx["la"], fx["lh"])
                p_start = avail * p_start_raw
                p_sub_play = avail * (1 - p_start_raw) * min(1.0, p_sub / max(1e-6, 1 - p_start_raw)) if p_start_raw < 1 else 0
                p_play = p_start + p_sub_play
                p60 = p_start * p60_start
                e_mins = p_start * mins_start + p_sub_play * mins_sub
                mfrac = e_mins / 90
                base_att = self.avg * self.att[team]
                att_ratio = lf / base_att
                e_g = r["xg90"] * mfrac * att_ratio
                e_a = r["xa90"] * mfrac * att_ratio
                p_cs = math.exp(-la)
                xp = p60 * sc["long_play"] + (p_play - p60) * sc["short_play"]
                xp += e_g * sc[f"goals_scored_{P}"] + e_a * sc["assists"]
                xp += p60 * p_cs * sc[f"clean_sheets_{P}"]
                if sc[f"goals_conceded_{P}"]:
                    xp += sc[f"goals_conceded_{P}"] * e_floor_div(la * min(1, mins_start / 90), sc["concede_limit"]) * p_start
                if pos == 1:
                    shots_ratio = la / (self.avg * self.dfn[team])
                    xp += sc["saves"] * e_floor_div(r["saves90"] * shots_ratio, sc["saves_limit"]) * p_start
                bonus_mod = (0.5 + 0.5 * att_ratio) if pos >= 3 else (0.5 + 0.5 * p_cs / 0.3)
                xp += r["bonus90"] * mfrac * min(1.8, bonus_mod)
                xp += r["p_dc"] * p60 * sc.get(f"defensive_contribution_{P}", 0)
                xp += r["yc90"] * mfrac * sc["yellow_cards"]
                xp *= mult
                row["xp"] += xp
                row["e_g"] += e_g
                row["e_a"] += e_a
                row["e_mins"] += e_mins
                row["fixtures"].append({"opp": self.teams[fx["a"] if home else fx["h"]]["short_name"],
                                        "home": home, "cs": round(p_cs, 2), "xg_for": round(lf, 2)})
            for key in ("xp", "e_g", "e_a", "e_mins"):
                row[key] = round(row[key], 2)
            gws.append(row)

        first = gws[0] if gws else {"xp": 0, "e_g": 0, "e_a": 0, "p_start": 0}
        egi = first["e_g"] + first["e_a"]
        ce = self.classic_by_code.get(el["code"], {})
        recent = [self.pstats[pid].get(g, {}).get("total_points") for g in range(max(1, self.last_done - 4), self.last_done + 1)]
        return {
            "id": pid, "name": el["web_name"], "full_name": f"{el['first_name']} {el['second_name']}",
            "team": self.teams[team]["short_name"], "team_id": team, "pos": P,
            "status": el.get("status"), "news": el.get("news") or "", "news_added": el.get("news_added"),
            "chance": el.get("chance_of_playing_next_round"),
            "set_pieces": {
                "penalties": ce.get("penalties_order"), "corners": ce.get("corners_and_indirect_freekicks_order"),
                "free_kicks": ce.get("direct_freekicks_order"),
            },
            "risks": [r_["notes"] for r_ in (ce.get("scout_risks") or [])],
            "gws": gws,
            "xp_next": first["xp"],
            "xp_total": round(sum(g["xp"] for g in gws), 2),
            "p_start_next": first["p_start"],
            "p_return_next": round(1 - math.exp(-egi), 2),
            "p_haul_next": round(1 - math.exp(-egi) * (1 + egi), 2),
            "ceiling": round(first["xp"] + 2.5 * egi, 2),
            "security": round(sum(g["p_start"] for g in gws) / max(1, len(gws)), 2),
            "season": {"pts": el.get("total_points", 0), "mins": el.get("minutes", 0), "starts": el.get("starts", 0),
                       "g": el.get("goals_scored", 0), "a": el.get("assists", 0),
                       "xg": round(fnum(el.get("expected_goals")), 2), "xa": round(fnum(el.get("expected_assists")), 2),
                       "form": fnum(el.get("form")), "ppg": fnum(el.get("points_per_game"))},
            "recent_pts": [p if p is not None else 0 for p in recent],
            "intel_applied": bool(eff),
            "long_term_out": self.long_term_out(el),
        }
