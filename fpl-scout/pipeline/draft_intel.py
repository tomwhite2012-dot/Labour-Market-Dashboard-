"""Draft-specific intelligence: line-ups, rivals' likely waiver claims, trades, buy-low watchlist."""
from collections import Counter, defaultdict
from itertools import product

POS_ORDER = ("GKP", "DEF", "MID", "FWD")
NOISE_SD = 2.5   # how far a rival's valuation may differ from ours (xP over the horizon)
TYPE_POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


# ---------------------------------------------------------------- line-ups
def best_xi(players, gw_index):
    by = {p: sorted([x for x in players if x["pos"] == p], key=lambda x: -x["gws"][gw_index]["xp"]) for p in POS_ORDER}
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
    return {"formation": formation, "xp": round(best_val, 1), "xi": [x["id"] for x in xi], "bench": [x["id"] for x in bench]}


def xi_total(players):
    """Best-XI expected points summed over every gameweek in the horizon."""
    if not players or not players[0]["gws"]:
        return 0.0
    return sum((best_xi(players, i) or {"xp": 0})["xp"] for i in range(len(players[0]["gws"])))


# ---------------------------------------------------------------- rivals
def rival_habits(transactions, elements, entries, last_gw):
    """Activity and positional preferences from the league's waiver/free-agent history."""
    out = {}
    tx = transactions.get("transactions", [])
    for eid, e in entries.items():
        mine = [t for t in tx if t["entry"] == eid]
        w = [t for t in mine if t["kind"] == "w"]
        weeks_active = len({t["event"] for t in w})
        pos = Counter(TYPE_POS[elements[t["element_in"]]["element_type"]] for t in mine
                      if t["element_in"] in elements)
        out[eid] = {
            "team": e["entry_name"],
            "claims": len(w), "claims_won": sum(1 for t in w if t["result"] == "a"),
            "free_agent_moves": sum(1 for t in mine if t["kind"] == "f" and t["result"] == "a"),
            "active_weeks": weeks_active,
            "p_active": round(min(1.0, (weeks_active + 1) / (last_gw + 2)), 2),
            "favourite_position": pos.most_common(1)[0][0] if pos else None,
            "position_mix": dict(pos),
        }
    return out


def simulate_waivers(rival_lists, my_entry, my_claims, waiver_order, habits, n=3000, seed=11):
    """Monte Carlo of FPL Draft waiver rounds.

    rival_lists: entry -> ordered target ids. my_claims: ordered (in, out) pairs.
    Each round, teams go in waiver order and take their highest-listed player still free
    (one per team per round). Rivals submit their list with probability p_active.
    Returns P(you land each target) and the expected xP gain.
    """
    import random
    rng = random.Random(seed)
    order = sorted(waiver_order, key=lambda e: waiver_order[e])
    got = defaultdict(int)
    for _ in range(n):
        lists = {}
        for e in order:
            if e == my_entry or rng.random() >= habits[e]["p_active"]:
                continue
            # rivals don't value players exactly as this model does: add noise to their ranking
            noisy = sorted(rival_lists.get(e, []), key=lambda ig: -(ig[1] + rng.gauss(0, NOISE_SD)))
            lists[e] = [i for i, _ in noisy[:3]]
        taken, dropped = set(), set()
        mine = list(my_claims)
        for _round in range(4):
            for e in order:
                if e == my_entry:
                    while mine:
                        i, o = mine.pop(0)
                        if i not in taken and o not in dropped:
                            taken.add(i)
                            dropped.add(o)
                            got[(i, o)] += 1
                            break
                else:
                    lst = lists.get(e, [])
                    while lst:
                        i = lst.pop(0)
                        if i not in taken:
                            taken.add(i)
                            break
    return {(i, o): got[(i, o)] / n for i, o in my_claims}


def best_claim_order(moves, rival_lists, my_entry, waiver_order, habits):
    """Try every order of the main claims (backups after) and keep the best expected gain."""
    from itertools import permutations
    best = None
    for perm in permutations(moves):
        claims = [(m["in"], m["out"]) for m in perm]
        claims += [(b["id"], m["out"]) for m in perm for b in m["backups"]]
        gains = {(m["in"], m["out"]): m["gain"] for m in perm}
        gains.update({(b["id"], m["out"]): b["gain"] for m in perm for b in m["backups"]})
        probs = simulate_waivers(rival_lists, my_entry, claims, waiver_order, habits)
        ev = sum(probs[k] * gains[k] for k in claims)
        if best is None or ev > best[0]:
            best = (ev, list(perm), probs)
    return best


