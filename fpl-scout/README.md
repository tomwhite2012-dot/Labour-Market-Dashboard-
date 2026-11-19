# FPL Draft Scouting Room

A chief scout and 20 club junior scouts that tell **Herring Berg** (league *Fisherman's Trawl*) whether to hold or
make waiver claims each week.

## Run it

```bash
python3 pipeline/report.py     # fetch free public data, build xP model + recommendations
python3 pipeline/build.py      # bundle the interface with the data
open web/index.html            # or publish dist/scout.html
```

No third-party Python packages and no paid APIs.

## What's in here

| Path | What it does |
| --- | --- |
| `config.json` | Team and league IDs, locked players, the +2 xP swap rule, a 3-gameweek horizon |
| `pipeline/fetch.py` | Downloads FPL Draft and classic FPL API data, plus football-data.co.uk match odds when published |
| `pipeline/model.py` | Expected-points baseline: minutes, team goal expectancy, goals, assists, clean sheets, saves, bonus, defensive contributions |
| `pipeline/report.py` | Chief verdict (HOLD/SWAP), waiver claims with backups, short-term and medium-term picks, junior scout reports, track record |
| `pipeline/intel.py` | Turns scout claims into numbers: conflict rules, two-source rule, source reliability scoring |
| `pipeline/draft_intel.py` | Draft edge: rivals' likely claims, waiver-round simulation and best claim order, trade ideas, buy-low watchlist |
| `pipeline/backtest.py` | Replays past gameweeks to measure accuracy; `--tune` grid-searches the model settings into `config.json` |
| `pipeline/should_run.py` | Tells the scheduled run whether to do a FULL run, a LATE news check, or nothing |
| `pipeline/build.py` | Inlines the data into `web/index.html` and `dist/scout.html` |
| `web/app.html` | The interface: pitch, animated cartoon scouts, report tabs |
| `intel/gw<N>/<CLUB>.json` | Junior scout field intel (see below) |
| `history/gw<N>.json` | Each week's advice, scored later for the track record |

## Rules the chief follows

- **Swap only if** the claimed player beats the dropped player by **at least +2.0 xP** over the next 3 gameweeks.
  Otherwise the verdict is **HOLD**.
- **Locked players** (Calafiori, Matheus Nunes, Gakpo, Rice, Szoboszlai, João Pedro) are never dropped unless FPL
  news shows a long-term absence (unknown return date, or expected back more than 4 weeks out).
- Claims are listed in priority order, with backups in case a team higher on waiver priority takes the first choice.
- Double and blank gameweeks are flagged for the next 10 gameweeks.

## Junior scout intel format

Each club scout writes `intel/gw<N>/<CLUB>.json` (e.g. `intel/gw11/CHE.json`):

```json
{
  "club": "CHE",
  "generated": "2026-11-19T15:00Z",
  "summary": "One or two sentences in the scout's voice.",
  "claims": [
    {
      "player_id": 157,
      "player": "Estêvão",
      "text": "Trained fully on Wednesday and is expected to start.",
      "source_name": "Local paper",
      "source_url": "https://...",
      "date": "2026-11-18",
      "tag": "confirmed",
      "effect": {"start_prob": 0.85, "gameweeks": [11]}
    }
  ]
}
```

- `tag` is one of `confirmed`, `rumour` or `fan_speculation`. Claims without a `source_url` are dropped.
- `effect` is optional. It accepts `avail`, `start_prob`, `xp_multiplier`, `penalty_taker` (true/false) and an
  optional `gameweeks` list.
- How claims count:
  - The newest confirmed claim wins outright.
  - Rumours count 25% from a single website and 75% when two or more independent websites agree.
  - Fan speculation is shown but never changes the numbers.
  - Each website's weight is scaled by how often its past start/availability claims proved right
    (`history/sources.json`).

## Other inputs in `config.json`

- `model_params`: tuned by `python3 pipeline/backtest.py --tune`.
- `european_matches`: clubs with a European game near a league match. Start chances drop around them.
- `new_managers`: start chances are less certain for 3 gameweeks after a manager change.
