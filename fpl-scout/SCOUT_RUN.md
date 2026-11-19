# Scheduled scout run

The scheduled task fires at 08:37 and 15:37 UK time every day. Most firings end straight away. The run only does
work around a waiver deadline.

- Repository: `tomwhite2012-dot/Labour-Market-Dashboard-`
- Branch: `claude/fpl-scout-interface-utz73k`
- Folder: `fpl-scout/`
- Published page: https://claude.ai/artifact/YA3JaiJTYjzu2Co8qnHc9V

## 0. Attach the repository with push access (always first)

Call `add_repo` with owner `tomwhite2012-dot`, repo `Labour-Market-Dashboard-` and `access: "push"`. Do this even
if a copy of the code is already on disk: without it, pushes are refused. Then clone, or fetch and check out, the
branch above and pull.

## 1. Decide what to do

In `fpl-scout/`, run `python3 pipeline/should_run.py`.

- `NONE`: stop. Don't publish anything, and end with one line saying nothing was due.
- `FULL`: do steps 2–6.
- `LATE`: do step 7, then step 6.

## 2. Baseline (FULL)

Run `python3 pipeline/report.py`, then `python3 pipeline/focus.py`. `focus.py` writes `intel/gw<N>/focus.json`
listing the clubs that matter this week, up to 12.

## 3. Junior scouts (FULL)

For each club in `focus.json` only:

1. **Search the fixture first.** Start with one search for "<home team> v <away team> team news" for this
   gameweek's match. Then search for any listed player the fixture search didn't cover.
2. **Answer this checklist for each listed player:**
   - fit or injured, and when back
   - predicted to start or not
   - minutes risk: rotation, a European match within 3 days, or a new manager
   - set-piece role, including any change of penalty taker
   - any role or position change
3. **Use these sources, in order:**
   - official club sites and press-conference reports
   - local newspapers (Liverpool Echo, Manchester Evening News, football.london, Chronicle Live, Yorkshire Evening
     Post, Coventry Telegraph, Sunderland Echo, Nottingham Post, Birmingham Mail, Hull Daily Mail, East Anglian
     Daily Times, The Argus, Bournemouth Echo)
   - official club fan forums
   - BBC Sport
   - free pages on Fantasy Football Scout and FPL Review
4. **Respect sites' terms.** No bookmaker scraping and no invented claims. Leave out pages you can't read.
5. **Keep config current.** If a club has a European match within 3 days of its league game, add it to
   `european_matches` in `config.json` with the club, date, competition and source. If a club has a new manager,
   add them to `new_managers` with the club, the gameweek they started (`since_gw`), their name and the source.

## 4. Write intel (FULL)

Write `intel/gw<N>/<CLUB>.json` using the schema in `README.md`.

- **Every claim needs** a `source_url`, a `date` and a `tag`:
  - `confirmed`: the club or manager said it on the record.
  - `rumour`: journalists are reporting it.
  - `fan_speculation`: forums or opinion.
- **Use `player_id` values** from `focus.json`.
- **Add an `effect`** when the claim should change the numbers. Supported effects:
  - `avail`: chance of being available
  - `start_prob`: chance of starting
  - `xp_multiplier`: scales expected points
  - `penalty_taker`: true or false
  - `gameweeks`: optional, limits the effect to those gameweeks

  You don't need to weigh claims yourself. The code does it:
  - The newest confirmed claim wins.
  - Two independent websites count much more than one.
  - Fan speculation is display only.
  - Each website's past accuracy scales its weight.
- **Keep earlier claims.** If an earlier run this week already wrote intel, add to it rather than deleting it.

## 5. Rebuild (FULL)

Run `python3 pipeline/report.py`, then `python3 pipeline/build.py`, then `python3 pipeline/should_run.py --record FULL`.

## 6. Save and publish (FULL and LATE)

1. Commit `fpl-scout/` changes and push to `claude/fpl-scout-interface-utz73k`. Never open a pull request.
2. If the push is refused, still do step 3. Then put the exact error first in your summary.
3. Read the artifact URL above with the Artifact tool. Publish `fpl-scout/dist/scout.html` to that same `url`.
   If the publish is refused because a newer version exists, merge that version's intel into yours before
   publishing again.
4. End with a short summary:
   - the HOLD/SWAP verdict
   - the claims in order, with each one's chance of landing and its backups
   - any trade idea
   - clubs where research found nothing

## 7. Late-news check (LATE)

This is a quick pass before waivers process.

1. Run `python3 pipeline/report.py`.
2. Search only for today's press-conference news on:
   - the recommended claims and their backups
   - your squad players marked doubtful or with a low start chance
3. Add any new claims to the existing intel files.
4. Rebuild with `python3 pipeline/report.py`, then `python3 pipeline/build.py`.
5. Run `python3 pipeline/should_run.py --record LATE`.
6. In the summary, say clearly whether any claim changed since the full run.