def rival_claims(P, owners, pool_ids, entries, habits, my_entry, waiver_order):
    """Each rival's most likely claims, and the chance each free agent survives to your pick."""
    squads = defaultdict(list)
    for pid, o in owners.items():
        if o and pid in P:
            squads[o].append(P[pid])
    pool = [P[i] for i in pool_ids if i in P]
    my_pick = waiver_order[my_entry]
    rivals = {}
    take = defaultdict(list)   # target -> [(rival, prob)]
    for eid in entries:
        if eid == my_entry:
            continue
        worst = {pos: min((p["xp_total"] for p in squads[eid] if p["pos"] == pos), default=0) for pos in POS_ORDER}
        wants = sorted(((c["xp_total"] - worst[c["pos"]], c) for c in pool), key=lambda gc: -gc[0])
        wants = [(g, c) for g, c in wants if g > 0.5][:3]
        pa = habits[eid]["p_active"]
        picks = []
        for rank, (g, c) in enumerate(wants):
            prob = pa * (0.55, 0.25, 0.10)[rank]
            picks.append({"id": c["id"], "gain": round(g, 1), "prob": round(prob, 2)})
            if waiver_order[eid] < my_pick:
                take[c["id"]].append((entries[eid]["entry_name"], prob))
        rivals[eid] = {"team": entries[eid]["entry_name"], "waiver": waiver_order[eid],
                       "ahead": waiver_order[eid] < my_pick, "likely_claims": picks,
                       "targets": [(c["id"], g) for g, c in sorted(((c["xp_total"] - worst[c["pos"]], c) for c in pool),
                                                                   key=lambda gc: -gc[0]) if g > 0.5][:8]}
    survival = {}
    for c in pool:
        s = 1.0
        for _, prob in take.get(c["id"], []):
            s *= 1 - prob
        survival[c["id"]] = {"p": round(s, 2), "threats": [t for t, _ in take.get(c["id"], [])]}
    return rivals, survival


# ---------------------------------------------------------------- trades
def suggest_trades(P, owners, entries, my_entry, locked, threshold, limit=5):
    squads = defaultdict(list)
    for pid, o in owners.items():
        if o and pid in P:
            squads[o].append(P[pid])
    mine = squads[my_entry]
    base_me = xi_total(mine)
    ideas = []
    for eid, theirs in squads.items():
        if eid == my_entry:
            continue
        base_them = xi_total(theirs)
        for a in mine:
            if a["id"] in locked:
                continue
            for b in theirs:
                if b["pos"] != a["pos"] or b["xp_total"] <= a["xp_total"]:
                    continue
                me_new = xi_total([x for x in mine if x["id"] != a["id"]] + [b])
                gain_me = me_new - base_me
                if gain_me < threshold:
                    continue
                them_new = xi_total([x for x in theirs if x["id"] != b["id"]] + [a])
                gain_them = them_new - base_them
                if gain_them < 0:
                    continue   # they would be worse off: unlikely to accept
                ideas.append({"give": a["id"], "get": b["id"], "with": entries[eid]["entry_name"],
                              "gain_me": round(gain_me, 1), "gain_them": round(gain_them, 1)})
    ideas.sort(key=lambda t: -(t["gain_me"] + 0.5 * min(t["gain_them"], 2)))
    seen, out = set(), []
    for t in ideas:
        if t["get"] in seen:
            continue
        seen.add(t["get"])
        out.append(t)
        if len(out) == limit:
            break
    return out


# ---------------------------------------------------------------- watchlist
def watchlist(model, P, pool_ids, transactions, entries, last_gw, my_entry, exclude=(), limit=8):
    out = []
    recent_drops = {}
    for t in transactions.get("transactions", []):
        if t["result"] == "a" and t["event"] >= last_gw - 1 and t.get("element_out") and t["entry"] != my_entry:
            recent_drops[t["element_out"]] = entries.get(t["entry"], {}).get("entry_name")
    for pid in pool_ids:
        p = P.get(pid)
        if not p or pid in exclude:
            continue
        if p["status"] in ("d", "i") and not p["long_term_out"]:
            fx = model.fit_xp(pid)
            if fx >= 3.5:
                out.append({"id": pid, "reason": "injured", "fit_xp": fx, "note": p["news"]})
        elif pid in recent_drops and p["xp_total"] >= 6:
            out.append({"id": pid, "reason": "dropped", "fit_xp": p["xp_next"],
                        "note": f"Dropped by {recent_drops[pid]} recently"})
    out.sort(key=lambda w: -w["fit_xp"])
    return out[:limit]
