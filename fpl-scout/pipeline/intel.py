"""Junior-scout intel: loading, conflict rules and source reliability.

Rules for turning claims into changes to the numbers (per player, per effect type):
  1. A claim needs a source_url and a valid tag, or it is ignored.
  2. Fan speculation is shown but never changes the numbers.
  3. If any confirmed claim exists (on the record from the club or manager), the newest
     confirmed claim wins outright, at full weight.
  4. Otherwise rumours count only lightly on their own; two or more independent sources
     (different websites) agreeing count much more. Newer claims count double.
  5. Every weight is scaled by how reliable that website has proved in past weeks.
"""
import glob
import json
import os
from collections import defaultdict
from urllib.parse import urlparse

EFFECT_KEYS = ("avail", "start_prob", "xp_multiplier", "penalty_taker")


def domain(url):
    d = urlparse(url or "").netloc.lower()
    return d[4:] if d.startswith("www.") else d


def reliability_table(root):
    path = os.path.join(root, "history", "sources.json")
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def rel_score(table, dom):
    r = table.get(dom, {})
    return (r.get("hits", 0) + 1) / (r.get("total", 0) + 2)   # 0.5 with no history


def load_intel(root, gw, cfg):
    w = cfg["intel_weights"]
    table = reliability_table(root)
    claims_by_club, groups = {}, defaultdict(list)
    for path in glob.glob(os.path.join(root, "intel", f"gw{gw}", "*.json")):
        if path.endswith("focus.json"):
            continue
        try:
            doc = json.load(open(path))
        except (OSError, ValueError):
            continue
        claims = []
        for c in doc.get("claims", []):
            if c.get("tag") not in ("confirmed", "rumour", "fan_speculation") or not c.get("source_url"):
                continue
            c = dict(c)
            c["domain"] = domain(c["source_url"])
            c["reliability"] = round(rel_score(table, c["domain"]), 2)
            claims.append(c)
            pid = c.get("player_id")
            for k, v in (c.get("effect") or {}).items():
                if pid and k in EFFECT_KEYS:
                    groups[(int(pid), k)].append((c, v))
        claims_by_club[doc.get("club")] = {"claims": claims, "summary": doc.get("summary", ""),
                                          "generated": doc.get("generated")}

    effects, decisions = defaultdict(list), []
    for (pid, key), items in groups.items():
        conf = [(c, v) for c, v in items if c["tag"] == "confirmed"]
        rum = [(c, v) for c, v in items if c["tag"] == "rumour"]
        if conf:
            c, v = max(conf, key=lambda cv: cv[0].get("date", ""))
            weight, basis, used = w["confirmed"], "confirmed", [c]
            value = v
        elif rum:
            doms = {c["domain"] for c, _ in rum}
            rum.sort(key=lambda cv: cv[0].get("date", ""))
            latest = rum[-1][0].get("date", "")
            wts = [2.0 if c.get("date", "") == latest else 1.0 for c, _ in rum]
            if isinstance(rum[0][1], bool):
                value = rum[-1][1]
            else:
                value = sum(wt * v for wt, (_, v) in zip(wts, rum)) / sum(wts)
            if len(doms) >= 2:
                weight, basis = w["rumour_two_sources"], f"{len(doms)} independent sources"
            else:
                weight, basis = w["rumour_single"], "single rumour"
            used = [c for c, _ in rum]
            c = rum[-1][0]
        else:
            continue
        rel = sum(x["reliability"] for x in used) / len(used)
        weight = min(1.0, weight * (0.6 + 0.8 * rel))
        e = {key: value, "weight": round(weight, 2), "basis": basis,
             "domains": sorted({x["domain"] for x in used})}
        gws = (c.get("effect") or {}).get("gameweeks")
        if gws:
            e["gameweeks"] = gws
        effects[pid].append(e)
        decisions.append({"player_id": pid, "effect": key, "value": value, "weight": e["weight"], "basis": basis,
                          "domains": e["domains"], "ignored": len(items) - len(used)})
    return claims_by_club, dict(effects), decisions


def score_sources(root, model):
    """Grade past availability/start claims against what happened; update history/sources.json."""
    tally = defaultdict(lambda: {"hits": 0, "total": 0})
    for path in glob.glob(os.path.join(root, "history", "gw*.json")):
        h = json.load(open(path))
        g = h.get("gw")
        if not g or g > model.last_done:
            continue
        for d in h.get("intel_effects", []):
            if d["effect"] not in ("start_prob", "avail"):
                continue
            st = model.actual.get(d["player_id"], {}).get(g, {})
            happened = st.get("starts", 0) >= 1 if d["effect"] == "start_prob" else st.get("minutes", 0) > 0
            predicted = d["value"] >= 0.5
            for dom in d["domains"]:
                tally[dom]["total"] += 1
                tally[dom]["hits"] += int(happened == predicted)
    table = dict(tally)
    os.makedirs(os.path.join(root, "history"), exist_ok=True)
    json.dump(table, open(os.path.join(root, "history", "sources.json"), "w"), indent=1, sort_keys=True)
    return table
