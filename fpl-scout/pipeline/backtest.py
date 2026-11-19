"""Replay past gameweeks to measure and tune the xP model.

For each finished gameweek g (from START_GW), the model is rebuilt using only matches
before g and its predictions are compared with the points players actually scored.
Two simple benchmarks are scored the same way: points per game so far, and the average
of the last five matches.

    python3 pipeline/backtest.py           # score current settings
    python3 pipeline/backtest.py --tune    # grid-search settings, save the best to config.json

Writes history/backtest.json for the interface.
"""
import itertools
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch import ROOT, fetch_all  # noqa: E402
from model import DEFAULT_PARAMS, Model  # noqa: E402

START_GW = 4
GRID = {
    "recent_weight": [0.0, 0.3, 0.5],
    "prior_mins": [270, 450, 720],
    "strength_shrink": [2.0, 4.0, 8.0],
    "home_adv": [1.10, 1.15, 1.20],
    "cs_scale": [0.8, 0.9, 1.0],
}


def evaluate(data, cfg, params, detail=False):
    pred, act, naive_ppg, naive_l5, mins_err, pos_l = [], [], [], [], [], []
    per_gw = []
    for g in range(START_GW, data["last_finished_gw"] + 1):
        m = Model(data, cfg, cutoff=g, params=params)
        gp, ga, gn = [], [], []
        for pid in m.elements:
            t = m.tot[pid]
            if t["minutes"] == 0:
                continue  # only players who had played before g
            p = m.player(pid)
            st = m.actual[pid].get(g, {})
            a = st.get("total_points", 0)
            games = len(m.team_gws[m.elements[pid]["team"]]) or 1
            last5 = sorted(m.pstats[pid].keys())[-5:]
            pred.append(p["xp_next"])
            act.append(a)
            naive_ppg.append(t["points"] / games)
            naive_l5.append(sum(m.pstats[pid][x].get("total_points", 0) for x in last5) / 5)
            mins_err.append(abs(p["e_mins"] - st.get("minutes", 0)))
            pos_l.append(p["pos"])
            gp.append(p["xp_next"])
            ga.append(a)
            gn.append(t["points"] / games)
        gp, ga, gn = map(np.array, (gp, ga, gn))
        top = np.argsort(-gp)[:30]
        topn = np.argsort(-gn)[:30]
        per_gw.append({"gw": g, "mae": round(float(np.mean(np.abs(gp - ga))), 3),
                       "top30_actual": round(float(ga[top].mean()), 2),
                       "top30_naive": round(float(ga[topn].mean()), 2)})
    pred, act, naive_ppg, naive_l5 = map(np.array, (pred, act, naive_ppg, naive_l5))
    res = {
        "players_scored": int(len(pred)),
        "gameweeks": [START_GW, data["last_finished_gw"]],
        "mae": round(float(np.mean(np.abs(pred - act))), 3),
        "bias": round(float(np.mean(pred - act)), 3),
        "corr": round(float(np.corrcoef(pred, act)[0, 1]), 3),
        "mae_ppg": round(float(np.mean(np.abs(naive_ppg - act))), 3),
        "mae_last5": round(float(np.mean(np.abs(naive_l5 - act))), 3),
        "corr_ppg": round(float(np.corrcoef(naive_ppg, act)[0, 1]), 3),
        "mins_mae": round(float(np.mean(mins_err)), 1),
        "top30_actual": round(float(np.mean([x["top30_actual"] for x in per_gw])), 2),
        "top30_naive": round(float(np.mean([x["top30_naive"] for x in per_gw])), 2),
        "per_gw": per_gw,
    }
    if detail:
        res["by_pos"] = {}
        pos_a = np.array(pos_l)
        for p in ("GKP", "DEF", "MID", "FWD"):
            msk = pos_a == p
            res["by_pos"][p] = {"mae": round(float(np.mean(np.abs(pred[msk] - act[msk]))), 3),
                                "bias": round(float(np.mean(pred[msk] - act[msk])), 3)}
        # calibration: predicted bands vs actual mean
        bands = []
        for lo, hi in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 99)):
            msk = (pred >= lo) & (pred < hi)
            if msk.sum():
                bands.append({"band": f"{lo}-{hi if hi < 99 else '+'}", "n": int(msk.sum()),
                              "pred": round(float(pred[msk].mean()), 2), "actual": round(float(act[msk].mean()), 2)})
        res["calibration"] = bands
    return res


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    data = fetch_all(cfg["entry_id"], cfg["league_id"], use_cache=True)
    base = dict(DEFAULT_PARAMS)
    base.update(cfg.get("model_params", {}))
    if "--tune" in sys.argv:
        before = evaluate(data, cfg, base)
        best, best_score = None, 9e9
        keys = list(GRID)
        for combo in itertools.product(*GRID.values()):
            params = dict(base)
            params.update(dict(zip(keys, combo)))
            if params["home_adv"] != base["home_adv"]:
                params["away_adv"] = round(2 - params["home_adv"] - 0.02, 2)
            r = evaluate(data, cfg, params)
            score = r["mae"] - 0.5 * (r["top30_actual"] / 10)   # accuracy, plus picking the best players
            if score < best_score:
                best, best_score = params, score
        print("before:", {k: before[k] for k in ("mae", "corr", "top30_actual")})
        cfg["model_params"] = {k: best[k] for k in DEFAULT_PARAMS}
        json.dump(cfg, open(os.path.join(ROOT, "config.json"), "w"), indent=2, ensure_ascii=False)
        base = best
        print("tuned params:", cfg["model_params"])
    res = evaluate(data, cfg, base, detail=True)
    res["params"] = {k: base[k] for k in DEFAULT_PARAMS}
    os.makedirs(os.path.join(ROOT, "history"), exist_ok=True)
    json.dump(res, open(os.path.join(ROOT, "history", "backtest.json"), "w"), indent=1)
    print({k: v for k, v in res.items() if k not in ("per_gw", "calibration", "params")})
    print("calibration:", res["calibration"])


if __name__ == "__main__":
    main()
