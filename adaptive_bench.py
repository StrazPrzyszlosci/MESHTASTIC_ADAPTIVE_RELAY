#!/usr/bin/env python3
"""Parallel, resumable benchmark driver for ADAPTIVE_RELAY experiments.

Each (scenario, router/variant, seed) triple runs as a separate subprocess of
adaptive_run.py (bit-deterministic). Results appended to a per-matrix CSV; runs
already present in the CSV are skipped (crash-safe resume).

Usage:
  python adaptive_bench.py --matrix dev          # DEV seeds
  python adaptive_bench.py --matrix validation   # VALIDATION seeds (frozen)
  python adaptive_bench.py --matrix custom --scenarios dense,bridge \
      --variants MF,AR --seeds 1,2,3 --simtime-s 600

Writes:
  results_ar/raw_<matrix>.csv        one row per run (full metrics)
  results_ar/agg_<matrix>.csv        mean/median/std/P5/P95 per (scenario, variant)
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OUTDIR = os.path.join(HERE, 'results_ar')

# --- benchmark universes ------------------------------------------------
DEV_SEEDS = list(range(1, 21))
VAL_SEEDS = list(range(1001, 1021))

SCENARIOS_MAIN = ['sparse', 'linear', 'dense', 'bridge', 'hub', 'moving']
SCENARIOS_STRESS = ['very_dense', 'topo_distributed', 'dense_core_edge', 'three_clusters', 'random30']

VARIANTS = {
    # name -> (router, ar_params_overrides)
    'MF':  ('MANAGED_FLOOD', None),
    'AR':  ('ADAPTIVE_RELAY', {}),
    'AR_LPR': ('ADAPTIVE_RELAY', {'enable_lpr': True}),
    'AR_CEF': ('ADAPTIVE_RELAY', {'enable_cef': True}),
    # v0.13 C1: frontier-aware additive theta (paired panel C2)
    'AR_CEF_C1': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_theta_mode': 'v013'}),
    # v0.13 C3: tiered hash-slot jitter (burst de-correlation)
    'AR_CEF_SLOT': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_slot_jitter': True}),
    # v0.13 C1b: frontier rescue (cef_cost death -> tiered echo-cancel defer)
    'AR_CEF_F': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True}),
    # v0.13 C1c: late-slot rescue (deferred-time evidence cancels dense redundancy)
    'AR_CEF_F2': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True,
                                     'cef_frontier_delay_mult': 3.0}),
    # v0.13 C1d: hard-echo rescue (first overheard copy cancels the pending)
    'AR_CEF_F3': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True,
                                     'cef_frontier_delay_mult': 3.0}),
    # v0.13 C1e: regret-gated rescue (suppression-regret EWMA opens the gate)
    'AR_CEF_F4': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True,
                                     'cef_frontier_delay_mult': 3.0}),
    # v0.13 C4: DRF timing sweep (FW+ fixed hop bias x slot width; linear family)
    'AR_HB20_SW10': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 20, 'ripple_jitter_step_ms': 10}),
    'AR_HB20_SW30': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 20, 'ripple_jitter_step_ms': 30}),
    'AR_HB40_SW10': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 40, 'ripple_jitter_step_ms': 10}),
    'AR_HB40_SW20': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 40, 'ripple_jitter_step_ms': 20}),
    'AR_HB60_SW10': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 60, 'ripple_jitter_step_ms': 10}),
    'AR_HB60_SW30': ('ADAPTIVE_RELAY', {'ripple_hop_bias_ms': 60, 'ripple_jitter_step_ms': 30}),
    # v0.13 C4c: ripple must be explicitly ON (ablated OFF by default since v0.9 DRF)
    'AR_DRF_LEG': ('ADAPTIVE_RELAY', {'ripple_enabled': True}),
    'AR_HB20_SW10_R': ('ADAPTIVE_RELAY', {'ripple_enabled': True, 'ripple_hop_bias_ms': 20, 'ripple_jitter_step_ms': 10}),
    'AR_HB40_SW10_R': ('ADAPTIVE_RELAY', {'ripple_enabled': True, 'ripple_hop_bias_ms': 40, 'ripple_jitter_step_ms': 10}),
    'AR_HB60_SW10_R': ('ADAPTIVE_RELAY', {'ripple_enabled': True, 'ripple_hop_bias_ms': 60, 'ripple_jitter_step_ms': 10}),
    # v0.13 C6: chain-aware DM watchdog timeout (round trips in 100-chains
    # exceed the 12 s default -> ~22-27% of failures are false)
    'AR_DM_T20': ('ADAPTIVE_RELAY', {'echo_timeout_ms': 20000}),
    # v0.14 D0: TOP-K bounded neighbor state (K CORE + PROTECTED frontier)
    'AR_K6':  ('ADAPTIVE_RELAY', {'topk_enabled': True, 'topk_k': 6}),
    'AR_K8':  ('ADAPTIVE_RELAY', {'topk_enabled': True, 'topk_k': 8}),
    'AR_K12': ('ADAPTIVE_RELAY', {'topk_enabled': True, 'topk_k': 12}),
    'AR_K16': ('ADAPTIVE_RELAY', {'topk_enabled': True, 'topk_k': 16}),
    # v0.14 D0: strict protection (bottleneck segment only) — real RAM bound
    'AR_K8S': ('ADAPTIVE_RELAY', {'topk_enabled': True, 'topk_k': 8,
                                  'topk_protect_mode': 'strict'}),
    # v0.14: mobility self-demotion (frequent movers act as CLIENT_MUTE)
    'AR_MOB': ('ADAPTIVE_RELAY', {'mobility_enabled': True, 'mobility_demote': True}),
    # v0.14 D2-D4: minimal SHEPHERD-COLLECT (CEF <-> CEF_F4 mode switching)
    'AR_CEF_SHEP': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True,
                                       'shepherd_enabled': True}),
    # v0.14 D2c: lease-ONLY gate (local regret path OFF — proven insufficient
    # in dense; the shepherd's network evidence is the sole rescue authority)
    # + segment-scoped quorum + rescue budget
    'AR_CEF_SHEP2': ('ADAPTIVE_RELAY', {'enable_cef': True, 'cef_frontier_override': True,
                                        'cef_frontier_min_regret': 2.1,
                                        'shepherd_enabled': True}),
    # v0.14 N1: receiver-evidence census (E1 CBB baseline / E2 N1 variants)
    'CBB_K2': ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'fixed', 'n1_k': 2}),
    'CBB_K3': ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'fixed', 'n1_k': 3}),
    'CBB_K4': ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'fixed', 'n1_k': 4}),
    'N1B':    ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'degree'}),
    'N1C':    ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'adaptive'}),
    # v0.14 N E2b: census-window sweep — the window must cover peers'
    # defer+airtime for copies to arrive before the decision (E2: W=1.0
    # closed with copies_before_decision ~0.4-0.8 -> 0 suppressions)
    'N1B_W15': ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'degree', 'n1_w_toa': 1.5}),
    'N1B_W2':  ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'degree', 'n1_w_toa': 2.0}),
    'N1B_W3':  ('ADAPTIVE_RELAY', {'n1_enabled': True, 'n1_mode': 'degree', 'n1_w_toa': 3.0}),
    # v0.14 N3 (E4): ranked whisper — order policies as explicit arms
    'N3_DESIG':   ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'designated'}),
    'N3_EDGE':    ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'edge_first'}),
    'N3_STRONG':  ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first'}),
    # v0.14 E5a: gap x K sweep on the bridge-winner policy (fixed K for control)
    'N3S_K2_G075': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                       'n1_mode': 'fixed', 'n1_k': 2, 'n3_gap_toa': 0.75}),
    'N3S_K2_G125': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                       'n1_mode': 'fixed', 'n1_k': 2, 'n3_gap_toa': 1.25}),
    'N3S_K3_G125': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                       'n1_mode': 'fixed', 'n1_k': 3, 'n3_gap_toa': 1.25}),
    'N3E_K2_G125': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'edge_first',
                                        'n1_mode': 'fixed', 'n1_k': 2, 'n3_gap_toa': 1.25}),
    # v0.14 E8: N3 + SHEPHERD hybrid — census suppression under a COLLECT
    # lease + budget becomes a rescue forward; N3 suppressions feed the
    # shepherd regret trigger (fast local layer, network regulation second)
    'N3S_SHEP2': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                      'n1_mode': 'fixed', 'n1_k': 2, 'n3_gap_toa': 1.25,
                                      'shepherd_enabled': True}),
    # v0.14 E5c: OOD-corrective arm — degree-adaptive census (suppression
    # OFF at frontier degree / high structural risk) for chain/corridor
    # families (rural_corridor falsification fix)
    'N3S_SHEP2D': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                       'n1_mode': 'degree', 'n3_gap_toa': 1.25,
                                       'shepherd_enabled': True}),
    'N3S_SHEP2D_NDB': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                            'n1_mode': 'ndb', 'n3_gap_toa': 1.25,
                                            'shepherd_enabled': True, 'ndb_enabled': True,
                                            'ndb_k2_shield': True}),
    # v0.14 E7/N4: airtime-budgeted rescue on the hybrid (bucket in ms of
    # real ToA; AIMD; structural multiplier)
    'N3S_SHEP2_N4': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                         'n1_mode': 'fixed', 'n1_k': 2, 'n3_gap_toa': 1.25,
                                         'shepherd_enabled': True, 'n4_enabled': True}),
    # v0.14 CEF_BOOST: 'traffic-jam mode' — N3+SHEP2D normally, pure CEF
    # when sustained busy+redundant+low-frontier evidence, instant exit
    # on any frontier signal. The FSM closes the dense/mixed dichotomy.
    'N3S_BOOST': ('ADAPTIVE_RELAY', {'n3_enabled': True, 'n3_order_policy': 'strong_first',
                                      'n1_mode': 'degree', 'n3_gap_toa': 1.25,
                                      'shepherd_enabled': True,
                                      'enable_cef': True, 'cef_boost_enabled': True}),
    # v0.12b HERD layer (B2/B3): cohesion/order wired into decisions
    'AR_HERD':     ('ADAPTIVE_RELAY', {'enable_herd': True}),
    'AR_CEF_HERD': ('ADAPTIVE_RELAY', {'enable_cef': True, 'enable_herd': True}),
    'AR_tuned': ('ADAPTIVE_RELAY', {'density_sparse': 6,
                                    'probe_cooldown_ms': 60000}),
    # ablations (sec. X.8/X.28/§28)
    'AR_noW':       ('ADAPTIVE_RELAY', {'weight_enabled': False}),
    'AR_noInhib':   ('ADAPTIVE_RELAY', {'inhibition_enabled': False}),
    'AR_noBak':     ('ADAPTIVE_RELAY', {'backups_enabled': False}),
    'AR_gossip':    ('ADAPTIVE_RELAY', {'gossip_mode': 'gossip'}),
    'AR_backup_gossip': ('ADAPTIVE_RELAY', {'gossip_mode': 'backup_plus_gossip'}),
    'AR_seg_off':   ('ADAPTIVE_RELAY', {'segmentation': 'off'}),
    'AR_seg_geo':   ('ADAPTIVE_RELAY', {'segmentation': 'geo'}),
    'AR_seg2':      ('ADAPTIVE_RELAY', {'sector_count': 2}),
    'AR_seg6':      ('ADAPTIVE_RELAY', {'sector_count': 6}),
    'AR_seg8':      ('ADAPTIVE_RELAY', {'sector_count': 8}),
}

DEFAULT_SIMTIME = {'dense': 900, 'very_dense': 900, 'moving': 900}
DEFAULT_SIMTIME_DEFAULT = 600


def run_one(scenario, variant, seed, simtime_s, period_s, hop_limit, dms,
            capture_db=None, drift_ppm=None, modem=None,
            n_nodes=None, xsize=None, ysize=None, extra=None):
    router, ar_over = VARIANTS[variant]
    cmd = [PY, 'adaptive_run.py', '--scenario', scenario, '--router', router,
           '--seed', str(seed), '--simtime-s', str(simtime_s),
           '--period-s', str(period_s)]
    if hop_limit:
        cmd += ['--hop-limit', str(hop_limit)]
    if dms is not None:
        cmd += ['--dms', str(dms)]
    if capture_db is not None:
        cmd += ['--capture-db', str(capture_db)]
    if drift_ppm is not None:
        cmd += ['--clock-drift-ppm', str(drift_ppm)]
    if modem is not None:
        cmd += ['--modem', str(modem)]
    if n_nodes is not None:
        cmd += ['--n-nodes', str(n_nodes)]
    if xsize is not None:
        cmd += ['--xsize', str(xsize)]
    if ysize is not None:
        cmd += ['--ysize', str(ysize)]
    # D5/D6 pass-through: kill_at_s=300 kill_role=bridge revive_at_s=420
    # deaf_at_s=300 deaf_until_s=420 deaf_nodes=bridge (generic key=val)
    for kv in (extra or []):
        k, v = kv.split('=', 1)
        flag = '--' + k.replace('_', '-')
        # boolean convention: k=1/true/on -> bare flag (store_true)
        if v.lower() in ('1', 'true', 'on', 'yes'):
            cmd += [flag]
        else:
            cmd += [flag, v]
    tmpf = None
    if ar_over is not None and router == 'ADAPTIVE_RELAY':
        tmpf = os.path.join(OUTDIR, f'_tmp_params_{variant}_{scenario}_{seed}.json')
        with open(tmpf, 'w') as f:
            json.dump(ar_over, f)
        cmd += ['--ar-params', tmpf]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, cwd=HERE)
        line = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not line:
            return None, f'rc={proc.returncode} err={proc.stderr[-800:]}'
        row = json.loads(line[-1])
        row['variant'] = variant
        row['ar_params'] = None  # drop bulky echo from CSV; variants identify params
        return row, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'
    finally:
        if tmpf and os.path.exists(tmpf):
            try:
                os.remove(tmpf)
            except OSError:
                pass


def flatten_row(row):
    flat = {}
    for k, v in row.items():
        if isinstance(v, (dict, list)):
            if k == 'adaptive_stats':
                for sk, sv in v.items():
                    flat[sk] = sv
            elif k == 'meta':
                for sk, sv in v.items():
                    if not isinstance(sv, (dict, list)):
                        flat[f'meta_{sk}'] = sv
            # skip raw series (tx_per_60s etc.) in CSV; keep in JSON if needed
        else:
            flat[k] = v
    return flat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--matrix', default='dev',
                    help='dev/validation are predefined; any other name runs a '
                         'custom matrix writing raw_<name>.csv (seeds must be '
                         'passed explicitly, resume works per name)')
    ap.add_argument('--scenarios', default=None)
    ap.add_argument('--variants', default=None)
    ap.add_argument('--seeds', default=None)
    ap.add_argument('--simtime-s', type=int, default=None)
    ap.add_argument('--period-s', type=int, default=30)
    ap.add_argument('--hop-limit', type=int, default=None)
    ap.add_argument('--dms', type=int, default=None)
    ap.add_argument('--capture-db', type=float, default=None)      # v0.14 D1
    ap.add_argument('--clock-drift-ppm', type=float, default=None)  # v0.14 D1
    ap.add_argument('--modem', default=None)                        # v0.14 S4
    ap.add_argument('--n-nodes', type=int, default=None)           # v0.14 S5
    ap.add_argument('--xsize', type=float, default=None)           # v0.14 S5
    ap.add_argument('--ysize', type=float, default=None)           # v0.14 S5
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--extra', default=None,
                    help='runner flag pass-through, comma-separated key=val, '
                         'e.g. --extra kill_at_s=300,kill_role=bridge,revive_at_s=420 '
                         '(D5/D6 failure experiments)')
    args = ap.parse_args()

    os.makedirs(OUTDIR, exist_ok=True)
    scenarios = args.scenarios.split(',') if args.scenarios else SCENARIOS_MAIN
    variants = args.variants.split(',') if args.variants else ['MF', 'AR']
    if args.seeds:
        seeds = [int(x) for x in args.seeds.split(',')]
    elif args.matrix == 'validation':
        seeds = VAL_SEEDS
    else:
        seeds = DEV_SEEDS
    name = args.matrix

    csv_path = os.path.join(OUTDIR, f'raw_{name}.csv')
    jsonl_path = os.path.join(OUTDIR, f'raw_{name}.jsonl')

    done = set()
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            for r in csv.DictReader(f):
                done.add((r['scenario'], r['variant'], int(r['seed'])))

    jobs = []
    for scenario in scenarios:
        st = args.simtime_s or DEFAULT_SIMTIME.get(scenario, DEFAULT_SIMTIME_DEFAULT)
        for variant in variants:
            for seed in seeds:
                if (scenario, variant, seed) not in done:
                    jobs.append((scenario, variant, seed, st))

    print(f'{len(jobs)} runs to do ({len(done)} already complete). Matrix={name}')
    t0 = time.time()
    done_n = 0
    failed = []

    cf = open(csv_path, 'a', newline='')
    jf = open(jsonl_path, 'a')
    writer = None
    fields = None

    def _expand_header(missing):
        """v0.13: union header — the first completed row may lack keys that
        later rows carry (e.g. MF rows have no adaptive_stats diagnostics).
        Rewrite the CSV with the union of fields so diagnostic columns are
        not silently dropped; crash-safe (tmp + os.replace)."""
        nonlocal cf, writer, fields
        cf.close()
        with open(csv_path, newline='') as f0:
            existing = list(csv.DictReader(f0))
        fields = fields + missing
        tmp = csv_path + '.tmp'
        with open(tmp, 'w', newline='') as f1:
            w1 = csv.DictWriter(f1, fieldnames=fields)
            w1.writeheader()
            for r0 in existing:
                w1.writerow({k: (r0.get(k) or '') for k in fields})
        os.replace(tmp, csv_path)
        cf = open(csv_path, 'a', newline='')
        writer = csv.DictWriter(cf, fieldnames=fields)

    try:
        extra = args.extra.split(';') if args.extra else []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_one, sc, va, se, st, args.period_s, args.hop_limit, args.dms,
                               args.capture_db, args.clock_drift_ppm, args.modem,
                               args.n_nodes, args.xsize, args.ysize, extra):
                    (sc, va, se) for (sc, va, se, st) in jobs}
            for fut in as_completed(futs):
                sc, va, se = futs[fut]
                row, err = fut.result()
                done_n += 1
                if row is None:
                    failed.append((sc, va, se, err))
                    print(f'FAIL {sc}/{va}/s{se}: {err}')
                    continue
                flat = flatten_row(row)
                if writer is None:
                    fields = list(flat.keys())
                    if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                        # reuse existing header
                        with open(csv_path) as f0:
                            fields = next(csv.reader(f0))
                    writer = csv.DictWriter(cf, fieldnames=fields)
                    if os.path.getsize(csv_path) == 0:
                        writer.writeheader()
                missing = [k for k in flat if k not in fields]
                if missing:
                    _expand_header(missing)
                writer.writerow({k: flat.get(k, '') for k in fields})
                cf.flush()
                jf.write(json.dumps(row) + '\n')
                jf.flush()
                if done_n % 10 == 0 or done_n == len(jobs):
                    rate = done_n / (time.time() - t0)
                    eta = (len(jobs) - done_n) / max(rate, 1e-9)
                    print(f'[{done_n}/{len(jobs)}] {rate:.2f} runs/s ETA {eta/60:.1f} min')
    finally:
        cf.close()
        jf.close()

    if failed:
        print(f'{len(failed)} FAILED runs')
        for f in failed[:10]:
            print('  ', f)
    print(f'Done in {(time.time()-t0)/60:.1f} min -> {csv_path}')


if __name__ == '__main__':
    main()
