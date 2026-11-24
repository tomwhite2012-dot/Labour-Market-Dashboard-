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
    d["transactions"] = get_json(f"{DRAFT}/draft/league/{league_id}/transactions", "transactions.json", use_cache)
    d["trades"] = get_json(f"{DRAFT}/draft/league/{league_id}/trades", "trades.json", use_cache)
    d["classic_boot"] = get_json(f"{CLASSIC}/bootstrap-static/", "classic_boot.json", use_cache)
    d["fixtures"] = get_json(f"{CLASSIC}/fixtures/", "fixtures.json", use_cache)

    current = d["game"]["current_event"] or 0
    finished = d["game"]["current_event_finished"]
    # treat the current gameweek as done once every match is provisionally finished (bonus may still be pending)
    cur_fx = [f for f in d["fixtures"] if f.get("event") == current]
    if not finished and cur_fx and all(f.get("finished") or f.get("finished_provisional") for f in cur_fx):
        finished = True
    last_done = current if finished else current - 1
    d["live"] = {}
    for gw in range(1, last_done + 1):
        # finished gameweeks never change, so always allow cache for them
        d["live"][gw] = get_json(f"{DRAFT}/event/{gw}/live", f"live_{gw}.json", use_cache=(gw < last_done) or use_cache)
    d["last_finished_gw"] = last_done
    fd = fetch_odds(use_cache)
    api, d["odds_api_status"] = fetch_odds_api(use_cache)
    d["odds"] = fd + api      # later rows win, so The Odds API overrides football-data.co.uk
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
                "source": "football-data.co.uk",
            })
        except (KeyError, ValueError):
            continue
    return rows


ODDS_API = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"


def fetch_odds_api(use_cache=False):
    """Premier League match odds from The Odds API (free Starter plan), if ODDS_API_KEY is set.

    One call per run: UK bookmakers, match result (h2h) and over/under goals (totals), which
    costs 2 credits. Odds are averaged across bookmakers. The key is never written to disk or
    printed. With --cache, the last saved response is reused and no credits are spent.
    """
    path = os.path.join(RAW, "odds_api.json")
    key = os.environ.get("ODDS_API_KEY", "").strip()
    status = {"enabled": bool(key), "remaining": None, "error": None}
    events = None
    if use_cache or not key:
        if os.path.exists(path):
            saved = json.load(open(path))
            events, status["remaining"] = saved.get("events"), saved.get("remaining")
            status["cached"] = True
    if events is None and key:
        url = f"{ODDS_API}?regions=uk&markets=h2h,totals&oddsFormat=decimal&apiKey={key}"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                events = json.loads(r.read())
                status["remaining"] = r.headers.get("x-requests-remaining")
            os.makedirs(RAW, exist_ok=True)
            json.dump({"events": events, "remaining": status["remaining"]}, open(path, "w"))
        except Exception as e:  # never include the URL: it carries the key
            code = getattr(e, "code", None)
            status["error"] = f"HTTP {code}" if code else type(e).__name__
            return [], status
    return parse_odds_api(events or []), status


def parse_odds_api(events, now=None):
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    rows = []
    for ev in events:
        ct = ev.get("commence_time")
        if ct and datetime.fromisoformat(ct.replace("Z", "+00:00")) < now:
            continue   # already kicked off: stale
        home, away = ev.get("home_team"), ev.get("away_team")
        h, d, a, over, under = [], [], [], [], []
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                if m.get("key") == "h2h":
                    for o in m.get("outcomes", []):
                        (h if o["name"] == home else a if o["name"] == away else d).append(o["price"])
                elif m.get("key") == "totals":
                    for o in m.get("outcomes", []):
                        if abs(float(o.get("point", 0)) - 2.5) < 1e-6:
                            (over if o["name"] == "Over" else under).append(o["price"])
        if h and d and a:
            avg = lambda xs: sum(xs) / len(xs)
            rows.append({"date": (ev.get("commence_time") or "")[:10], "home": home, "away": away,
                         "h": avg(h), "d": avg(d), "a": avg(a),
                         "o25": avg(over) if over else None, "u25": avg(under) if under else None,
                         "source": "The Odds API", "books": len(ev.get("bookmakers", []))})
    return rows
