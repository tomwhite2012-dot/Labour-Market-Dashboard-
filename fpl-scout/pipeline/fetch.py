"""Fetch raw data from free, public sources.

Sources (all free, no keys, no scraping of bookmaker sites):
  - FPL Draft API (draft.premierleague.com/api): players, league, ownership, live GW stats
  - FPL classic API (fantasy.premierleague.com/api): set-piece order, scout risks, fixtures
  - football-data.co.uk fixtures.csv: published average match odds (1X2, over/under 2.5)
  - Understat: optional, used only if the site is up
"""
import csv
import io
import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
UA = {"User-Agent": "fpl-scout/0.1 (personal FPL Draft helper)"}

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"


def _get(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # network hiccup: back off and retry
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def get_json(url, name, use_cache=False):
    os.makedirs(RAW, exist_ok=True)
    path = os.path.join(RAW, name)
    if use_cache and os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    data = json.loads(_get(url))
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def fetch_all(entry_id, league_id, use_cache=False):
    d = {}
    d["game"] = get_json(f"{DRAFT}/game", "game.json", use_cache)
    d["boot"] = get_json(f"{DRAFT}/bootstrap-static", "draft_boot.json", use_cache)
    d["league"] = get_json(f"{DRAFT}/league/{league_id}/details", "league.json", use_cache)
    d["status"] = get_json(f"{DRAFT}/league/{league_id}/element-status", "element_status.json", use_cache)
    d["entry"] = get_json(f"{DRAFT}/entry/{entry_id}/public", "entry.json", use_cache)
    d["classic_boot"] = get_json(f"{CLASSIC}/bootstrap-static/", "classic_boot.json", use_cache)
    d["fixtures"] = get_json(f"{CLASSIC}/fixtures/", "fixtures.json", use_cache)

    current = d["game"]["current_event"] or 0
    finished = d["game"]["current_event_finished"]
    last_done = current if finished else current - 1
    d["live"] = {}
    for gw in range(1, last_done + 1):
        # finished gameweeks never change, so always allow cache for them
        d["live"][gw] = get_json(f"{DRAFT}/event/{gw}/live", f"live_{gw}.json", use_cache=(gw < last_done) or use_cache)
    d["last_finished_gw"] = last_done
    d["odds"] = fetch_odds(use_cache)
    return d


def fetch_odds(use_cache=False):
    """Average pre-match odds for upcoming Premier League games (football-data.co.uk)."""
    path = os.path.join(RAW, "fd_fixtures.csv")
    try:
        if use_cache and os.path.exists(path):
            text = open(path, encoding="utf-8-sig").read()
        else:
            text = _get("https://www.football-data.co.uk/fixtures.csv").decode("utf-8-sig", "replace")
            open(path, "w").write(text)
    except Exception:
        return []
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if r.get("Div") != "E0":
            continue
        try:
            rows.append({
                "date": r["Date"], "home": r["HomeTeam"], "away": r["AwayTeam"],
                "h": float(r["AvgH"]), "d": float(r["AvgD"]), "a": float(r["AvgA"]),
                "o25": float(r["Avg>2.5"]) if r.get("Avg>2.5") else None,
                "u25": float(r["Avg<2.5"]) if r.get("Avg<2.5") else None,
            })
        except (KeyError, ValueError):
            continue
    return rows
