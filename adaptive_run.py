#!/usr/bin/env python3
"""Deterministic single-run executor for ADAPTIVE_RELAY experiments.

Guarantees:
  - same (scenario, seed) => same topology and same MAC randomness for every router
  - no GUI, no plotting
  - full metric JSON on stdout (single line)

Usage:
  python adaptive_run.py --scenario dense --router ADAPTIVE_RELAY --seed 7 \
      [--simtime-s 900] [--period-s 30] [--hop-limit N] [--dms 0|1] \
      [--ar-params file.json] \
      [--kill-at-s T --kill-nodes 1,2] [--revive-at-s T]
"""
import argparse
import json
import random
import sys

import numpy as np


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario', required=True)
    ap.add_argument('--router', required=True)
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--simtime-s', type=int, default=900)
    ap.add_argument('--period-s', type=int, default=30)
    ap.add_argument('--hop-limit', type=int, default=None)
    ap.add_argument('--dms', type=int, default=None)
    ap.add_argument('--capture-db', type=float, default=None,
                   help='PHY capture threshold dB (0 = OFF; default 6 = historical)')
    ap.add_argument('--clock-drift-ppm', type=float, default=None,
                   help='per-node deterministic clock drift +/-ppm (0 = perfect clocks)')
    ap.add_argument('--n-nodes', type=int, default=None, help='topology override')
    ap.add_argument('--ar-params', default=None)
    ap.add_argument('--kill-at-s', type=float, default=None)
    ap.add_argument('--kill-nodes', default=None, help='comma list')
    ap.add_argument('--kill-role', default=None,
                    help='kill by scenario role: bridge|hub|middle (resolved from scenario meta)')
    ap.add_argument('--kill-mode', default='relay', choices=('relay', 'dead'),
                    help="relay: node stops relaying but keeps generating (demotion); "
                         "dead: full silence (hardware death)")
    ap.add_argument('--revive-at-s', type=float, default=None)
    ap.add_argument('--deaf-at-s', type=float, default=None,
                    help='D6: start a deaf window (RX disabled) on --deaf-nodes')
    ap.add_argument('--deaf-until-s', type=float, default=None, help='end of the deaf window')
    ap.add_argument('--deaf-nodes', default=None, help='comma list, or role name like bridge|hub')
    ap.add_argument('--no-hopstart', action='store_true',
                    help='ablation: packets do not carry hopStart (NodeDB-evidence '
                         'absent world; router must degrade to baseline)')
    ap.add_argument('--phy-model', type=int, default=5)
    ap.add_argument('--modem', default='LONG_FAST')
    ap.add_argument('--xsize', type=float, default=None)
    ap.add_argument('--ysize', type=float, default=None)
    args = ap.parse_args()

    from lib.config import Config
    from lib.discrete_event_sim import DiscreteEventSim
    from adaptive_scenarios import build_scenario, SCENARIOS

    conf = Config()
    conf.SEED = args.seed
    conf.NR_NODES = 0  # set later
    conf.SIMTIME = args.simtime_s * 1000
    conf.PERIOD = args.period_s * 1000
    conf.MODEL = args.phy_model
    if args.no_hopstart:
        conf.MODEL_HOPSTART = False
    conf.MODEM_PRESET = args.modem
    if args.xsize:
        conf.XSIZE = args.xsize
    if args.ysize:
        conf.YSIZE = args.ysize
    conf.SELECTED_ROUTER_TYPE = Config.ROUTER_TYPE(args.router)
    if args.hop_limit is not None:
        conf.hopLimit = args.hop_limit
    else:
        conf.hopLimit = SCENARIOS[args.scenario][3]
    if args.dms is not None:
        conf.DMs = bool(args.dms)
    else:
        conf.DMs = SCENARIOS[args.scenario][4]

    # v0.14 D1: PHY/MAC realism knobs (echoed for auditability)
    if args.capture_db is not None:
        conf.CAPTURE_THRESHOLD_DB = args.capture_db
    if args.clock_drift_ppm is not None:
        conf.CLOCK_DRIFT_PPM = args.clock_drift_ppm

    if args.ar_params:
        with open(args.ar_params) as f:
            conf.AR_PARAMS.update(json.load(f))
    conf.update_router_dependencies()

    # Movement: only 'moving' scenario is mobile; everything else static so that
    # reach comparisons are not diluted by motion unless intended.
    conf.MOVEMENT_ENABLED = (args.scenario == 'moving')

    # Deterministic everything: placement, movement-independent MAC draws
    random.seed(args.seed)

    node_configs, meta = build_scenario(conf, args.scenario, args.seed,
                                        n_override=args.n_nodes)
    conf.NR_NODES = len(node_configs)
    random.seed(args.seed)  # re-seed right before the sim (post-generation)

    sim = DiscreteEventSim(conf, node_configs)

    # planned failure / revival events (failure tests)
    kill_nodes = []
    if args.kill_role:
        # resolve by scenario role: the structurally critical node(s)
        if args.kill_role == 'bridge':
            kill_nodes = list(meta.get('bridge_ids', []))
        elif args.kill_role == 'hub':
            kill_nodes = [meta.get('hub_id', 0)]
        elif args.kill_role == 'middle':
            kill_nodes = [len(node_configs) // 2]
        else:
            raise SystemExit(f'unknown --kill-role {args.kill_role}')
        if not kill_nodes:
            raise SystemExit(f'scenario {args.scenario} has no {args.kill_role} role')
        log(f'kill-role {args.kill_role} -> nodes {kill_nodes}')
    elif args.kill_at_s is not None and args.kill_nodes:
        kill_nodes = [int(x) for x in args.kill_nodes.split(',') if x != '']
    t_kill = args.kill_at_s * 1000 if kill_nodes and args.kill_at_s is not None else None
    env = sim.get_env()

    def _kill_process():
        yield env.timeout(t_kill)
        for nid in kill_nodes:
            node = sim.mutated_state.nodes[nid]
            node.failed = True
            if args.kill_mode == 'dead':
                node.dead = True
            log(f't={env.now}: node {nid} KILLED (mode={args.kill_mode})')
        if args.revive_at_s is not None:
            yield env.timeout(args.revive_at_s * 1000 - t_kill)
            for nid in kill_nodes:
                node = sim.mutated_state.nodes[nid]
                node.failed = False
                node.dead = False
                log(f't={env.now}: node {nid} REVIVED')

    if t_kill is not None:
        env.process(_kill_process())

    # D6 deafness window: RX disabled on given nodes (TX unaffected)
    deaf_nodes = []
    if args.deaf_at_s is not None:
        dn = args.deaf_nodes or ''
        if dn in ('bridge', 'hub', 'middle'):
            # same role resolution as kill
            if dn == 'bridge':
                deaf_nodes = list(meta.get('bridge_ids', []))
            elif dn == 'hub':
                deaf_nodes = [meta.get('hub_id', 0)]
            else:
                deaf_nodes = [len(node_configs) // 2]
        else:
            deaf_nodes = [int(x) for x in dn.split(',') if x != '']
        if not deaf_nodes:
            raise SystemExit('--deaf-at-s given but no --deaf-nodes resolved')

        def _deaf_process():
            yield env.timeout(args.deaf_at_s * 1000)
            for nid in deaf_nodes:
                node = sim.mutated_state.nodes[nid]
                node.deaf_until = (args.deaf_until_s if args.deaf_until_s is not None
                                   else args.simtime_s) * 1000
                log(f't={env.now}: node {nid} DEAF until {node.deaf_until}')
            if args.deaf_until_s is not None:
                yield env.timeout(args.deaf_until_s * 1000 - args.deaf_at_s * 1000)
                for nid in deaf_nodes:
                    node = sim.mutated_state.nodes[nid]
                    node.deaf_until = None
                    log(f't={env.now}: node {nid} hearing restored')

        env.process(_deaf_process())

    sim.run_simulation()

    # ---------------- metrics ----------------
    pkts = sim.mutated_state.packets
    nodes = sim.mutated_state.nodes
    msgs = sim.mutated_state.messageSeq.peek()
    messages = sim.data_tracking.messages
    N = conf.NR_NODES

    tx = len(pkts)
    col = sum(sum(p.collidedAtN) for p in pkts)
    sensed = sum(sum(p.sensedByN) for p in pkts)
    received = sum(sum(p.receivedAtN) for p in pkts)
    useful = sum(n.usefulPackets for n in nodes)
    delays = sim.data_tracking.delays
    reach = useful / max(msgs * (N - 1), 1)
    dropped = sum(n.droppedByDelay for n in nodes)
    tx_airtime_ms = sum(n.txAirUtilization for n in nodes)

    # TX per node (fairness)
    tx_per_node = [0] * N
    for p in pkts:
        tx_per_node[p.txNodeId] += 1
    tx_per_node_sorted = sorted(tx_per_node)

    # retransmissions (origin retries due to missing ACK/implicit ACK)
    retransmissions = sum(1 for p in pkts
                          if p.txNodeId == p.origTxNodeId and p.retransmissions < conf.maxRetransmission)

    # latency
    d = np.array(delays) if delays else np.array([np.nan])

    # window series (60 s)
    W = 60000
    n_win = int(conf.SIMTIME / W) + 1
    tx_win = [0] * n_win
    useful_win = [0] * n_win
    for p in pkts:
        w = min(int(p.startTime / W), n_win - 1)
        tx_win[w] += 1
    # deliveries by reception time
    for n in nodes:
        for p in pkts:
            if p.receivedAtN[n.nodeid] and p.origTxNodeId != n.nodeid:
                pass  # (delivered below via receivedAtN scan with seq dedup)
    # per-message first-delivery bookkeeping
    seen_pairs = set()
    deliv_win = [0] * n_win
    for p in pkts:
        for n in nodes:
            if p.receivedAtN[n.nodeid] and n.nodeid != p.origTxNodeId:
                key = (p.seq, n.nodeid)
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    w = min(int(max(p.startTime, 0) / W), n_win - 1)
                    deliv_win[w] += 1

    # v0.14 D9: BIDIRECTIONAL CONNECTIVITY (OFFLINE diagnostic only — reads
    # the oracle-level reception matrix; NEVER an algorithm input. Doctrine
    # refinement: usable connectivity > TX; reachability != bidirectional
    # connectivity — a flood that reached Z does not imply Z can answer A.)
    conn_pairs = set()
    for p in pkts:
        for n in nodes:
            if p.receivedAtN[n.nodeid] and n.nodeid != p.origTxNodeId:
                conn_pairs.add((p.origTxNodeId, n.nodeid))
    bidi_pairs = sum(1 for (a, b) in conn_pairs if (b, a) in conn_pairs)
    one_way_pairs = len(conn_pairs)
    # request-response conversations: ACK received by the requester (dms runs)
    conversations = 0
    for p in pkts:
        if getattr(p, 'isAck', False) and p.destId is not None \
                and p.destId != 0xFFFFFFFF and p.receivedAtN[p.destId]:
            conversations += 1

    # cold-start: reach of messages generated in the first 60s / 300s / overall
    def reach_generated_before(t_ms):
        sel = [m for m in messages if m.genTime <= t_ms]
        if not sel:
            return None
        sets = {m.seq for m in sel}
        recv = 0
        total = 0
        for m in sel:
            msg_pkts = [p for p in pkts if p.seq == m.seq]
            for n in nodes:
                if n.nodeid == m.origTxNodeId:
                    continue
                total += 1
                if any(p.receivedAtN[n.nodeid] for p in msg_pkts):
                    recv += 1
        return recv / max(total, 1)

    out = {
        # experiment identity (auditability / determinism)
        'router': args.router, 'scenario': args.scenario, 'seed': args.seed,
        'nodes': N, 'simtime_s': args.simtime_s, 'period_s': args.period_s,
        'hop_limit': conf.hopLimit, 'dms': conf.DMs, 'modem': args.modem,
        'capture_db': getattr(conf, 'CAPTURE_THRESHOLD_DB', 6),
        'clock_drift_ppm': getattr(conf, 'CLOCK_DRIFT_PPM', 0),
        'model': args.phy_model, 'packet_len': conf.PACKETLENGTH,
        'movement': conf.MOVEMENT_ENABLED,
        'kill_nodes': kill_nodes, 'kill_at_s': args.kill_at_s,
        'revive_at_s': args.revive_at_s, 'kill_mode': args.kill_mode,
        'deaf_nodes': deaf_nodes,
        'deaf_at_s': args.deaf_at_s, 'deaf_until_s': args.deaf_until_s,
        'ar_params': getattr(conf, 'AR_PARAMS', {}) if args.router == 'ADAPTIVE_RELAY' else None,

        # core metrics
        'messages_generated': msgs,
        'messages_delivered_pairs': useful,
        'reach': reach,
        'tx_count': tx,
        'tx_airtime_ms': tx_airtime_ms,
        'collisions': col,
        'sensed': sensed, 'received': received,
        'collision_rate': col / max(sensed, 1),
        'latency_mean_ms': float(np.nanmean(d)),
        'latency_p95_ms': float(np.nanpercentile(d, 95)),
        'retransmissions': retransmissions,
        'dropped': dropped,
        'tx_per_delivered': tx / max(useful, 1),
        # v0.14 D9: bidirectional connectivity (offline diagnostics)
        'one_way_pairs': one_way_pairs,
        'bidirectional_pairs': bidi_pairs,
        'bidirectional_reach': round(bidi_pairs / max(1, one_way_pairs), 4),
        'conversations': conversations,
        'ce_conversations_per_airtime_s': round(
            conversations / max(1e-9, tx_airtime_ms / 1000.0), 6),
        'max_tx_per_node': max(tx_per_node) if tx_per_node else 0,
        'avg_tx_per_node': float(sum(tx_per_node) / max(N, 1)),
        'p95_tx_per_node': int(tx_per_node_sorted[int(0.95 * (N - 1))]) if N else 0,

        # cold start
        'reach_gen_first60s': reach_generated_before(60000),
        'reach_gen_first300s': reach_generated_before(300000),

        # series
        'tx_per_60s': tx_win, 'delivered_per_60s': deliv_win,

        # scenario meta (reporting only)
        'meta': meta,
    }

    # v0.13 C3: transmission burstiness — global TX timeline analysis
    # (startTime/endTime are set in node.transmit at ACTUAL TX start;
    # default 0/0 for canceled/never-sent packets -> filter both > 0)
    tx_events = []
    for n in nodes:
        for pk in getattr(n, 'packets', []):
            st = getattr(pk, 'startTime', 0)
            et = getattr(pk, 'endTime', 0)
            if st > 0 and et > st:
                tx_events.append((st, et))
    if len(tx_events) >= 2:
        starts = sorted(s for s, _ in tx_events)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        gaps.sort()

        def _q(xs, p):
            return xs[int(p * (len(xs) - 1))]

        out['tx_start_interarrival_p50_ms'] = _q(gaps, 0.50)
        out['tx_start_interarrival_p95_ms'] = _q(gaps, 0.95)
        evs = []
        for s, e in tx_events:
            evs.append((s, 1))
            evs.append((e, -1))
        evs.sort()
        cur = peak = 0
        for _, d in evs:
            cur += d
            peak = max(peak, cur)
        out['simultaneous_tx_peak'] = peak
        buckets = {}
        for s in starts:
            buckets[int(s // 100)] = buckets.get(int(s // 100), 0) + 1
        sizes = sorted(buckets.values())
        out['tx_burst_size_p95'] = _q(sizes, 0.95)
    if out.get('tx_count'):
        out['collision_per_tx'] = round(
            out['collisions'] / max(1, out['tx_count']), 4)
    if out.get('messages_delivered_pairs'):
        out['collision_per_delivered'] = round(
            out['collisions'] / max(1, out['messages_delivered_pairs']), 4)

    # router-specific adaptive counters
    if args.router == 'ADAPTIVE_RELAY':
        agg = {}
        fatigues = []
        n_ar = 0
        for n in nodes:
            if getattr(n, 'adaptive', None):
                a = n.adaptive
                n_ar += 1
                for k, v in a.stats.items():
                    if isinstance(v, list):
                        # bounded G samples: collect for cef_G_mean below
                        agg.setdefault('cef_G_samples', []).extend(v)
                        continue
                    if not isinstance(v, (int, float)):
                        continue
                    agg[k] = agg.get(k, 0) + v
                fatigues.append(a.relay_fatigue)
                # v0.3 state diagnostics (why the algorithm behaves as it does)
                agg['known_1hop'] = agg.get('known_1hop', 0) + len(a.neighbors)
                if a.neighbors:
                    agg['known_2hop'] = agg.get('known_2hop', 0) + len(a.two_hop_view(next(iter(a.neighbors))))
                else:
                    agg['known_2hop'] = agg.get('known_2hop', 0) + 0
                agg['known_2hop_entries'] = agg.get('known_2hop_entries', 0) + len(a.neighbors_of_nb)
                agg['advertised_entries'] = agg.get('advertised_entries', 0) + len(a.advertised_neighbors)
                agg['functional_segments'] = agg.get('functional_segments', 0) + len(set(a.segment_of.values()))
                agg['segment_changes'] = agg.get('segment_changes', 0) + getattr(a, '_segment_changes', 0)
        if fatigues:
            agg['relay_fatigue_mean'] = float(sum(fatigues) / len(fatigues))
            agg['relay_fatigue_max'] = float(max(fatigues))
        # v0.14 D0: table-size gauge — sum over nodes is meaningless; mean
        if 'topk_table_high' in agg:
            agg['topk_table_high_mean'] = round(agg.pop('topk_table_high') / n_ar, 3)
        # v0.9 CEF diagnostics
        g_samples = agg.pop('cef_G_samples', [])
        if g_samples:
            agg['cef_G_mean'] = float(sum(g_samples) / len(g_samples))
            g_sorted = sorted(g_samples)
            agg['cef_G_p95'] = float(g_sorted[int(0.95 * (len(g_sorted) - 1))])
        # v0.12b B1' herd diagnostics: running sums -> per-decision means
        herd_n = agg.pop('herd_samples', 0)
        if herd_n:
            for mk in ('herd_cohesion', 'herd_repulsion', 'herd_alignment',
                       'herd_routing_order', 'herd_packet_need',
                       'herd_structural_risk'):
                s = agg.pop(mk + '_sum', None)
                if s is not None:
                    agg[mk + '_mean'] = round(s / herd_n, 4)
            agg['high_packet_need_frac'] = round(
                agg.pop('high_packet_need_seen', 0) / herd_n, 4)
        # role split (v0.9 tactical roles)
        role_tx = {}
        for n in nodes:
            r = getattr(n.role, 'value', str(getattr(n, 'role', '?')))
            role_tx[r] = role_tx.get(r, 0) + tx_per_node[n.nodeid]
        agg['tx_by_role'] = json.dumps(role_tx)
        # v0.12 B1: packet-death diagnostics ("where does the wave die?")
        reasons = {}
        degrees, alts, confs, resids, uniques, segs = [], [], [], [], [], []
        n_deaths = 0
        for n in nodes:
            a = getattr(n, 'adaptive', None)
            if not a:
                continue
            for d in getattr(a, 'death_log', []):
                n_deaths += 1
                reasons[d['reason']] = reasons.get(d['reason'], 0) + 1
                degrees.append(d.get('local_degree', 0))
                alts.append(d.get('min_alt_paths', 0))
                confs.append(d.get('confidence', 0))
                resids.append(d.get('residual_cov', 0))
                uniques.append(d.get('my_unique_cov', 0))
                segs.append(d.get('segment_count', 0))
        if n_deaths:
            def _mean(xs):
                return round(sum(xs) / len(xs), 3)
            agg['death_count'] = n_deaths
            agg['death_reasons'] = json.dumps(reasons)
            agg['death_mean_local_degree'] = _mean(degrees)
            agg['death_mean_alt_paths'] = _mean(alts)
            agg['death_mean_confidence'] = _mean(confs)
            agg['death_mean_residual_cov'] = _mean(resids)
            agg['death_mean_my_unique_cov'] = _mean(uniques)
            agg['death_mean_segment_count'] = _mean(segs)
            agg['death_frac_unique_gt0'] = round(
                sum(1 for u in uniques if u > 0) / len(uniques), 3)
            agg['death_frac_alt_le1'] = round(
                sum(1 for a_ in alts if a_ <= 1.0) / len(alts), 3)
        # v0.13 C6: linear DM optimization metrics — where does the chain
        # spend its transmissions? fallbacks_per_delivered_dm separates
        # PRIMARY-chain cost from escalation cost (mini-floods, watchdog
        # retries, client repeats, segment fallbacks).
        dm_acked = 0
        for p in pkts:
            if getattr(p, 'isAck', False) and p.destId is not None \
                    and p.destId != 0xFFFFFFFF and p.receivedAtN[p.destId]:
                dm_acked += 1
        succ_hops = agg.get('adaptive_primary_success', 0)
        fallbacks = (agg.get('adaptive_primary_failure', 0)
                     + agg.get('client_repeats', 0)
                     + agg.get('adaptive_segment_fallback', 0)
                     + agg.get('adaptive_mini_flood_triggered', 0))
        agg['dm_acked'] = dm_acked
        agg['tx_per_successful_hop'] = round(
            out['tx_count'] / max(1, succ_hops), 4)
        agg['fallbacks_per_delivered_dm'] = round(
            fallbacks / max(1, dm_acked), 4)
        out['adaptive_stats'] = agg

    print(json.dumps(out))


if __name__ == '__main__':
    main()
