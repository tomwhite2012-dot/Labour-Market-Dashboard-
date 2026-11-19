"""Build the weekly scouting report.

Usage:
    python3 pipeline/report.py            # fetch fresh data and build
    python3 pipeline/report.py --cache    # reuse data/raw (offline rebuild)

Writes:
    web/data/report.json      data for the interface
    history/gw<N>.json        this week's recommendations, for the track record
"""
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from draft_intel import best_claim_order, best_xi, rival_claims, rival_habits, suggest_trades, watchlist  # noqa: E402
from fetch import ROOT, fetch_all  # noqa: E402
from intel import load_intel, score_sources  # noqa: E402
from model import Model  # noqa: E402

CLUB_LABEL = {"Man City": "Man City", "Man Utd": "Man Utd", "Spurs": "Spurs", "Nott'm Forest": "Forest"}
FPL_URL = "https://fantasy.premierleague.com/"
DRAFT_URL = "https://draft.premierleague.com/"


def load_cfg():
    with open(os.path.join(ROOT, "config.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------- moves
def plan_moves(squad, pool, locked, threshold, max_moves=3):
    outs = [p for p in squad if p["id"] not in locked]
    pool = [p for p in pool if p["status"] != "u"]
    moves, used_in, used_out = [], set(), set()
    while len(moves) < max_moves:
        best = None
        for o in outs:
            if o["id"] in used_out:
                continue
            for i in pool:
                if i["pos"] != o["pos"] or i["id"] in used_in:
                    continue
                gain = i["xp_total"] - o["xp_total"]
                if best is None or gain > best[0]:
                    best = (gain, i, o)
        if not best or best[0] < threshold:
            break
        gain, i, o = best
        backups = [b for b in sorted(pool, key=lambda x: -x["xp_total"])
                   if b["pos"] == o["pos"] and b["id"] not in used_in and b["id"] != i["id"]
                   and b["xp_total"] - o["xp_total"] >= threshold][:2]
        moves.append({"in": i["id"], "out": o["id"], "gain": round(gain, 2),
                      "gain_next": round(i["xp_next"] - o["xp_next"], 2),
                      "backups": [{"id": b["id"], "gain": round(b["xp_total"] - o["xp_total"], 2)} for b in backups]})
        used_in.add(i["id"])
        used_out.add(o["id"])
    return moves


def pick_vs_squad(cand, squad, locked):
    same = [p for p in squad if p["pos"] == cand["pos"] and p["id"] not in locked]
    if not same:
        return None
    worst = min(same, key=lambda x: x["xp_total"])
    return {"in": cand["id"], "out": worst["id"], "gain": round(cand["xp_total"] - worst["xp_total"], 2),
            "gain_next": round(cand["xp_next"] - worst["xp_next"], 2)}


# ---------------------------------------------------------------- track record
def write_history(cfg, gw, chief, decisions):
    os.makedirs(os.path.join(ROOT, "history"), exist_ok=True)
    rec = {"gw": gw, "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "horizon": chief["horizon"], "verdict": chief["verdict"], "moves": chief["moves"],
           "short_term": chief["short_term"], "medium_term": chief["medium_term"],
           "intel_effects": decisions}
    with open(os.path.join(ROOT, "history", f"gw{gw}.json"), "w") as f:
        json.dump(rec, f, indent=1)


def score_history(model, cfg, names, owners):
    entries, hits, graded, net_pred, net_actual = [], 0, 0, 0.0, 0.0
    for path in sorted(glob.glob(os.path.join(ROOT, "history", "gw*.json")),
                       key=lambda p: int(os.path.basename(p)[2:-5])):
        h = json.load(open(path))
        done = [g for g in h["horizon"] if g <= model.last_done]

        def pts(pid):
            return sum(model.pstats[pid].get(g, {}).get("total_points", 0) for g in done)

        row = {"gw": h["gw"], "verdict": h["verdict"], "complete": len(done) == len(h["horizon"]),
               "gws_scored": done, "moves": []}
        for m in h["moves"]:
            mv = {"in": names.get(m["in"]), "out": names.get(m["out"]), "pred_gain": m["gain"],
                  "followed": owners.get(m["in"]) == cfg["entry_id"]}
            if done:
                mv["actual_gain"] = pts(m["in"]) - pts(m["out"])
                if row["complete"]:
                    graded += 1
                    hits += mv["actual_gain"] > 0
                    net_pred += m["gain"]
                    net_actual += mv["actual_gain"]
            row["moves"].append(mv)
        if h.get("verdict") == "HOLD" and h.get("medium_term") and done:
            st = h["medium_term"]
            row["hold_check"] = {"best_waiver": names.get(st["in"]), "your_player": names.get(st["out"]),
                                 "waiver_pts": pts(st["in"]), "your_pts": pts(st["out"])}
        entries.append(row)
    return {"entries": entries, "graded_moves": graded, "hits": hits,
            "hit_rate": round(hits / graded, 2) if graded else None,
            "predicted_gain": round(net_pred, 1), "actual_gain": round(net_actual, 1)}


# ---------------------------------------------------------------- narrative
def chief_speech(verdict, moves, short, medium, P, threshold, horizon_n):
    lines = []
    if verdict == "HOLD":
        lines.append(f"Hold your nerve this week. Nobody on waivers beats a player you can drop by {threshold:g}+ expected points over the next {horizon_n} gameweeks.")
    else:
        n = len(moves)
        lines.append(f"Make {'a move' if n == 1 else f'{n} moves'} this week.")
        for m in moves:
            land = f", about {int(round(m['p_land'] * 100))}% chance he reaches you" if m.get("p_land") is not None else ""
            lines.append(f"Bring in {P[m['in']]['name']} for {P[m['out']]['name']}: +{m['gain']:.1f} xP over {horizon_n} gameweeks{land}.")
    in_moves = {m["in"] for m in moves}
    if short:
        s = P[short["in"]]
        if short["in"] in in_moves:
            lines.append(f"{s['name']} is also my short-term pick: the highest ceiling on waivers next week, "
                         f"{int(s['p_return_next'] * 100)}% chance of a goal or assist.")
        else:
            lines.append(f"Short-term punt: {s['name']} ({s['team']}) has the highest ceiling on waivers next week, "
                     f"{int(s['p_return_next'] * 100)}% chance of a goal or assist.")
    if medium and medium["in"] in in_moves:
        m = P[medium["in"]]
        lines.append(f"{m['name']} doubles as my steady medium-term pick, starting {int(m['security'] * 100)}% of the time.")
    elif medium:
        m = P[medium["in"]]
        lines.append(f"Steady option: {m['name']} ({m['team']}) projects {m['xp_total']:.1f} xP over {horizon_n} gameweeks "
                     f"with a {int(m['security'] * 100)}% start rate.")
    return lines


def main():
    use_cache = "--cache" in sys.argv
    cfg = load_cfg()
    data = fetch_all(cfg["entry_id"], cfg["league_id"], use_cache=use_cache)
    next_gw = data["game"]["next_event"]
    elements = {e["id"]: e for e in data["boot"]["elements"]}
    sources = score_sources(ROOT, Model(data, cfg))          # grade past scout claims first
    intel_claims, intel_effects, decisions = load_intel(ROOT, next_gw, cfg)
    model = Model(data, cfg, intel_effects)

    owners = {s["element"]: s["owner"] for s in data["status"]["element_status"]}
    entries = {e["entry_id"]: e for e in data["league"]["league_entries"]}
    le_by_id = {e["id"]: e for e in data["league"]["league_entries"]}

    P = {}
    for pid, el in elements.items():
        if el.get("status") == "u" and el.get("minutes", 0) == 0:
            continue
        p = model.player(pid)
        o = owners.get(pid)
        p["owner"] = o
        p["owner_name"] = entries[o]["entry_name"] if o in entries else None
        P[pid] = p
    names = {pid: p["name"] for pid, p in P.items()}
    for d in decisions:
        if d["player_id"] in P:
            P[d["player_id"]].setdefault("intel_decisions", []).append(d)

    squad = [p for p in P.values() if p["owner"] == cfg["entry_id"]]
    pool = [p for p in P.values() if p["owner"] is None]
    locked = set()
    for p in squad:
        p["locked"] = p["name"] in cfg["locks"] and not p["long_term_out"]
        p["lock_released"] = p["name"] in cfg["locks"] and p["long_term_out"]
        if p["locked"]:
            locked.add(p["id"])

    thr = cfg["swap_threshold_xp"]
    moves = plan_moves(squad, pool, locked, thr)
    verdict = "SWAP" if moves else "HOLD"

    # draft intelligence: rivals' likely claims, survival to your pick, trades, watchlist
    waiver_order = {e["entry_id"]: e["waiver_pick"] for e in data["league"]["league_entries"]}
    habits = rival_habits(data["transactions"], elements, entries, model.last_done)
    pool_ids = [p["id"] for p in sorted(pool, key=lambda x: -x["xp_total"])[:60]]
    rivals, survival = rival_claims(P, owners, pool_ids, entries, habits, cfg["entry_id"], waiver_order)
    claim_ev = None
    main_ins = {m["in"] for m in moves}
    for m in moves:   # a player who is already a main claim can't also be a backup
        m["backups"] = [b for b in m["backups"] if b["id"] not in main_ins]
    if moves:
        rival_lists = {e: r["targets"] for e, r in rivals.items()}
        claim_ev, moves, probs = best_claim_order(moves, rival_lists, cfg["entry_id"], waiver_order, habits)
        for m in moves:
            m["p_land"] = round(probs[(m["in"], m["out"])], 2)
            m["threats"] = survival.get(m["in"], {}).get("threats", [])
            for b in m["backups"]:
                b["p_land"] = round(probs[(b["id"], m["out"])], 2)
        claim_ev = round(claim_ev, 1)
    trades = suggest_trades(P, owners, entries, cfg["entry_id"], locked, thr)
    exclude = {m["in"] for m in moves} | {b["id"] for m in moves for b in m["backups"]}
    watch = watchlist(model, P, [p["id"] for p in pool], data["transactions"], entries, model.last_done,
                      cfg["entry_id"], exclude)

    attackers = [p for p in pool if p["pos"] in ("MID", "FWD") and p["p_start_next"] >= 0.5]
    short_c = max(attackers, key=lambda x: x["ceiling"], default=None)
    steady = [p for p in pool if p["security"] >= 0.75]
    medium_c = max(steady, key=lambda x: x["xp_total"], default=None)
    if medium_c and short_c and medium_c["id"] == short_c["id"]:
        steady = [p for p in steady if p["id"] != short_c["id"]]
        medium_c = max(steady, key=lambda x: x["xp_total"], default=None)
    short = pick_vs_squad(short_c, squad, locked) if short_c else None
    medium = pick_vs_squad(medium_c, squad, locked) if medium_c else None
    for pick in (short, medium):
        if pick:
            pick["meets_threshold"] = pick["gain"] >= thr
            pick["threats"] = survival.get(pick["in"], {}).get("threats", [])

    # projected XI after recommended moves
    after = [p for p in squad if p["id"] not in {m["out"] for m in moves}] + [P[m["in"]] for m in moves]
    xi_now = best_xi(squad, 0) if squad and squad[0]["gws"] else None
    xi_after = best_xi(after, 0) if after and after[0]["gws"] else None

    horizon = model.horizon
    chief = {
        "verdict": verdict, "horizon": horizon, "threshold": thr,
        "moves": moves, "short_term": short, "medium_term": medium,
        "speech": chief_speech(verdict, moves, short, medium, P, thr, len(horizon)),
        "xi_now": xi_now, "xi_after": xi_after,
        "flags": model.gw_flags(),
        "trades": trades, "watchlist": watch, "claims_expected_gain": claim_ev,
        "squad_xp": round(sum(p["xp_total"] for p in squad), 1),
    }

    # junior scouts
    scouts = []
    for tid, t in sorted(model.teams.items(), key=lambda kv: kv[1]["name"]):
        players = sorted([p for p in P.values() if p["team_id"] == tid], key=lambda x: -x["xp_total"])
        top = [p["id"] for p in players if p["security"] >= 0.3][:6]
        avail = [p["id"] for p in players if p["owner"] is None and p["security"] >= 0.2][:4]
        mine = [p["id"] for p in players if p["owner"] == cfg["entry_id"]]
        claims = []
        for p in players:
            if p["news"]:
                claims.append({"player_id": p["id"], "player": p["name"], "text": p["news"],
                               "source_name": "Official FPL injury & availability news", "source_url": FPL_URL,
                               "date": (p["news_added"] or "")[:10], "tag": "confirmed"})
            for r in p["risks"]:
                claims.append({"player_id": p["id"], "player": p["name"], "text": r,
                               "source_name": "Official FPL scout risks", "source_url": FPL_URL,
                               "date": datetime.now(timezone.utc).date().isoformat(), "tag": "confirmed"})
        intel = intel_claims.get(t["short_name"], {})
        label = CLUB_LABEL.get(t["name"], t["name"])
        scouts.append({
            "club": t["short_name"], "club_name": t["name"], "label": f"The {label} Junior Scout",
            "outlook": model.team_outlook(tid),
            "new_manager": tid in model.new_mgr,
            "attack": round(model.att[tid], 2), "defence": round(model.dfn[tid], 2),
            "top": top, "available": avail, "mine": mine,
            "set_pieces": {k: [p["name"] for p in sorted([x for x in players if x["set_pieces"][k]],
                                                          key=lambda x: x["set_pieces"][k])][:3]
                           for k in ("penalties", "corners", "free_kicks")},
            "official": claims[:12],
            "intel": intel.get("claims", []), "intel_summary": intel.get("summary", ""),
            "intel_generated": intel.get("generated"),
        })

    # league table
    standings = []
    for s in data["league"]["standings"]:
        le = le_by_id[s["league_entry"]]
        standings.append({"rank": s["rank"], "team": le["entry_name"], "manager": le["player_first_name"],
                          "total": s["total"], "gw": s["event_total"], "waiver": le["waiver_pick"],
                          "me": le["entry_id"] == cfg["entry_id"], "entry_id": le["entry_id"],
                          "habits": habits.get(le["entry_id"]), "likely_claims": rivals.get(le["entry_id"], {}).get("likely_claims", [])})

    keep = set(p["id"] for p in squad) | set(p["id"] for p in sorted(pool, key=lambda x: -x["xp_total"])[:60])
    for s in scouts:
        keep |= set(s["top"]) | set(s["available"]) | set(s["mine"])
    for m in moves:
        keep |= {b["id"] for b in m["backups"]} | {m["in"], m["out"]}
    for pick in (short, medium):
        if pick:
            keep |= {pick["in"], pick["out"]}
    for t in trades:
        keep |= {t["give"], t["get"]}
    keep |= {w["id"] for w in watch}
    for r in rivals.values():
        keep |= {c["id"] for c in r["likely_claims"]}
    keep = {k for k in keep if k in P}
    for pid in keep:   # Monte Carlo range for every player shown
        P[pid]["range"] = model.simulate(pid, model.fixture_components(pid), n=2000)
        P[pid]["survival"] = survival.get(pid, {}).get("p")
    try:
        accuracy = json.load(open(os.path.join(ROOT, "history", "backtest.json")))
    except (OSError, ValueError):
        accuracy = None

    ev = model.events.get(next_gw, {})
    report = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "next_gw": next_gw, "horizon": horizon, "deadline": ev.get("deadline_time"),
            "waivers": ev.get("waivers_time"), "last_finished_gw": model.last_done,
            "team_name": cfg["team_name"], "entry_id": cfg["entry_id"], "league": data["league"]["league"]["name"],
            "odds_fixtures": sum(1 for f in model.fix.values() if f["src"] == "odds"),
            "intel_clubs": len(intel_claims),
            "european_matches": cfg.get("european_matches", []), "new_managers": cfg.get("new_managers", []),
            "sources": [
                {"name": "FPL Draft API", "url": DRAFT_URL, "use": "players, ownership, live stats, league"},
                {"name": "FPL classic API", "url": FPL_URL, "use": "set-piece order, injury news, scout risks, fixtures"},
                {"name": "football-data.co.uk", "url": "https://www.football-data.co.uk/", "use": "published average match odds when available"},
            ],
        },
        "league": standings,
        "chief": chief,
        "squad": [p["id"] for p in squad],
        "pool_top": [p["id"] for p in sorted(pool, key=lambda x: -x["xp_total"])[:60]],
        "scouts": scouts,
        "players": {str(pid): P[pid] for pid in keep},
        "track": score_history(model, cfg, names, owners),
        "accuracy": accuracy,
        "sources_reliability": sources,
        "model_params": model.P,
    }
    os.makedirs(os.path.join(ROOT, "web", "data"), exist_ok=True)
    with open(os.path.join(ROOT, "web", "data", "report.json"), "w") as f:
        json.dump(report, f, separators=(",", ":"))
    write_history(cfg, next_gw, chief, decisions)
    print(f"GW{next_gw} verdict: {verdict}")
    for line in chief["speech"]:
        print(" -", line)


if __name__ == "__main__":
    main()
