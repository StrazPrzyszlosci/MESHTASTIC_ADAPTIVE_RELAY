#!/usr/bin/env python3
"""ADAPTIVE_RELAY router for Meshtasticator.

Local, passive, learning relay selection:
  - passive neighbor learning + 2-hop knowledge,
  - relay PDR/ETX from watchdog observations (optional neuron-inspired per-segment
    weights with reinforcement + decay),
  - unicast: designated PRIMARY + deterministic SILENT BACKUPs,
  - broadcast: self-election tiers + local coverage (MPR-style, oracle-free)
    + lateral inhibition on ECHO,
  - mini-flood fallback (bounded TTL), confidence levels, dense/sparse adaptation,
  - optional local segmentation (geo from POSITION packets or topological),
    protecting unique-coverage (bridge) relays.

NO-ORACLE RULE (hard): this module must decide using ONLY information a real
node could have: fields of packets it actually decoded (txNodeId, origTxNodeId,
destId, seq, hopLimit, authenticated, local RSSI at SELF, optional position
carried in a POSITION packet), the node's own learned state, the node's own
position, and its own channel-utilization counter. It must never read
LINK_OFFSET, sensedByN/detectedByN/collidedAtN/receivedAtN of packets, other
nodes' simulator objects/state, or global topology.
tests/test_adaptive.py::TestNoOracle statically enforces this.
"""
import logging
import math
import random

from lib.phy import get_current_slot_time
from lib.potential import (PotentialTable, LinkCostModel,
                           PACKET_CLASS_EMERGENCY as PCLASS_EMERGENCY,
                           PACKET_CLASS_DM as PCLASS_DM,
                           PACKET_CLASS_BROADCAST as PCLASS_BROADCAST,
                           PACKET_CLASS_TELEMETRY_LOCAL as PCLASS_TELEMETRY)

logger = logging.getLogger(__name__)

BROADCAST_ID = 0xFFFFFFFF

# roles inside a designation list / self-election tiers
PRIMARY, BACKUP1, BACKUP2, SUPPRESSED = 0, 1, 2, 3


def _ewma(old, sample, alpha):
    return old * (1.0 - alpha) + sample * alpha


class NeighborInfo:
    """Locally learned state about one directly heard neighbor."""
    __slots__ = ('last_seen', 'first_seen', 'obs', 'rssi_ema', 'snr_ema',
                 'relay_attempts', 'relay_successes', 'pdr', 'weight',
                 'weight_seg', 'pos', 'stability', 'velocity', '_last_pos',
                 'rssi_slope', '_rssi_prev_ema', '_rssi_prev_time')

    def __init__(self, now, rssi, noise_level, weight_init, pdr_init, n_segments):
        self.last_seen = now
        self.first_seen = now
        self.obs = 1
        self.rssi_ema = float(rssi)
        self.snr_ema = float(rssi) - noise_level
        self.relay_attempts = 0
        self.relay_successes = 0
        self.pdr = pdr_init
        self.weight = weight_init
        self.weight_seg = [weight_init] * max(1, n_segments)
        self.pos = None        # (x, y) only if learned from a POSITION packet
        self.stability = 0.0   # EWMA of presence
        self.velocity = 0.0    # m/s EWMA from POSITION deltas (locally realistic)
        self._last_pos = None
        # v0.8b RSSI derivative (dRSSI/dt, dBm/s) — mobility/link-expiration
        # predictor without Doppler (CFO makes Doppler unusable)
        self.rssi_slope = 0.0
        self._rssi_prev_ema = None
        self._rssi_prev_time = None

    @property
    def etx(self):
        return 1.0 / max(self.pdr, 1e-6)


