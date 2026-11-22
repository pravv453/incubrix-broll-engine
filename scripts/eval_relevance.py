#!/usr/bin/env python3
"""Relevance metrics from a filled ratings sheet (the batch runner's ratings_template.csv).

Computes, from YOUR human ratings (column human_rating_1_5):
  * mean relevance, share of scenes rated >= 4 (Outstanding target 85%), coverage
    (share of narration TIME covered by scenes rated >= 3), per-scenario means;
  * agreement between the independent judge (judge_score) and the human ratings:
    Spearman rho, exact agreement, within-1 agreement, quadratic-weighted Cohen's kappa.
Rows without a human rating are ignored. Nothing is estimated or filled in.

  python scripts/eval_relevance.py test-results/run1/ratings_template.csv --out test-results/run1/relevance.json
"""
import argparse
import csv
import json
from collections import defaultdict


def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float | None:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    return round(cov / (va * vb), 4) if va and vb else None


def qw_kappa(a: list[int], b: list[int], k: int = 5) -> float | None:
    n = len(a)
    if n < 3:
        return None
    obs = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[x - 1][y - 1] += 1
    ha = [sum(obs[i]) for i in range(k)]
    hb = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    w = [[(i - j) ** 2 / (k - 1) ** 2 for j in range(k)] for i in range(k)]
    num = sum(w[i][j] * obs[i][j] for i in range(k) for j in range(k))
    den = sum(w[i][j] * ha[i] * hb[j] / n for i in range(k) for j in range(k))
    return round(1 - num / den, 4) if den else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ratings_csv")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(open(a.ratings_csv)) if r.get("human_rating_1_5", "").strip()]
    if not rows:
        raise SystemExit("no human ratings filled in yet (column human_rating_1_5)")
    human = [int(float(r["human_rating_1_5"])) for r in rows]
    dur = [float(r["duration_s"]) for r in rows]
    per = defaultdict(list)
    for r, h in zip(rows, human):
        per[r["scenario"]].append(h)
    res = {
        "rated_scenes": len(rows),
        "mean_human_relevance": round(sum(human) / len(human), 3),
        "share_rated_4_or_5": round(sum(1 for h in human if h >= 4) / len(human), 4),
        "coverage_time_rated_3_plus": round(sum(d for d, h in zip(dur, human) if h >= 3) / sum(dur), 4),
        "per_scenario_mean": {k: round(sum(v) / len(v), 3) for k, v in per.items()},
    }
    pairs = [(int(r["judge_score"]), h) for r, h in zip(rows, human) if r.get("judge_score", "").strip()]
    if pairs:
        j, h = [p[0] for p in pairs], [p[1] for p in pairs]
        res["judge_vs_human"] = {
            "n": len(pairs), "mean_judge": round(sum(j) / len(j), 3),
            "spearman_rho": pearson(ranks(j), ranks(h)),
            "exact_agreement": round(sum(1 for x, y in pairs if x == y) / len(pairs), 4),
            "within_1_agreement": round(sum(1 for x, y in pairs if abs(x - y) <= 1) / len(pairs), 4),
            "quadratic_weighted_kappa": qw_kappa(j, h),
        }
    auto = [(float(r["auto_score"]), h) for r, h in zip(rows, human) if r.get("auto_score", "").strip()]
    if auto:
        res["selector_score_vs_human_spearman"] = pearson(ranks([x for x, _ in auto]), ranks([y for _, y in auto]))
    print(json.dumps(res, indent=2))
    if a.out:
        open(a.out, "w").write(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
