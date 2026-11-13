# Thursday scout run

Instructions for the weekly scheduled run (Thursdays, finishing by 17:00 UK time).
Repository: `tomwhite2012-dot/Labour-Market-Dashboard-`, branch `claude/fpl-scout-interface-utz73k`, folder `fpl-scout/`.
Published page: https://claude.ai/artifact/YA3JaiJTYjzu2Co8qnHc9V

## Steps

1. **Get the code.** Check out branch `claude/fpl-scout-interface-utz73k` and pull. If the repository is not in
   the session, attach it with `add_repo` first.
2. **Baseline.** In `fpl-scout/`, run `python3 pipeline/report.py`, then `python3 pipeline/focus.py`.
   `focus.py` lists the clubs that matter this week and writes `intel/gw<N>/focus.json`.
3. **Junior scouts.** For each club in `focus.json` only, research the listed players. Keep it to a few searches
   per club. Look for:
   - injuries, fitness and suspensions, especially from the manager's pre-match press conference
   - predicted line-ups and likely minutes or rotation
   - penalty, corner and free-kick takers
   - form, role or position changes

   Sources, in order of preference:
   - official club sites and press-conference reports
   - local newspapers (e.g. Liverpool Echo, Manchester Evening News, football.london, Chronicle Live,
     Yorkshire Evening Post, Coventry Telegraph, Sunderland Echo, Nottingham Post, Birmingham Mail, Hull Daily Mail,
     East Anglian Daily Times, The Argus, Bournemouth Echo)
   - official club fan forums
   - BBC Sport
   - free pages on Fantasy Football Scout and FPL Review

   Respect each site's terms. Never scrape bookmaker sites. Never invent a claim. If a page can't be read, leave it out.
4. **Write intel.** Write `intel/gw<N>/<CLUB>.json` for each researched club, using the schema in `README.md`:
   - Every claim needs a `source_url`, a `date` and a `tag` of `confirmed`, `rumour` or `fan_speculation`.
     - `confirmed` means the club or manager said it on the record.
     - `rumour` means journalists are reporting it.
     - `fan_speculation` means forums or opinion.
   - Use `player_id` values from `focus.json`.
   - Add an `effect` only when the claim should change the numbers, e.g. `{"avail": 0}` for ruled out,
     `{"start_prob": 0.4}` for likely benched, or `{"xp_multiplier": 1.1}` for a confirmed new penalty taker.
   - The `summary` is one or two plain sentences in the scout's voice. If nothing useful was found, say so and
     leave `claims` empty.
5. **Rebuild.** Run `python3 pipeline/report.py`, then `python3 pipeline/build.py`.
6. **Save.** Commit `intel/`, `history/`, `web/` and `dist/`, then push to `claude/fpl-scout-interface-utz73k`.
7. **Publish.** Read the artifact URL above with the Artifact tool, then publish `fpl-scout/dist/scout.html` to
   that same `url`, so the link stays the same.
8. **Finish** with a short summary: the verdict, the claims, and any club where research found nothing.

## Midweek gameweeks

The report always covers the next gameweek. If its waivers process before Thursday (some midweek rounds have
Monday waivers), the Thursday report covers the round after. Run these steps by hand before those waivers.