class AdaptiveRelay:
    """Per-node ADAPTIVE_RELAY state and decision logic."""

    # master behavior switch: True => behave exactly like MANAGED_FLOOD
    # (kept for regression checks: AR_PARAMS['passthrough']=True forces it).
    def __init__(self, node):
        self.node = node            # owning MeshNode (only own state used)
        self.conf = node.conf
        self.p = dict(getattr(self.conf, 'AR_PARAMS', {}))
        self.env = node.env
        self.noise = self.conf.NOISE_LEVEL
        self.n_segments = max(2, int(self.p.get('sector_count', 4)))
        self.passthrough = bool(self.p.get('passthrough', False))

        # own RNG stream — MUST NOT touch node.nodeRng / node.moveRng because
        # those drive message generation and movement (traffic comparability).
        self.rng = random.Random(node.nodeid * 104729 + int(self.conf.SEED) * 31
                                 + int(self.p.get('rng_salt', 1337)))

        # --- learned state (all local, passive) ---
        self.neighbors = {}              # nid -> NeighborInfo
        self.neighbors_of_nb = {}        # nid -> set(nid) learned 2-hop topology
        # v0.3: advertised 2-hop map from NEIGHBORINFO (explicit exchange)
        # nid -> {'nbs': {other_nid: (pdr_class, etx_class, fresh)},
        #         'time': ts, 'conf': 0..1, 'obs': int}
        self.advertised_neighbors = {}
        self._ni_seq_counter = 0         # own NEIGHBORINFO sequence namespace
        self._last_ni_sent = -1e18
        self._last_ni_sig = None
        # v0.4 binary protocol state
        self.neighbor_table_version = 0  # uint16; increments on table change
        self._last_ni_table = None       # last sent table [(nid, qbyte)]
        self._ni_since_full = 0          # NIs since last FULL snapshot
        self._local_id_map = {}          # real_id -> local short id (stable, first-seen)
        self._ni_version_dirty = False
        # v0.5 PROBE-FIRST (stale-triggered route discovery, FW+ lesson)
        self._probe_event = {}           # dest -> simpy event (probe response)
        self._probe_response_seen = set()
        self._probe_sent_at = {}         # dest -> t of last probe (cooldown)
        self._probe_seq_counter = 0
        # v0.5 reach-first guard: min hopLimit heard per seq (progressed evidence)
        self._min_hop_heard = {}
        self._mini_flood_done = set()    # seq -> max one mini-flood fallback
        # v0.9 DRF: min hopLimit among REPEAT copies (ripple layer inhibition)
        self._rep_min_hop = {}
        # v0.9 DRF: Time-of-Arrival mapping (passive topology discovery):
        # seq -> {txNodeId: arrival time}
        self._relay_arrivals = {}
        # v0.9 Echo-Probe: minimal 1-hop neighbor discovery at cold start
        self._probe_ack_sent = {}      # source -> t of last PROBE_ACK
        # v0.12 B1: packet-death diagnostics ("where does the wave die?")
        # bounded per node; exported by adaptive_run for offline analysis.
        # DIAGNOSTICS ONLY — never read by any decision path (bit-identical).
        self.death_log = []
        self._death_seq_seen = set()
        self.packet_relayers = {}        # seq -> set(txNodeId heard relaying seq)
        self.pending = {}                # seq -> defer bookkeeping (my scheduled relay)
        self.expected_relay = {}         # seq -> {'relays': set, 'amb': bool}
        self.routes = {}                 # dest -> {'primary','backup1','backup2',...}
        self.segment_of = {}             # nid -> segment index (local segmentation)
        self.last_seg_recompute = 0.0
        # v0.13 C1e: per-node suppression-regret EWMA over frontier-candidate
        # suppressions (None = no evidence yet -> rescue gate CLOSED)
        self._rescue_regret_ewma = None
        self._rescue_regret_n = 0
        # v0.14: local mobility score (own-position delta when known — the
        # GPS-haves signal; neighbor-table churn as the GPS-free fallback)
        self._mob_score = None
        self._mob_last_pos = None
        self._mob_prev_nids = None
        # v0.14 D2-D4: minimal SHEPHERD-COLLECT state machine
        self._shepherd_epoch = 0             # my last announced epoch
        self._collect_until = 0.0             # COLLECT lease expiry (env.now ms)
        self._shepherd_timer_armed = False    # election timer pending
        # v0.14 CEF_BOOST: traffic-jam mode state (sustained-evidence FSM)
        self._cef_boost = False               # currently in pure-CEF mode
        self._boost_stable = 0                # consecutive stable-jam windows
        self._boost_win = {}                  # per-window snapshot of counters
        # v0.14 N1: receiver-evidence census (bounded, eviction by age)
        self._n1_census = {}
        self._seg_sig = None             # signature of learned topology (event trigger)
        self.segmentation_mode_active = None  # 'geo'|'topo' actually used (adaptive)
        self.fail_streak = 0             # consecutive unresolved relay failures
        # v0.8 LPR: local potential table (hop-depth distance-vector estimates)
        self.potential = PotentialTable(
            decay_lambda=float(self.p.get('lpr_decay_lambda', 0.05)),
            hysteresis=float(self.p.get('lpr_hysteresis', 0.1)),
            cost_model=LinkCostModel(
                congestion_weight=float(self.p.get('lpr_congestion_weight', 0.5)),
                battery_weight=float(self.p.get('lpr_battery_weight', 0.3)),
                uncertainty_weight=float(self.p.get('lpr_uncertainty_weight', 0.3)),
                class_weight=float(self.p.get('lpr_class_weight', 0.2))))
        self.observations = 0
        self._in_air = {}                # seq -> count of frames currently being RX'd
        # v0.3 homeostasis
        self.relay_fatigue = 0.0         # 0..1, decays over time
        self._fatigue_last = 0.0
        # v0.3 diagnostics
        self._segment_changes = 0
        self._low_conf_since = None
        self._recent_primary = {}        # dest -> (primary_id, since) flapping ctrl

        # statistics exported by adaptive_run.py
        self.stats = {
            'adaptive_primary_selected': 0,
            'adaptive_primary_success': 0,
            'adaptive_primary_failure': 0,
            'adaptive_backup1_triggered': 0,
            'adaptive_backup1_success': 0,
            'adaptive_backup2_triggered': 0,
            'adaptive_backup2_success': 0,
            'adaptive_mini_flood_triggered': 0,
            'adaptive_mpr_forward': 0,
            'adaptive_suppressed_forward': 0,
            'adaptive_route_switches': 0,
            'adaptive_primary_flaps': 0,
            'adaptive_sector_candidates': 0,
            'adaptive_sector_primary_selected': 0,
            'adaptive_unique_coverage_preserved': 0,
            'adaptive_cross_sector_suppression': 0,
            'adaptive_same_sector_inhibition': 0,
            'adaptive_segment_fallback': 0,
            'adaptive_confidence_low_time_ms': 0,
            'adaptive_gossip_forwards': 0,
            # F0.6 diagnostic metrics (WHY the algorithm behaves as it does)
            'ar_cancel_in_air': 0,           # deferred relay cancelled: seq on-air
            'ar_cancel_residual_empty': 0,   # cancelled: residual coverage empty
            'ar_cancel_echo': 0,             # cancelled: echo heard
            'ar_covered_elsewhere': 0,       # watchdog timeout skipped: another relay covered
            'watchdog_false_failure': 0,     # failure marked, packet propagated later
            'suppression_regret': 0,         # suppressed, no propagation evidence later
            'unique_relay_suppressed': 0,    # suppressed while having unique coverage
            'backup1_useful': 0, 'backup1_redundant': 0,
            'backup2_useful': 0, 'backup2_redundant': 0,
            'mini_flood_delivered': 0,
            # v0.14 D0: TOP-K bounded state (firmware RAM realism)
            'topk_evictions': 0,
            'topk_table_high': 0,
            # v0.14: mobility self-demotion
            'mobility_recommend_mute': 0,
            'mobility_relay_suppressed': 0,
            # v0.14 D2-D4: minimal SHEPHERD-COLLECT
            'shepherd_msgs_tx': 0,
            'collect_hears': 0,
            'collect_ignored_scope': 0,
            'rescues_during_collect': 0,
            'rescue_budget_denied': 0,
            'shepherd_election_cancelled': 0,
            # v0.14 CEF_BOOST
            'boost_windows': 0,
            'boost_exits': 0,
            # v0.14 N1: receiver-evidence census
            'suppressed_by_census': 0,
            'copies_before_suppress': 0,
            'copies_before_forward': 0,
            # v0.14 N3: ranked whisper
            'n3_forward_rank0': 0,
            'n3_forward_rank1': 0,
            'n3_forward_rank2': 0,
            'n3_fallback_suppressed': 0,
            'n3_shep_rescues': 0,
            # v0.14 N4: airtime token bucket
            'n4_airtime_granted': 0.0,
            'n4_starved': 0,
            # v0.3 NEIGHBORINFO cost (measured separately, never hidden in TX)
            'neighborinfo_tx': 0,
            'neighborinfo_bytes': 0,
            'neighborinfo_airtime': 0.0,
            # v0.4 binary protocol metrics
            'neighborinfo_full_tx': 0,
            'neighborinfo_delta_tx': 0,
            'neighborinfo_delta_miss': 0,
            'neighborinfo_raw_bytes': 0,
            'neighborinfo_encoded_bytes': 0,
            'known_1hop': 0,
            'known_2hop': 0,
            'unique_bridge_detected': 0,
            'suppression_blocked_unique': 0,
            'suppression_blocked_low_alt': 0,
            'adaptive_inhibition_weak': 0,
            'adaptive_inhibition_medium': 0,
            'adaptive_inhibition_strong': 0,
        }

    # ==================================================================
    # ENTRY POINTS (called from MeshNode)
    # ==================================================================
    def on_received(self, p):
        """Called at the exact point MANAGED_FLOOD would rebroadcast `p`,
        and also for packets that need no relay decision (learning only
        happens in _learn which is invoked from receive() for every decoded
        packet — see MeshNode hook)."""
        if self.passthrough:
            return self._flood_passthrough(p)

        if self.node.is_client_mute:
            return False
        # v0.3: NEIGHBORINFO control packets are never relayed (no storms);
        # their content is merged into the local map in learn()
        if getattr(p, 'is_neighborinfo', False):
            return False
        # v0.5: route-discovery probes have their own handling
        if getattr(p, 'is_probe', False):
            return self._handle_probe(p)
        # v0.9: Echo-Probe / PROBE_ACK — 1-hop neighbor discovery, never relayed
        if getattr(p, 'is_echo_probe', False):
            return self._handle_echo_probe(p)
        if getattr(p, 'is_probe_ack', False):
            return False  # the prober learns from it in learn(); never relay
        if p.destId == BROADCAST_ID:
            return self._handle_broadcast(p)
        return self._handle_dm(p)

    def designate_on_send(self, p):
        """Called by MeshNode.send_packet for freshly created packets.
        For DM traffic on ADAPTIVE_RELAY: embed ordered relay designation
        (realistic analogue of Meshtastic's relay_node header, extended to
        an ordered short list).
        v0.5 PROBE-FIRST: when the route to the destination is unknown, stale
        or unconfirmed (LOW confidence), send a route-discovery probe FIRST
        and delay the DM until the response arrives (or timeout) — the FW+
        lesson: do not guess, probe. Returns True when the packet was taken
        over (deferred; MeshNode.send_packet skips its own transmit)."""
        if self.passthrough:
            return False
        # v0.6: SOS marking (sim-level test hook: a configured fraction of
        # messages marked emergency; real firmware: dedicated portnum)
        if self.p.get('emergency_enabled', True) and \
                self.p.get('emergency_ratio', 0) > 0:
            if self.rng.random() < float(self.p['emergency_ratio']):
                p.is_emergency = True
                p.no_dupe_cancel = True
                p.flood_ttl = max(p.flood_ttl or 0,
                                  int(self.p['mini_flood_ttl']) + 2)
                self.stats['emergency_originated'] = \
                    self.stats.get('emergency_originated', 0) + 1
        if p.destId == BROADCAST_ID:
            return False
        if self.p.get('probe_enabled', True) and self._should_probe_first(p.destId):
            self._probe_then_send(p)
            return True
        try:
            prim, b1, b2 = self._pick_relays_toward(p.destId)
            lst = [x for x in (prim, b1, b2) if x is not None]
            if lst:
                p.relay_designation = lst
                self.stats['adaptive_primary_selected'] += (prim is not None)
        except Exception:
            logger.exception('AR designate_on_send failed')
        return False

    # ==================================================================
    # v0.5 PROBE-FIRST (stale-triggered route discovery)
    # ==================================================================
    def _should_probe_first(self, dest):
        """Locally decidable: no fresh, confirmed route to dest -> probe.
        Cooldown first: a recent probe for this dest means we already tried —
        send via the current best path instead of re-probing."""
        now = self.env.now
        if dest in self._probe_sent_at and now - self._probe_sent_at[dest] < self.p.get('probe_cooldown_ms', 30000):
            return False
        route = self.routes.get(dest)
        if route is None:
            return True
        if now - route.get('last_updated', 0) > self.p.get('probe_route_stale_ms', 120000):
            return True
        if self._confidence_level() == 0:
            return True
        # v0.8 LPR residual sensor: inconsistent potential knowledge -> probe
        if self.p.get('enable_lpr', False):
            phi_self = self.potential.get_phi(dest)
            if phi_self != float('inf'):
                phis = [self.potential.get_neighbor_phi(nid, dest)
                        for nid in self.neighbors]
                residual = self.potential.detect_residual(dest, phis, phi_self)
                if residual != float('inf') and residual > self.p.get('lpr_residual_probe', 1.5):
                    return True
        return False

    def _probe_then_send(self, p):
        """Send a route-discovery probe for p.destId; transmit the DM after
        the probe response arrives (fresh route) or on timeout (fallback)."""
        dest = p.destId
        now = self.env.now
        self._send_probe(dest, now)
        self._probe_sent_at[dest] = now
        self.env.process(self._probe_wait_send(dest, p))

    def _send_probe(self, dest, now):
        """Flood a cheap route-discovery probe (simulator Traceroute).
        Only the probe_target responds; the flood builds routes toward the
        ORIGIN along the way (passive learning from relay->origin pairs)."""
        from lib.packet import MeshPacket
        node = self.node
        self._probe_seq_counter += 1
        seq = 2_000_000 + node.nodeid * 100_000 + self._probe_seq_counter
        pProbe = MeshPacket(self.conf, node.nodes, node.nodeid, BROADCAST_ID,
                            node.nodeid, int(self.p.get('probe_len', 12)),
                            seq, now, False, False, None, now,
                            node.connectivity_map, node.baseline_pathloss_matrix)
        pProbe.is_probe = True
        pProbe.probe_target = dest
        pProbe.hopLimit = self.conf.hopLimit
        ttl = int(self.p.get('probe_ttl', 0))
        pProbe.flood_ttl = ttl if ttl > 0 else self.conf.hopLimit  # full flood
        node.packets.append(pProbe)
        node.env.process(node.transmit(pProbe))
        self.stats['probe_tx'] = self.stats.get('probe_tx', 0) + 1
        self.stats['probe_airtime'] = self.stats.get('probe_airtime', 0.0) + pProbe.timeOnAir

    def _probe_wait_send(self, dest, p):
        """Wait for the probe response (fresh route) or timeout, then send
        the DM. Reach-first: on timeout the DM still goes out via the best
        current path (designation may be empty -> bounded fallback)."""
        from simpy import AnyOf
        ev = self.env.event()
        self._probe_event[dest] = ev
        to = self.env.timeout(self.p.get('probe_timeout_ms', 8000))
        yield AnyOf(self.env, [ev, to])
        self._probe_event.pop(dest, None)
        if self.node.failed:
            return
        try:
            prim, b1, b2 = self._pick_relays_toward(dest)
            lst = [x for x in (prim, b1, b2) if x is not None]
            if lst:
                p.relay_designation = lst
                self.stats['adaptive_primary_selected'] += 1
        except Exception:
            logger.exception('AR probe-wait designation failed')
        self.node.packets.append(p)
        self.node.env.process(self.node.transmit(p))

    def _handle_probe(self, p):
        """Route-discovery probe handling: the TARGET responds (unicast back
        to the origin); others relay the flood while TTL allows."""
        me = self.node.nodeid
        if getattr(p, 'probe_target', None) == me:
            self._send_probe_response(p)
            return False  # target replies, does not relay further
        if (getattr(p, 'flood_ttl', 0) or 0) > 0:
            return self._relay_packet(p, designations=None,
                                      next_flood_ttl=p.flood_ttl - 1)
        return False

    def _send_probe_response(self, p):
        """Unicast probe response back to the origin, routed via the learned
        reverse path (designation chain). Every relay on the way learns
        routes toward BOTH the origin and me (route building for free)."""
        from lib.packet import MeshPacket
        me = self.node.nodeid
        self._probe_seq_counter += 1
        seq = 3_000_000 + me * 100_000 + self._probe_seq_counter
        pResp = MeshPacket(self.conf, self.node.nodes, me, p.origTxNodeId, me,
                           int(self.p.get('probe_resp_len', 12)), seq,
                           self.env.now, False, False, None, self.env.now,
                           self.node.connectivity_map, self.node.baseline_pathloss_matrix)
        pResp.is_probe_response = True
        pResp.hopLimit = self.conf.hopLimit
        prim, b1, b2 = self._pick_relays_toward(p.origTxNodeId, exclude={me})
        desig = [x for x in (prim, b1, b2) if x is not None and x != me]
        if desig:
            pResp.relay_designation = desig
            pResp.fast_cw = True
        else:
            pResp.flood_ttl = max(1, int(self.p['mini_flood_ttl']))
            self.stats['adaptive_mini_flood_triggered'] += 1
        self.node.packets.append(pResp)
        self.node.env.process(self.node.transmit(pResp))
        self.stats['probe_responses'] = self.stats.get('probe_responses', 0) + 1
        self.stats['probe_airtime'] = self.stats.get('probe_airtime', 0.0) + pResp.timeOnAir

    # ==================================================================
    # v0.9 ECHO-PROBE: minimal 1-hop neighbor discovery at cold start
    # (FieldMesh localness; replaces NEIGHBORINFO). Tiny packet, TTL 1,
    # never relayed; direct neighbors answer with a rate-limited PROBE_ACK;
    # the prober learns neighbors + RSSI without any multi-hop traffic.
    # ==================================================================
    def echo_probe_loop(self):
        """Send echo-probes while confidence is LOW (cold start); stop when
        the neighborhood is learned or the max probe time passes."""
        rng = random.Random(self.node.nodeid * 13 + int(self.conf.SEED) + 7
                            + int(self.p.get('rng_salt', 1337)))
        yield self.env.timeout(rng.uniform(0, 3000))
        while self._confidence_level() == 0 and \
                self.env.now < self.p.get('probe_max_time_ms', 300000):
            if not self.node.failed:
                self._send_echo_probe()
            yield self.env.timeout(self.p.get('echo_probe_period_ms', 30000)
                                   * rng.uniform(0.8, 1.2))

    def _send_echo_probe(self):
        from lib.packet import MeshPacket
        node = self.node
        self._probe_seq_counter += 1
        seq = 4_000_000 + node.nodeid * 100_000 + self._probe_seq_counter
        pProbe = MeshPacket(self.conf, node.nodes, node.nodeid, BROADCAST_ID,
                            node.nodeid, int(self.p.get('probe_len', 12)),
                            seq, self.env.now, False, False, None, self.env.now,
                            node.connectivity_map, node.baseline_pathloss_matrix)
        pProbe.is_echo_probe = True
        pProbe.hopLimit = 1          # 1-hop by nature
        pProbe.packet_class = PCLASS_TELEMETRY
        pProbe.packet_scope = 1      # LOCAL
        node.packets.append(pProbe)
        node.env.process(node.transmit(pProbe))
        self.stats['echo_probe_tx'] = self.stats.get('echo_probe_tx', 0) + 1
        self.stats['probe_airtime'] = self.stats.get('probe_airtime', 0.0) + pProbe.timeOnAir

    def _handle_echo_probe(self, p):
        """Answer a direct neighbor's echo-probe with a short PROBE_ACK
        (rate-limited per source, seeded jitter; never relayed)."""
        me = self.node.nodeid
        now = self.env.now
        last = self._probe_ack_sent.get(p.txNodeId, -1e18)
        if now - last < 60000:
            return False   # rate limit: max 1 ACK per source per minute
        self.env.process(self._probe_ack_send(p))
        return False

    def _probe_ack_send(self, p):
        rng = random.Random(self.node.nodeid * 17 + p.txNodeId
                            + int(self.p.get('rng_salt', 1337)))
        yield self.env.timeout(rng.uniform(0, 200))
        if self.node.failed:
            return
        from lib.packet import MeshPacket
        me = self.node.nodeid
        self._probe_seq_counter += 1
        seq = 5_000_000 + me * 100_000 + self._probe_seq_counter
        pAck = MeshPacket(self.conf, self.node.nodes, me, p.txNodeId, me,
                          int(self.p.get('probe_ack_len', 8)), seq,
                          self.env.now, False, False, None, self.env.now,
                          self.node.connectivity_map, self.node.baseline_pathloss_matrix)
        pAck.is_probe_ack = True
        pAck.hopLimit = 1
        self._probe_ack_sent[p.txNodeId] = self.env.now
        self.node.packets.append(pAck)
        self.node.env.process(self.node.transmit(pAck))
        self.stats['echo_probe_acks'] = self.stats.get('echo_probe_acks', 0) + 1
        self.stats['probe_airtime'] = self.stats.get('probe_airtime', 0.0) + pAck.timeOnAir

    # ==================================================================
    # RF-LOCAL event hooks (my own radio's RX start/end — no oracle)
    # ==================================================================
    def note_rx_start(self, p, collided=False):
        c = self._in_air.get(p.seq, 0)
        self._in_air[p.seq] = c + (0 if collided else 1)

    def on_own_tx_cancelled(self, seq):
        """Our own relayed copy was suppressed by a duplicate during the MAC
        wait (someone else covered this packet). Drop the watchdog expectation
        and any pending backup WITHOUT counting a failure or retrying."""
        ent = self.expected_relay.pop(seq, None)
        pend = self.pending.pop(seq, None)
        if pend is not None:
            pend['cancelled'] = True

    def note_rx_end(self, p):
        if self._in_air.get(p.seq, 0) > 0:
            self._in_air[p.seq] -= 1

    # ==================================================================
    # v0.3 NEIGHBORINFO: explicit local neighbor-table exchange
    # ==================================================================
    def _topology_signature(self):
        """Cheap deterministic signature of local knowledge (hysteresis)."""
        return (frozenset(self.neighbors.keys()),
                tuple(sorted((nid, len(cov)) for nid, cov in self.neighbors_of_nb.items())),
                tuple(sorted(self.advertised_neighbors.keys())))

    def _quantize_quality(self, nb):
        """PDR class 0-3, ETX class 0-3, freshness 0-1 — 1 byte in real firmware."""
        pdr_c = 0 if nb.pdr < 0.5 else (1 if nb.pdr < 0.75 else (2 if nb.pdr < 0.9 else 3))
        etx = nb.etx
        etx_c = 0 if etx > 4 else (1 if etx > 2 else (2 if etx > 1.3 else 3))
        fresh = 1 if (self.env.now - nb.last_seen) < self.p['neighbor_expiry_ms'] / 3 else 0
        return (pdr_c, etx_c, fresh)

    def neighborinfo_loop(self):
        """Periodic + event-triggered NEIGHBORINFO sending process.

        Never sends on every change: min period, max period, event triggers
        (new important neighbor / neighbor expired / topology change), seeded
        jitter. The airtime cost is metered separately (neighborinfo_* stats).
        """
        rng = random.Random(self.node.nodeid * 7 + int(self.conf.SEED) + 1
                            + int(self.p.get('rng_salt', 1337)))
        yield self.env.timeout(rng.uniform(0, self.p.get('ni_check_interval_ms', 5000)))
        # v0.7 Trickle timer (RFC 6206 simplified): exponential doubling when
        # stable, reset on topology change — keeps control TX off a busy channel
        from lib.neighborinfo import TrickleTimer
        tt = TrickleTimer(self.p.get('ni_min_period_ms', 60000),
                          self.p.get('ni_max_period_ms', 300000))
        while True:
            now = self.env.now
            if self.node.failed:
                yield self.env.timeout(self.p.get('ni_check_interval_ms', 5000))
                continue
            # v0.3 fix: NEIGHBORINFO costs REAL airtime — near channel
            # saturation never send (radio-real: LBT would back off anyway)
            util = self.node.channel_utilization_percent()
            if util > 40:
                yield self.env.timeout(self.p.get('ni_check_interval_ms', 5000))
                continue
            sig = self._topology_signature()
            since = now - self._last_ni_sent
            # adaptive min period by LOCAL density: dense networks pay double
            # airtime for full-table NIs — slow them down (ablatable)
            n_nb = len(self.neighbors)
            if n_nb > self.p.get('density_dense', 12):
                min_p = self.p.get('ni_min_period_dense_ms', 180000)
            elif n_nb > self.p.get('density_sparse', 4):
                min_p = self.p.get('ni_min_period_ms', 60000)
            else:
                min_p = self.p.get('ni_min_period_sparse_ms', 45000)
            min_p = min(max(min_p, tt.interval), self.p.get('ni_max_period_ms', 300000))
            should = False
            if since >= self.p.get('ni_max_period_ms', 300000):
                should = True
            elif sig != self._last_ni_sig and since >= min_p:
                should = True
            elif self._new_important_neighbor() and since >= min_p:
                should = True
            if should:
                self._send_neighborinfo(now)
                self._last_ni_sent = now
                self._last_ni_sig = sig
            else:
                tt.on_tick()
            if sig != self._last_ni_sig:
                tt.on_inconsistency()
            yield self.env.timeout(self.p.get('ni_check_interval_ms', 5000)
                                   * rng.uniform(0.8, 1.2))

    def _new_important_neighbor(self):
        """True when a neighbor providing UNIQUE coverage exists and enough
        time passed since the last NEIGHBORINFO (event trigger — bridges are
        worth announcing even without other topology changes)."""
        uniq = self._unique_coverage_any()
        return uniq > 0 and (self.env.now - self._last_ni_sent) > self.p.get('ni_min_period_ms', 60000)

    def _unique_coverage_any(self):
        """Max unique coverage across my neighbors (bridge detection)."""
        best = 0
        for nid in self.neighbors:
            u = self._unique_coverage(nid)
            if u > best:
                best = u
        return best

    def _send_neighborinfo(self, now):
        """Broadcast my OWN neighbor table (only what I really know).

        v0.4 binary protocol: FULL snapshot (first contact / periodic resync /
        big topology change) with AUTO encoding (raw32 / delta_varint /
        local_bitmap by shortest representation), else DELTA update (only
        changes vs the last sent version). The airtime cost is metered
        separately and computed on the BINARY payload size. Aggregation of
        changes happens naturally via the min-period accumulation (changes
        between sends collapse into one DELTA).
        In the simulator the receiver consumes the decoded equivalent
        (packet attributes); parsing is unit-tested as lossless.
        """
        from lib.packet import MeshPacket
        from lib import neighborinfo as NIcodec
        node = self.node
        quality = bool(self.p.get('ni_quality', True))
        # bump version when the table changed since the last send
        if self._ni_version_dirty:
            self.neighbor_table_version = (self.neighbor_table_version + 1) & 0xFFFF
            self._ni_version_dirty = False
        # stable local short-id dictionary (first-seen order; from really
        # exchanged data — the FULL snapshot carries it in entry order)
        for nid in self.neighbors:
            if nid not in self._local_id_map:
                if len(self._local_id_map) < 256:
                    self._local_id_map[nid] = len(self._local_id_map)
        # current snapshot, capped (bridges/unique first), in local-id order
        entries = []
        for nid, nb in sorted(self.neighbors.items()):
            qb = (NIcodec.quality_byte(nb.pdr, now - nb.last_seen,
                                       self.p['neighbor_expiry_ms'])
                  if quality else None)
            entries.append((nid, qb))
        max_n = int(self.p.get('ni_max_neighbors', 12))
        if len(entries) > max_n:
            uniq_ids = {nid for nid in entries if self._unique_coverage(nid) > 0}
            entries.sort(key=lambda e: (e[0] not in uniq_ids,
                                        self._local_id_map.get(e[0], 1 << 30)))
            entries = sorted(entries[:max_n], key=lambda e: self._local_id_map.get(e[0], 1 << 30))
        if not entries:
            return

        # FULL vs DELTA: first send / periodic resync / version too old
        need_full = (self._last_ni_table is None
                     or self._ni_since_full >= int(self.p.get('ni_full_every', 5)))
        ops = None
        if not need_full:
            ops = self._ni_compute_delta(self._last_ni_table, entries)
            if ops is None:
                need_full = True  # delta would be bigger than FULL
        if need_full:
            name, payload, flags = NIcodec.choose_best_neighbor_encoding(
                entries, self._local_id_map, quality=quality)
            if name == 'local_bitmap':
                # bitmap needs the receiver's dictionary = my last FULL order;
                # keep only when a FULL with the same order was sent before
                if self._last_ni_table is None or self._ni_since_full > 0:
                    name, payload, flags = NIcodec.choose_best_neighbor_encoding(
                        entries, {}, quality=quality)
            ni_type = NIcodec.NI_TYPE_FULL
            decoded = entries
            self.stats['neighborinfo_full_tx'] += 1
            self._ni_since_full = 0
        else:
            payload, flags = NIcodec.encode_delta(ops)
            ni_type = NIcodec.NI_TYPE_DELTA
            decoded = ops
            self.stats['neighborinfo_delta_tx'] += 1
            self._ni_since_full += 1
        # compression accounting (raw = RAW32 equivalent)
        raw_payload, _ = NIcodec.encode_full_raw32(entries, quality=quality)
        self.stats['neighborinfo_raw_bytes'] += len(raw_payload) + NIcodec.HEADER_LEN
        self.stats['neighborinfo_encoded_bytes'] += len(payload) + NIcodec.HEADER_LEN

        self._ni_seq_counter += 1
        ni_seq = 1_000_000 + node.nodeid * 100_000 + self._ni_seq_counter
        ni_len = len(payload) + NIcodec.HEADER_LEN
        pNew = MeshPacket(self.conf, node.nodes, node.nodeid, BROADCAST_ID,
                          node.nodeid, ni_len, ni_seq, now, False, False, None, now,
                          node.connectivity_map, node.baseline_pathloss_matrix)
        pNew.hopLimit = 0           # control packet: never relayed
        pNew.is_neighborinfo = True
        pNew.ni_type = ni_type
        pNew.ni_version = self.neighbor_table_version
        pNew.ni_base_version = (self.neighbor_table_version - 1) & 0xFFFF
        pNew.ni_entries = decoded      # decoded equivalent (receiver consumes)
        node.packets.append(pNew)
        node.env.process(node.transmit(pNew))
        self.stats['neighborinfo_tx'] += 1
        self.stats['neighborinfo_bytes'] += ni_len + self.conf.HEADERLENGTH
        self.stats['neighborinfo_airtime'] += pNew.timeOnAir
        self._last_ni_table = list(entries)

    def _ni_compute_delta(self, old_table, new_entries):
        """Diff old vs new table into ADD/REMOVE/QUALITY ops.
        Returns None when the delta would not be smaller than a FULL."""
        from lib import neighborinfo as NIcodec
        old = {nid: qb for nid, qb in (old_table or [])}
        new = {nid: qb for nid, qb in new_entries}
        ops = []
        for nid, qb in new.items():
            if nid not in old:
                ops.append((NIcodec.NI_OP_ADD, nid, qb))
            elif old[nid] != qb:
                ops.append((NIcodec.NI_OP_QUALITY, nid, qb))
        for nid in old:
            if nid not in new:
                ops.append((NIcodec.NI_OP_REMOVE, nid, None))
        if not ops:
            return []           # no changes: empty delta (suppressed at send)
        raw_size = len(new_entries) * (5 if new_entries[0][1] is not None else 4)
        # delta size estimate: per op ~4B
        if len(ops) * 4 >= raw_size:
            return None         # delta not worth it -> FULL
        return ops

    def _receive_neighborinfo(self, p):
        """v0.4: merge sender's advertised table into my local map.
        FULL -> replace stored table + store entry order (implicit local
        dictionary for bitmap decoding). DELTA -> apply ADD/REMOVE/QUALITY ops
        only when the sender's table is known (base_version present); else
        count delta_miss and wait for the periodic FULL resync."""
        now = self.env.now
        sender = p.txNodeId
        ni_type = getattr(p, 'ni_type', None)
        version = getattr(p, 'ni_version', 0)
        known = sender in self.advertised_neighbors

        if ni_type is None:
            nbs = p.ni_neighbors or ()      # legacy tuple format
            adv = self.advertised_neighbors.get(sender)
            if adv is None:
                adv = {'nbs': {}, 'time': now, 'conf': 0.0, 'obs': 0,
                       'prev': None, 'version': None, 'order': []}
                self.advertised_neighbors[sender] = adv
            new_set = {e[0] for e in nbs}
            if adv['prev'] is not None:
                same = len(new_set & adv['prev']) / max(1, len(new_set | adv['prev']))
                adv['conf'] = _ewma(adv['conf'], same, 0.3)
            else:
                adv['conf'] = _ewma(adv['conf'], 0.5, 0.3)
            adv['prev'] = new_set
            adv['obs'] += 1
            adv['time'] = now
            adv['nbs'] = {e[0]: e for e in nbs}
        elif ni_type == 1:  # FULL: resync / first contact
            entries = getattr(p, 'ni_entries', None) or []
            adv = self.advertised_neighbors.get(sender)
            if adv is None:
                adv = {'nbs': {}, 'time': now, 'conf': 0.5, 'obs': 0,
                       'prev': None, 'version': version, 'order': []}
                self.advertised_neighbors[sender] = adv
            adv['nbs'] = {e[0]: (e[1] if len(e) > 1 else None) for e in entries}
            adv['order'] = [e[0] for e in entries]   # implicit local dictionary
            adv['version'] = version
            adv['obs'] += 1
            adv['time'] = now
        else:  # DELTA
            if not known:
                # unknown base version: ignore the delta, need FULL snapshot
                self.stats['neighborinfo_delta_miss'] += 1
                return
            adv = self.advertised_neighbors[sender]
            ops = getattr(p, 'ni_entries', None) or []
            from lib import neighborinfo as NIcodec
            for optype, nid, qb in ops:
                if optype == NIcodec.NI_OP_ADD:
                    adv['nbs'][nid] = qb
                elif optype == NIcodec.NI_OP_REMOVE:
                    adv['nbs'].pop(nid, None)
                elif optype == NIcodec.NI_OP_QUALITY:
                    if nid in adv['nbs']:
                        adv['nbs'][nid] = qb
            adv['obs'] += 1
            adv['time'] = now
        # cap memory
        if len(self.advertised_neighbors) > 100:
            oldest = sorted(self.advertised_neighbors.items(),
                            key=lambda kv: kv[1]['time'])[:30]
            for k, _ in oldest:
                del self.advertised_neighbors[k]

    def _expire_advertised(self, now):
        exp = self.p.get('ni_expiry_ms', 300000)
        stale = [nid for nid, a in self.advertised_neighbors.items() if now - a['time'] > exp]
        for nid in stale:
            del self.advertised_neighbors[nid]

    def two_hop_view(self, nid):
        """Unified local 2-hop view of neighbor nid. v0.3 variants:
        'union'      — advertised UNION passive (may overestimate coverage)
        'replace'    — fresh advertised REPLACES passive (sender-confirmed)
        'passive'    — ignore NI for coverage (NI only for segmentation)
        Default 'replace': accurate views, no overestimation."""
        mode = self.p.get('ni_view_mode', 'replace')
        passive = set(self.neighbors_of_nb.get(nid, set()))
        adv = self.advertised_neighbors.get(nid)
        view = passive
        if adv is not None and mode != 'passive':
            fresh = (self.env.now - adv['time']) < self.p.get('ni_expiry_ms', 300000) / 3
            if mode == 'replace' and fresh:
                view = set(adv['nbs'].keys())
            elif mode == 'union':
                view = passive | set(adv['nbs'].keys())
        view.discard(self.node.nodeid)
        return view

    # ==================================================================
    # v0.3 FUNCTIONAL SEGMENTATION (union/find over Jaccard of 2-hop views)
    # ==================================================================
    def _segments_functional(self):
        """Functional segments: cluster neighbors by similarity of their 2-hop
        coverage N(i) (Jaccard >= threshold => same segment). Union/find,
        O(n^2 * a) on LOCAL neighbors — cheap enough for MCU. Segment count
        emerges from local structure (not fixed). GEO hint (when positions
        are known) additionally splits segments by direction."""
        ids = list(self.neighbors.keys())
        if not ids:
            return {}
        thr = self.p.get('seg_overlap_threshold', 0.5)
        covers = {nid: self.two_hop_view(nid) for nid in ids}

        # geo hint availability (adaptive_functional): most neighbors positioned
        geo_hint = False
        if self.p.get('segmentation', 'adaptive') == 'adaptive_functional':
            n_pos = sum(1 for nb in self.neighbors.values() if nb.pos is not None)
            geo_hint = bool(self.neighbors) and n_pos >= len(self.neighbors) * float(
                self.p.get('seg_pos_ratio', 0.6))

        parent = {nid: nid for nid in ids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                ua, ub = covers[a], covers[b]
                u = len(ua | ub)
                if not u or len(ua & ub) / u < thr:
                    continue
                if geo_hint:
                    sa = self._geo_sector_of(a)
                    sb = self._geo_sector_of(b)
                    if sa is not None and sb is not None and sa != sb:
                        continue  # same coverage but opposite directions -> split
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

        seg_of = {}
        seg_map = {}
        for nid in ids:
            r = find(nid)
            if r not in seg_map:
                seg_map[r] = len(seg_map)
            seg_of[nid] = seg_map[r]
        return seg_of

    def _geo_sector_of(self, nid):
        """Sector index of neighbor with known position, else None."""
        nb = self.neighbors.get(nid)
        if nb is None or nb.pos is None:
            return None
        me = self.node.position
        dx = nb.pos[0] - me.x
        dy = nb.pos[1] - me.y
        ang = math.degrees(math.atan2(dy, dx)) % 360.0
        k = self.n_segments
        return int(ang / (360.0 / k)) % k

    # ==================================================================
    # v0.3 ALTERNATIVE PATH SCORE (local approximation, per segment)
    # ==================================================================
    def alt_paths(self, seg):
        """Local alternative-path count for a functional segment: number of
        neighbors leading to it, weighted by PDR health. ~1 -> bridge/linear
        topology (careful suppression); >> 1 -> redundant (aggressive).
        Overlap is NOT discounted here — it belongs to backup diversity."""
        contributors = [nid for nid in self.neighbors if self.segment_of.get(nid) == seg]
        return sum(0.5 + 0.5 * self.neighbors[nid].pdr for nid in contributors)

    def _alt_paths_global(self):
        """Worst-case (minimum) alt-paths across my segments — the bottleneck."""
        segs = set(self.segment_of.values())
        if not segs:
            return 0.0
        return min(self.alt_paths(s) for s in segs)

    # ==================================================================
    # v0.3 ADAPTIVE INHIBITION — replaces "duplicate = cancel" as a
    # universal rule with a local inhibition strength.
    # ==================================================================
    def inhibition_strength(self, seg=None):
        """'weak' | 'medium' | 'strong'.

        weak:   bridge/unique route, few alternative paths, or LOW confidence
                -> do not cancel pending forwards on duplicates; suppression
                blocked for unique coverage.
        medium: default (cancel on echo at MED/HIGH confidence).
        strong: dense redundant topology with HIGH confidence -> cancel on
                first echo.
        v0.12b B3 (herd): routing_order gates the level — suppression beyond
        'weak' requires a CLEAR local winner (high order); chaotic/unclear
        situations (low order) stay reach-first. ablatable: enable_herd.
        """
        if self.p.get('inhibition_strength_mode', 'adaptive') == 'fixed_medium':
            return 'medium'
        alts = self.alt_paths(seg) if seg is not None else self._alt_paths_global()
        if self._confidence_level() == 0:
            return 'weak'
        if alts <= self.p['alt_paths_sparse'] or self._unique_coverage_any() > 0:
            return 'weak'
        if alts >= self.p['alt_paths_dense']:
            level = 'strong'
        else:
            level = 'medium'
        if self.p.get('enable_herd', False):
            order = self.routing_order()
            if level == 'strong' and order < self.p.get('herd_order_min_strong', 0.6):
                level = 'medium'
            if level == 'medium' and order < self.p.get('herd_order_min_medium', 0.35):
                level = 'weak'
        return level

    def _my_unique_for_packet(self, p, residual, relayers):
        """Residual nodes only I can cover (bridge protection for MY relay)."""
        return sum(1 for x in residual
                   if all(x not in self.two_hop_view(r) for r in relayers))

    # ==================================================================
    # v0.3 HOMEOSTASIS / RELAY FATIGUE (experimental, ablatable)
    # ==================================================================
    def _note_forward_load(self):
        """Called on each of my own relay transmissions: fatigue rises,
        decays over time, never beats unique coverage."""
        if not self.p.get('fatigue_enabled', False):
            return
        now = self.env.now
        self._fatigue_decay(now)
        self.relay_fatigue = min(1.0, self.relay_fatigue + 0.10)
        self._fatigue_last = now

    def _fatigue_decay(self, now):
        if not self.p.get('fatigue_enabled', False):
            return
        dt = max(0.0, now - getattr(self, '_fatigue_last', now))
        lam = -math.log(0.5) / max(1.0, float(self.p.get('fatigue_decay_s', 60000)))
        self.relay_fatigue = self.relay_fatigue * math.exp(-lam * dt)
        self._fatigue_last = now

    # ==================================================================
    # LEARNING (ETAP 3): passive, runs for EVERY decoded packet
    # ==================================================================
    def learn(self, p, rssi):
        """Passive learning from one decoded packet + echo/inhibition tracking."""
        # ECHO/inhibition FIRST: must run for every decoded copy, even when this
        # packet is an implicit-ACK for something in our TX queue (in that case
        # MeshNode.receive() skips the relay branch entirely).
        self._observe_copy(p)
        now = self.env.now
        tx = p.txNodeId
        is_ni = getattr(p, 'is_neighborinfo', False)
        is_probe = getattr(p, 'is_probe', False) or getattr(p, 'is_probe_response', False)

        # v0.5: probe-response arrival at the probe origin = fresh route;
        # wake the waiting DM send (probe-first flow)
        if getattr(p, 'is_probe_response', False) and p.destId == self.node.nodeid:
            self._probe_response_seen.add(p.origTxNodeId)
            ev = self._probe_event.pop(p.origTxNodeId, None)
            if ev is not None and not ev.triggered:
                ev.succeed()
            self.stats['probe_responses_received'] = self.stats.get('probe_responses_received', 0) + 1

        # v0.5 reach-first guard: track min hopLimit heard per data seq
        # (progressed evidence for the PRIMARY dupe-ignore)
        if not is_ni and not is_probe:
            mh = self._min_hop_heard.get(p.seq)
            self._min_hop_heard[p.seq] = p.hopLimit if mh is None else min(mh, p.hopLimit)
            if len(self._min_hop_heard) > 500:
                for k in list(self._min_hop_heard.keys())[:250]:
                    del self._min_hop_heard[k]
            # v0.8 LPR: passive potential observation — a packet originated at
            # dest relayed by tx reached me at hop_depth hops from the origin;
            # local distance-vector estimate (no oracle, no global map)
            if self.p.get('enable_lpr', False) and p.origTxNodeId != tx:
                hop_depth = self.conf.hopLimit - p.hopLimit
                if hop_depth > 0:
                    hop_cost = p.timeOnAir * 1.25   # ETX 0.8 per-hop assumption
                    self.potential.observe_relayed_packet(
                        p.origTxNodeId, tx, hop_depth, hop_cost, now)
                    # packet-class accounting (inert metadata)
                    p.packet_class = (PCLASS_EMERGENCY if getattr(p, 'is_emergency', False)
                                      else PCLASS_DM)

        # NEIGHBORINFO: merge advertised table, update sender, done
        if is_ni:
            self._receive_neighborinfo(p)
            nb = self.neighbors.get(tx)
            if nb is None:
                nb = NeighborInfo(now, rssi, self.noise,
                                  self.p['weight_init'], self.p['pdr_init'], self.n_segments)
                self.neighbors[tx] = nb
            else:
                nb.obs += 1
                nb.last_seen = now
            self.observations += 1
            if self.observations % int(self.p['obs_prune_interval']) == 0:
                self._expire_stale(now)
                self._expire_advertised(now)
                if self.p.get('segmentation', 'adaptive') != 'off':
                    self._recompute_segments(now)
            return
        nb = self.neighbors.get(tx)
        if nb is None:
            nb = NeighborInfo(now, rssi, self.noise,
                              self.p['weight_init'], self.p['pdr_init'], self.n_segments)
            self.neighbors[tx] = nb
            self._ni_version_dirty = True   # v0.4: table changed
        else:
            nb.obs += 1
            nb.rssi_ema = _ewma(nb.rssi_ema, rssi, 0.3)
            nb.snr_ema = _ewma(nb.snr_ema, rssi - self.noise, 0.3)
            nb.last_seen = now
        nb.stability = _ewma(nb.stability, 1.0, 0.2)

        # neighbor position only if carried by the packet (POSITION semantics)
        pos = getattr(p, 'pos_x', None)
        if pos is not None and getattr(p, 'pos_y', None) is not None:
            if nb.pos is not None and nb._last_pos is not None:
                # v0.6: neighbor velocity from POSITION deltas (locally
                # realistic: a real node computes this from received updates)
                dt = (now - nb.last_seen) / 1000.0
                if dt > 0:
                    d = math.hypot(p.pos_x - nb.pos[0], p.pos_y - nb.pos[1])
                    nb.velocity = _ewma(nb.velocity, d / dt, 0.3)
            nb.pos = (p.pos_x, p.pos_y)
            nb._last_pos = now

        # 2-hop topology: tx heard seq originated by origTx -> they are linked
        if p.origTxNodeId != tx:
            self.neighbors_of_nb.setdefault(tx, set()).add(p.origTxNodeId)
            self.neighbors_of_nb.setdefault(p.origTxNodeId, set()).add(tx)

        # who already relayed this seq (for coverage/inhibition)
        self.packet_relayers.setdefault(p.seq, set()).add(tx)
        # v0.9 DRF: Time-of-Arrival mapping (passive topology discovery)
        arr = self._relay_arrivals.setdefault(p.seq, {})
        arr[tx] = now
        if len(self._relay_arrivals) > 300:
            for k in list(self._relay_arrivals.keys())[:150]:
                del self._relay_arrivals[k]
        # v0.9 DRF: min hopLimit among REPEAT copies (ripple layer inhibition)
        if self.node.timesReceived.get(p.seq, 0) > 1:
            rmh = self._rep_min_hop.get(p.seq)
            self._rep_min_hop[p.seq] = p.hopLimit if rmh is None else min(rmh, p.hopLimit)

        # reverse-path route learning: the neighbor we heard origin O through
        # is one hop closer to O (classic distance-vector hint, local only)
        if tx != p.origTxNodeId:
            r = self.routes.get(p.origTxNodeId)
            if r is None or now - r.get('last_updated', 0) > self.p['route_expiry_ms']:
                self.routes[p.origTxNodeId] = {
                    'primary': tx, 'backup1': None, 'backup2': None,
                    'score': 0.0, 'confidence': self.confidence(),
                    'last_updated': now, 'learned_passive': True}
        if len(self.packet_relayers) > 500:
            for k in list(self.packet_relayers.keys())[:250]:
                del self.packet_relayers[k]

        self.observations += 1

        # periodic housekeeping (bounded CPU-cost)
        if self.observations % int(self.p['obs_prune_interval']) == 0:
            self._expire_stale(now)
            self.potential.decay(now)   # v0.8 LPR: slow confidence decay
            if self.p.get('segmentation', 'adaptive') != 'off':
                self._recompute_segments(now)

    def _expire_stale(self, now):
        nb_exp = self.p['neighbor_expiry_ms']
        stale = [nid for nid, n in self.neighbors.items() if now - n.last_seen > nb_exp]
        for nid in stale:
            del self.neighbors[nid]
            self.neighbors_of_nb.pop(nid, None)
            self.segment_of.pop(nid, None)
        if stale:
            self._ni_version_dirty = True   # v0.4: table changed
        rt_exp = self.p['route_expiry_ms']
        stale_r = [d for d, r in self.routes.items() if now - r['last_updated'] > rt_exp]
        for d in stale_r:
            del self.routes[d]
        # v0.14 D0: TOP-K bounded neighbor state (firmware RAM realism).
        # Runs on the prune cadence; disabled by default (ALL = current).
        self._topk_evict(now)
        # v0.14: mobility score observation on the same cadence
        self._mobility_observe(now)
        # v0.14 CEF_BOOST: traffic-jam window observer (same cadence)
        self._cef_boost_observe(now)

    # ==================================================================
    # v0.14 D0: TOP-K bounded neighbor state — CORE + PROTECTED classes
    # ==================================================================
    def _topk_protected(self, nid):
        """PROTECTED neighbors are never evicted regardless of ranking:
        unique-coverage holders, sole/lone segment contributors (their
        segment dies with them), alt-starved links. These are exactly the
        frontier/bridge links the router doctrine protects elsewhere.
        v0.14 D0 finding: 'generous' over-protects in dense (every node has
        singleton segments -> ~21/27 protected -> table NOT bounded).
        'strict' protects only the GLOBAL-BOTTLENECK segment's contributors
        plus true unique-coverage holders."""
        if self._unique_coverage(nid) > 0:
            return True
        seg = self.segment_of.get(nid)
        if seg is None:
            return False
        if self.p.get('topk_protect_mode', 'generous') == 'strict':
            segs = set(self.segment_of.values())
            if not segs:
                return False
            worst = min(segs, key=lambda s: (self.alt_paths(s), s))
            return seg == worst and self.alt_paths(seg) <= 1.5
        return self.alt_paths(seg) <= 1.5

    def _topk_rank(self, nid):
        """CORE information value:
        I_j = w_p*PDR + w_s*Stability + w_n*Novelty - w_e*ETX_n."""
        nb = self.neighbors[nid]
        p = self.p
        nov = min(1.0, self._unique_coverage(nid) / 3.0)
        etx_n = 1.0 / (1.0 + nb.etx)
        return (float(p.get('topk_w_pdr', 1.0)) * nb.pdr
                + float(p.get('topk_w_stab', 0.5)) * nb.stability
                + float(p.get('topk_w_nov', 1.0)) * nov
                - float(p.get('topk_w_etx', 0.5)) * etx_n)

    def _topk_evict(self, now):
        """Bound the table to K CORE + PROTECTED. Evicted entries drop their
        derived state (neighbors_of_nb, segment_of); the table version bump
        triggers NEIGHBORINFO resync. Deterministic (rank, nid) ordering."""
        if not self.p.get('topk_enabled', False):
            return
        k = int(self.p.get('topk_k', 8))
        if len(self.neighbors) <= k:
            return
        keep = set()
        if self.p.get('topk_protect', True):
            keep = {nid for nid in self.neighbors if self._topk_protected(nid)}
        core = [nid for nid in self.neighbors if nid not in keep]
        core.sort(key=lambda nid: (self._topk_rank(nid), nid), reverse=True)
        keep |= set(core[:k])
        for nid in [nid for nid in self.neighbors if nid not in keep]:
            del self.neighbors[nid]
            self.neighbors_of_nb.pop(nid, None)
            self.segment_of.pop(nid, None)
            self.stats['topk_evictions'] += 1
        self.stats['topk_table_high'] = max(
            self.stats.get('topk_table_high', 0), len(self.neighbors))
        self._ni_version_dirty = True   # v0.4: table changed

    # ==================================================================
    # v0.14: mobility self-demotion (frequently moving node acts as
    # CLIENT_MUTE for broadcast relaying; emergency/DM still pass)
    # ==================================================================
    def _mobility_observe(self, now):
        """Local mobility score EWMA. Signals (either suffices):
        - own-position delta / mobility_dist_norm_m — GPS-haves signal
          (own position is LOCAL knowledge; the deployment has a GPS
          MINORITY, so churn is the primary signal for the rest);
        - neighbor-table churn since last observation (adds+removals
          normalized by 20% of table size) — GPS-free fallback."""
        if not self.p.get('mobility_enabled', False):
            return
        cur = set(self.neighbors)
        churn = 0.0
        if self._mob_prev_nids is not None:
            churn = min(1.0, (len(cur - self._mob_prev_nids)
                              + len(self._mob_prev_nids - cur))
                        / (0.2 * max(1, len(cur))))
        self._mob_prev_nids = cur
        delta = 0.0
        pos = getattr(self.node, 'position', None)
        if pos is not None:
            if self._mob_last_pos is not None:
                d = math.hypot(pos.x - self._mob_last_pos[0],
                               pos.y - self._mob_last_pos[1])
                delta = min(1.0, d / float(self.p.get('mobility_dist_norm_m', 300.0)))
            self._mob_last_pos = (pos.x, pos.y)
        a = float(self.p.get('mobility_alpha', 0.2))
        raw = max(delta, churn)
        self._mob_score = raw if self._mob_score is None \
            else (1.0 - a) * self._mob_score + a * raw
        if self._mob_score >= float(self.p.get('mobility_threshold', 0.5)):
            self.stats['mobility_recommend_mute'] += 1

    # ==================================================================
    # v0.14 D2-D4: minimal SHEPHERD-COLLECT
    # ==================================================================
    def _shepherd_score(self):
        """D4-lite election score (local only): higher -> shorter timer and
        wins cancel-on-better. stability + segment visibility + link quality
        + structural risk - load. Note: confidence is deliberately NOT the
        dominant term (mixed30 deaths came with HIGH-but-wrong confidence)."""
        if not self.neighbors:
            return 0.0
        nb_stab = sum(nb.stability for nb in self.neighbors.values()) / len(self.neighbors)
        visibility = min(1.0, len(set(self.segment_of.values())) / 4.0)
        linkq = sum(nb.pdr for nb in self.neighbors.values()) / len(self.neighbors)
        risk = self.structural_risk()
        load = min(1.0, self.node.channel_utilization_percent() / 50.0)
        return (0.3 * nb_stab + 0.2 * visibility + 0.2 * linkq
                + 0.2 * risk - 0.2 * load + 0.2)

    def _shepherd_candidacy(self):
        """Trigger = SUSTAINED local suppression-regret (the C1e EWMA:
        my recent frontier-candidate suppressions left packets dead) at
        frontier-sized degree. The degree cap is the ultra-dense silence
        guard: dense 'frontier-looking' singleton segments are redundancy,
        not fronts (v0.13 C2b-e lesson)."""
        if not self.p.get('shepherd_enabled', False) \
                or self._shepherd_timer_armed \
                or self._collect_until > self.env.now:
            return False
        if self._rescue_regret_ewma is None \
                or self._rescue_regret_n < int(self.p.get('shepherd_min_checks', 3)) \
                or self._rescue_regret_ewma < float(self.p.get('shepherd_regret_threshold', 0.5)):
            return False
        return len(self.neighbors) <= int(self.p.get('shepherd_max_degree', 16))

    def _arm_shepherd_election(self):
        if self._shepherd_timer_armed:
            return
        self._shepherd_timer_armed = True
        self.env.process(self._shepherd_elect())

    def _shepherd_elect(self):
        """Self-election timer: score-scaled delay; fires COLLECT if the
        regret is still sustained when the timer expires."""
        score = self._shepherd_score()
        delay = float(self.p.get('shepherd_timer_base_ms', 3000.0)) * (1.5 - score)
        yield self.env.timeout(max(100.0, delay) / self.node.clock_scale)
        self._shepherd_timer_armed = False
        if self._rescue_regret_ewma is None or \
                self._rescue_regret_ewma < float(self.p.get('shepherd_regret_threshold', 0.5)):
            return   # regret cooled while waiting — stand down
        self._fire_collect()

    def _fire_collect(self):
        """D2: minimal COLLECT message: segment (implicit — my position),
        epoch, mode, lease, reach_bias. Tiny broadcast, hopLimit 1 (the
        frontier neighborhood is where the dying relays are)."""
        from lib.packet import MeshPacket
        now = self.env.now
        self._shepherd_epoch += 1
        self._collect_until = now + float(self.p.get('shepherd_lease_ms', 45000.0))
        seq = 2_000_000 + self.node.nodeid * 1000 + (self._shepherd_epoch & 0xFF)
        pNew = MeshPacket(self.conf, self.node.nodes, self.node.nodeid, BROADCAST_ID,
                          self.node.nodeid, 8, seq, now, False, False, None, now,
                          self.node.connectivity_map, self.node.baseline_pathloss_matrix)
        pNew.hopLimit = 1
        pNew.is_shepherd = True
        pNew.shepherd_epoch = self._shepherd_epoch
        pNew.shepherd_lease_ms = float(self.p.get('shepherd_lease_ms', 45000.0))
        pNew.shepherd_score = self._shepherd_score()
        self.node.packets.append(pNew)
        self.node.env.process(self.node.transmit(pNew))
        self.stats['shepherd_msgs_tx'] += 1

    def _collect_active(self):
        return (self.p.get('shepherd_enabled', False)
                and self._collect_until > self.env.now)

    # ==================================================================
    # v0.14 CEF_BOOST: 'traffic-jam mode' — CEF as an aggressive congestion
    # regime gated by SUSTAINED local evidence, not a fixed router.
    # Enter: busy AND redundant AND low-frontier-risk, STABLE for N windows.
    # Exit: IMMEDIATELY on any frontier signal (regret, rescue, lease, drop
    # in any of the jam conditions). This is the FSM: N3+SHEP2D (normal)
    # → pure CEF (jam) → back (frontier). Doctrine: sustained-evidence
    # entry + instant exit = no oscillation, no topological guessing.
    # ==================================================================
    def _cef_boost_exit(self, reason=''):
        """Instant exit from jam mode — any frontier signal."""
        if self._cef_boost:
            self._cef_boost = False
            self._boost_stable = 0
            self.stats['boost_exits'] += 1

    def _cef_boost_observe(self, now):
        """Window observer (prune cadence). Computes per-window deltas of
        the evidence counters, checks the jam condition set, and manages
        the hysteresis counter."""
        if not self.p.get('cef_boost_enabled', False):
            return
        self.stats['boost_observed'] = self.stats.get('boost_observed', 0) + 1
        cur = {
            'rescues': self.stats.get('rescues_during_collect', 0),
            'fwd': self.stats.get('n3_forward_rank0', 0) + self.stats.get('n3_forward_rank1', 0)
                   + self.stats.get('n3_forward_rank2', 0),
            'copies': self.stats.get('copies_before_forward', 0),
            'supp': self.stats.get('suppressed_by_census', 0),
            'regret': self.stats.get('frontier_suppression_regret', 0),
        }
        prev = self._boost_win
        self._boost_win = cur
        if not prev:
            return   # first window: baseline only

        d_resc = cur['rescues'] - prev['rescues']
        d_fwd = cur['fwd'] - prev['fwd']
        d_copies = cur['copies'] - prev['copies']
        d_regret = cur['regret'] - prev['regret']

        # --- EXIT (checked first: any frontier signal = instant) ---
        if self._cef_boost:
            if d_resc > 0 or d_regret > 0 \
                    or self._rescue_regret_ewma is not None \
                    and self._rescue_regret_ewma >= float(self.p.get('shepherd_regret_threshold', 0.5)) \
                    or self._collect_until > now:
                self._cef_boost_exit('frontier_signal')
                return
            # stay-conditions: still busy + still redundant
            still_jam = (self._observed_channel_busy() >= float(self.p.get('boost_chanutil_min', 0.30))
                         and len(self.neighbors) >= int(self.p.get('boost_degree_min', 12))
                         and self._cef_frontier_risk() <= float(self.p.get('boost_frontier_max', 0.3)))
            if not still_jam:
                self._cef_boost_exit('conditions_dropped')
            return

        # --- ENTRY: all jam conditions must hold ---
        # v0.14 doctrine: each condition maps to a REAL firmware observable:
        # busy = channelUtilizationPercent (nRF52840 AirTime module)
        # degree = local neighbor table size
        # redundant = census copies heard per own forward (real RX evidence)
        # no_rescue = zero rescue/regret activity (nobody is calling for help)
        # The OLD frontier check (structural_risk <= 0.3) was REMOVED:
        # singleton-segment artifacts inflate alt_risk in dense, and the
        # redundant + no_rescue conditions ARE the real-world frontier
        # indicator (many copies heard + nobody dying = NOT a frontier)
        # busy = channel activity from THIS node's perspective: RX airtime of
        # all heard packets + own TX. In firmware this maps to
        # airtime->channelUtilizationPercent (channel-wide, not own-TX).
        # The simulator's channel_utilization_percent is own-TX only — use
        # the census RX evidence instead: total packet receptions per window
        # (copies + suppressions + forwards) × ToA / window_duration.
        heard = (cur['copies'] - prev.get('copies', 0)) \
              + (cur['supp'] - prev.get('supp', 0)) \
              + (cur['fwd'] - prev.get('fwd', 0))
        if d_resc or heard == 0:
            busy = False
        else:
            busy = heard >= int(self.p.get('boost_busy_pkts_min', 20))
        degree = len(self.neighbors) >= int(self.p.get('boost_degree_min', 12))
        redundant = d_fwd > 0 and d_copies >= d_fwd * float(self.p.get('boost_dupe_ratio_min', 0.8))
        no_rescue = d_resc == 0 and d_regret == 0 \
            and (self._rescue_regret_ewma is None
                 or self._rescue_regret_ewma < float(self.p.get('shepherd_regret_threshold', 0.5)))

        # per-condition diagnostics (numeric counters for the runner export)
        for cond, val in (('busy', busy), ('degree', degree),
                          ('redundant', redundant), ('rescue', no_rescue)):
            if not val:
                self.stats['boost_fail_' + cond] = \
                    self.stats.get('boost_fail_' + cond, 0) + 1

        if busy and degree and redundant and no_rescue:
            self._boost_stable += 1
            if self._boost_stable >= int(self.p.get('boost_stable_windows', 3)):
                self._cef_boost = True
                self.stats['boost_windows'] += 1
        else:
            self._boost_stable = 0   # any single failure resets the count

    def _rescue_credit(self, p=None):
        """v0.14 D3/N4: budgeted rescue. Token bucket with LAZY regeneration
        (no timers, MCU-friendly). Two modes:
        - D3 (default): packet-count bucket scaled by airtime headroom.
        - N4 (n4_enabled): AIRTIME-ms bucket — each rescue costs the packet's
          real ToA; AIMD: under LOW chanUtil the regen rate grows (+alpha),
          under HIGH it multiplicatively decays (x beta); the budget cap
          carries a structural multiplier (frontier/bridge links get more
          capacity — never starved by pure degree economics)."""
        now = self.env.now
        if self.p.get('n4_enabled', False) and p is not None:
            return self._n4_credit(p, now)
        regen = float(self.p.get('shepherd_rescue_regen_ms', 15000.0))
        headroom = max(0.0, 1.0 - self.node.channel_utilization_percent() / 100.0)
        bmax = float(self.p.get('shepherd_rescue_budget', 4.0)) * headroom
        dt = now - getattr(self, '_rescue_credits_t', now)
        c = min(bmax, getattr(self, '_rescue_credits', bmax) + dt / max(1.0, regen))
        self._rescue_credits_t = now
        if c < 1.0:
            self._rescue_credits = c
            return False
        self._rescue_credits = c - 1.0
        return True

    def _n4_credit(self, p, now):
        """N4 airtime token bucket (ms of airtime; lazy regen; AIMD;
        structural multiplier; emergency is exempt upstream)."""
        base = float(self.p.get('n4_base_tokens_ms', 3000.0))
        regen_s = float(self.p.get('n4_regen_period_ms', 30000.0))
        util = self.node.channel_utilization_percent() / 100.0
        # AIMD on the REGEN RATE, re-evaluated lazily per access
        rate = base / regen_s                       # ms of airtime per ms of wall
        if util < float(self.p.get('n4_util_low', 0.30)):
            rate *= (1.0 + float(self.p.get('n4_alpha', 0.5)))
        elif util > float(self.p.get('n4_util_high', 0.60)):
            self._n4_bucket = getattr(self, '_n4_bucket', base) \
                * float(self.p.get('n4_beta', 0.7))   # multiplicative decrease
            self._n4_bucket_t = now
        # structural multiplier: frontier risk raises the CAP, never lowers
        struct = 1.0 + float(self.p.get('n4_struct_w', 1.0)) \
            * self._cef_frontier_risk(p)
        bmax = base * struct
        dt = now - getattr(self, '_n4_bucket_t', now)
        c = min(bmax, getattr(self, '_n4_bucket', base) + rate * dt)
        self._n4_bucket_t = now
        cost = self._packet_toa_ms(p)                 # real airtime of THIS TX
        if c < cost:
            self._n4_bucket = c
            self.stats['n4_starved'] += 1
            return False
        self._n4_bucket = c - cost
        self.stats['n4_airtime_granted'] += cost
        return True

    # ==================================================================
    # v0.14 N1: receiver-evidence adaptive forwarding (CBB / N1a / N1b /
    # N1c family). The node does NOT predict redundancy from a score —
    # it waits W x ToA (+hash jitter <= 0.25 ToA) and counts ACTUAL
    # retransmissions of the same (origTxNodeId, seq), then decides
    # FORWARD/SUPPRESS. Static bounded state (MAX_CENSUS_IDS, MAX_PENDING).
    # ==================================================================
    def _packet_toa_ms(self, p):
        """Airtime of the given packet under the CURRENT PHY profile —
        all N1 timers are expressed as ToA multiples (never hardcoded ms)."""
        return float(getattr(p, 'timeOnAir', 369.0))

    def _n1_k(self, p):
        """Census threshold K (copies needed to suppress).
        fixed: CBB baseline. degree (N1b): MORE redundancy -> EASIER to
        stay silent (never inverted); sparse/frontier -> suppression off.
        adaptive (N1c): K tinted by degree/alt/chanUtil — but the final
        decision is ALWAYS on actually-heard copies, never on the score."""
        mode = self.p.get('n1_mode', 'fixed')
        if mode == 'fixed':
            return int(self.p.get('n1_k', 3))
        deg = len(self.neighbors)
        if mode == 'degree':
            if deg <= self.p['density_sparse'] or self.structural_risk() >= 0.5:
                return 99                      # frontier: suppression disabled
            if deg >= self.p.get('density_dense', 12):
                return 2                       # dense: silence comes cheap
            return 3
        # n1c adaptive
        k = 2 + int(2.0 * self._cef_frontier_risk(p))   # front risk -> high K
        if self.node.channel_utilization_percent() > 50.0:
            k = max(2, k - 1)                  # heavy channel: lean quieter
        return min(6, k)

    def _n1_handle(self, p):
        """Census bookkeeping + one-time decision deferral per seq."""
        ent = self._n1_census.get(p.seq)
        if ent is not None:
            if ent['decided']:
                return False
            ids = ent['ids']
            tx = p.txNodeId
            if tx != self.node.nodeid and tx not in ids:
                if len(ids) < int(self.p.get('n1_max_census_ids', 8)):
                    ids.append(tx)             # else: spec — ignore extra ids
            return False
        # first copy: open census (bounded; evict oldest when full)
        if len(self._n1_census) >= int(self.p.get('n1_max_pending', 64)):
            oldest = next(iter(self._n1_census))
            del self._n1_census[oldest]
        self._n1_census[p.seq] = {'ids': [p.txNodeId] if p.txNodeId != self.node.nodeid else [],
                                  'decided': False, 't0': self.env.now}
        self.env.process(self._n1_decide(p))
        return True

    def _n1_decide(self, p):
        toa = self._packet_toa_ms(p)
        w = float(self.p.get('n1_w_toa', 1.0)) * toa
        jitter = self._packet_hash_slot(p, k=16) / 16.0 * 0.25 * toa
        yield self.env.timeout(max(1.0, w + jitter) / self.node.clock_scale)
        ent = self._n1_census.get(p.seq)
        if ent is None or ent['decided']:
            return
        ent['decided'] = True
        copies = len(ent['ids'])
        k = self._n1_k(p)
        if copies >= k:
            self.stats['suppressed_by_census'] += 1
            self.stats['copies_before_suppress'] += copies
            self._log_death(p, 'census_suppress', {'copies': copies, 'k': k})
            return
        self.stats['copies_before_forward'] += copies
        self._relay_packet(p)

    # ==================================================================
    # v0.14 N3: ranked two-phase whisper — preferred relays fire first
    # (RANK0), the main body waits one gap and judges on the census,
    # the silent backup (RANK2) fires only in near-silence. Silence IS
    # information, but one missing echo != failure: windows are ToA-based
    # so the previous class physically finishes first (E2 lesson).
    # Order policies are an explicit experiment dimension:
    #   designated  — PRIMARY/B1 first (current AR machinery)
    #   edge_first  — weak-margin forwarders first (= Meshtastic MT)
    #   strong_first— strong links first (= MeshCore rxdelay)
    # Bounded edge doctrine (N2): weak links are preferred ONLY above the
    # reliability floor (no 'same weak screamer always wins' pathology).
    # ==================================================================
    def _n3_rank(self, p):
        policy = self.p.get('n3_order_policy', 'designated')
        if policy == 'designated':
            desig = getattr(p, 'relay_designation', None) or []
            return 0 if self.node.nodeid in desig else 1
        try:
            rssi = p.rssiAtN[self.node.nodeid]
        except (AttributeError, IndexError, TypeError):
            rssi = -100.0
        margin = float(rssi) - float(self.conf.current_preset['sensitivity'])
        if margin < float(self.p.get('n3_min_link_margin_db', 6.0)):
            return 2                       # below reliability floor: never preferred
        weak = margin <= float(self.p.get('n3_edge_margin_db', 12.0))
        if policy == 'edge_first':
            return 0 if weak else 1
        return 1 if weak else 0             # strong_first

    def _n3_handle(self, p):
        ent = self._n1_census.get(p.seq)
        if ent is not None:
            if ent['decided']:
                return False
            ids = ent['ids']
            tx = p.txNodeId
            if tx != self.node.nodeid and tx not in ids \
                    and len(ids) < int(self.p.get('n1_max_census_ids', 8)):
                ids.append(tx)
            return False
        if len(self._n1_census) >= int(self.p.get('n1_max_pending', 64)):
            oldest = next(iter(self._n1_census))
            del self._n1_census[oldest]
        rank = self._n3_rank(p)
        self._n1_census[p.seq] = {'ids': [p.txNodeId] if p.txNodeId != self.node.nodeid else [],
                                  'decided': False, 'rank': rank, 't0': self.env.now}
        self.env.process(self._n3_decide(p, rank))
        return True

    def _n3_decide(self, p, rank):
        toa = self._packet_toa_ms(p)
        gap = float(self.p.get('n3_gap_toa', 1.25)) * toa
        t0 = float(self.p.get('n3_t0_frac', 0.25)) * toa
        jitter = self._packet_hash_slot(p, k=8) / 8.0 * 0.25 * toa
        base = t0 if rank == 0 else t0 + 0.25 * toa + gap * rank
        yield self.env.timeout(max(1.0, base + jitter) / self.node.clock_scale)
        ent = self._n1_census.get(p.seq)
        if ent is None or ent['decided']:
            return
        ent['decided'] = True
        copies = len(ent['ids'])
        if rank == 0:
            k = 99                          # preferred: forward (echo-cancel still applies)
        elif rank == 1:
            k = self._n1_k(p)               # census threshold (mode-dependent)
        else:
            k = int(self.p.get('n3_k2', 2))  # fallback: fires only in near-silence
        if copies >= k:
            # v0.14 E8: SHEPHERD as the SECOND line — a census suppression
            # under an active COLLECT lease means the network corroborated
            # that this segment's fronts die; lease (permission) + budget
            # (capacity) convert the suppression into a rescue forward.
            # Fast local layer first, network regulation second.
            if self.p.get('shepherd_enabled', False) and self._collect_active() \
                    and self._rescue_credit(p):
                self.stats['cef_frontier_rescued'] = \
                    self.stats.get('cef_frontier_rescued', 0) + 1
                self.stats['rescues_during_collect'] += 1
                self.stats['n3_shep_rescues'] += 1
                self.stats['copies_before_forward'] += copies
                self.stats['n3_forward_rank%d' % rank] += 1
                self._relay_packet(p)
                return
            self.stats['suppressed_by_census'] += 1
            self.stats['copies_before_suppress'] += copies
            if rank == 2:
                self.stats['n3_fallback_suppressed'] += 1
            # feed the shepherd regret machinery from census suppressions
            # (the trigger needs evidence that suppressed packets died)
            if self.p.get('shepherd_enabled', False):
                relays0 = len(self.packet_relayers.get(p.seq, set()))
                self.env.process(self._frontier_regret_check(p, relays0,
                                                             self._had_ack(p.seq)))
            self._log_death(p, 'census_suppress', {'copies': copies, 'k': k, 'rank': rank})
            return
        self.stats['copies_before_forward'] += copies
        self.stats['n3_forward_rank%d' % rank] += 1
        self._relay_packet(p)

    def _mobility_demoted(self):
        """True when this node should act as CLIENT_MUTE (high mobility,
        demotion enabled). Zero-oracle: local signals only."""
        return (self.p.get('mobility_demote', False)
                and self._mob_score is not None
                and self._mob_score >= float(self.p.get('mobility_threshold', 0.5)))

    # ==================================================================
    # PDR / ETX / weights (ETAP 4) — fed by watchdog (echo of my designation)
    # ==================================================================
    def _seg_weight_ref(self, nb, seg):
        """Return mutable index into per-segment weights, growing as needed.
        Topological segmentation can yield more segments than n_segments."""
        if seg is None:
            return None
        ws = nb.weight_seg
        while len(ws) <= seg:
            ws.append(self.p['weight_init'])
        return seg

    def _relay_outcome(self, nid, success):
        nb = self.neighbors.get(nid)
        now = self.env.now
        if nb is None:
            nb = NeighborInfo(now, -120, self.noise,
                              self.p['weight_init'], self.p['pdr_init'], self.n_segments)
            self.neighbors[nid] = nb
        nb.relay_attempts += 1
        a = self.p['pdr_alpha']
        nb.pdr = min(1.0, max(self.p['pdr_floor'],
                              _ewma(nb.pdr, 1.0 if success else 0.0, a)))
        if success:
            nb.relay_successes += 1
            if self.p['weight_enabled']:
                nb.weight = min(1.0, nb.weight + self.p['weight_lr_pos'] * (1.0 - nb.weight))
                seg = self._seg_weight_ref(nb, self.segment_of.get(nid))
                if seg is not None:
                    ws = nb.weight_seg
                    ws[seg] = min(1.0, ws[seg] + self.p['weight_lr_pos'] * (1.0 - ws[seg]))
        else:
            if self.p['weight_enabled']:
                nb.weight = max(0.0, nb.weight - self.p['weight_lr_neg'] * nb.weight)
                seg = self._seg_weight_ref(nb, self.segment_of.get(nid))
                if seg is not None:
                    ws = nb.weight_seg
                    ws[seg] = max(0.0, ws[seg] - self.p['weight_lr_neg'] * ws[seg])

    def _decay_weights(self, now):
        """Slow decay on weights without fresh observations."""
        lam = self.p.get('weight_decay_lambda', 0.0)
        if lam <= 0:
            return
        for nb in self.neighbors.values():
            dt_h = max(0.0, (now - nb.last_seen)) / 3600000.0
            if dt_h <= 0:
                continue
            f = math.exp(-lam * dt_h)
            base = self.p['weight_init']
            nb.weight = base + (nb.weight - base) * f

    # ==================================================================
    # CONFIDENCE (ETAP 9): global + per-segment
    # ==================================================================
    def confidence(self, segment=None):
        """0..1. Grows with observations (slowly — v0.3 fix: several early
        observations must NOT enable aggressive suppression), neighbor
        stability, PDR health and freshness of the information."""
        if not self.p.get('confidence_enabled', True):
            return 1.0
        n_obs = self.observations
        c_obs = min(1.0, n_obs / max(1.0, float(self.p['conf_obs_full'])))
        now = self.env.now
        nbs = list(self.neighbors.values())
        if segment is not None:
            nbs = [n for nid, n in self.neighbors.items()
                   if self.segment_of.get(nid) == segment]
        if nbs:
            stab = sum(n.stability for n in nbs) / len(nbs)
            pdr = sum(n.pdr for n in nbs) / len(nbs)
            # freshness of information (age penalty): stale neighbors lower
            # confidence even if they were reliable once
            fresh = sum(1.0 if (now - n.last_seen) < self.p['neighbor_expiry_ms'] / 3 else 0.0
                        for n in nbs) / len(nbs)
        else:
            stab, pdr, fresh = 0.0, self.p['pdr_init'], 0.0
        # v0.3 confidence: slower growth + freshness + NEIGHBORINFO consistency
        ni_conf = None
        if self.advertised_neighbors:
            ni_conf = sum(a['conf'] for a in self.advertised_neighbors.values()) / len(self.advertised_neighbors)
        c = 0.30 * c_obs + 0.20 * stab + 0.20 * pdr + 0.15 * fresh
        if ni_conf is not None:
            c += 0.15 * ni_conf
        else:
            c += 0.15 * c_obs  # no NI: observations carry that share
        return max(0.0, min(1.0, c))

    def _confidence_level(self, segment=None):
        c = self.confidence(segment)
        if c < self.p['conf_low']:
            return 0  # LOW
        if c < self.p['conf_high']:
            return 1  # MEDIUM
        return 2      # HIGH

    # ==================================================================
    # LOCAL SEGMENTATION (X): geo from learned positions, or topological
    # ==================================================================
    def _recompute_segments(self, now, force=False):
        """ADAPTIVE segmentation (X.20): event-triggered (only when local
        knowledge changed), minimum-interval bound (CPU cost), hysteresis
        (re-segment only when a significant fraction of neighbors changes),
        and per-node mode selection (geo when enough positions are known,
        topological otherwise). 'geo'/'topo'/'off' remain as forced modes
        for ablation."""
        mode = self.p.get('segmentation', 'adaptive')
        if mode == 'off':
            self.segment_of = {}
            self.segmentation_mode_active = 'off'
            return
        # event trigger: skip when learned topology is unchanged
        sig = (frozenset(self.neighbors.keys()),
               tuple(sorted((nid, len(cov)) for nid, cov in self.neighbors_of_nb.items())))
        if not force and sig == self._seg_sig:
            return
        if not force and now - self.last_seg_recompute < self.p.get('seg_recompute_s', 30) * 1000:
            return
        self._seg_sig = sig
        self.last_seg_recompute = now
        old_seg = dict(self.segment_of)

        if mode in ('functional', 'adaptive_functional'):
            self.segmentation_mode_active = 'functional'
            self.segment_of = self._segments_functional()
        elif mode == 'geo':
            self.segmentation_mode_active = 'geo'
            self._segments_geo()
        elif mode == 'topo':
            self.segmentation_mode_active = 'topo'
            self.segment_of = self._segments_topo()
        else:  # adaptive: per-node choice from LOCAL knowledge only
            n_pos = sum(1 for nb in self.neighbors.values() if nb.pos is not None)
            if self.neighbors and n_pos >= len(self.neighbors) * float(self.p.get('seg_pos_ratio', 0.6)):
                self.segmentation_mode_active = 'geo'
                self._segments_geo()
            else:
                self.segmentation_mode_active = 'functional'
                self.segment_of = self._segments_functional()
        self._segment_changes += 1

        # hysteresis: keep the old mapping when the change is insignificant
        thr = float(self.p.get('seg_hysteresis', 0.05))
        if old_seg and self.segment_of:
            changed = sum(1 for nid, s in self.segment_of.items() if old_seg.get(nid) != s)
            if changed <= len(self.segment_of) * thr:
                self.segment_of = old_seg
                return
        # weight carry-over: a neighbor that changed segment starts its new
        # per-segment weight from its global weight (no stale-value mixing)
        for nid, s in self.segment_of.items():
            nb = self.neighbors.get(nid)
            if nb is not None and old_seg.get(nid) != s:
                while len(nb.weight_seg) <= s:
                    nb.weight_seg.append(self.p['weight_init'])
                nb.weight_seg[s] = nb.weight

    def _segments_geo(self):
        """N sectors by direction to each KNOWN-position neighbor,
        relative to own position. Adaptive granularity (X.29): fewer sectors
        when few positioned neighbors are known. Neighbors without known
        position -> topological grouping."""
        me = self.node.position  # own position: allowed (a real node knows it)
        seg_of = {}
        positioned = [nid for nid, nb in self.neighbors.items() if nb.pos is not None]
        k = self.n_segments
        if positioned:
            # adaptive sector count: at least seg_min_per_sector neighbors per sector
            k = max(2, min(self.n_segments,
                           int(math.ceil(len(positioned) / float(self.p.get('seg_min_per_sector', 3))))))
        leftover = []
        for nid, nb in self.neighbors.items():
            if nb.pos is not None:
                dx = nb.pos[0] - me.x
                dy = nb.pos[1] - me.y
                ang = math.degrees(math.atan2(dy, dx)) % 360.0
                seg_of[nid] = int(ang / (360.0 / k)) % k
            else:
                leftover.append(nid)
        # unknown-position neighbors fall back to topological grouping
        topo = self._segments_topo(nodes=leftover)
        seg_of.update(topo)
        self.segment_of = seg_of

    def _segments_topo(self, nodes=None):
        """Group neighbors by overlap of their known 2-hop sets
        (Jaccard >= threshold => same segment). Greedy union, O(n^2) on
        neighbors — cheap enough for MCU."""
        ids = list(self.neighbors.keys()) if nodes is None else list(nodes)
        thr = self.p.get('seg_overlap_threshold', 0.5)
        seg_of = {}
        next_seg = 0
        rep_cover = {}   # seg -> union coverage (for incremental assignment)
        for nid in ids:
            cov = self.neighbors_of_nb.get(nid, set())
            best_seg, best_ov = None, 0.0
            for s, seg_cov in rep_cover.items():
                u = len(cov | seg_cov)
                ov = (len(cov & seg_cov) / u) if u else 0.0
                if ov > best_ov:
                    best_seg, best_ov = s, ov
            if best_seg is None or best_ov < thr:
                best_seg = next_seg
                next_seg += 1
                rep_cover[best_seg] = set()
            seg_of[nid] = best_seg
            rep_cover[best_seg] |= cov
        return seg_of

    # ==================================================================
    # COVERAGE helpers (known topology only — never global truth)
    # ==================================================================
    def _my_two_hop(self):
        cov = set()
        for nb in self.neighbors:
            cov |= self.two_hop_view(nb)
        cov.discard(self.node.nodeid)
        return cov

    def _unique_coverage(self, nid):
        """Known 2-hop nodes reachable ONLY via neighbor nid — bridge protection."""
        cov_n = self.two_hop_view(nid)
        if not cov_n:
            return 0
        others = set()
        for nb in self.neighbors:
            if nb != nid:
                others |= self.two_hop_view(nb)
        return len(cov_n - others)

    # ==================================================================
    # RELAY RANKING toward a unicast destination (ETAP 5)
    # ==================================================================
    def _candidate_score(self, nid, dest):
        nb = self.neighbors[nid]
        p = self.p
        # RSSI normalized to [0,1] over sensitivity..PTX range
        sens = self.conf.current_preset['sensitivity']
        rssi_n = max(0.0, min(1.0, (nb.rssi_ema - sens) / max(1.0, (self.conf.PTX - sens))))
        etx_n = 1.0 / (1.0 + nb.etx)     # ETX ~[1,inf) -> (0, .5]
        uniq = self._unique_coverage(nid)
        uniq_boost = p['w_unique_coverage'] * min(1.0, uniq / 3.0)
        # v0.10: stronger unique-coverage bonus in the activation score
        # (NOT a dupe-cancel bypass — audit confirmed those are harmful)
        if uniq > 0:
            uniq_boost += float(self.p.get('unique_coverage_bonus_extra', 0.2))
        # does this neighbor plausibly lead toward dest?
        toward = 0.0
        if nid == dest:
            toward = 1.0
        elif dest in self.neighbors_of_nb.get(nid, set()):
            toward = 0.7
        elif self.routes.get(dest, {}).get('primary') == nid:
            toward = 1.0
        seg = self.segment_of.get(nid)
        w_seg = (nb.weight_seg[seg]
                 if (seg is not None and 0 <= seg < len(nb.weight_seg))
                 else nb.weight)
        w_eff = 0.5 * nb.weight + 0.5 * w_seg if self.p['weight_enabled'] else 0.5
        score = (p['w_reliability'] * nb.pdr
                 + p['w_etx'] * etx_n
                 + p['w_rssi'] * rssi_n
                 + p['w_stability'] * nb.stability
                 + w_eff * 0.25
                 + uniq_boost
                 + 0.45 * toward)
        # v0.8 LPR: potential progress term (soft scoring — unknown potential
        # contributes 0, never a hard suppression; reach-first preserved).
        # Hysteresis is RELATIVE: only progress > 10% of Phi_i counts as a
        # real improvement (prevents flapping between similar routes).
        if self.p.get('enable_lpr', False):
            cost_ij = self._lpr_link_cost(nid)
            phi_j = self.potential.get_neighbor_phi(nid, dest)
            progress = self.potential.compute_progress(dest, cost_ij, phi_j,
                                                       now=self.env.now)
            if progress != float('-inf') and phi_j != float('inf'):
                phi_self = self.potential.get_phi(dest)
                progress_n = max(0.0, min(1.0, progress / max(1e-9, phi_self)))
                if progress_n > self.potential.hysteresis:
                    score += self.p.get('lpr_score_weight', 0.5) * progress_n
        return score

    def _lpr_link_cost(self, nid):
        """Local link cost to neighbor nid: airtime x ETX + congestion
        (channel utilization, locally known) + uncertainty."""
        nb = self.neighbors[nid]
        sens = self.conf.current_preset['sensitivity']
        base_airtime = self.conf.PACKETLENGTH and 1042.0  # LONG_FAST 40B estimate
        util = self.node.channel_utilization_percent() / 100.0
        uncertainty = max(0.0, 1.0 - min(1.0, nb.obs / 10.0))
        return self.potential.cost_model.link_cost(
            base_airtime, nb.etx, congestion=util,
            battery_penalty=0.0,   # battery not modeled in the simulator
            uncertainty=uncertainty,
            packet_class_penalty=0.1)

    def _pick_relays_toward(self, dest, exclude=()):
        """Return (PRIMARY, BACKUP1, BACKUP2) for DM toward dest.

        Backup prefers LOW 2-hop overlap with PRIMARY (path diversity, X.19).
        Applies switch hysteresis against the stored route to avoid flapping.
        `exclude` removes candidates (e.g. the previous hop — never relay back).
        """
        cands = [nid for nid in self.neighbors if nid not in exclude]
        if not cands:
            return None, None, None
        # v0.6 relay eligibility: mobile/overloaded nodes are never PRIMARY
        # (MeshCore roles: only stable nodes route); they stay backup-eligible
        primary_cands = [nid for nid in cands
                         if self.relay_eligibility(nid) >= self.p.get('eligibility_floor', 0.5)]
        if not primary_cands:
            primary_cands = cands  # everybody degraded -> fall back to all
        scored = sorted(((self._candidate_score(nid, dest), nid) for nid in primary_cands),
                        key=lambda t: -t[0])
        primary, primary_score = scored[0][1], scored[0][0]
        # v0.7 WalkFlood cone: prefer FORWARD-progress candidates (geo cone
        # or topological gradient); empty cone = blind alley -> backtrack
        # with a bounded TTL 1 (recorded on the route entry)
        backtrack = False
        cone = self._forwarding_cone(dest, exclude=set(exclude) | {primary})
        if cone:
            if primary not in cone:
                cone_scored = sorted(((self._candidate_score(nid, dest), nid)
                                      for nid in cone), key=lambda t: -t[0])
                if cone_scored and cone_scored[0][1] != primary:
                    self.stats['adaptive_route_switches'] += 1
                    primary, primary_score = cone_scored[0][1], cone_scored[0][0]
        else:
            backtrack = True  # blind alley: keep best-ETX pick, bounded TTL

        # hysteresis: keep existing primary unless challenger clearly better
        route = self.routes.get(dest)
        if route and route.get('primary') in self.neighbors:
            cur = route['primary']
            if cur != primary:
                cur_score = self._candidate_score(cur, dest)
                if primary_score < cur_score + self.p['switch_margin']:
                    primary, primary_score = cur, cur_score
                else:
                    self.stats['adaptive_route_switches'] += 1
                    self.stats['adaptive_primary_flaps'] += 1

        b1 = b2 = None
        if self.p['backups_enabled']:
            def diversity(nid):
                a = self.neighbors_of_nb.get(primary, set())
                b = self.neighbors_of_nb.get(nid, set())
                u = len(a | b)
                return 1.0 - (len(a & b) / u if u else 0.0)
            rest = [nid for nid in scored if nid[1] != primary]
            if rest:
                dw = self.p['backup_diversity_weight']
                ranked_b = sorted(rest,
                                  key=lambda t: -(t[0] * (1 - dw) + dw * diversity(t[1])))
                b1 = ranked_b[0][1]
                # v0.5 shorter designation chain: BACKUP2 only at LOW confidence
                # (fewer cancel-states, fewer backup collisions, simpler failover)
                if len(ranked_b) > 1 and self._confidence_level() == 0:
                    b2 = ranked_b[1][1]
        self.routes[dest] = {'primary': primary, 'backup1': b1, 'backup2': b2,
                             'score': primary_score,
                             'confidence': self.confidence(),
                             'last_updated': self.env.now,
                             'backtrack': backtrack}
        return primary, b1, b2

    # ==================================================================
    # v0.7 WALKFLOOD MODULE: progress cone (WHERE) — geo when positions
    # are known, topological gradient otherwise (few-GPS constraint)
    # ==================================================================
    def _dest_pos(self, dest):
        """Position of dest if locally known (direct neighbor with learned
        position). Far destinations are unknown — topological mode."""
        nb = self.neighbors.get(dest)
        if nb is not None and nb.pos is not None:
            return nb.pos
        return None

    def _topological_progress(self, nid, dest):
        """No-GPS forward-progress test: does nid plausibly lead toward dest?
        (nid == dest / dest in nid's 2-hop view / known route via nid)"""
        if nid == dest:
            return True
        if dest in self.two_hop_view(nid):
            return True
        if self.routes.get(dest, {}).get('primary') == nid:
            return True
        return False

    def _forwarding_cone(self, dest, exclude=()):
        """WalkFlood progress cone: candidates that make FORWARD progress.

        Geo mode (own + neighbor positions known): angle between
        (dest - me) and (neighbor - me) < cone_half_angle AND neighbor
        closer to dest than me.
        No GPS: topological gradient (routes / 2-hop / hop-depth).
        Empty cone = blind alley — caller may backtrack (best ETX, TTL 1).
        """
        me = self.node.position
        dest_pos = self._dest_pos(dest)
        cands = []
        for nid in self.neighbors:
            if nid in exclude:
                continue
            if dest_pos is not None:
                nb_pos = self.neighbors[nid].pos
                if nb_pos is not None:
                    vdx, vdy = dest_pos[0] - me.x, dest_pos[1] - me.y
                    vnx, vny = nb_pos[0] - me.x, nb_pos[1] - me.y
                    d_dest = math.hypot(vdx, vdy)
                    d_nb = math.hypot(vnx, vny)
                    if d_nb >= d_dest:
                        continue  # no progress: neighbor not closer to dest
                    dot = vdx * vnx + vdy * vny
                    cos_a = dot / max(1e-9, d_dest * d_nb)
                    ang = math.degrees(math.acos(max(-1.0, min(1.0, cos_a))))
                    if ang > self.p.get('cone_half_angle', 60.0):
                        continue
                    cands.append(nid)
                else:
                    if self._topological_progress(nid, dest):
                        cands.append(nid)
            else:
                if self._topological_progress(nid, dest):
                    cands.append(nid)
        return cands

    # ==================================================================
    # v0.6 RELAY ELIGIBILITY (MeshCore roles): mobile / fatigued nodes are
    # degraded and never designated PRIMARY. Battery is not modeled in the
    # simulator (documented); velocity is self-known (own movement) and
    # neighbor velocity is learned from POSITION deltas (locally realistic).
    # ==================================================================
    def _own_velocity(self):
        """EWMA of own movement speed (m/s) — a real node knows its own GPS."""
        me = self.node.position
        last = getattr(self, '_own_pos_last', None)
        now = self.env.now
        if last is None:
            self._own_pos_last = (me.x, me.y, now)
            return 0.0
        dt = (now - last[2]) / 1000.0
        if dt <= 0:
            return getattr(self, '_own_velocity_ewma', 0.0)
        d = math.hypot(me.x - last[0], me.y - last[1])
        v = d / dt
        self._own_pos_last = (me.x, me.y, now)
        self._own_velocity_ewma = _ewma(getattr(self, '_own_velocity_ewma', 0.0), v, 0.3)
        return self._own_velocity_ewma

    def relay_eligibility(self, nid=None):
        """0..1. < eligibility_floor -> never designated PRIMARY."""
        if not self.p.get('eligibility_enabled', True):
            return 1.0
        score = 1.0
        if nid is None:
            vel = self._own_velocity()
        else:
            nb = self.neighbors.get(nid)
            vel = nb.velocity if nb is not None else 0.0
        if vel > self.p.get('velocity_walk_threshold', 1.5):
            score *= 0.3   # mobile node = weak PRIMARY
        if getattr(self, 'relay_fatigue', 0.0) > 0.7:
            score *= 0.5   # homeostasis: overloaded relay
        return score

    # ==================================================================
    # v0.9 CEF: Critical Epidemic Forwarding (marginal-gain threshold)
    # ==================================================================
    # CEF replaces the coarse weak/medium/strong inhibition by a continuous
    # adaptive threshold. G = MC / (A_norm * (1+kc*C_i)^gamma * role_cost),
    # forward iff G > theta. Reach-first via reach_guard at LOW confidence.
    # ==================================================================
    def _cef_estimated_total_demand(self, p):
        """M_i(p): expected NEW deliveries from one more relay of packet p."""
        relayers = set(self.packet_relayers.get(p.seq, set()))
        n_relayers = len(relayers)
        if n_relayers == 0:
            return 0.5  # blind gain fallback: assume we are needed

        d_sum = 0.0
        for rn in relayers:
            nb = self.neighbors.get(rn)
            if nb is None:
                continue
            # P_relay_success: current PDR estimate
            p_relay = nb.pdr
            # probability this relay is already covered by the senders:
            # roughly fraction of my neighbors who have already relayed /
            # are currently relaying this packet.
            d_est = min(1.0, len(relayers & self.neighbors_of_nb.get(rn, set()) | set()) / max(1, n_relayers))
            d_sum += p_relay * (1.0 - d_est) * self._unique_coverage(rn)
        return max(0.0, d_sum)

    def _cef_get_class_weight(self, p):
        """Class weight W_class: higher for higher-priority traffic."""
        if getattr(p, 'is_emergency', False):
            return float(self.p['cef_class_weights']['EMERGENCY'])
        if p.destId == BROADCAST_ID:
            # broadcast telemetry/position: lowest priority
            if getattr(p, 'pos_x', None) is not None:
                return float(self.p['cef_class_weights']['TELEMETRY'])
            return float(self.p['cef_class_weights']['BROADCAST'])
        return float(self.p['cef_class_weights']['DM'])

    def _cef_role_cost(self):
        """R_cost: role multiplier. ROUTER is cheapest (best infra), CLIENT_MUTE
        is most expensive (battery-constrained). v0.14: a mobility-demoted
        node pays the CLIENT_MUTE cost (self-demotion)."""
        if self._mobility_demoted():
            return float(self.p['cef_role_cost']['CLIENT_MUTE'])
        role = self.node.role.value if hasattr(self.node.role, 'value') else str(self.node.role)
        return float(self.p['cef_role_cost'].get(role, 5.0))

    def _cef_estimate(self, p):
        """Compute normalized marginal-gain G (dimensionless):
        G = (M_i * W_class) / (A_norm * (1 + kc*C_i)^gamma * R_cost)."""
        c_i = self.node.channel_utilization_percent() / 100.0
        u_cost = 1.0 + self.p.get('cef_collision_weight', 0.5) * c_i
        A_ms = max(1.0, float(p.timeOnAir))   # timeOnAir is already in ms
        a_norm = max(0.1, A_ms / float(self.p['cef_A_ref_ms']))
        gamma = float(self.p['cef_gamma'])

        M = self._cef_estimated_total_demand(p)
        W_class = self._cef_get_class_weight(p)
        R_cost = self._cef_role_cost()

        numerator = M * W_class
        denominator = a_norm * (u_cost ** gamma) * R_cost
        G = numerator / max(1e-9, denominator)
        return dict(G=G, M=M, W=W_class, R=R_cost, u_cost=u_cost,
                    a_norm=a_norm, A_ms=A_ms)

    def _cef_suppression_pressure(self):
        """v0.12 B4: suppression_pressure = redundancy x confidence.
        REDUNDANCY answers 'may I suppress?' (local alt_paths normalized);
        CONFIDENCE answers 'may I trust that suppression?'. Channel load
        is deliberately NOT here — it is a cost/timing signal, not a
        bridge/redundancy assessment."""
        if not self.neighbors:
            return 0.0
        redundancy = min(1.0, self._alt_paths_global() / 3.0) \
            if self.segment_of else min(1.0, len(self.neighbors) / 6.0)
        return redundancy * self.confidence()

    def _cef_reach_guard(self, p=None):
        """v0.12 B3: reach_guard = max(structural_risk, packet_need,
        1 - confidence) — the three separate reasons to weaken suppression.
        structural_risk and confidence are kept SEPARATE (low confidence is
        not a bridge; it only means 'act carefully')."""
        guard = max(self.structural_risk(), 1.0 - self.confidence())
        if p is not None:
            guard = max(guard, self.packet_need(p))
        return guard

    def _cef_frontier_risk(self, p=None):
        """v0.13 C1: how likely is MY SILENCE to cut the propagation front?
        frontier_risk = max(packet_need, local_cut_risk, alt_path_risk).
        - packet_need covers unique_residual (same metric: unique share of
          this packet's residual coverage);
        - local_cut_risk == structural_risk() (boundary x segment risk);
        - alt_path_risk = 1/(1+alt_paths) — the scale axis from B6: even
          100 nodes can be functionally sparse. CRITICAL: this discount does
          NOT pass through confidence — the mixed30 death diagnosis (B1)
          showed HIGH-but-WRONG confidence (0.887) at weak alternatives;
          structural starvation must be able to lower theta regardless of
          how confident the node (wrongly) feels."""
        fr = self.structural_risk()
        if p is not None:
            fr = max(fr, self.packet_need(p))
        alts = self._alt_paths_global() if self.segment_of \
            else float(len(self.neighbors))
        fr = max(fr, 1.0 / (1.0 + alts))
        return min(1.0, fr)

    def _cef_theta_v013(self, p=None):
        """v0.13 C1 — additive, frontier-aware theta (replaces the v0.12
        multiplicative form when cef_theta_mode == 'v013'):
            theta_eff = theta_base
                     + k_redundancy * suppression_pressure   (dense: theta UP)
                     + k_dense       * density_score          (degree: theta UP)
                     - k_front       * frontier_risk         (front: theta DOWN)
                     - k_lowconf     * (1 - confidence)
        Semantics unchanged: forward iff G > theta. dense/redundant -> more
        suppression; frontier/bridge -> less — keeps the ultra_dense wins
        (-89% TX) while protecting fronts in mixed/random sparse scale."""
        base = float(self.p.get('cef_theta_base', 0.7))
        pressure = self._cef_suppression_pressure()
        density = min(1.0, len(self.neighbors)
                      / float(self.p.get('cef_dense_degree_ref', 12)))
        fr = self._cef_frontier_risk(p)
        conf = self.confidence()
        t = (base
             + float(self.p.get('cef_k_redundancy', 0.4)) * pressure
             + float(self.p.get('cef_k_dense', 0.4)) * density
             - float(self.p.get('cef_k_front', 0.5)) * fr
             - float(self.p.get('cef_k_lowconf', 0.3)) * (1.0 - conf))
        return max(float(self.p.get('cef_theta_min', 0.2)),
                   min(t, float(self.p.get('cef_theta_max', 2.0))))

    def _cef_theta(self, p=None):
        """v0.12 B4 — corrected semantics:
            theta_eff = theta_base * (1 + K_SUPPRESS*suppression_pressure
                                      - K_REACH*reach_guard)
        theta UP => harder to forward => MORE suppression (the old proposal
        had this inverted). Channel utilization is NOT in theta (it enters
        once, on the cost side of G). The v0.11 linear-budget theta x1.25
        moved to the timing/jitter channel (own load delays, decisions unchanged).
        v0.13 C1: cef_theta_mode='v013' switches to the additive
        frontier-aware form (_cef_theta_v013) — scale-aware via
        alt_path_risk instead of the raw neighbor-count axis.
        """
        if self.p.get('cef_theta_mode', 'v012') == 'v013':
            return self._cef_theta_v013(p)
        base = float(self.p.get('cef_theta_base', 0.7))
        ksupp = float(self.p.get('cef_k_suppress', 1.0))
        kreach = float(self.p.get('cef_k_reach', 0.8))
        pressure = self._cef_suppression_pressure()
        guard = self._cef_reach_guard(p)
        t = base * (1.0 + ksupp * pressure - kreach * guard)
        # v0.11 sparse: degree <= 3 keeps the front alive (structural, not
        # channel — retained as an explicit structural override)
        if len(self.neighbors) <= 3:
            t *= 0.6
        return max(float(self.p.get('cef_theta_min', 0.2)),
                   min(t, float(self.p.get('cef_theta_max', 2.0))))

    def _cef_should_forward(self, p):
        """Return bool: forward packet p iff marginal gain G > theta_eff.
        Reach-first override: LOW confidence, emergency, sparse always forward."""
        if getattr(p, 'is_emergency', False):
            return True   # emergency always forwards: maximum aggression

        if not self.p.get('enable_cef', False):
            return None   # caller falls back to the tiered logic

        if len(self.neighbors) <= self.p.get('density_sparse', 4):
            self.stats['cef_guarded_forward'] = self.stats.get('cef_guarded_forward', 0) + 1
            return True

        est = self._cef_estimate(p)
        theta_eff = self._cef_theta(p) * est['R']

        # reach guard: LOW confidence -> forward (front must not die)
        if self._confidence_level() == 0:
            self.stats['cef_guarded_routing'] = self.stats.get('cef_guarded_routing', 0) + 1
            return True

        # residual coverage still exists -> the packet can still add value;
        # forward when G is at least meaningful
        residual = self._residual_coverage(p, set(self.packet_relayers.get(p.seq, set())))
        self.stats['cef_decisions'] = self.stats.get('cef_decisions', 0) + 1
        if est['G'] > theta_eff:
            self._cef_g_sample(est['G'])
            return True
        # v0.13 C1b/c/e: frontier rescue. C2 panel proved the theta discount
        # alone does not move the needle (blind-low G at boundary nodes);
        # C2b/c/d proved the LOCAL state cannot separate dense-redundant from
        # mixed-critical (identical death context; echoes lost in dense
        # collisions). C1e gates the rescue on the ONLY deferred-time
        # feedback that discriminates: suppression REGRET — did my recent
        # frontier-candidate suppressions leave the packet dead? Dense: no
        # (others relay) -> gate closed; mixed/random: yes -> gate opens.
        # Passive observation, zero messages, zero-oracle.
        frontier_candidate = False
        if self.p.get('cef_frontier_override', False) and len(residual) > 0:
            fr = self._cef_frontier_risk(p)
            if fr >= float(self.p.get('cef_frontier_threshold', 0.4)):
                # v0.14 D3: COLLECT lease = network-corroborated regret —
                # the rescue gate opens WITHOUT local evidence (the shepherd
                # already proved the segment is dying); CEF <-> CEF+F4 mode
                # switching by lease, exactly the roadmap architecture.
                # D3-budget: each rescue costs a token (lazy-regen bucket,
                # scaled by local airtime headroom) — a lease grants
                # PERMISSION, the bucket grants CAPACITY.
                if self._collect_active():
                    if self._rescue_credit(p):
                        self.stats['cef_frontier_rescued'] = \
                            self.stats.get('cef_frontier_rescued', 0) + 1
                        self.stats['rescues_during_collect'] += 1
                        return 'rescue'
                    self.stats['rescue_budget_denied'] += 1
                n = self._rescue_regret_n
                ewma = self._rescue_regret_ewma
                if ewma is not None \
                        and n >= int(self.p.get('cef_frontier_min_checks', 3)) \
                        and ewma >= float(self.p.get('cef_frontier_min_regret', 0.4)):
                    self.stats['cef_frontier_rescued'] = \
                        self.stats.get('cef_frontier_rescued', 0) + 1
                    return 'rescue'
                frontier_candidate = True   # suppressed now -> observe regret
        # suppressed by cost — diagnostics with the real G and theta
        self._cef_g_sample(est['G'])
        self.stats['cef_suppressed_by_cost'] = self.stats.get('cef_suppressed_by_cost', 0) + 1
        if len(residual) > 0:
            self.stats['cef_suppressed_with_residual'] = \
                self.stats.get('cef_suppressed_with_residual', 0) + 1
        else:
            self.stats['cef_suppressed_clean'] = \
                self.stats.get('cef_suppressed_clean', 0) + 1
        if frontier_candidate:
            # v0.13 C1e: observe the deferred-time outcome of this suppressed
            # frontier candidate — feeds the rescue-regret EWMA (the gate).
            relays0 = len(self.packet_relayers.get(p.seq, set()))
            self.env.process(self._frontier_regret_check(p, relays0,
                                                         self._had_ack(p.seq)))
        self._log_death(p, 'cef_cost',
                        {'cef_G': round(est['G'], 4),
                         'theta_eff': round(theta_eff, 4)})
        return False

    def _cef_g_sample(self, g):
        """Bounded G samples for diagnostics (cef_G_mean metric)."""
        s = self.stats.setdefault('cef_G_samples', [])
        if len(s) < 500:
            s.append(round(g, 4))

    # ==================================================================
    # v0.12b HERD LAYER (B2/B3): cohesion / repulsion / alignment /
    # routing_order — pure LOCAL primitives (zero-oracle), continuous,
    # no rigid flock roles. Diagnostics first (B1'), then decision wiring
    # behind enable_herd (ablatable).
    # ==================================================================
    def routing_order(self, p=None):
        """0..1 — does the local relay decision have a clear winner?
        Entropy of candidate scores: order~1 = one/two relays clearly
        dominate (safe to suppress the rest); order~0 = candidates similar
        (chaotic — reach-first, weaker suppression).
        Safe semantics: 0 candidates -> 0.0 (isolated, reach-first);
        1 candidate -> 1.0 (that one clearly dominates)."""
        cands = list(self.neighbors.keys())
        if not cands:
            return 0.0
        if len(cands) == 1:
            return 1.0
        dest = p.origTxNodeId if p is not None else BROADCAST_ID
        try:
            scores = [max(1e-9, self._candidate_score(nid, dest)) for nid in cands]
        except Exception:
            return 0.5   # neutral-safe on incomplete state
        total = sum(scores)
        if total <= 0:
            return 0.0
        w = [s / total for s in scores]
        h = -sum(x * math.log(x) for x in w if x > 0)
        hmax = math.log(len(w))
        return 1.0 - (h / hmax if hmax > 0 else 0.0)

    def cohesion_pressure(self, p=None):
        """0..1 — 'how much does propagation NEED me now?' = max(packet_need,
        structural_risk, 1-confidence). Cohesion (herd must stay together):
        high -> forward more likely, weaker inhibition, more fallback."""
        return self._cef_reach_guard(p)

    def repulsion_pressure(self, p=None):
        """0..1 — 'why NOT to retransmit' = redundancy + duplicates +
        observed channel pressure + fatigue. NOTE: own TX load and observed
        busy are SEPARATE signals; neither is a 'collision rate'."""
        if not self.neighbors:
            return 0.0
        redundancy = min(1.0, self._alt_paths_global() / 3.0) \
            if self.segment_of else min(1.0, len(self.neighbors) / 6.0)
        dupes = min(1.0, (self.node.timesReceived.get(p.seq, 0) - 1) / 3.0) \
            if p is not None else 0.0
        busy = self._observed_channel_busy()
        fatigue = getattr(self, 'relay_fatigue', 0.0) \
            if self.p.get('fatigue_enabled', False) else 0.0
        return min(1.0, (redundancy + dupes + busy) / 3.0 + 0.5 * fatigue)

    def alignment_score(self, p=None):
        """0..1 — agreement with the local propagation direction (no GPS):
        hop progress + relay designation + route/LPR gradient (geo terms
        only if positions actually known)."""
        a = 0.5   # neutral baseline
        if p is not None:
            if p.hopLimit < self.conf.hopLimit:
                a += 0.1   # packet already travelled — I am along the wave
            desig = getattr(p, 'relay_designation', None)
            if desig and self.node.nodeid in desig:
                a += 0.3   # designated: strong alignment for this hop
            if p.destId != BROADCAST_ID:
                # LPR/progress alignment (DM): my route primary matches
                route = self.routes.get(p.destId, {})
                if route.get('primary') in self.neighbors or p.destId in self.neighbors:
                    a += 0.1
        return max(0.0, min(1.0, a))

    def _herd_sample_metrics(self, p):
        """B1'-style bounded running sums for herd_* diagnostics (one sample
        per broadcast decision; exported as means by the runner)."""
        s = self.stats
        pn = self.packet_need(p)
        vals = {
            'herd_cohesion_sum': self.cohesion_pressure(p),
            'herd_repulsion_sum': self.repulsion_pressure(p),
            'herd_alignment_sum': self.alignment_score(p),
            'herd_routing_order_sum': self.routing_order(p),
            'herd_packet_need_sum': pn,
            'herd_structural_risk_sum': self.structural_risk(),
        }
        for key, val in vals.items():
            s[key] = s.get(key, 0.0) + val
        s['herd_samples'] = s.get('herd_samples', 0) + 1
        if pn >= 0.6:
            s['high_packet_need_seen'] = s.get('high_packet_need_seen', 0) + 1

    # ==================================================================
    # v0.9 asymmetric-link tolerance + S&F gatekeeper + zero-jitter
    # ==================================================================
    def _asymmetric_blind(self, nid):
        """True when the link to the neighbor is an asymmetric blind spot:
        v0.10 audit: BOTH conditions required (rssi < threshold AND pdr <
        asymmetric_pdr_max) — the rssi-only variant marked too many links
        blind and blocked needed failover in dense_roles."""
        nb = self.neighbors.get(nid)
        if nb is None:
            return False
        return (nb.rssi_ema < float(self.p.get('asymmetric_rssi_min', -115))
                and nb.pdr < float(self.p.get('asymmetric_pdr_max', 0.2)))

    # ==================================================================
    # v0.10 SPARSE GUARD: the front must not die at the coverage edge
    # ==================================================================
    def _sparse_guard_active(self):
        """v0.11: sparse guard is ONLY local_degree-based (<= sparse_degree_threshold).
        The v0.10 confidence condition leaked into bridge/hub (their confidence
        can be LOW early) and weakened suppression there (+4-5% TX regression).
        Bridge/hub nodes have local_degree > 8 anyway."""
        if not self.p.get('sparse_guard', True):
            return False
        return len(self.neighbors) <= self.p.get('sparse_degree_threshold', 4)

    def _sf_gatekeeper_allow(self, p):
        """Store & Forward gatekeeper (protects Flash/RAM on nRF52/ESP32):
        only DM/EMERGENCY traffic may be stored; telemetry/position/public
        flood is dropped BEFORE any flash write. In the simulator this maps
        to: telemetry/position is never relayed (localcast) and never queued."""
        if getattr(p, 'is_emergency', False):
            return True
        if p.destId != BROADCAST_ID:
            return True   # DM: allow
        if getattr(p, 'pos_x', None) is not None:
            self.stats['sf_gatekeeper_dropped'] = \
                self.stats.get('sf_gatekeeper_dropped', 0) + 1
            return False  # position/telemetry: drop before flash write
        return True       # public broadcast: allow (bounded by relaying)

    def _battery_role(self):
        """True when this node's role is battery-constrained
        (CLIENT_MUTE / SENSOR): zero-jitter policy applies."""
        role = self.node.role.value if hasattr(self.node.role, 'value') else str(self.node.role)
        return role in ('CLIENT_MUTE', 'SENSOR')

    # ==================================================================
    # v0.12: deterministic packet-hash jitter (B5)
    # ==================================================================
    def _packet_hash_slot(self, p, k=None):
        """Deterministic slot from (nodeid, origTxNodeId, seq).
        Knuth multiplicative mixing — stable across runs (unlike hash()),
        independent of PYTHONHASHSEED. Relay ORDER changes between packets
        (nodes 1/11/21 no longer share a class) but stays reproducible."""
        k = k or int(self.p.get('cef_hash_k', 10))
        a = (self.node.nodeid * 73856093) & 0xFFFFFFFF
        b = (p.origTxNodeId * 19349663) & 0xFFFFFFFF
        c = (p.seq * 83492791) & 0xFFFFFFFF
        return ((a ^ b ^ c) >> 13) % max(1, k)

    # ==================================================================
    # v0.13 C3: tiered hash-slot jitter (CEF burst de-correlation)
    # ==================================================================
    def _seg_sig_hash(self):
        """Deterministic digest of the learned-topology signature. Python's
        hash() of frozensets/strings is PYTHONHASHSEED-randomized — use a
        numeric fold so slots stay reproducible across processes. Cached
        per _seg_sig object (recomputed only when segmentation changes)."""
        cached = getattr(self, '_seg_sig_h_cache', None)
        if cached is not None and cached[0] is self._seg_sig:
            return cached[1]
        sig = self._seg_sig
        if sig is None:
            h = 0
        else:
            h = 2166136261
            for nid in sorted(sig[0]):
                h = ((h ^ ((nid * 2654435761) & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
            for nid, c in sig[1]:
                h = ((h ^ ((nid * 1000003 + c) & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
        self._seg_sig_h_cache = (self._seg_sig, h)
        return h

    def _cef_slot_jitter_ms(self, p, tier=None):
        """v0.13 C3: tiered deterministic slot jitter — de-correlates CEF
        forwards (B6 anomaly: MORE absolute collisions than MF despite -89%
        TX; the few forwards cluster temporally at wave fronts). slot from
        (nodeid, origTxNodeId, seq, segment_signature); tier classes give
        better candidates earlier cancellation slots:
            PRIMARY 0-2, BACKUP 3-5, weak/undesignated 6-7 (of 8).
        No RNG — reproducible; the echo-cancel (still_useful) still applies
        after the deferral, so hearing a good relay earlier still cancels."""
        n_slots = int(self.p.get('cef_slot_count', 8))
        width = float(self.p.get('cef_slot_width_ms', 15.0))
        a = (self.node.nodeid * 73856093) & 0xFFFFFFFF
        b = (p.origTxNodeId * 19349663) & 0xFFFFFFFF
        c = (p.seq * 83492791) & 0xFFFFFFFF
        d = self._seg_sig_hash()
        slot = (((((a ^ b ^ c) & 0xFFFFFFFF) * 2654435761) >> 13) ^ d) \
            % max(1, n_slots)
        if tier == PRIMARY:
            slot %= 3
        elif tier is not None:
            slot = 3 + (slot % 3)
        return slot * width

    def _ripple_delay_ms(self, p, score, dmin, dspan, base):
        """v0.9 DRF ripple delay. v0.13 C4: ripple_hop_bias_ms > 0 switches
        the hop term to the firmware-representative FIXED bias (FW+ uses
        ~20-60 ms/hop, clamp 25-500 ms) instead of alpha*airtime — the old
        term gave ~370-1000 ms/hop at LONG_FAST, ~10x firmware timing,
        which masked the DRF idea behind inflated latency."""
        hop_depth = self.conf.hopLimit - p.hopLimit   # my distance from origin
        slot_term = self._packet_hash_slot(p, k=int(self.p['ripple_k'])) \
            * self.p['ripple_jitter_step_ms']
        hb = float(self.p.get('ripple_hop_bias_ms', 0) or 0)
        if hb > 0:
            return dmin + hb * hop_depth + (1.0 - score) * dspan + slot_term
        return dmin + self.p['ripple_alpha'] * hop_depth * base \
            + (1.0 - score) * dspan + slot_term

    # ==================================================================
    # v0.12 B2: STRUCTURAL RISK (local cut risk — a PRESUMPTION, not a fact;
    # local 2-hop knowledge cannot prove a global articulation point)
    # ==================================================================
    def structural_risk(self):
        """0..1: 'how dangerous might my silence be?' — from LOCAL signals
        only (existing AR state: alt_paths, segments, cross-segment overlap).
        max(alt_risk, boundary * segment_risk)."""
        if not self.neighbors:
            return 1.0   # isolated: my silence kills everything I could carry
        alt = self._alt_paths_global() if self.segment_of else float(len(self.neighbors))
        alt_risk = 1.0 - min(1.0, alt / 3.0)
        seg_count = len(set(self.segment_of.values())) if self.segment_of else 1
        if seg_count <= 1:
            boundary = 0.0
            seg_risk = 0.0
        else:
            # boundary = 1 - mean cross-segment Jaccard (weak overlap between
            # my functional groups = packets may need crossing through me)
            ids = list(self.neighbors)
            cross = []
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    if self.segment_of.get(ids[i]) != self.segment_of.get(ids[j]):
                        a, b = self.two_hop_view(ids[i]), self.two_hop_view(ids[j])
                        u = len(a | b)
                        if u:
                            cross.append(len(a & b) / u)
            boundary = 1.0 - (sum(cross) / len(cross) if cross else 0.0)
            seg_risk = min(1.0, (seg_count - 1) / 3.0)
        return max(alt_risk, boundary * seg_risk)

    # ==================================================================
    # v0.12 B3: PACKET-SPECIFIC NEED — takes PRECEDENCE over node-level
    # classification: 'is my retransmission the only one covering an
    # unreached fragment for THIS packet?'
    # ==================================================================
    def packet_need(self, p):
        """0..1: fraction of this packet's residual coverage that only I
        can provide (from my local 2-hop view)."""
        rels = set(self.packet_relayers.get(p.seq, set()))
        residual = self._residual_coverage(p, rels)
        if not residual:
            return 0.0
        uniq = self._my_unique_for_packet(p, residual, rels)
        return min(1.0, uniq / max(1, len(residual)))

    # ==================================================================
    # v0.12: local channel pressure — SEPARATED (own TX load vs observed
    # busy; the former is NOT 'collision rate'). Timing/cost input only —
    # never used for the redundancy/bridge assessment.
    # ==================================================================
    def _own_tx_load(self):
        """My own airtime share (0..1, approx): recent TX ms per window."""
        return min(1.0, self.node.channel_utilization_percent() / 100.0)

    def _observed_channel_busy(self):
        """Observed channel occupancy (0..1): RX airtime + others' TX.
        In the simulator this maps to channelUtilizationPercent (local
        measurement, as firmware AirTime does)."""
        return min(1.0, self.node.channel_utilization_percent() / 100.0)

    # ==================================================================
    # v0.6 CLIENT REPEAT (FieldMesh off-grid lesson): last-resort emergency
    # retransmission for packets that are dying with nobody taking over.
    # ==================================================================
    def _client_repeat_allowed(self):
        """Hard storm guard: max N emergency repeats per minute per node."""
        now = self.env.now
        times = getattr(self, '_client_repeat_times', [])
        times = [t for t in times if now - t < 60000]
        self._client_repeat_times = times
        return len(times) < int(self.p.get('client_repeat_max_per_min', 1))

    def _schedule_client_repeat(self, p):
        """Non-designated node: wait the T2 window; if NO echo arrives
        (nobody took over) and the packet is near its hop limit, break the
        suppression rules for ONE emergency retransmission.
        v0.9: skipped when the previous hop is an asymmetric blind spot
        (it cannot hear me — repeating toward it is futile)."""
        if not self.p.get('client_repeat_enabled', True):
            return False
        if self.p.get('asymmetric_block_fallback', True) and \
                self._asymmetric_blind(p.txNodeId):
            self.stats['asymmetric_link_suppression'] = \
                self.stats.get('asymmetric_link_suppression', 0) + 1
            return False
        if p.hopLimit > 2 and self._confidence_level() != 0:
            return False  # not a last-resort situation
        base = self._relay_timeout_params(p)
        delay = (self.p['backup2_slots'] + 1.0) * base
        self.env.process(self._client_repeat_fire(p, delay))
        return True

    def _client_repeat_fire(self, p, delay_ms):
        heard_at_start = self.node.timesReceived.get(p.seq, 0)
        yield self.env.timeout(max(1.0, delay_ms))
        if self.node.timesReceived.get(p.seq, 0) > heard_at_start:
            return  # someone took over — stay silent
        if not self._client_repeat_allowed():
            return  # storm guard
        self._client_repeat_times = getattr(self, '_client_repeat_times', [])
        self._client_repeat_times.append(self.env.now)
        self.stats['client_repeats'] = self.stats.get('client_repeats', 0) + 1
        self._relay_packet(p, next_dm=True)

    # ==================================================================
    # ECHO / WATCHDOG
    # ==================================================================
    def _register_expected(self, seq, relay_ids, timeout_ms=None, ctx=None):
        if not relay_ids:
            return
        to = timeout_ms if timeout_ms is not None else self.p['echo_timeout_ms']
        self.expected_relay[seq] = {'relays': set(relay_ids), 't0': self.env.now,
                                    'ctx': ctx or {}}
        self.env.process(self._expected_timeout(seq, to))

    def _expected_timeout(self, seq, timeout_ms):
        """v0.5 reach-first watchdog: timeout != automatic failure.

        Round 1: another relay's copy heard -> hop covered elsewhere (no
        failure). No evidence -> UNKNOWN: re-arm a SHORT second check instead
        of punishing the relay (half-duplex deafness / busy channel are not
        the relay's fault).
        Round 2 (still no evidence) -> FAILURE: weight penalty + failover.
        A late-arriving copy after a failure is counted as a false failure.
        """
        yield self.env.timeout(max(1.0, timeout_ms))
        ent = self.expected_relay.pop(seq, None)
        if ent is None:
            return
        ctx = ent['ctx']
        p0 = ctx.get('packet')
        prev_hop = p0.txNodeId if p0 is not None else None

        # covered elsewhere? (I relayed + heard at least one more copy from
        # a node that is neither the previous hop nor an expected relay)
        relayers = set(self.packet_relayers.get(seq, set()))
        others = relayers - set(ent['relays'])
        if prev_hop is not None:
            others.discard(prev_hop)
        if others:
            self.stats['ar_covered_elsewhere'] += 1
            return  # hop is being handled by someone else — no failure

        round_no = ent.get('round', 0)
        if round_no == 0:
            # UNKNOWN: give the relay one short second chance (reach-first:
            # do not punish half-duplex deafness on the first timeout)
            ent['round'] = 1
            self.expected_relay[seq] = ent
            from lib.phy import airtime as _airtime
            short = 2.0 * _airtime(self.conf, self.conf.current_preset['sf'],
                                   self.conf.current_preset['cr'],
                                   self.conf.PACKETLENGTH,
                                   self.conf.current_preset['bw'])
            self.env.process(self._expected_timeout(seq, short))
            return

        # round 2 without any evidence -> FAILURE
        for rid in ent['relays']:
            self._relay_outcome(rid, False)
            # v0.8 LPR: watchdog failure raises the route potential
            if self.p.get('enable_lpr', False) and p0 is not None:
                self.potential.update_on_failure(rid, p0.destId, 0.5, self.env.now)
        self.stats['adaptive_primary_failure'] += 1
        self.fail_streak += 1
        # false-failure check: did the packet propagate later after all?
        base = len(self.packet_relayers.get(seq, set()))
        self.env.process(self._false_failure_check(seq, base))
        self._failover(seq, ent, ctx, p0)

    def _false_failure_check(self, seq, base_count):
        """If the packet propagates after we declared failure, the failure
        was false (deafness / late arrival), not the relay's fault."""
        yield self.env.timeout(2.0 * float(self.p.get('echo_timeout_ms', 12000)) / 3.0)
        if len(self.packet_relayers.get(seq, set())) > base_count:
            self.stats['watchdog_false_failure'] += 1

    def _suppression_regret_check(self, p, base_count, base_acked):
        """If NOBODY relays and no ACK arrives after we suppressed our forward,
        the suppression may have cost delivery (suppression regret)."""
        yield self.env.timeout(3.0 * float(self.p.get('echo_timeout_ms', 12000)) / 4.0)
        heard = len(self.packet_relayers.get(p.seq, set()))
        acked = self._had_ack(p.seq)
        if heard <= base_count and not acked and not base_acked:
            self.stats['suppression_regret'] += 1

    def _frontier_regret_check(self, p, relays0, acked0):
        """v0.13 C1e: passive deferred-time outcome of a suppressed frontier
        candidate — feeds the per-node rescue-regret EWMA (the rescue gate).
        Same observation window as the tiered suppression-regret check."""
        yield self.env.timeout(3.0 * float(self.p.get('echo_timeout_ms', 12000)) / 4.0)
        self._frontier_regret_observe(p, relays0, acked0)

    def _frontier_regret_observe(self, p, relays0, acked0):
        """Pure outcome evaluator (unit-testable): regret=1 when nobody
        relayed after my suppression and no ACK arrived."""
        heard = len(self.packet_relayers.get(p.seq, set()))
        regret = 0.0 if (heard > relays0 or self._had_ack(p.seq) or acked0) else 1.0
        a = float(self.p.get('cef_frontier_regret_alpha', 0.2))
        self._rescue_regret_ewma = regret \
            if self._rescue_regret_ewma is None \
            else (1.0 - a) * self._rescue_regret_ewma + a * regret
        self._rescue_regret_n += 1
        if regret:
            self.stats['frontier_suppression_regret'] = \
                self.stats.get('frontier_suppression_regret', 0) + 1
        # v0.14 D2: sustained regret + frontier-sized degree -> shepherd candidacy
        if self._shepherd_candidacy():
            self._arm_shepherd_election()
        # v0.14 CEF_BOOST: sustained regret = frontier proof -> exit jam mode
        if self._cef_boost and regret >= 1.0:
            self._cef_boost_exit('sustained_regret')

    def _failover(self, seq, ent, ctx, p0):
        """Hop-level failover: re-relay through the next-best candidates.
        v0.9: skipped entirely when the previous hop is an asymmetric blind
        spot (mini-flood/retry toward it would be futile).
        v0.10 linear TX budget: an overloaded node (tx_per_delivered above
        budget) reduces its retry count to 1."""
        if self.p.get('asymmetric_block_fallback', True) and p0 is not None and \
                self._asymmetric_blind(p0.txNodeId):
            self.stats['asymmetric_link_suppression'] = \
                self.stats.get('asymmetric_link_suppression', 0) + 1
            self.stats['asym_suppressed_but_lost'] = \
                self.stats.get('asym_suppressed_but_lost', 0) + 1
            return
        retries = ctx.get('retries', 0)
        # v0.5 hub guard: retry budget shrinks in dense/equivalent neighborhoods
        max_retries = int(self.p.get('max_hop_retries', 2))
        if len(self.neighbors) >= self.p.get('density_dense', 12):
            max_retries = min(max_retries, int(self.p.get('hub_retry_limit', 1)))
        # v0.10: TX-budget-aware retry reduction
        if self.p.get('linear_tx_budget', True):
            tpd = self.node.nrPacketsSent / max(1, self.node.usefulPackets)
            if tpd > float(self.p.get('tx_per_delivered_threshold', 1.2)):
                max_retries = min(max_retries, 1)
        if p0 is None or p0.hopLimit <= 0:
            return
        if retries >= max_retries:
            # all retries exhausted -> bounded mini-flood as last resort
            self._mini_flood(p0)
            return
        exclude = set(ctx.get('excluded', ())) | set(ent['relays']) | {p0.txNodeId}
        prim, b1, b2 = self._pick_relays_toward(p0.destId, exclude=exclude)
        desig = [x for x in (prim, b1, b2) if x is not None]
        if not desig:
            self._mini_flood(p0)
            return
        self.stats['adaptive_segment_fallback'] += 1
        new_ctx = {'packet': p0, 'retries': retries + 1, 'excluded': exclude}
        self._relay_packet(p0, next_dm=True, designations=desig,
                           expect_ctx=new_ctx)

    # ==================================================================

    def _observe_copy(self, p):
        """Every overheard copy of seq: if it came from an expected relay,
        count success and cancel pending expectation; else if I have a pending
        backup role for this seq, inhibit it.
        A real ACK (requestId == expected seq) is the strongest confirmation:
        the packet reached the destination."""
        tx = p.txNodeId
        seq = p.seq
        if getattr(p, 'isAck', False):
            target = getattr(p, 'requestId', None)
            if target is not None and target in self.expected_relay:
                ent = self.expected_relay.pop(target)
                for rid in ent['relays']:
                    self._relay_outcome(rid, True)
                    # v0.8 LPR: watchdog success feeds the potential table
                    if self.p.get('enable_lpr', False):
                        ctx0 = ent.get('ctx') or {}
                        dest0 = ctx0.get('packet').destId if ctx0.get('packet') is not None else None
                        if dest0 is not None:
                            self.potential.update_on_success(
                                rid, dest0, self._lpr_link_cost(rid), self.env.now)
                self.stats['adaptive_primary_success'] += 1
                self.fail_streak = 0
                pend = self.pending.pop(target, None)
                if pend is not None:
                    pend['cancelled'] = True
            return
        ent = self.expected_relay.get(seq)
        if ent is not None and tx in ent['relays']:
            del self.expected_relay[seq]
            self._relay_outcome(tx, True)
            self.stats['adaptive_primary_success'] += 1
            self.fail_streak = 0
        # inhibition of my own pending backup — ADAPTIVE (v0.3): a duplicate
        # cancels my forward only when inhibition is not weak (bridge-like /
        # low-confidence situations keep the pending alive; the deferred path
        # re-checks residual coverage at fire time).
        pend = self.pending.get(seq)
        if pend is not None and tx != self.node.nodeid:
            if self.p['inhibition_enabled']:
                if pend.get('rescue'):
                    # v0.13 C1d: rescued frontier defers are HARD-inhibited —
                    # the first overheard copy is the deferred-time PROOF of
                    # redundancy. Weak-inhibition tolerance does not apply:
                    # CEF forwarders already had their chance; silence after
                    # them means nobody took it (mixed) -> fire, one echo
                    # means covered (dense) -> cancel.
                    self.stats['rescue_cancel_echo'] = \
                        self.stats.get('rescue_cancel_echo', 0) + 1
                    self.pending.pop(seq, None)
                    pend['cancelled'] = True
                    return
                strength = self.inhibition_strength(pend.get('segment'))
                if strength == 'weak':
                    self.stats['adaptive_inhibition_weak'] += 1
                    return  # keep pending; residual re-checked at fire time
                self.stats['adaptive_inhibition_medium' if strength == 'medium'
                           else 'adaptive_inhibition_strong'] += 1
                self.pending.pop(seq, None)
                pend['cancelled'] = True
                role_seg = pend.get('segment')
                same_seg = role_seg is None or self.segment_of.get(tx) == role_seg
                if same_seg:
                    self.stats['adaptive_same_sector_inhibition'] += 1
                else:
                    self.stats['adaptive_cross_sector_suppression'] += 1

    # ==================================================================
    # UNICAST (DM) handling  (ETAP 5/6/7)
    # ==================================================================
    def _handle_dm(self, p):
        me = self.node.nodeid
        designation = getattr(p, 'relay_designation', None) or []
        flood_ttl = getattr(p, 'flood_ttl', 0) or 0

        if flood_ttl > 0:
            # inside a mini-flood radius: forward unconditionally (bounded)
            return self._relay_packet(p, designations=None,
                                      next_flood_ttl=flood_ttl - 1)

        if me in designation:
            idx = designation.index(me)
            if idx == 0:
                self.stats['adaptive_primary_selected'] += 0  # selected by sender side
                return self._relay_dm_as(p, role=PRIMARY)
            elif idx == 1:
                self.stats['adaptive_backup1_triggered'] += 1
                return self._schedule_backup(p, BACKUP1)
            else:
                self.stats['adaptive_backup2_triggered'] += 1
                return self._schedule_backup(p, BACKUP2)

        # Not designated: legacy/unknown packet — act only if we plausibly
        # know a route to dest, else stay silent (conf LOW -> mini-flood).
        route = self.routes.get(p.destId)
        if route and route.get('primary') == me:
            return self._relay_dm_as(p, role=PRIMARY)
        if getattr(p, 'is_emergency', False):
            # v0.6 SOS bypass: maximum aggression — guaranteed delivery
            return self._relay_packet_emergency(p)
        if self._confidence_level() == 0:
            return self._mini_flood(p)
        # v0.6 CLIENT REPEAT (FieldMesh): last resort — T2 window, no echo,
        # packet near death -> one emergency retransmission
        return self._schedule_client_repeat(p)

    def _relay_packet_emergency(self, p):
        """SOS / emergency bypass (FieldMesh lesson): inhibition OFF,
        eligibility ignored, mini-flood TTL +2 — guarantee delivery of a
        critical packet at maximum aggression."""
        self.stats['emergency_relayed'] = self.stats.get('emergency_relayed', 0) + 1
        ttl = max(1, int(self.p['mini_flood_ttl'])) + 2
        return self._relay_packet(p, designations=None, next_flood_ttl=ttl,
                                  emergency=True)

    def _relay_timeout_params(self, p):
        slot = get_current_slot_time()
        base = max(p.timeOnAir, slot)
        return base

    def _relay_dm_as(self, p, role):
        """Forward a DM one hop further, designating the next hop chain."""
        if role == PRIMARY:
            defer = 0.0
        elif role == BACKUP1:
            defer = self.p['backup1_slots'] * self._relay_timeout_params(p)
        else:
            defer = self.p['backup2_slots'] * self._relay_timeout_params(p)
        if defer <= 0:
            return self._relay_packet(p, next_dm=True)
        self.env.process(self._deferred_relay(p, defer, next_dm=True))
        return True

    def _deferred_relay(self, p, delay_ms, next_dm=False, suppress_check=None,
                        rescue=False):
        myid = self.node.nodeid
        self.pending[p.seq] = {'deadline': self.env.now + delay_ms,
                               'segment': self.segment_of.get(p.txNodeId),
                               'cancelled': False,
                               'rescue': rescue}
        yield self.env.timeout(max(1.0, delay_ms) / self.node.clock_scale)
        pend = self.pending.pop(p.seq, None)
        if pend is None or pend.get('cancelled'):
            return  # inhibited by ECHO (lateral inhibition)
        if suppress_check is not None and not suppress_check():
            self.stats['adaptive_suppressed_forward'] += 1
            # F0.6 suppression-regret probe: did the packet die anyway?
            base_count = len(self.packet_relayers.get(p.seq, set()))
            self.env.process(self._suppression_regret_check(
                p, base_count, self._had_ack(p.seq)))
            return
        if not next_dm:
            self.stats['adaptive_mpr_forward'] += 1
        self._relay_packet(p, next_dm=next_dm)

    def _had_ack(self, seq):
        return any(qa.requestId == seq for qa in self.node.packets
                   if getattr(qa, 'isAck', False))

    # ==================================================================
    # v0.12 B1: packet-death diagnostics (DIAGNOSTICS ONLY — no decision
    # path reads this; guarantees bit-identical behavior with B0)
    # ==================================================================
    def _log_death(self, p, reason, extra=None):
        """Record the local context at the moment this node decided NOT to
        relay packet p (the wave may die here). Bounded; exported offline."""
        if len(self.death_log) >= 200:
            return
        relayers = set(self.packet_relayers.get(p.seq, set()))
        residual = self._residual_coverage(p, relayers)
        entry = {
            't': round(self.env.now),
            'seq': p.seq,
            'reason': reason,                    # in_air / residual_empty /
                                                  # echo / cef_cost / guard_off
            'local_degree': len(self.neighbors),
            'segment_count': len(set(self.segment_of.values())),
            'min_alt_paths': round(self._alt_paths_global(), 2) if self.segment_of else 0.0,
            'confidence': round(self.confidence(), 3),
            'residual_cov': len(residual),
            'my_unique_cov': self._my_unique_for_packet(p, residual, relayers),
            'heard_dupes': self.node.timesReceived.get(p.seq, 0),
            'hop_limit': p.hopLimit,
            'cef': bool(self.p.get('enable_cef', False)),
            'inhibition': self.inhibition_strength(self.segment_of.get(p.txNodeId)),
        }
        if extra:
            entry.update(extra)
        self.death_log.append(entry)

    def _relay_packet(self, p, designations=None, next_dm=False, next_flood_ttl=None,
                      expect_ctx=None, emergency=False):
        """Actually transmit a relayed copy (router-agnostic machinery)."""
        from lib.packet import MeshPacket
        node = self.node
        if p.hopLimit <= 0:
            return False
        pNew = MeshPacket(self.conf, node.nodes, p.origTxNodeId, p.destId,
                          node.nodeid, p.packetLen, p.seq, p.genTime,
                          p.wantAck, False, None, self.env.now,
                          node.connectivity_map, node.baseline_pathloss_matrix)
        pNew.hopLimit = p.hopLimit - 1
        if next_flood_ttl:
            pNew.flood_ttl = next_flood_ttl
        # v0.7 WalkFlood backtrack: blind alley -> bounded TTL 1 (recorded on
        # the route entry by _pick_relays_toward)
        if emergency:
            # SOS: no dupe-cancel, no eligibility, flood wide
            pNew.no_dupe_cancel = True
        # designated primary relays use the short contention window (single
        # designated transmitter per hop; firmware-realistic)
        pNew.fast_cw = bool(designations) or (p.destId != BROADCAST_ID and next_dm)
        # v0.9 zero-jitter policy for battery roles (CLIENT_MUTE/SENSOR):
        # when they DO forward, transmit immediately (no backoff jitter)
        if self.p.get('zero_jitter_battery', True) and self._battery_role():
            pNew.fast_cw = True
        # Narrow ablated experiment: a copy whose sender is the DESIGNATED
        # PRIMARY of the received packet never yields to duplicates (the
        # designated relay must forward; only redundant copies yield).
        i_am_primary = bool(getattr(p, 'relay_designation', None)
                            and p.relay_designation and p.relay_designation[0] == node.nodeid)
        pNew.no_dupe_cancel = i_am_primary and bool(
            self.p.get('primary_bypass_dupe_cancel', True))
        # v0.3 adaptive unique-coverage protection: a node that is the only
        # known gateway for part of this packet's residual coverage keeps its
        # TX despite duplicates (bridge must not be silenced by an echo).
        # ABLATION-GATED: bypassing MAC dupe-cancel breaks wave termination
        # (storms in bridge/hub) — default OFF; the post-defer residual check
        # in still_useful provides the unique-coverage protection instead.
        if (not pNew.no_dupe_cancel
                and self.p.get('unique_dupe_bypass', False)
                and self.p.get('inhibition_strength_mode', 'adaptive') == 'adaptive'):
            rels = set(self.packet_relayers.get(p.seq, set()))
            residual = self._residual_coverage(p, rels)
            if residual and self._my_unique_for_packet(p, residual, rels) > 0:
                if self.inhibition_strength(self.segment_of.get(p.txNodeId)) == 'weak':
                    pNew.no_dupe_cancel = True
                    self.stats['suppression_blocked_unique'] += 1
        # v0.3 homeostasis: note forward load on this node
        self._note_forward_load()
        # v0.5 reach-first guard: designated PRIMARY of a DM in WEAK-inhibition
        # (low path diversity) keeps its TX on plain duplicates — cancels only
        # on progressed evidence (ar_guard hook in transmit); hard-limited to
        # one guarded TX per packet per node by construction
        if (self.p.get('linear_guard', True) and pNew.fast_cw
                and getattr(p, 'relay_designation', None)
                and p.relay_designation and p.relay_designation[0] == node.nodeid
                and self.inhibition_strength(self.segment_of.get(p.txNodeId)) == 'weak'):
            pNew.ar_guard = True
        desig = None
        if p.destId != BROADCAST_ID and next_dm:
            if designations:
                desig = [d for d in designations if d != node.nodeid]
            else:
                # never designate the node we received this from (no relay-back)
                prim, b1, b2 = self._pick_relays_toward(p.destId, exclude={p.txNodeId})
                desig = [x for x in (prim, b1, b2) if x is not None and x != node.nodeid]
                # v0.7 WalkFlood backtrack: blind alley -> bounded TTL 1
                route = self.routes.get(p.destId)
                if route and route.get('backtrack'):
                    pNew.flood_ttl = max(pNew.flood_ttl or 0, 1)
            if desig:
                pNew.relay_designation = desig
            elif self._confidence_level() == 0:
                # no idea where to send: bounded fallback
                pNew.flood_ttl = max(pNew.flood_ttl or 0, 1)
                self.stats['adaptive_mini_flood_triggered'] += 1
        node.packets.append(pNew)
        node.env.process(node.transmit(pNew))
        # v0.9 DRF: stamp the relayer's potential + TX time (wave ordering +
        # ToA mapping; real firmware stamps at DIO1 interrupt)
        pNew.timestamp_tx = self.env.now
        if p.destId != BROADCAST_ID:
            if self.p.get('enable_lpr', False):
                pNew.source_potential = self.potential.get_phi(p.destId)
            else:
                pNew.source_potential = float(self.conf.hopLimit - p.hopLimit)
        if desig:
            ctx = expect_ctx if expect_ctx is not None else {'packet': p, 'retries': 0,
                                                             'excluded': set()}
            factor = float(self.p.get('echo_timeout_factor', 0))
            timeout_ms = factor * pNew.timeOnAir if factor > 0 else None
            # watchdog covers ALL designated relays: echo from ANY of them
            # confirms the hop (half-duplex deafness of one relay is normal)
            self._register_expected(pNew.seq, desig, timeout_ms=timeout_ms, ctx=ctx)
        return True

    def _schedule_backup(self, p, role):
        base = self._relay_timeout_params(p)
        delay = (self.p['backup1_slots'] if role == BACKUP1 else self.p['backup2_slots']) * base
        self.env.process(self._backup_fire(p, delay, role))
        return True

    def ar_guard_should_cancel(self, packet):
        """v0.5 reach-first guard: TRUE only when there is progressed evidence
        that the packet already travelled further than my hop (a copy with a
        lower hopLimit was heard). Plain duplicates from same-hop relays do
        NOT cancel the designated PRIMARY's transmission."""
        my_hop = packet.hopLimit
        min_heard = self._min_hop_heard.get(packet.seq)
        return min_heard is not None and min_heard < my_hop

    def _backup_fire(self, p, delay_ms, role):
        myid = self.node.nodeid
        heard_at_start = self.node.timesReceived.get(p.seq, 0)
        yield self.env.timeout(max(1.0, delay_ms))
        if self.p['inhibition_enabled']:
            if self.node.timesReceived.get(p.seq, 0) > heard_at_start:
                # someone relayed in the meantime -> cancel (silently)
                self.stats['ar_cancel_echo'] += 1
                return
        if role == BACKUP1:
            self.stats['adaptive_backup1_success'] += 1
        elif role == BACKUP2:
            self.stats['adaptive_backup2_success'] += 1
        self._relay_packet(p, next_dm=True)
        # F0.6: was the backup useful (chain continued) or redundant?
        base_count = len(self.packet_relayers.get(p.seq, set()))
        self.env.process(self._backup_outcome_check(p, base_count, role))

    def _backup_outcome_check(self, p, base_count, role):
        """Backup fired: if the chain continued (echo/ACK afterwards) it was
        useful; if no evidence of further propagation, it was redundant."""
        yield self.env.timeout(3.0 * float(self.p.get('echo_timeout_ms', 12000)) / 4.0)
        heard = len(self.packet_relayers.get(p.seq, set()))
        acked = self._had_ack(p.seq)
        useful = heard > base_count or acked
        key = 'backup1_useful' if role == BACKUP1 else 'backup2_useful'
        red = 'backup1_redundant' if role == BACKUP1 else 'backup2_redundant'
        self.stats[key] += (1 if useful else 0)
        self.stats[red] += (0 if useful else 1)

    def _mini_flood(self, p):
        """Bounded-radius flood. LOW confidence (cold start / lost knowledge)
        or WEAK inhibition escalates the radius: more redundancy until the
        network is learned. Max one mini-flood fallback per packet."""
        self.stats['adaptive_mini_flood_triggered'] += 1
        self.stats['adaptive_segment_fallback'] += 1
        if p.seq in self._mini_flood_done:
            return False  # one fallback per packet (storm guard)
        self._mini_flood_done.add(p.seq)
        ttl = max(1, int(self.p['mini_flood_ttl']))
        strength = self.inhibition_strength(self.segment_of.get(p.txNodeId))
        if self._confidence_level() == 0:
            ttl += int(self.p.get('low_conf_ttl_bonus', 2))
        elif strength == 'weak':
            ttl += 1   # v0.5: bolder fallback in low path diversity
        if self._sparse_guard_active():
            ttl = max(3, ttl)   # v0.11: sparse keeps the front alive (TTL >= 3)
        r = self._relay_packet(p, designations=None, next_flood_ttl=ttl)
        # F0.6: did the fallback deliver (echo/ACK afterwards)?
        base_count = len(self.packet_relayers.get(p.seq, set()))
        self.env.process(self._mini_flood_outcome_check(p, base_count))
        return r

    def _mini_flood_outcome_check(self, p, base_count):
        yield self.env.timeout(3.0 * float(self.p.get('echo_timeout_ms', 12000)) / 4.0)
        heard = len(self.packet_relayers.get(p.seq, set()))
        acked = self._had_ack(p.seq)
        if heard > base_count or acked:
            self.stats['mini_flood_delivered'] += 1

    # ==================================================================
    # BROADCAST handling (ETAP 8): self-election tiers + coverage + inhibition
    # ==================================================================
    def _broadcast_score(self, p, residual, relayers):
        """Activation score in [0, 1] for relaying broadcast packet p.
        v0.3: fatigue penalty applies to the quality/coverage components but
        can NEVER beat the unique-coverage bonus (a bridge must forward)."""
        nbs = self.neighbors
        nb_in = nbs.get(p.txNodeId)
        pdr = nb_in.pdr if nb_in else self.p['pdr_init']
        w = nb_in.weight if nb_in else self.p['weight_init']
        sens = self.conf.current_preset['sensitivity']
        rssi_n = 0.5
        if nb_in:
            rssi_n = max(0.0, min(1.0, (nb_in.rssi_ema - sens) / max(1.0, (self.conf.PTX - sens))))
        my_cover = self._my_two_hop() | set(nbs.keys())
        coverage_term = min(1.0, len(residual) / max(1.0, len(my_cover) / 2))
        uniq = sum(1 for x in residual
                   if all(x not in self.two_hop_view(r) for r in relayers))
        base = 0.30 * pdr + 0.20 * w + 0.10 * rssi_n + 0.30 * coverage_term
        if self.p.get('fatigue_enabled', False):
            self._fatigue_decay(self.env.now)
            base = max(0.0, base - float(self.p['fatigue_penalty']) * self.relay_fatigue)
        score = base + (self.p['w_unique_coverage'] * 0.3 if uniq > 0 else 0.0)
        if self.p['weight_enabled'] and nb_in is not None:
            seg = self.segment_of.get(p.txNodeId)
            if seg is not None and 0 <= seg < len(nb_in.weight_seg):
                score += 0.10 * nb_in.weight_seg[seg]
        return max(0.0, min(1.0, score))

    def _handle_broadcast(self, p):
        nbs = self.neighbors

        # v0.14 D2: SHEPHERD-COLLECT lease message — the frontier nodes
        # hearing it open their rescue gate for the lease duration; the
        # message itself is never relayed (hopLimit 1, frontier-local).
        # D4-quorum (segment scoping): the lease opens the gate ONLY for
        # receivers whose segment TOWARD THE SHEPHERD is thin FOR THEM —
        # a dense-core node hearing an edge shepherd sees a well-fed segment
        # and ignores the lease (D2 data: msgs_tx=39/run leaked into dense
        # via degree<=16 edge nodes; collect_hears=875 network-wide).
        if getattr(p, 'is_shepherd', False):
            seg = self.segment_of.get(p.txNodeId)
            scoped_ok = seg is None or \
                self.alt_paths(seg) <= float(self.p.get('shepherd_scope_alt_max', 1.5))
            if scoped_ok:
                until = self.env.now + float(getattr(p, 'shepherd_lease_ms', 45000.0))
                if until > self._collect_until:
                    self._collect_until = until
                self.stats['collect_hears'] += 1
                # D4-lite: cancel-on-better while my election is pending
                if self._shepherd_timer_armed \
                        and getattr(p, 'shepherd_score', 0.0) >= self._shepherd_score():
                    self._shepherd_timer_armed = False
                    self.stats['shepherd_election_cancelled'] += 1
            else:
                self.stats['collect_ignored_scope'] += 1
            return False

        # v0.6 SCOPE-BASED LOCALCAST (FieldMesh lesson): position/telemetry
        # packets are LOCAL scope — every neighbor already heard them
        # directly; relaying them multi-hop wastes airtime and burns the
        # watchdog on packets nobody beyond 1 hop cares about. Learn, never
        # relay (gated: non-AR routers relay as before).
        if self.p.get('position_scope', 'local') == 'local' and \
                getattr(p, 'pos_x', None) is not None:
            return False

        # v0.9 S&F gatekeeper: telemetry/position is never queued or relayed
        # (protects Flash/RAM; DM/EMERGENCY passes)
        if self.p.get('sf_gatekeeper', True) and not self._sf_gatekeeper_allow(p):
            return False

        # v0.14: mobility self-demotion — a frequently moving node acts as
        # CLIENT_MUTE for broadcast relaying (emergency still passes; DM
        # relaying goes through the route paths, untouched here)
            if self._mobility_demoted() and not getattr(p, 'is_emergency', False) \
                    and p.destId == BROADCAST_ID:
                self.stats['mobility_relay_suppressed'] += 1
                return False

        # v0.14 N1: receiver-evidence census defer — replaces the downstream
        # broadcast decision paths when enabled (broadcast only; emergency
        # and DMs untouched). The node waits W x ToA and counts ACTUAL
        # retransmissions instead of predicting redundancy.
        if self.p.get('n1_enabled', False) and p.destId == BROADCAST_ID \
                and not getattr(p, 'is_emergency', False):
            return self._n1_handle(p)

        # v0.14 N3: ranked two-phase whisper — preferred class first,
        # census-judged main body, silent-backup fallback (ToA windows).
        # CEF_BOOST: in JAM mode, CEF takes PRECEDENCE over N3 — the whole
        # point of the traffic-jam mode is that CEF's aggressive suppression
        # is the RIGHT strategy when the network is demonstrably busy+redundant.
        in_boost = (self.p.get('cef_boost_enabled', False)
                    and self._cef_boost
                    and self.p.get('enable_cef', False))
        if self.p.get('n3_enabled', False) and p.destId == BROADCAST_ID \
                and not getattr(p, 'is_emergency', False) and not in_boost:
            return self._n3_handle(p)

        # sparse safety: few alternates -> always forward immediately
        if len(nbs) <= self.p['density_sparse']:
            return self._relay_packet(p)

        # v0.10 CEF conditional gating: only at high local degree AND
        # >= MEDIUM confidence AND not in the sparse guard (CEF is a dense/
        # bridge/hub tool; in sparse/mixed it kills the front)
        # v0.14 CEF_BOOST ('traffic-jam mode'): CEF as an AGGRESSIVE
        # CONGESTION MODE, not a fixed router — enters only on SUSTAINED
        # evidence (busy + redundant + low frontier risk, stable N windows)
        # and EXITS on the first frontier/rescue signal. Architecture:
        #   normal: N3 + SHEP2D; jam: pure CEF; frontier: back to N3+SHEP2D
        cef = (self.p.get('enable_cef', False)
               and (not self.p.get('cef_boost_enabled', False) or self._cef_boost)
               and len(nbs) >= self.p.get('cef_min_degree', 8)
               and self._confidence_level() >= 1
                and not self._sparse_guard_active())
        rescued = False
        if cef:
            decision = self._cef_should_forward(p)
            if decision is True:
                # v0.12 B5: packet-hash jitter — deterministic slot from
                # (nodeid, origTxNodeId, seq): relay ORDER changes between
                # packets (nodes 1/11/21 no longer share a class) but stays
                # reproducible per test. Plus a local-load timing component
                # (own TX load delays MY forward -> more cancel chances;
                # the linear TX budget moved HERE from theta, per the
                # cost/timing-vs-decision separation).
                slot = self._packet_hash_slot(p)
                if self.p.get('cef_slot_jitter', False):
                    # v0.13 C3: tiered deterministic slots — burst
                    # de-correlation; designated relays cancel earlier
                    desig = getattr(p, 'relay_designation', None) or []
                    tier = None
                    if desig and self.node.nodeid in desig:
                        tier = PRIMARY if desig[0] == self.node.nodeid else BACKUP1
                    jitter = self._cef_slot_jitter_ms(p, tier)
                else:
                    jitter = (slot * float(self.p.get('cef_jitter_step_ms', 5.0))
                              + self.rng.uniform(0, float(self.p.get('cef_jitter_max_ms', 50.0))))
                if self.p.get('linear_tx_budget', True):
                    tpd = self.node.nrPacketsSent / max(1, self.node.usefulPackets)
                    if tpd > float(self.p.get('tx_per_delivered_threshold', 1.2)):
                        jitter += 50.0 * float(self.p.get('cef_jitter_step_ms', 5.0)) / 5.0
                return self._defer_or_cancel_broadcast(p, jitter, lambda: True)
            if decision != 'rescue':
                return False
            rescued = True
            # v0.13 C1b/c 'rescue': frontier node escapes the cef_cost death.
            # Falls through to the tiered defer below — echo-cancel during
            # deferral IS the redundancy test. C2b: plain fall-through
            # recovered mixed/random but destroyed the dense win (rescuers
            # recreate tiered redundancy). C1c: rescued defers are LATE
            # (x cef_frontier_delay_mult) so CEF-designated forwards cover
            # first and deferred-time evidence (echo/in-air/residual-empty)
            # cancels redundant rescuers in dense while silent mixed fronts
            # still fire.

        # update segments if stale
        now = self.env.now
        if self.p.get('segmentation', 'adaptive') != 'off' and \
                now - self.last_seg_recompute > self.p.get('seg_recompute_s', 30) * 1000:
            self._recompute_segments(now)

        conf_level = self._confidence_level()

        relayers = set(self.packet_relayers.get(p.seq, set()))
        score = self._broadcast_score(p, self._residual_coverage(p, relayers), relayers)

        # v0.12b B1' herd diagnostics (pure observation — runs BEFORE any
        # decision change; enable_herd=False keeps decisions bit-identical)
        self._herd_sample_metrics(p)
        herd = self.p.get('enable_herd', False)
        herd_cohesion = self.cohesion_pressure(p) if herd else 0.0
        herd_repulsion = self.repulsion_pressure(p) if herd else 0.0

        # Contention-based self-election: defer inversely to score, then cancel
        # on overheard copies (lateral inhibition) and re-check residual needs.
        base = self._relay_timeout_params(p)
        dmin, dspan = 1.0 * base, 3.0 * base
        if conf_level == 0:                 # LOW confidence: everyone earlier, more redundancy
            dmin *= 0.5
            dspan *= 0.5
        delay = dmin + (1.0 - score) * dspan
        # v0.9 DRF ripple formula: deterministic wave ordering instead of
        # random backoff — hop-layer spacing + score within layer + ID
        # tie-breaker (no RNG; the wave expands ring by ring).
        # v0.13 C4: ripple_hop_bias_ms > 0 switches the hop term to the
        # firmware-representative fixed-ms bias (FW+ 20-60 ms/hop) instead
        # of alpha*airtime (~10x too long at LONG_FAST) — see _ripple_delay_ms.
        if self.p.get('ripple_enabled', True):
            delay = self._ripple_delay_ms(p, score, dmin, dspan, base)
        # v0.12b B2: repulsion (redundancy+dupes+busy) delays MY forward —
        # more cancel chances; cohesion does NOT delay (reach-first).
        # AFTER the ripple formula so the deterministic wave stays intact
        # and the extra delay is not clobbered by the ripple reassignment.
        if herd and herd_repulsion > 0.5:
            delay += self.p.get('herd_repulsion_jitter_weight', 30.0) * herd_repulsion
        # v0.13 C1c: rescued frontier nodes defer LATE — the CEF-designated
        # forwards (jitter 0-105 ms) cover first; their echoes then cancel
        # redundant rescuers in dense (residual_empty / echo / in-air),
        # while in mixed/random silence lets the rescue fire.
        if rescued:
            delay *= float(self.p.get('cef_frontier_delay_mult', 3.0))

        heard0 = self.node.timesReceived.get(p.seq, 0)
        seg = self.segment_of.get(p.txNodeId)
        strength = self.inhibition_strength(seg)

        def still_useful():
            if rescued:
                # v0.13 C1d: HARD rules for rescued frontier defers — one echo
                # or empty residual cancels (deferred-time proof). Weak-tier
                # echo tolerance does not apply: CEF forwarders already had
                # their chance; the deferred wait IS the redundancy test.
                if self._in_air.get(p.seq, 0) > 0:
                    self.stats['ar_cancel_in_air'] += 1
                    self._log_death(p, 'rescue_in_air')
                    return False
                rels = set(self.packet_relayers.get(p.seq, set()))
                residual = self._residual_coverage(p, rels)
                if len(residual) == 0:
                    self.stats['ar_cancel_residual_empty'] += 1
                    self._log_death(p, 'rescue_residual_empty')
                    return False
                heard = self.node.timesReceived.get(p.seq, 0) - heard0
                if heard >= 1:
                    self.stats['rescue_cancel_echo'] = \
                        self.stats.get('rescue_cancel_echo', 0) + 1
                    self._log_death(p, 'rescue_echo', {'heard': heard})
                    return False
                return True
            # v0.3 final: mid-air duplicate always cancels (wave termination,
            # radio-real); residual-empty cancels; echo tolerance by strength.
            # NOTE: the my_uniq-based unique-block variant was ablated — it
            # hurt sparse reach (every node has unique coverage there -> the
            # block becomes "everyone relays" -> collisions). Bridge protection
            # is provided by the score boost + weak-inhibition pending instead.
            rels = set(self.packet_relayers.get(p.seq, set()))
            residual = self._residual_coverage(p, rels)
            my_uniq = self._my_unique_for_packet(p, residual, rels)
            if self._in_air.get(p.seq, 0) > 0:
                self.stats['ar_cancel_in_air'] += 1
                if my_uniq > 0:
                    self.stats['unique_relay_suppressed'] += 1
                self._log_death(p, 'in_air')
                return False
            if len(residual) == 0:
                self.stats['ar_cancel_residual_empty'] += 1
                self._log_death(p, 'residual_empty')
                return False
            if not self.p['inhibition_enabled']:
                return True
            heard = self.node.timesReceived.get(p.seq, 0) - heard0
            # v0.10 sparse guard: require TWO independent copies before
            # cancelling (the front is subcritical at the coverage edge)
            if self.p.get('require_two_copies_cancel', True) and \
                    self._sparse_guard_active() and heard < 2:
                return True
            if strength == 'weak':
                # v0.9 DRF ripple refinement: an echo from MY ripple layer or
                # closer to the source covers my area even at weak inhibition
                # — but ONLY when my residual coverage is small (cold 2-hop
                # knowledge must not silence relays covering new areas)
                if self.p.get('ripple_enabled', True):
                    rmh = self._rep_min_hop.get(p.seq)
                    if rmh is not None and rmh >= p.hopLimit and \
                            len(residual) <= self.p['density_sparse']:
                        self.stats['ripple_suppression'] = self.stats.get('ripple_suppression', 0) + 1
                        self._log_death(p, 'ripple_layer')
                        return False
                # bridge-like / LOW confidence: tolerate 1 echo (more redundancy)
                if heard < 2:
                    return True
                self.stats['ar_cancel_echo'] += 1
                self._log_death(p, 'echo_weak')
                return False
            # v0.12b B2: high cohesion (packet_need/structural risk/low conf)
            # tolerates ONE extra echo — the herd must not scatter a front
            # that only this node's coverage can keep together
            if herd and herd_cohesion >= self.p.get('herd_cohesion_weak_threshold', 0.6):
                if heard < 2:
                    return True
                self.stats['herd_cohesion_tolerated'] = \
                    self.stats.get('herd_cohesion_tolerated', 0) + 1
            # medium/strong: first echo wins (deterministic silent backup)
            if heard < 1:
                return True
            self.stats['ar_cancel_echo'] += 1
            if my_uniq > 0:
                self.stats['unique_relay_suppressed'] += 1
            self._log_death(p, 'echo_med_strong',
                            {'herd_cohesion': round(herd_cohesion, 3) if herd else None})
            return False

        self.stats['adaptive_backup1_triggered'] += 1
        return self._defer_or_cancel_broadcast(p, delay, still_useful,
                                              rescue=rescued)

    def _residual_coverage(self, p, relayers):
        covered = set(relayers)
        for r in relayers:
            covered |= self.neighbors_of_nb.get(r, set())
        return (self._my_two_hop() | set(self.neighbors.keys())) - covered - {p.origTxNodeId}

    def _defer_or_cancel_broadcast(self, p, delay, still_useful, rescue=False):
        self.env.process(self._deferred_relay(p, delay, next_dm=False,
                                              suppress_check=still_useful,
                                              rescue=rescue))
        return True

    # ==================================================================
    # passthrough (ETAP 2): bit-identical to MANAGED_FLOOD
    # ==================================================================
    def _flood_passthrough(self, p):
        node = self.node
        if node.is_client_mute:
            return False
        from lib.packet import MeshPacket
        pNew = MeshPacket(self.conf, node.nodes, p.origTxNodeId, p.destId,
                          node.nodeid, p.packetLen, p.seq, p.genTime,
                          p.wantAck, False, None, self.env.now,
                          node.connectivity_map, node.baseline_pathloss_matrix)
        pNew.hopLimit = p.hopLimit - 1
        node.packets.append(pNew)
        node.env.process(node.transmit(pNew))
        return True
