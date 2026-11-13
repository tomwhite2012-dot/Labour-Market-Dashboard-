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
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import ROOT, fetch_all  # noqa: E402
from model import Model, POS  # noqa: E402

CLUB_LABEL = {"Man City": "Man City", "Man Utd": "Man Utd", "Spurs": "Spurs", "Nott'm Forest": "Forest"}
FPL_URL = "https://fantasy.premierleague.com/"
DRAFT_URL = "https://draft.premierleague.com/"


def load_cfg():
    with open(os.path.join(ROOT, "config.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------- intel
def load_intel(gw, cfg, elements):
    """Junior-scout claims from intel/gw<N>/<CLUB>.json (written by the Thursday scout run)."""
    weights = cfg["intel_weights"]
    by_name = {}
    for e in elements.values():
        by_name.setdefault((e["web_name"].lower(), e["team"]), e["id"])
    claims_by_club, effects = {}, {}
    for path in glob.glob(os.path.join(ROOT, "intel", f"gw{gw}", "*.json")):
        try:
            doc = json.load(open(path))
        except (OSError, ValueError):
            continue
        club = doc.get("club")
        claims = []
        for c in doc.get("claims", []):
            tag = c.get("tag", "fan_speculation")
            if tag not in weights or not c.get("source_url"):
                continue  # every claim must carry a source and a valid tag
            claims.append(c)
            pid = c.get("player_id")
            if pid and c.get("effect"):
                e = dict(c["effect"])
                e["weight"] = weights[tag]
                if e["weight"] > 0:
                    effects.setdefault(int(pid), []).append(e)
        claims_by_club[club] = {"claims": claims, "summary": doc.get("summary", ""),
                                "generated": doc.get("generated")}
    return claims_by_club, effects


# ---------------------------------------------------------------- lineup
def best_xi(players, gw_index):
    by = {p: sorted([x for x in players if x["pos"] == p], key=lambda x: -x["gws"][gw_index]["xp"]) for p in POS.values()}
    best, best_val = None, -1
    for d, m, f in product(range(3, 6), range(2, 6), range(1, 4)):
        if d + m + f != 10 or len(by["DEF"]) < d or len(by["MID"]) < m or len(by["FWD"]) < f or not by["GKP"]:
            continue
        xi = by["GKP"][:1] + by["DEF"][:d] + by["MID"][:m] + by["FWD"][:f]
        val = sum(x["gws"][gw_index]["xp"] for x in xi)
        if val > best_val:
            best, best_val = (xi, f"{d}-{m}-{f}"), val
    if not best:
        return None
    xi, formation = best
    ids = {x["id"] for x in xi}
    bench = sorted([x for x in players if x["id"] not in ids], key=lambda x: (x["pos"] != "GKP", -x["gws"][gw_index]["xp"]))
    return {"formation": formation, "xp": round(best_val, 1),
            "xi": [x["id"] for x in xi], "bench": [x["id"] for x in bench]}


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
def write_history(cfg, gw, chief):
    os.makedirs(os.path.join(ROOT, "history"), exist_ok=True)
    rec = {"gw": gw, "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
           "horizon": chief["horizon"], "verdict": chief["verdict"], "moves": chief["moves"],
           "short_term": chief["short_term"], "medium_term": chief["medium_term"]}
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
            lines.append(f"Bring in {P[m['in']]['name']} for {P[m['out']]['name']}: +{m['gain']:.1f} xP over {horizon_n} gameweeks.")
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
    intel_claims, intel_effects = load_intel(next_gw, cfg, elements)
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
                          "me": le["entry_id"] == cfg["entry_id"]})

    keep = set(p["id"] for p in squad) | set(p["id"] for p in sorted(pool, key=lambda x: -x["xp_total"])[:60])
    for s in scouts:
        keep |= set(s["top"]) | set(s["available"]) | set(s["mine"])
    for m in moves:
        keep |= {b["id"] for b in m["backups"]} | {m["in"], m["out"]}
    for pick in (short, medium):
        if pick:
            keep |= {pick["in"], pick["out"]}

    ev = model.events.get(next_gw, {})
    report = {
        "meta": {
            "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "next_gw": next_gw, "horizon": horizon, "deadline": ev.get("deadline_time"),
            "waivers": ev.get("waivers_time"), "last_finished_gw": model.last_done,
            "team_name": cfg["team_name"], "entry_id": cfg["entry_id"], "league": data["league"]["league"]["name"],
            "odds_fixtures": sum(1 for f in model.fix.values() if f["src"] == "odds"),
            "intel_clubs": len(intel_claims),
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
    }
    os.makedirs(os.path.join(ROOT, "web", "data"), exist_ok=True)
    with open(os.path.join(ROOT, "web", "data", "report.json"), "w") as f:
        json.dump(report, f, separators=(",", ":"))
    write_history(cfg, next_gw, chief)
    print(f"GW{next_gw} verdict: {verdict}")
    for line in chief["speech"]:
        print(" -", line)


if __name__ == "__main__":
    main()
