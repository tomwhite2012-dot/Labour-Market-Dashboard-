"""Decide what the scheduled run should do right now.

The scheduled task fires twice a day (08:37 and 15:37 UK time). This script looks at the
next waiver deadline and history/runs.json and prints one of:

    FULL  - full junior-scout research and report (18 to 48 hours before waivers)
    LATE  - quick late-news check of claims, backups and doubtful starters (under 18 hours
            before waivers, after a FULL run)
    NONE  - nothing to do; end the session

    python3 pipeline/should_run.py            # print the decision
    python3 pipeline/should_run.py --record FULL   # log a completed run
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "history", "runs.json")
UA = {"User-Agent": "fpl-scout/0.1 (personal FPL Draft helper)"}


def load_runs():
    try:
        return json.load(open(RUNS))
    except (OSError, ValueError):
        return []


def next_waivers():
    def get(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return json.loads(r.read())
    game = get("https://draft.premierleague.com/api/game")
    boot = get("https://draft.premierleague.com/api/bootstrap-static")
    gw = game["next_event"]
    ev = {e["id"]: e for e in boot["events"]["data"]}[gw]
    wt = ev.get("waivers_time") or ev["deadline_time"]
    return gw, datetime.fromisoformat(wt.replace("Z", "+00:00")), game.get("waivers_processed")


def decide(now=None):
    now = now or datetime.now(timezone.utc)
    gw, waivers, processed = next_waivers()
    hours = (waivers - now).total_seconds() / 3600
    done = {r["kind"] for r in load_runs() if r["gw"] == gw}
    if processed or hours <= 1:
        return "NONE", gw, hours
    if 18 <= hours <= 48 and "FULL" not in done:
        return "FULL", gw, hours
    if hours < 18 and "FULL" not in done:
        return "FULL", gw, hours          # missed the window: do the full run now
    if hours < 18 and "LATE" not in done:
        return "LATE", gw, hours
    return "NONE", gw, hours


def main():
    if "--record" in sys.argv:
        kind = sys.argv[sys.argv.index("--record") + 1]
        gw, _, _ = next_waivers()
        runs = load_runs()
        runs.append({"gw": gw, "kind": kind, "at": datetime.now(timezone.utc).isoformat(timespec="minutes")})
        os.makedirs(os.path.dirname(RUNS), exist_ok=True)
        json.dump(runs, open(RUNS, "w"), indent=1)
        print(f"recorded {kind} for GW{gw}")
        return
    kind, gw, hours = decide()
    print(f"{kind} GW{gw} ({hours:.1f}h to waivers)")


if __name__ == "__main__":
    main()
