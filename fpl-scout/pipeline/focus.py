"""Which clubs should the junior scouts research this week?

Reads web/data/report.json (run report.py first) and prints the clubs that hold players
who matter: your squad, recommended claims and their backups, the short/medium-term picks
and the best free agents. Writes intel/gw<N>/focus.json for the scout run.
"""
import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_CLUBS = 12


def main():
    r = json.load(open(os.path.join(ROOT, "web", "data", "report.json")))
    P, c = r["players"], r["chief"]
    why = defaultdict(set)
    for pid in r["squad"]:
        why[pid].add("your squad")
    for m in c["moves"]:
        why[m["in"]].add("recommended claim")
        why[m["out"]].add("recommended drop")
        for b in m["backups"]:
            why[b["id"]].add("backup claim")
    for key, label in (("short_term", "short-term pick"), ("medium_term", "medium-term pick")):
        if c.get(key):
            why[c[key]["in"]].add(label)
    for pid in r["pool_top"][:8]:
        why[pid].add("top free agent")

    weight = {"recommended claim": 5, "backup claim": 3, "short-term pick": 4, "medium-term pick": 4,
              "recommended drop": 3, "top free agent": 2, "your squad": 2}
    clubs = defaultdict(lambda: {"score": 0, "players": []})
    for pid, reasons in why.items():
        p = P[str(pid)]
        clubs[p["team"]]["score"] += max(weight[x] for x in reasons)
        clubs[p["team"]]["players"].append({"id": pid, "name": p["name"], "pos": p["pos"],
                                            "why": sorted(reasons), "status": p["status"], "news": p["news"]})
    ranked = sorted(clubs.items(), key=lambda kv: -kv[1]["score"])[:MAX_CLUBS]
    out = {"gw": r["meta"]["next_gw"], "clubs": [{"club": k, **v} for k, v in ranked]}
    d = os.path.join(ROOT, "intel", f"gw{out['gw']}")
    os.makedirs(d, exist_ok=True)
    json.dump(out, open(os.path.join(d, "focus.json"), "w"), indent=1, ensure_ascii=False)
    for cl in out["clubs"]:
        print(f"{cl['club']}: " + ", ".join(f"{p['name']} ({'/'.join(p['why'])})" for p in cl["players"]))


if __name__ == "__main__":
    main()
