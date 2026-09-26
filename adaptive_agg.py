#!/usr/bin/env python3
"""Aggregate benchmark CSVs into comparison tables (mean/median/std/P5/P95).

Usage:
  python adaptive_agg.py results_ar/raw_dev.csv [--baseline MF] [--out results_ar/agg_dev.csv]
"""
import argparse
import csv
import os
import sys

import numpy as np

METRICS = [
    'reach', 'tx_count', 'tx_airtime_ms', 'collisions', 'collision_rate',
    'latency_mean_ms', 'latency_p95_ms', 'messages_generated',
    'tx_per_delivered', 'max_tx_per_node', 'avg_tx_per_node', 'p95_tx_per_node',
    'retransmissions', 'dropped',
    'adaptive_primary_selected', 'adaptive_primary_success',
    'adaptive_primary_failure', 'adaptive_backup1_triggered',
    'adaptive_backup1_success', 'adaptive_backup2_triggered',
    'adaptive_mini_flood_triggered', 'adaptive_mpr_forward',
    'adaptive_suppressed_forward', 'adaptive_route_switches',
    'adaptive_primary_flaps', 'adaptive_same_sector_inhibition',
    'adaptive_cross_sector_suppression', 'adaptive_gossip_forwards',
    'adaptive_unique_coverage_preserved',
]

PCT_STATS = ['mean', 'median', 'std', 'p5', 'p95']


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def load(path):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def agg(rows, group_key):
    """rows -> {group: {metric: {stat: value}}}"""
    groups = {}
    for r in rows:
        g = r[group_key]
        groups.setdefault(g, []).append(r)
    out = {}
    for g, rs in groups.items():
        m = {}
        for met in METRICS:
            vals = np.array([fnum(r.get(met, np.nan)) for r in rs], dtype=float)
            vals = vals[~np.isnan(vals)]
            if len(vals) == 0:
                m[met] = {s: np.nan for s in PCT_STATS}
                continue
            m[met] = {
                'mean': float(np.mean(vals)),
                'median': float(np.median(vals)),
                'std': float(np.std(vals)),
                'p5': float(np.percentile(vals, 5)),
                'p95': float(np.percentile(vals, 95)),
                'n': len(vals),
            }
        out[g] = m
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv_path')
    ap.add_argument('--baseline', default='MF')
    ap.add_argument('--out', default=None)
    ap.add_argument('--per-scenario', action='store_true', default=True)
    args = ap.parse_args()

    rows = load(args.csv_path)
    out_path = args.out or args.csv_path.replace('raw_', 'agg_')

    # group by (scenario, variant)
    keyed = []
    for r in rows:
        r2 = dict(r)
        r2['group'] = f"{r['scenario']}|{r['variant']}"
        keyed.append(r2)
    A = agg(keyed, 'group')

    scenarios = sorted({r['scenario'] for r in rows})
    variants = sorted({r['variant'] for r in rows})

    lines = []
    hdr = ['scenario', 'variant', 'n'] + [f'{m}.{s}' for m in METRICS for s in PCT_STATS]
    writer = None
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(hdr)
        for sc in scenarios:
            for va in variants:
                g = f'{sc}|{va}'
                if g not in A:
                    continue
                row = [sc, va, A[g]['reach'].get('n', 0)]
                for met in METRICS:
                    st = A[g][met]
                    for s in PCT_STATS:
                        v = st.get(s)
                        row.append('' if v is None or (isinstance(v, float) and np.isnan(v)) else round(v, 5))
                writer.writerow(row)

    # console comparison table vs baseline
    def get(sc, va, met, stat='mean'):
        g = f'{sc}|{va}'
        if g not in A:
            return np.nan
        return A[g][met].get(stat, np.nan)

    print(f'== {args.csv_path} (baseline {args.baseline}, mean values) ==')
    mets = ['reach', 'tx_count', 'collisions', 'latency_mean_ms', 'tx_per_delivered', 'max_tx_per_node']
    print(f"{'scenario':<18}" + ''.join(f'{m:>14}' for m in mets))
    for sc in scenarios:
        for va in variants:
            vals = [get(sc, va, m) for m in mets]
            print(f'{sc:<18}' + ''.join(f'{v:>14.3f}' if not np.isnan(v) else f'{"n/a":>14}' for v in vals))
        # deltas vs baseline
        base_reach = get(sc, args.baseline, 'reach')
        for va in variants:
            if va == args.baseline:
                continue
            r = get(sc, va, 'reach')
            tx = get(sc, va, 'tx_count')
            bt = get(sc, args.baseline, 'tx_count')
            co = get(sc, va, 'collisions')
            bc = get(sc, args.baseline, 'collisions')
            if not np.isnan(base_reach) and base_reach > 0:
                dr = 100 * (r - base_reach) / base_reach
                dt = 100 * (tx - bt) / bt if bt else np.nan
                dc = 100 * (co - bc) / bc if bc else np.nan
                print(f'  {va:>14} vs {args.baseline}: reach {dr:+.1f}%  tx {dt:+.1f}%  coll {dc:+.1f}%')
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
