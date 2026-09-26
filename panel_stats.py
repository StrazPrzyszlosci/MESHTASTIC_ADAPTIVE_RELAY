#!/usr/bin/env python3
"""Paired panel statistics (v0.14 doctrine): for each (scenario, candidate)
vs a baseline, report mean/median/SD, paired differences on identical
seeds, 95% CI of the paired mean (normal approx), and the seed win-rate.

Usage:
  python panel_stats.py results_ar/raw_<matrix>.jsonl --baseline MF \
      [--candidates N3S_K2_G125,N3S_SHEP2] [--metrics reach,tx_count]
"""
import argparse
import json
import math
import statistics as st
import sys


def load_rows(path):
    rows = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            key = (r['scenario'], r['variant'], r['seed'])
            rows[key] = r          # last occurrence wins (dedup)
    return rows


def ci95(pairs):
    n = len(pairs)
    if n < 2:
        return (float('nan'), float('nan'))
    m = st.mean(pairs)
    sd = st.stdev(pairs)
    return (m - 1.96 * sd / math.sqrt(n), m + 1.96 * sd / math.sqrt(n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('jsonl')
    ap.add_argument('--baseline', default='MF')
    ap.add_argument('--candidates', default=None,
                    help='comma list; default = all non-baseline variants')
    ap.add_argument('--metrics', default='reach,tx_count')
    ap.add_argument('--scenarios', default=None)
    args = ap.parse_args()

    rows = load_rows(args.jsonl)
    scenarios = sorted({k[0] for k in rows})
    if args.scenarios:
        scenarios = [s for s in scenarios if s in args.scenarios.split(',')]
    variants = sorted({k[1] for k in rows})
    cands = (args.candidates.split(',') if args.candidates
             else [v for v in variants if v != args.baseline])
    metrics = args.metrics.split(',')

    for scen in scenarios:
        seeds = sorted({k[2] for k in rows if k[0] == scen and k[1] == args.baseline})
        if not seeds:
            continue
        print(f"== {scen} (n={len(seeds)} seeds, baseline {args.baseline})")
        for cand in cands:
            cseeds = sorted({k[2] for k in rows if k[0] == scen and k[1] == cand})
            common = sorted(set(seeds) & set(cseeds))
            if not common:
                continue
            line = [f"   {cand:16s}"]
            for met in metrics:
                base_vals = [rows[(scen, args.baseline, s)].get(met) for s in common]
                cand_vals = [rows[(scen, cand, s)].get(met) for s in common]
                pairs = [c - b for c, b in zip(cand_vals, base_vals)
                         if c is not None and b is not None]
                if not pairs:
                    line.append(f" {met}: n/a")
                    continue
                wins = sum(1 for d in pairs if d > 0)
                lo, hi = ci95(pairs)
                pm = st.mean(pairs)
                line.append(
                    f" {met}: d={pm:+.4f} [{lo:+.4f},{hi:+.4f}] "
                    f"med={st.median(pairs):+.4f} sd={st.stdev(pairs):.4f} "
                    f"wins={wins}/{len(pairs)}")
            print(''.join(line).rstrip())
        print()


if __name__ == '__main__':
    sys.exit(main())
