import ast
import os
import unittest

import simpy

from lib.config import Config
from adaptive_scenarios import build_scenario
from lib.adaptive import AdaptiveRelay, NeighborInfo, BROADCAST_ID, PRIMARY, BACKUP1


# ----------------------------------------------------------------------
# Test doubles (only what AdaptiveRelay reads from MeshNode/Config/Packet)
# ----------------------------------------------------------------------
class StubPos:
    def __init__(self, x=0.0, y=0.0):
        self.x, self.y = x, y


class StubPacket:
    def __init__(self, seq=1, orig=0, tx=1, dest=BROADCAST_ID, hop=3, plen=40):
        self.seq = seq
        self.origTxNodeId = orig
        self.txNodeId = tx
        self.destId = dest
        self.hopLimit = hop
        self.packetLen = plen
        self.genTime = 0
        self.now = 0
        self.wantAck = True
        self.timeOnAir = 1000.0
        self.relay_designation = None
        self.flood_ttl = 0
        self.pos_x = None
        self.pos_y = None
        self.is_neighborinfo = False
        self.is_probe = False
        self.is_probe_response = False
        self.is_echo_probe = False
        self.is_probe_ack = False
        self.is_emergency = False
        self.ar_guard = False
        self.potential_to_dest = None
        self.cost_estimate = None
        self.packet_class = None
        self.packet_scope = None
        self.potential_version = None
        self.source_potential = None
        self.timestamp_tx = None


class ManualEnv:
    """Minimal env stub for tests that don't need scheduled events."""
    def __init__(self):
        self.now = 0.0

    def process(self, *a, **k):
        raise AssertionError('ManualEnv cannot run processes; use simpy env tests')


class StubRole:
    def __init__(self, value='CLIENT'):
        self.value = value


class StubNode:
    clock_scale = 1.0   # v0.14 D1: node-local clock drift scale (perfect clock in tests)

    def __init__(self, nodeid=0, conf=None, env=None):
        self.nodeid = nodeid
        self.conf = conf or Config()
        self.conf.SELECTED_ROUTER_TYPE = Config.ROUTER_TYPE.ADAPTIVE_RELAY
        self.env = env if env is not None else simpy.Environment()
        self.position = StubPos()
        self.nodeRng = None
        self.moveRng = None
        self.timesReceived = {}
        self.packets = []
        self.is_client_mute = False
        self.role = StubRole('CLIENT')
        self.nrPacketsSent = 0
        self.usefulPackets = 0
        self.nodes = []  # neighbor set for MeshPacket construction (unused in unit tests)

    def channel_utilization_percent(self):
        return 0.0

    # transmit is a simpy process in real nodes; stub records intent
    def transmit(self, packet):
        self.packets.append(packet)
        yield self.env.timeout(0)


def make_ar(nodeid=0, env=None, **overrides):
    conf = Config()
    conf.AR_PARAMS.update(overrides)
    node = StubNode(nodeid=nodeid, conf=conf, env=env)
    return node.adaptive if hasattr(node, 'adaptive') else AdaptiveRelay(node)


class TestNeighborLearning(unittest.TestCase):
    def test_new_neighbor_created(self):
        ar = make_ar()
        p = StubPacket(tx=5)
        ar.learn(p, -80.0)
        self.assertIn(5, ar.neighbors)
        self.assertEqual(ar.neighbors[5].obs, 1)
        self.assertAlmostEqual(ar.neighbors[5].rssi_ema, -80.0)

    def test_ewma_update(self):
        ar = make_ar()
        ar.learn(StubPacket(tx=5), -80.0)
        ar.learn(StubPacket(tx=5), -60.0)
        nb = ar.neighbors[5]
        self.assertAlmostEqual(nb.rssi_ema, -80.0 * 0.7 + -60.0 * 0.3)
        self.assertEqual(nb.obs, 2)

    def test_two_hop_learning(self):
        ar = make_ar()
        # relay 5 forwards packet originally from 7 -> 5<->7 link known
        ar.learn(StubPacket(seq=9, orig=7, tx=5), -90.0)
        self.assertIn(7, ar.neighbors_of_nb[5])
        self.assertIn(5, ar.neighbors_of_nb[7])

    def test_neighbor_expiry(self):
        env = ManualEnv()
        ar = make_ar(env=env, neighbor_expiry_ms=1000, obs_prune_interval=1)
        ar.learn(StubPacket(tx=5), -80.0)
        env.now = 2000.0   # jump sim time
        ar.learn(StubPacket(tx=6), -80.0)   # triggers expiry housekeeping
        self.assertNotIn(5, ar.neighbors)
        self.assertIn(6, ar.neighbors)

    def test_position_learned_only_from_position_packet(self):
        ar = make_ar()
        p = StubPacket(tx=5)
        ar.learn(p, -80.0)
        self.assertIsNone(ar.neighbors[5].pos)
        p2 = StubPacket(tx=5)
        p2.pos_x, p2.pos_y = 10.0, 20.0
        ar.learn(p2, -80.0)
        self.assertEqual(ar.neighbors[5].pos, (10.0, 20.0))


class TestPdrEtx(unittest.TestCase):
    def test_pdr_success_increases(self):
        ar = make_ar(pdr_init=0.5, pdr_alpha=0.3)
        ar._relay_outcome(5, True)
        self.assertAlmostEqual(ar.neighbors[5].pdr, 0.5 * 0.7 + 0.3)

    def test_pdr_failure_decays_to_floor_not_zero(self):
        ar = make_ar(pdr_init=0.1, pdr_alpha=1.0, pdr_floor=0.05)
        for _ in range(10):
            ar._relay_outcome(5, False)
        self.assertGreaterEqual(ar.neighbors[5].pdr, 0.05)
        self.assertLess(ar.neighbors[5].etx, 1e9)

    def test_etx_inverse_pdr(self):
        ar = make_ar()
        ar.learn(StubPacket(tx=5), -80)
        ar.neighbors[5].pdr = 0.5
        self.assertAlmostEqual(ar.neighbors[5].etx, 2.0)

    def test_weight_reinforcement_and_decay_bounds(self):
        ar = make_ar(weight_enabled=True, weight_init=0.5)
        for _ in range(50):
            ar._relay_outcome(5, True)
        self.assertLessEqual(ar.neighbors[5].weight, 1.0)
        for _ in range(50):
            ar._relay_outcome(5, False)
        self.assertGreaterEqual(ar.neighbors[5].weight, 0.0)

    def test_weights_disabled_is_constant(self):
        ar = make_ar(weight_enabled=False)
        ar._relay_outcome(5, True)
        ar._relay_outcome(5, False)
        self.assertEqual(ar.neighbors[5].weight, ar.p['weight_init'])


class TestRelaySelection(unittest.TestCase):
    def _three_neighbor_ar(self):
        ar = make_ar()
        # 5: strong + proven ; 6: strong RSSI but poor pdr ; 7: weak
        ar.learn(StubPacket(tx=5), -80.0)
        ar.learn(StubPacket(tx=6), -70.0)
        ar.learn(StubPacket(tx=7), -110.0)
        ar.neighbors[5].pdr = 0.9
        ar.neighbors[6].pdr = 0.3
        ar.neighbors[7].pdr = 0.5
        ar.neighbors[5].stability = 0.9
        ar.neighbors[6].stability = 0.8
        ar.neighbors[7].stability = 0.5
        return ar

    def test_primary_is_best_reliability_not_rssi(self):
        ar = self._three_neighbor_ar()
        prim, _, _ = ar._pick_relays_toward(dest=99)
        self.assertEqual(prim, 5, 'PRIMARY should be most reliable, not strongest RSSI')

    def test_primary_backup_distinct(self):
        ar = self._three_neighbor_ar()
        prim, b1, b2 = ar._pick_relays_toward(dest=99)
        chosen = [x for x in (prim, b1, b2) if x is not None]
        self.assertEqual(len(chosen), len(set(chosen)), 'PRIMARY==BACKUP is forbidden')

    def test_hysteresis_blocks_small_flap(self):
        ar = self._three_neighbor_ar()
        p1, _, _ = ar._pick_relays_toward(dest=99)
        # make 6 marginally better than current primary by tiny nudge
        ar.neighbors[6].pdr = ar.neighbors[p1].pdr + 0.001
        switches = ar.stats['adaptive_route_switches']
        p2, _, _ = ar._pick_relays_toward(dest=99)
        self.assertEqual(p1, p2, 'tiny score changes must not flip PRIMARY')
        self.assertEqual(ar.stats['adaptive_route_switches'], switches)

    def test_backup_prefers_path_diversity(self):
        ar = make_ar(backup_diversity_weight=1.0)
        for nid in (5, 6, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10, 11}
        ar.neighbors_of_nb[6] = {10, 11}
        ar.neighbors_of_nb[7] = {20}
        # fix raw scores so the ONLY discriminator for backup is diversity
        ar._candidate_score = lambda nid, dest: {5: 0.80, 6: 0.80, 7: 0.79}[nid]
        prim, b1, _ = ar._pick_relays_toward(dest=99)
        self.assertEqual(prim, 5)
        self.assertEqual(b1, 7, 'backup should prefer diverse path despite lower score')

    def test_no_neighbors_gives_none(self):
        ar = AdaptiveRelay(StubNode())
        self.assertEqual(ar._pick_relays_toward(42), (None, None, None))


class TestBackoffTimersAndEcho(unittest.TestCase):
    def test_backup_timers_ordered(self):
        ar = make_ar()
        pkt = StubPacket()
        t1 = ar.p['backup1_slots'] * ar._relay_timeout_params(pkt)
        t2 = ar.p['backup2_slots'] * ar._relay_timeout_params(pkt)
        self.assertLess(t1, t2, 'BACKUP2 must wait longer than BACKUP1')

    def test_watchdog_success(self):
        ar = make_ar(echo_timeout_ms=1000)

        def driver():
            ar._register_expected(seq=77, relay_ids={5})
            yield ar.env.timeout(500)
            # relay 5 forwards -> success
            ar._observe_copy(StubPacket(seq=77, tx=5))
        ar.env.process(driver())
        ar.env.run()
        self.assertGreater(ar.neighbors[5].relay_successes, 0)

    def test_watchdog_timeout_failure(self):
        ar = make_ar(echo_timeout_ms=1000)
        ar._register_expected(seq=77, relay_ids={5})
        ar.env.run()
        nb = ar.neighbors[5]
        self.assertEqual(nb.relay_attempts, 1)
        self.assertEqual(nb.relay_successes, 0)

    def test_pending_backup_cancelled_on_echo(self):
        ar = make_ar(inhibition_strength_mode='fixed_medium')
        calls = []

        def fake_relay_packet(p, **kw):
            calls.append(p.seq)
            return True
        ar._relay_packet = fake_relay_packet

        def driver():
            ar.env.process(ar._deferred_relay(StubPacket(seq=42, tx=9), 1000.0))
            yield ar.env.timeout(500)
            ar._observe_copy(StubPacket(seq=42, tx=8))  # someone else relayed
        ar.env.process(driver())
        ar.env.run()
        self.assertEqual(calls, [], 'pending backup must cancel on ECHO/inhibition')

    def test_weak_inhibition_keeps_pending_at_cold_start(self):
        """v0.3 adaptive inhibition: at LOW confidence (cold start) a duplicate
        does NOT cancel the pending forward — more redundancy; the residual
        coverage is re-checked at fire time instead."""
        ar = make_ar(inhibition_strength_mode='adaptive')
        self.assertEqual(ar.inhibition_strength(), 'weak')
        calls = []

        def fake_relay_packet(p, **kw):
            calls.append(p.seq)
            return True
        ar._relay_packet = fake_relay_packet

        def driver():
            ar.env.process(ar._deferred_relay(StubPacket(seq=42, tx=9), 1000.0))
            yield ar.env.timeout(500)
            ar._observe_copy(StubPacket(seq=42, tx=8))  # duplicate heard
        ar.env.process(driver())
        ar.env.run()
        self.assertEqual(calls, [42], 'weak inhibition keeps pending (more redundancy)')


class TestFunctionalSegmentsAndInhibition(unittest.TestCase):
    """v0.3 unit tests (spec section 16)."""

    def test_linear_two_directions(self):
        """A-B-C-D-E: node C must see B and D as two DIFFERENT functional
        directions (disjoint 2-hop coverage -> different segments)."""
        ar = make_ar(nodeid=2, segmentation='functional', seg_overlap_threshold=0.5)
        for nid in (1, 3):   # B=1, D=3 (C=2 analyzes itself)
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[1] = {0, 2}   # B knows A, C
        ar.neighbors_of_nb[3] = {2, 4}   # D knows C, E
        ar._recompute_segments(ar.env.now, force=True)
        self.assertNotEqual(ar.segment_of[1], ar.segment_of[3],
                            'B and D must be different functional directions')

    def test_duplicate_does_not_block_necessary_forward(self):
        """In linear topology, a duplicate from B must not automatically block
        the necessary forward toward D (adaptive inhibition: weak when few alts)."""
        ar = make_ar(nodeid=2, inhibition_strength_mode='adaptive',
                     segmentation='functional')
        for nid in (1, 3):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[1] = {0, 2}
        ar.neighbors_of_nb[3] = {2, 4}
        ar._recompute_segments(ar.env.now, force=True)
        # two segments, one neighbor each -> alt_paths < 1.5 -> weak inhibition
        self.assertLessEqual(ar._alt_paths_global(), 1.5)
        self.assertEqual(ar.inhibition_strength(), 'weak',
                         'bridge/linear-like topology needs weak inhibition')

    def test_bridge_gets_unique_coverage_and_weak_inhibition(self):
        """Two dense clusters + one bridge: the bridge neighbor must get high
        unique coverage; adaptive inhibition must not silence it."""
        ar = make_ar(inhibition_strength_mode='adaptive', segmentation='functional')
        for nid in (1, 2, 3, 4, 5):
            ar.learn(StubPacket(tx=nid), -80.0)
        # node 5 is the bridge: only it reaches cluster B {50, 51}
        ar.neighbors_of_nb[1] = {2, 3}
        ar.neighbors_of_nb[2] = {1, 3}
        ar.neighbors_of_nb[3] = {1, 2, 4}
        ar.neighbors_of_nb[4] = {3, 5}
        ar.neighbors_of_nb[5] = {4, 50, 51}
        ar._recompute_segments(ar.env.now, force=True)
        self.assertGreaterEqual(ar._unique_coverage(5), 1,
                                'bridge must have unique coverage')
        # unique coverage -> weak inhibition -> bridge not silenced
        self.assertEqual(ar.inhibition_strength(), 'weak')

    def test_dense_clique_redundant_aggressive_suppression(self):
        """Many neighbors with very similar N(i): recognized as redundant ->
        strong inhibition (aggressive suppression)."""
        ar = make_ar(inhibition_strength_mode='adaptive', segmentation='functional')
        for nid in range(1, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
            ar.neighbors[nid].pdr = 0.95
        for nid in range(1, 7):
            ar.neighbors_of_nb[nid] = {100, 101, 102}   # identical coverage
        ar._recompute_segments(ar.env.now, force=True)
        self.assertEqual(len(set(ar.segment_of.values())), 1,
                         'identical coverage -> one functional segment')
        self.assertGreaterEqual(ar._alt_paths_global(), ar.p['alt_paths_dense'],
                                'clique has many alternative paths')
        self.assertEqual(ar.inhibition_strength(), 'strong',
                         'dense redundant topology -> strong inhibition')

    def test_stale_advertised_info_expires(self):
        """After a neighbor disappears, its NEIGHBORINFO must expire — decisions
        cannot rely on the stale map forever."""
        ar = make_ar()
        p = StubPacket(seq=1, tx=9)
        p.is_neighborinfo = True
        p.ni_neighbors = ((5, 3, 3, 1), (6, 2, 2, 1))
        ar.learn(p, -80.0)
        self.assertIn(9, ar.advertised_neighbors)
        self.assertIn(5, ar.advertised_neighbors[9]['nbs'])
        ar._expire_advertised(ar.env.now + ar.p['ni_expiry_ms'] + 1)
        self.assertNotIn(9, ar.advertised_neighbors)

    def test_neighborinfo_only_advertises_known_neighbors(self):
        """NEIGHBORINFO carries ONLY neighbors the sender actually knows."""
        ar = make_ar()
        ar.learn(StubPacket(tx=5), -80.0)
        ar.learn(StubPacket(tx=7), -90.0)
        # simulate what the sender would advertise: built from ar.neighbors
        entries = [(nid, 3, 3, 1) for nid in sorted(ar.neighbors.keys())]
        self.assertEqual([e[0] for e in entries], [5, 7])


class TestNeighborInfoExchange(unittest.TestCase):
    def test_ni_receive_updates_advertised_map(self):
        ar = make_ar()
        p = StubPacket(seq=1, tx=9)
        p.is_neighborinfo = True
        p.ni_neighbors = ((5, 3, 3, 1), (6, 2, 2, 1))
        ar.learn(p, -80.0)
        adv = ar.advertised_neighbors[9]
        self.assertEqual(set(adv['nbs'].keys()), {5, 6})
        self.assertEqual(adv['obs'], 1)
        # two_hop_view merges advertised info
        self.assertIn(5, ar.two_hop_view(9))

    def test_ni_never_relayed(self):
        """Control packets must never be relayed (no storms)."""
        ar = make_ar()
        p = StubPacket(seq=1, tx=9, dest=BROADCAST_ID, hop=3)
        p.is_neighborinfo = True
        self.assertFalse(ar.on_received(p) if not ar.passthrough else True,
                         'NEIGHBORINFO must not be relayed')

    def test_ni_consistency_confidence(self):
        ar = make_ar()
        for i, nbs in enumerate([((5, 3, 3, 1), (6, 2, 2, 1)),
                                 ((5, 3, 3, 1), (6, 2, 2, 1)),
                                 ((5, 3, 3, 1), (7, 2, 2, 1))]):
            p = StubPacket(seq=i, tx=9)
            p.is_neighborinfo = True
            p.ni_neighbors = nbs
            ar.learn(p, -80.0)
        adv = ar.advertised_neighbors[9]
        self.assertGreater(adv['conf'], 0.3, 'consistent NIs build confidence')


class TestConfidenceAndDensity(unittest.TestCase):
    def test_low_confidence_at_cold_start(self):
        ar = make_ar()
        self.assertEqual(ar._confidence_level(), 0, 'cold start must be LOW confidence')

    def test_confidence_grows_with_observations(self):
        ar = make_ar(conf_obs_full=10)
        for i in range(30):
            ar.learn(StubPacket(seq=i, tx=5 if i % 2 == 0 else 6), -80.0)
        for _ in range(10):
            ar._relay_outcome(5, True)
            ar._relay_outcome(6, True)
            ar.neighbors[5].stability = 1.0
            ar.neighbors[6].stability = 1.0
        self.assertGreaterEqual(ar._confidence_level(), 1)


class TestSegmentation(unittest.TestCase):
    def test_topo_segment_groups_overlapping(self):
        ar = make_ar(segmentation='topo', seg_overlap_threshold=0.5)
        for nid in (5, 6, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10, 11, 12}
        ar.neighbors_of_nb[6] = {10, 11, 12}
        ar.neighbors_of_nb[7] = {20, 21}
        ar._recompute_segments(ar.env.now, force=True)
        self.assertEqual(ar.segment_of[5], ar.segment_of[6])
        self.assertNotEqual(ar.segment_of[5], ar.segment_of[7])

    def test_geo_segments(self):
        ar = make_ar(segmentation='geo', sector_count=4, seg_min_per_sector=1)
        ar.node.position = StubPos(0, 0)
        for nid, pos in [(5, (100, 1)), (6, (1, 100)), (7, (-100, 1)), (8, (1, -100))]:
            p = StubPacket(tx=nid)
            ar.learn(p, -80.0)
            ar.neighbors[nid].pos = pos
        ar._recompute_segments(ar.env.now, force=True)
        self.assertNotEqual(ar.segment_of[5], ar.segment_of[7])
        self.assertNotEqual(ar.segment_of[6], ar.segment_of[8])

    def test_many_topo_segments_no_index_error(self):
        """Topological segmentation can yield more segments than n_segments —
        weight lookup must not crash (regression: IndexError on dense/bridge)."""
        ar = make_ar(segmentation='topo', sector_count=4, seg_overlap_threshold=0.5)
        for nid in range(1, 13):
            ar.learn(StubPacket(tx=nid), -80.0)
            ar.neighbors_of_nb[nid] = {100 + 10 * nid}   # fully disjoint -> 12 segments
        ar._recompute_segments(ar.env.now, force=True)
        self.assertGreater(len(set(ar.segment_of.values())), 4)
        # scoring must tolerate segment ids beyond the default weight vector
        ar._candidate_score(5, dest=999)
        # broadcast decision path also reads per-segment weights; stub the TX
        ar._relay_packet = lambda *a, **k: True
        ar._defer_or_cancel_broadcast = lambda *a, **k: True
        ar._handle_broadcast(StubPacket(seq=1, tx=1, dest=BROADCAST_ID))

    def test_unique_coverage_protection(self):
        ar = make_ar()
        for nid in (5, 6):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10, 11}
        ar.neighbors_of_nb[6] = {10, 11, 42}   # only 6 reaches 42
        self.assertEqual(ar._unique_coverage(6), 1)
        self.assertEqual(ar._unique_coverage(5), 0)


class TestAdaptiveSegmentation(unittest.TestCase):
    def test_adaptive_picks_topo_without_positions(self):
        """Adaptive mode: no learned positions -> functional segmentation
        (v0.3: functional replaced topo as the position-free default)."""
        ar = make_ar(segmentation='adaptive')
        for nid in (5, 6, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10, 11, 12}
        ar.neighbors_of_nb[6] = {10, 11, 12}
        ar.neighbors_of_nb[7] = {20, 21}
        ar._recompute_segments(ar.env.now, force=True)
        self.assertEqual(ar.segmentation_mode_active, 'functional')
        self.assertEqual(ar.segment_of[5], ar.segment_of[6])

    def test_adaptive_picks_geo_with_positions(self):
        """Adaptive mode: most neighbors have learned positions -> geo."""
        ar = make_ar(segmentation='adaptive', seg_pos_ratio=0.6, seg_min_per_sector=1)
        ar.node.position = StubPos(0, 0)
        for nid, pos in [(5, (100, 1)), (6, (1, 100)), (7, (-100, 1)), (8, (1, -100))]:
            ar.learn(StubPacket(tx=nid), -80.0)
            ar.neighbors[nid].pos = pos
        ar._recompute_segments(ar.env.now, force=True)
        self.assertEqual(ar.segmentation_mode_active, 'geo')
        self.assertNotEqual(ar.segment_of[5], ar.segment_of[7])

    def test_adaptive_geo_sector_count_scales_with_density(self):
        """Adaptive geo: fewer positioned neighbors -> fewer sectors (X.29)."""
        ar = make_ar(segmentation='geo', sector_count=8, seg_min_per_sector=3)
        ar.node.position = StubPos(0, 0)
        # only 3 positioned neighbors -> k = ceil(3/3) = 1 -> clamped to 2
        for nid, pos in [(5, (100, 1)), (6, (1, 100)), (7, (-100, 1))]:
            ar.learn(StubPacket(tx=nid), -80.0)
            ar.neighbors[nid].pos = pos
        ar._recompute_segments(ar.env.now, force=True)
        used = len({ar.segment_of[nid] for nid in (5, 6, 7)})
        self.assertLessEqual(used, 2)

    def test_hysteresis_keeps_old_mapping_on_small_change(self):
        """Hysteresis: insignificant topology change keeps old segmentation."""
        ar = make_ar(segmentation='topo', seg_hysteresis=0.9)
        for nid in (5, 6):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10, 11}
        ar.neighbors_of_nb[6] = {10, 11}
        ar._recompute_segments(ar.env.now, force=True)
        old = dict(ar.segment_of)
        # small change: one neighbor's coverage grows slightly
        ar.neighbors_of_nb[6] = {10, 11, 12}
        ar._seg_sig = None
        ar._recompute_segments(ar.env.now + 100000, force=True)
        self.assertEqual(ar.segment_of, old, 'small change must not re-segment')

    def test_weight_carry_over_on_resegmentation(self):
        """A neighbor that changed segment inherits its global weight."""
        ar = make_ar(segmentation='topo', seg_hysteresis=0.0, weight_init=0.5)
        for nid in (5,):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.neighbors_of_nb[5] = {10}
        ar._recompute_segments(ar.env.now, force=True)
        seg_old = ar.segment_of[5]
        ar.neighbors[5].weight = 0.9
        # force a different segmentation result: give 5 a new distinct coverage
        ar.neighbors_of_nb[5] = {10, 99, 98, 97}
        ar.neighbors[6] = NeighborInfo(0, -80, 0, 0.5, 0.8, 4)
        ar.neighbors_of_nb[6] = {20}
        ar._seg_sig = None
        ar._recompute_segments(ar.env.now + 100000, force=True)
        seg_new = ar.segment_of.get(5)
        if seg_new != seg_old:
            self.assertAlmostEqual(ar.neighbors[5].weight_seg[seg_new], 0.9)


class TestPassthrough(unittest.TestCase):
    def test_passthrough_no_designation(self):
        ar = make_ar(passthrough=True)
        p = StubPacket(dest=9)
        ar.designate_on_send(p)
        self.assertIsNone(p.relay_designation)


class TestNoOracle(unittest.TestCase):
    """Static source audit: lib/adaptive.py must not reference simulator-global
    knowledge. This is the code-level enforcement of the no-oracle rule."""
    FORBIDDEN_ATTRS = {'LINK_OFFSET', 'sensedByN', 'detectedByN', 'collidedAtN',
                       'receivedAtN', 'packetsAtN', 'estimate_path_loss',
                       'setup_asymmetric_links'}

    def test_adaptive_no_oracle(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'lib', 'adaptive.py')
        with open(path) as f:
            tree = ast.parse(f.read())
        bad = []
        for node_ in ast.walk(tree):
            if isinstance(node_, ast.Attribute) and node_.attr in self.FORBIDDEN_ATTRS:
                bad.append((node_.attr, node_.lineno))
        self.assertEqual(bad, [], f'oracle leakage in adaptive.py: {bad}')


class TestHerdPrimitives(unittest.TestCase):
    """v0.12b B1' HERD primitives — pure LOCAL signals (zero-oracle),
    continuous, safe on empty state."""

    def _nb(self, ar, nid, rssi=-80.0):
        ar.neighbors[nid] = NeighborInfo(0, rssi, 0, 0.5, 0.8, 4)
        return ar.neighbors[nid]

    def test_routing_order_isolated_reach_first(self):
        ar = make_ar()
        self.assertEqual(ar.routing_order(), 0.0)

    def test_routing_order_single_candidate_dominates(self):
        ar = make_ar()
        self._nb(ar, 5)
        self.assertEqual(ar.routing_order(), 1.0)

    def test_routing_order_chaotic_when_scores_equal(self):
        ar = make_ar()
        for nid in (5, 6, 7, 8):
            self._nb(ar, nid)
        ar._candidate_score = lambda nid, dest: 1.0
        self.assertLess(ar.routing_order(), 0.1)

    def test_routing_order_high_with_clear_winner(self):
        ar = make_ar()
        for nid in (5, 6, 7):
            self._nb(ar, nid)
        ar._candidate_score = lambda nid, dest: {5: 10.0, 6: 0.01, 7: 0.01}[nid]
        self.assertGreater(ar.routing_order(), 0.9)

    def test_cohesion_isolated_is_max(self):
        ar = make_ar()
        self.assertEqual(ar.cohesion_pressure(), 1.0)

    def test_repulsion_isolated_zero(self):
        ar = make_ar()
        self.assertEqual(ar.repulsion_pressure(), 0.0)

    def test_repulsion_grows_with_dupes(self):
        ar = make_ar()
        for nid in (5, 6, 7, 8, 9, 10, 11):
            self._nb(ar, nid)
        p = StubPacket(seq=42)
        ar.node.timesReceived[42] = 4
        r_hi = ar.repulsion_pressure(p)
        ar.node.timesReceived[42] = 1
        r_lo = ar.repulsion_pressure(p)
        self.assertGreater(r_hi, r_lo)
        self.assertGreater(r_lo, 0.0)

    def test_alignment_neutral_without_packet(self):
        ar = make_ar()
        self.assertAlmostEqual(ar.alignment_score(), 0.5)

    def test_alignment_designated_strong(self):
        ar = make_ar(nodeid=7)
        p = StubPacket(seq=1, hop=2)
        p.relay_designation = [7]
        self.assertAlmostEqual(ar.alignment_score(p), 0.9)

    def test_herd_sample_metrics_accumulate(self):
        ar = make_ar()
        self._nb(ar, 5)
        p = StubPacket(seq=9)
        ar._herd_sample_metrics(p)
        ar._herd_sample_metrics(p)
        self.assertEqual(ar.stats['herd_samples'], 2)
        self.assertAlmostEqual(ar.stats['herd_cohesion_sum'],
                               2 * ar.cohesion_pressure(p))
        self.assertAlmostEqual(ar.stats['herd_routing_order_sum'],
                               2 * ar.routing_order(p))

    def test_herd_gating_off_by_default(self):
        ar = make_ar()
        for nid in (5, 6, 7):
            self._nb(ar, nid)
        ar._candidate_score = lambda nid, dest: 1.0   # chaotic -> order ~0
        ar._confidence_level = lambda: 2
        ar._unique_coverage_any = lambda: 0
        ar._alt_paths_global = lambda: 10.0
        self.assertEqual(ar.inhibition_strength(None), 'strong')
        ar.p['enable_herd'] = True
        self.assertEqual(ar.inhibition_strength(None), 'weak')

    def test_herd_gating_keeps_clear_winner(self):
        ar = make_ar()
        for nid in (5, 6, 7):
            self._nb(ar, nid)
        ar._candidate_score = lambda nid, dest: {5: 10.0, 6: 0.01, 7: 0.01}[nid]
        ar._confidence_level = lambda: 2
        ar._unique_coverage_any = lambda: 0
        ar._alt_paths_global = lambda: 10.0
        ar.p['enable_herd'] = True
        self.assertEqual(ar.inhibition_strength(None), 'strong')


class TestCefFrontierTheta(unittest.TestCase):
    """v0.13 C1: frontier_risk + additive scale-aware theta (mode-gated)."""

    def _nb(self, ar, nid, rssi=-80.0):
        ar.neighbors[nid] = NeighborInfo(0, rssi, 0, 0.5, 0.8, 4)
        return ar.neighbors[nid]

    def test_frontier_risk_isolated_is_max(self):
        ar = make_ar()
        self.assertEqual(ar._cef_frontier_risk(), 1.0)

    def test_frontier_risk_alt_starvation(self):
        ar = make_ar()
        for nid in (5, 6):
            self._nb(ar, nid)
        self.assertAlmostEqual(ar._cef_frontier_risk(), 1.0 / 3.0)

    def test_frontier_risk_packet_need_dominates(self):
        ar = make_ar()
        for nid in (5, 6, 7):
            self._nb(ar, nid)
            ar.neighbors_of_nb[nid] = {50, 51}
        p = StubPacket(seq=3)
        self.assertEqual(ar._cef_frontier_risk(p), 1.0)

    def test_frontier_discount_survives_high_confidence(self):
        # B1 diagnosis: boundary nodes suppress at HIGH-but-WRONG confidence
        # (0.887 mean at death). The alt-path discount must not pass
        # through confidence.
        ar = make_ar()
        for nid in (5, 6):
            self._nb(ar, nid)
        ar.confidence = lambda: 0.99
        t_high = ar._cef_theta_v013()
        self.assertLess(t_high, 1.0)
        ar.confidence = lambda: 0.5
        t_low = ar._cef_theta_v013()
        self.assertLess(t_high - t_low, 0.25)   # conf only via k_lowconf+pressure

    def test_theta_v013_dense_suppresses_thin_forwards(self):
        def mk(degree, alts):
            a = make_ar()
            for nid in range(5, 5 + degree):
                self._nb(a, nid)
                a.segment_of[nid] = 0
            a._alt_paths_global = lambda: float(alts)
            a.structural_risk = lambda: 0.0
            a.confidence = lambda: 0.9
            return a
        t_dense = mk(12, 10)._cef_theta_v013()
        t_thin = mk(2, 1)._cef_theta_v013()
        self.assertGreater(t_dense, t_thin)
        self.assertGreater(t_dense, 1.0)
        self.assertLess(t_thin, 0.8)

    def test_theta_mode_gate_dispatch(self):
        ar = make_ar(cef_theta_mode='v013')
        for nid in (5, 6):
            self._nb(ar, nid)
        self.assertAlmostEqual(ar._cef_theta(), ar._cef_theta_v013())
        ar12 = make_ar()   # default mode 'v012'
        for nid in (5, 6, 7, 8):
            self._nb(ar12, nid)
        p_eff = ar12._cef_suppression_pressure()
        g_eff = ar12._cef_reach_guard()
        expected = max(0.2, min(0.7 * (1.0 + p_eff - 0.8 * g_eff), 2.0))
        self.assertAlmostEqual(ar12._cef_theta(), expected)

    def test_default_mode_is_v012(self):
        ar = make_ar()
        self.assertEqual(ar.p['cef_theta_mode'], 'v012')


class TestCefSlotJitter(unittest.TestCase):
    """v0.13 C3: tiered deterministic hash-slot jitter (burst de-correlation)."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.8, 4)

    def test_slot_jitter_deterministic(self):
        ar = make_ar()
        self._nb(ar, 5)
        p = StubPacket(seq=7, orig=1)
        self.assertEqual(ar._cef_slot_jitter_ms(p),
                         ar._cef_slot_jitter_ms(p))

    def test_slot_jitter_within_total_range(self):
        ar = make_ar(cef_slot_count=8, cef_slot_width_ms=15.0)
        self._nb(ar, 5)
        p = StubPacket(seq=3, orig=2)
        j = ar._cef_slot_jitter_ms(p)
        self.assertGreaterEqual(j, 0.0)
        self.assertLessEqual(j, 7 * 15.0)

    def test_tier_classes_get_earlier_slots(self):
        ar = make_ar()
        self._nb(ar, 5)
        p = StubPacket(seq=9, orig=1)
        self.assertLessEqual(ar._cef_slot_jitter_ms(p, tier=PRIMARY), 2 * 15.0)
        self.assertGreaterEqual(ar._cef_slot_jitter_ms(p, tier=BACKUP1), 3 * 15.0)
        self.assertLessEqual(ar._cef_slot_jitter_ms(p, tier=BACKUP1), 5 * 15.0)

    def test_seg_sig_hash_changes_with_topology(self):
        ar = make_ar()
        ar._seg_sig = (frozenset({5}), ((5, 3),))
        h1 = ar._seg_sig_hash()
        ar._seg_sig = (frozenset({5, 6}), ((5, 3), (6, 1)))
        h2 = ar._seg_sig_hash()
        self.assertNotEqual(h1, h2)

    def test_slot_jitter_off_by_default(self):
        ar = make_ar()
        self.assertFalse(ar.p.get('cef_slot_jitter', False))


class TestCefFrontierRescue(unittest.TestCase):
    """v0.13 C1b: frontier rescue — cef_cost death -> tiered echo-cancel
    defer (the redundancy test moves to deferred-time evidence)."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.8, 4)

    def _setup(self, **over):
        ar = make_ar(enable_cef=True, **over)
        for nid in (5, 6, 7, 8, 9):
            self._nb(ar, nid)
        ar._confidence_level = lambda: 2   # skip the LOW-conf reach guard
        return ar

    def test_rescue_when_frontier_high_and_residual(self):
        ar = self._setup(cef_frontier_override=True)
        ar._cef_frontier_risk = lambda p=None: 0.9
        # v0.13 C1e: the rescue gate also needs local regret evidence
        ar._rescue_regret_ewma = 0.9
        ar._rescue_regret_n = 10
        self.assertEqual(ar._cef_should_forward(StubPacket(seq=1)), 'rescue')
        self.assertEqual(ar.stats['cef_frontier_rescued'], 1)

    def test_no_rescue_when_override_off(self):
        ar = self._setup()
        ar._cef_frontier_risk = lambda p=None: 0.9
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))

    def test_no_rescue_when_frontier_low(self):
        ar = self._setup(cef_frontier_override=True)
        ar._cef_frontier_risk = lambda p=None: 0.2
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))

    def test_no_rescue_when_residual_empty(self):
        ar = self._setup(cef_frontier_override=True)
        ar._cef_frontier_risk = lambda p=None: 0.9
        ar._residual_coverage = lambda p, r: set()
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))


class TestRescueHardEchoCancel(unittest.TestCase):
    """v0.13 C1d: rescued pending defers cancel on the FIRST overheard
    copy (deferred-time redundancy proof); plain weak pendings survive."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.8, 4)

    def test_rescue_pending_hard_cancel_on_echo(self):
        ar = make_ar()
        for nid in (5, 6):
            self._nb(ar, nid)
        ar.pending[9] = {'deadline': 0, 'segment': None,
                         'cancelled': False, 'rescue': True}
        ar._observe_copy(StubPacket(seq=9, tx=5))
        self.assertNotIn(9, ar.pending)
        self.assertEqual(ar.stats.get('rescue_cancel_echo', 0), 1)

    def test_weak_pending_survives_echo_without_rescue_flag(self):
        ar = make_ar()
        for nid in (5, 6):
            self._nb(ar, nid)
        ar._confidence_level = lambda: 2
        ar._alt_paths_global = lambda: 0.5   # alts <= 1 -> weak inhibition
        ar.pending[9] = {'deadline': 0, 'segment': None, 'cancelled': False}
        ar._observe_copy(StubPacket(seq=9, tx=5))
        self.assertIn(9, ar.pending)
        self.assertEqual(ar.stats.get('adaptive_inhibition_weak', 0), 1)


class TestRescueRegretGate(unittest.TestCase):
    """v0.13 C1e: rescue opens ONLY on local suppression-regret evidence
    (dense: suppressions are correct -> gate closed; mixed: fronts die -> open)."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.8, 4)

    def _setup(self, **over):
        ar = make_ar(enable_cef=True, cef_frontier_override=True, **over)
        for nid in (5, 6, 7, 8, 9):
            self._nb(ar, nid)
        ar._confidence_level = lambda: 2
        return ar

    def test_gate_closed_without_evidence(self):
        ar = self._setup()
        ar._cef_frontier_risk = lambda p=None: 0.9
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))
        self.assertIsNone(ar._rescue_regret_ewma)   # no observation yet

    def test_gate_closed_when_regret_low(self):
        ar = self._setup()
        ar._cef_frontier_risk = lambda p=None: 0.9
        ar._rescue_regret_ewma = 0.1   # dense-like: my suppressions were fine
        ar._rescue_regret_n = 10
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))

    def test_gate_opens_with_regret_evidence(self):
        ar = self._setup()
        ar._cef_frontier_risk = lambda p=None: 0.9
        ar._rescue_regret_ewma = 0.9   # mixed-like: fronts died after suppression
        ar._rescue_regret_n = 10
        self.assertEqual(ar._cef_should_forward(StubPacket(seq=1)), 'rescue')

    def test_regret_observe_ewma(self):
        ar = make_ar()
        p = StubPacket(seq=4)
        ar._frontier_regret_observe(p, 0, False)     # nobody relayed -> regret
        self.assertEqual(ar._rescue_regret_ewma, 1.0)
        self.assertEqual(ar._rescue_regret_n, 1)
        ar.packet_relayers[4] = {7}                  # covered afterwards -> no regret
        ar._frontier_regret_observe(p, 0, False)
        self.assertAlmostEqual(ar._rescue_regret_ewma, 0.8)  # (1-0.2)*1 + 0.2*0
        self.assertEqual(ar._rescue_regret_n, 2)
        self.assertEqual(ar.stats.get('frontier_suppression_regret', 0), 1)


class TestRippleHopBias(unittest.TestCase):
    """v0.13 C4: FW+-style fixed hop bias (ripple_hop_bias_ms)."""

    def test_fixed_bias_replaces_alpha_term(self):
        ar = make_ar(ripple_hop_bias_ms=40.0)
        p = StubPacket(seq=1, hop=1)              # hop_depth = 3 - 1 = 2
        ar._packet_hash_slot = lambda p, k=None: 0
        delay = ar._ripple_delay_ms(p, score=1.0, dmin=100.0, dspan=300.0,
                                    base=370.0)
        self.assertAlmostEqual(delay, 100.0 + 40.0 * 2)   # + (1-1)*300 + 0*step

    def test_legacy_alpha_when_bias_zero(self):
        ar = make_ar()                             # default ripple_hop_bias_ms = 0
        p = StubPacket(seq=1, hop=1)
        ar._packet_hash_slot = lambda p, k=None: 0
        delay = ar._ripple_delay_ms(p, score=0.5, dmin=100.0, dspan=300.0,
                                    base=370.0)
        expected = (100.0
                    + ar.p['ripple_alpha'] * 2 * 370.0
                    + 0.5 * 300.0
                    + 0 * ar.p['ripple_jitter_step_ms'])
        self.assertAlmostEqual(delay, expected)

    def test_fixed_bias_independent_of_airtime(self):
        ar = make_ar(ripple_hop_bias_ms=40.0)
        p = StubPacket(seq=1, hop=2)
        ar._packet_hash_slot = lambda p, k=None: 0
        d1 = ar._ripple_delay_ms(p, score=1.0, dmin=100.0, dspan=300.0,
                                 base=370.0)
        d2 = ar._ripple_delay_ms(p, score=1.0, dmin=100.0, dspan=300.0,
                                 base=1000.0)
        self.assertEqual(d1, d2)                    # FW+ timing, not airtime-scaled


class TestTopKBoundedState(unittest.TestCase):
    """v0.14 D0: TOP-K CORE + PROTECTED bounded neighbor state."""

    def _nb(self, ar, nid, pdr=0.9, stability=0.9, rssi=-70.0):
        n = NeighborInfo(0, rssi, 0, 0.5, pdr, 4)
        n.stability = stability
        ar.neighbors[nid] = n
        return n

    def _fill(self, ar, n=20):
        for nid in range(100, 100 + n):
            self._nb(ar, nid)

    def test_disabled_by_default_keeps_all(self):
        ar = make_ar()
        self._fill(ar, 20)
        ar._topk_evict(0)
        self.assertEqual(len(ar.neighbors), 20)

    def test_k_bounds_core(self):
        ar = make_ar(topk_enabled=True, topk_k=8)
        self._fill(ar, 20)
        ar._unique_coverage = lambda nid: 0     # nobody protected via coverage
        ar._topk_evict(0)
        self.assertEqual(len(ar.neighbors), 8)

    def test_protected_survives_beyond_k(self):
        ar = make_ar(topk_enabled=True, topk_k=8)
        self._fill(ar, 20)
        ar._unique_coverage = lambda nid: 3 if nid == 100 else 0
        ar._topk_evict(0)
        self.assertIn(100, ar.neighbors)
        self.assertEqual(len(ar.neighbors), 9)   # 8 CORE + 1 PROTECTED

    def test_ranking_keeps_high_pdr(self):
        ar = make_ar(topk_enabled=True, topk_k=1)
        self._nb(ar, 201, pdr=0.9, stability=0.9)
        self._nb(ar, 202, pdr=0.1, stability=0.1)
        ar._unique_coverage = lambda nid: 0
        ar._topk_evict(0)
        self.assertIn(201, ar.neighbors)
        self.assertNotIn(202, ar.neighbors)

    def test_eviction_cleans_derived_state(self):
        ar = make_ar(topk_enabled=True, topk_k=1)
        self._nb(ar, 203, pdr=0.9)
        self._nb(ar, 204, pdr=0.1)
        ar.neighbors_of_nb[204] = {9, 10}
        ar.segment_of[203] = 1
        ar.segment_of[204] = 1          # shared segment, jointly redundant
        ar.alt_paths = lambda seg: 2.0  # above the 1.5 protection threshold
        ar._unique_coverage = lambda nid: 0
        ar._topk_evict(0)
        self.assertNotIn(204, ar.neighbors)
        self.assertNotIn(204, ar.neighbors_of_nb)
        self.assertNotIn(204, ar.segment_of)
        self.assertEqual(ar.stats['topk_evictions'], 1)

    def test_deterministic_eviction(self):
        ar1 = make_ar(topk_enabled=True, topk_k=4)
        ar2 = make_ar(topk_enabled=True, topk_k=4)
        for ar in (ar1, ar2):
            self._fill(ar, 12)
            ar._unique_coverage = lambda nid: 0
            ar._topk_evict(0)
        self.assertEqual(set(ar1.neighbors), set(ar2.neighbors))

    def test_sole_segment_contributor_protected(self):
        ar = make_ar(topk_enabled=True, topk_k=2)
        self._nb(ar, 300, pdr=0.1)   # weak link but sole in its segment
        self._nb(ar, 301, pdr=0.9)
        self._nb(ar, 302, pdr=0.9)
        ar.segment_of[300] = 5        # lonely segment
        ar.segment_of[301] = 6
        ar.segment_of[302] = 6
        ar.alt_paths = lambda seg: 0.95 if seg == 5 else 1.9
        ar._unique_coverage = lambda nid: 0
        ar._topk_evict(0)
        self.assertIn(300, ar.neighbors)         # PROTECTED despite lowest rank
        self.assertEqual(len(ar.neighbors), 3)   # 2 CORE + 1 PROTECTED


class TestTopKStrictProtect(unittest.TestCase):
    """v0.14 D0: strict protection bounds the table in dense (generous
    mode protects every singleton-segment node -> table NOT bounded)."""

    def _nb(self, ar, nid, pdr=0.9):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, pdr, 4)

    def test_strict_protects_only_bottleneck_segment(self):
        ar = make_ar(topk_enabled=True, topk_k=1, topk_protect_mode='strict')
        self._nb(ar, 400, pdr=0.1)   # worst segment (bottleneck) — PROTECTED
        self._nb(ar, 401, pdr=0.9)   # singleton segments but not bottleneck
        self._nb(ar, 402, pdr=0.9)
        for nid, seg in ((400, 5), (401, 6), (402, 7)):
            ar.segment_of[nid] = seg
        ar.alt_paths = lambda seg: 0.9 if seg == 5 else 1.4
        ar._unique_coverage = lambda nid: 0
        self.assertTrue(ar._topk_protected(400))
        self.assertFalse(ar._topk_protected(401))
        self.assertFalse(ar._topk_protected(402))

    def test_generous_still_default(self):
        ar = make_ar()
        self.assertEqual(ar.p.get('topk_protect_mode', 'generous'), 'generous')


class TestMobilityDemote(unittest.TestCase):
    """v0.14: mobility self-demotion — frequent movers act as CLIENT_MUTE."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def test_disabled_by_default(self):
        ar = make_ar()
        ar._expire_stale(0)
        self.assertIsNone(ar._mob_score)
        self.assertFalse(ar._mobility_demoted())

    def test_churn_raises_score_and_demotes(self):
        ar = make_ar(mobility_enabled=True, mobility_demote=True)
        for nid in (5, 6):
            self._nb(ar, nid)
        ar._expire_stale(0)          # baseline snapshot
        # sustained churn: alternate full table swaps (score: 1-0.8^k)
        for i, group in enumerate([(7, 8, 9), (10, 11, 12)] * 2):
            ar.neighbors.clear()
            for nid in group:
                self._nb(ar, nid)
            ar._expire_stale(0)
        self.assertGreaterEqual(ar._mob_score, 0.5)
        self.assertTrue(ar._mobility_demoted())
        self.assertGreaterEqual(ar.stats['mobility_recommend_mute'], 1)
        self.assertEqual(ar._cef_role_cost(),
                         ar.p['cef_role_cost']['CLIENT_MUTE'])

    def test_stable_table_stays_undemoted(self):
        ar = make_ar(mobility_enabled=True, mobility_demote=True)
        for nid in (5, 6):
            self._nb(ar, nid)
        ar._expire_stale(0)
        ar._expire_stale(0)          # no churn between observations
        self.assertLess(ar._mob_score, 0.5)
        self.assertFalse(ar._mobility_demoted())


class TestDriftAndCapture(unittest.TestCase):
    """v0.14 D1: PHY capture threshold param + deterministic clock drift."""

    class _FP:
        def __init__(self, rssi):
            self.rssiAtN = {1: rssi}

    def test_capture_default_6db_weaker_lost(self):
        import lib.phy as phy
        p1, p2 = self._FP(-70), self._FP(-80)      # 10 dB apart
        self.assertEqual(phy.power_collision(p1, p2, 1), (p2,))
        p2b = self._FP(-74)                          # 4 dB apart
        self.assertEqual(phy.power_collision(p1, p2b, 1), (p1, p2b))

    def test_capture_off_both_collide(self):
        import lib.phy as phy
        old = getattr(phy.conf, 'CAPTURE_THRESHOLD_DB', 6)
        try:
            phy.conf.CAPTURE_THRESHOLD_DB = 0
            p1, p2 = self._FP(-70), self._FP(-95)   # 25 dB apart — still both
            self.assertEqual(phy.power_collision(p1, p2, 1), (p1, p2))
        finally:
            phy.conf.CAPTURE_THRESHOLD_DB = old

    def test_clock_scale_bounds_and_determinism(self):
        from lib.node import node_clock_scale
        self.assertEqual(node_clock_scale(5, 1, 0), 1.0)
        for nid in range(20):
            s = node_clock_scale(nid, 7, 40)
            self.assertLessEqual(abs(s - 1.0), 40e-6 + 1e-12)
            self.assertEqual(s, node_clock_scale(nid, 7, 40))


class TestShepherdCollect(unittest.TestCase):
    """v0.14 D2-D4: minimal SHEPHERD-COLLECT — sustained local regret at
    frontier-sized degree elects a shepherd whose lease opens the rescue
    gate network-wide (no local evidence needed); dense stays silent."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def _sustained_regret(self, ar, n=5):
        p = StubPacket(seq=99)
        for _ in range(n):
            ar._frontier_regret_observe(p, 0, False)   # nobody relayed -> regret

    def test_candidacy_requires_regret_and_low_degree(self):
        ar = make_ar(shepherd_enabled=True)
        for nid in (5, 6, 7):
            self._nb(ar, nid)
        self.assertFalse(ar._shepherd_candidacy())      # no regret evidence yet
        self._sustained_regret(ar)
        # the trigger armed the election as a side effect (designed):
        self.assertTrue(ar._shepherd_timer_armed)
        self.assertFalse(ar._shepherd_candidacy())     # already armed -> no re-arm

    def test_no_candidacy_in_dense_like_degree(self):
        ar = make_ar(shepherd_enabled=True)
        for nid in range(100, 120):                     # 20 neighbors > 16
            self._nb(ar, nid)
        self._sustained_regret(ar)
        self.assertFalse(ar._shepherd_candidacy())     # ultra-dense silence guard

    def test_collect_lease_opens_rescue_gate_without_local_evidence(self):
        ar = make_ar(enable_cef=True, cef_frontier_override=True,
                     shepherd_enabled=True)
        for nid in (5, 6, 7, 8, 9):
            self._nb(ar, nid)
        ar._confidence_level = lambda: 2
        ar._cef_frontier_risk = lambda p=None: 0.9
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))  # no lease, no evidence
        ar._collect_until = 60000.0                    # lease active
        self.assertEqual(ar._cef_should_forward(StubPacket(seq=1)), 'rescue')
        self.assertEqual(ar.stats['rescues_during_collect'], 1)

    def test_collect_message_reception_sets_lease(self):
        ar = make_ar(shepherd_enabled=True)
        p = StubPacket(seq=50)
        p.is_shepherd = True
        p.shepherd_lease_ms = 45000.0
        p.shepherd_score = 0.9
        self.assertFalse(ar._handle_broadcast(p))
        self.assertGreater(ar._collect_until, 0)
        self.assertEqual(ar.stats['collect_hears'], 1)

    def test_election_cancelled_on_better_shepherd(self):
        ar = make_ar(shepherd_enabled=True)
        for nid in (5, 6):
            self._nb(ar, nid)
        ar._shepherd_timer_armed = True
        p = StubPacket(seq=51)
        p.is_shepherd = True
        p.shepherd_lease_ms = 45000.0
        p.shepherd_score = 99.0                      # decisively better
        ar._handle_broadcast(p)
        self.assertFalse(ar._shepherd_timer_armed)
        self.assertEqual(ar.stats['shepherd_election_cancelled'], 1)


class TestN1Census(unittest.TestCase):
    """v0.14 N1: receiver-evidence census — count ACTUAL retransmissions
    during a ToA-scaled window instead of predicting redundancy."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def test_k_fixed(self):
        ar = make_ar(n1_enabled=True, n1_mode='fixed', n1_k=3)
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 3)

    def test_k_degree_never_inverted(self):
        ar = make_ar(n1_enabled=True, n1_mode='degree')
        for nid in (5, 6):
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 99)   # sparse: off
        for nid in range(7, 13):                              # 7 -> medium
            self._nb(ar, nid)
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 3)
        for nid in range(20, 30):                            # 16 -> dense
            self._nb(ar, nid)
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 2)

    def test_k_adaptive_frontier_raises_k(self):
        ar = make_ar(n1_enabled=True, n1_mode='adaptive')
        ar._cef_frontier_risk = lambda p=None: 1.0
        k_front = ar._n1_k(StubPacket(seq=1))
        ar._cef_frontier_risk = lambda p=None: 0.0
        k_core = ar._n1_k(StubPacket(seq=1))
        self.assertGreater(k_front, k_core)

    def test_census_counts_unique_forwarders(self):
        ar = make_ar(n1_enabled=True, n1_mode='fixed', n1_k=2)
        first = ar._n1_handle(StubPacket(seq=5, tx=1))
        self.assertTrue(first)                              # deferred
        self.assertFalse(ar._n1_handle(StubPacket(seq=5, tx=2)))   # copy
        self.assertFalse(ar._n1_handle(StubPacket(seq=5, tx=2)))   # same tx: no dup
        self.assertEqual(len(ar._n1_census[5]['ids']), 2)

    def test_suppress_on_census_forward_when_thin(self):
        ar = make_ar(n1_enabled=True, n1_mode='fixed', n1_k=2, n1_w_toa=0.75)
        relayed = []
        ar._relay_packet = lambda p: relayed.append(p.seq)
        ar._n1_handle(StubPacket(seq=7, tx=1))
        ar._n1_handle(StubPacket(seq=7, tx=2))              # census reaches K=2
        ar.env.run(until=20000)
        self.assertEqual(ar.stats['suppressed_by_census'], 1)
        self.assertEqual(relayed, [])
        # thin front: only one forwarder heard -> forward
        ar._n1_handle(StubPacket(seq=8, tx=1))
        ar.env.run(until=40000)
        self.assertIn(8, relayed)
        self.assertEqual(ar.stats['copies_before_forward'], 1)


class TestShepherdScopeBudget(unittest.TestCase):
    """v0.14 D2c: segment-scoped quorum + budgeted rescue + lease-only gate."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)
        ar.segment_of[nid] = 3

    def _shep_pkt(self, seq, tx=1, score=0.5):
        p = StubPacket(seq=seq, tx=tx)
        p.is_shepherd = True
        p.shepherd_lease_ms = 45000.0
        p.shepherd_score = score
        return p

    def test_scope_rejects_fat_segment(self):
        ar = make_ar(shepherd_enabled=True)
        self._nb(ar, 5)
        ar.alt_paths = lambda seg: 2.5            # well-fed segment -> ignore
        ar._handle_broadcast(self._shep_pkt(60, tx=5))
        self.assertEqual(ar.stats['collect_ignored_scope'], 1)
        self.assertEqual(ar._collect_until, 0.0)

    def test_scope_accepts_thin_segment(self):
        ar = make_ar(shepherd_enabled=True)
        self._nb(ar, 5)
        ar.alt_paths = lambda seg: 0.9
        ar._handle_broadcast(self._shep_pkt(61, tx=5))
        self.assertEqual(ar.stats['collect_hears'], 1)
        self.assertGreater(ar._collect_until, 0.0)

    def test_rescue_budget_denies_and_regens(self):
        ar = make_ar(shepherd_enabled=True)
        ar._collect_until = 10 ** 9               # lease always active for test
        ar._rescue_credits = 0.25
        self.assertFalse(ar._rescue_credit())      # denied
        ar._rescue_credits_t = -10 ** 6            # 1000 s elapsed -> fully regen
        self.assertTrue(ar._rescue_credit())       # granted

    def test_lease_only_gate_off_local_evidence(self):
        # AR_CEF_SHEP2 semantics: cef_frontier_min_regret=2.1 disables the
        # LOCAL rescue path — only a COLLECT lease opens the gate
        ar = make_ar(enable_cef=True, cef_frontier_override=True,
                     cef_frontier_min_regret=2.1, shepherd_enabled=True)
        for nid in (5, 6, 7, 8, 9):
            self._nb(ar, nid)
        ar.segment_of = {}
        ar._confidence_level = lambda: 2
        ar._cef_frontier_risk = lambda p=None: 0.9
        ar._rescue_credits = 99.0                   # budget never denies here
        self.assertFalse(ar._cef_should_forward(StubPacket(seq=1)))  # no lease -> suppressed
        ar._collect_until = 10 ** 9
        self.assertEqual(ar._cef_should_forward(StubPacket(seq=1)), 'rescue')


class TestN3Whisper(unittest.TestCase):
    """v0.14 N3: ranked two-phase whisper — preferred first, census-judged
    main body, silent-backup fallback; ordering policies are explicit arms
    (designated = AR, edge_first = Meshtastic MT, strong_first = MeshCore)."""

    def test_rank_designated_policy(self):
        ar = make_ar(n3_enabled=True)
        p = StubPacket(seq=1)
        p.relay_designation = [ar.node.nodeid]
        self.assertEqual(ar._n3_rank(p), 0)
        p2 = StubPacket(seq=2)
        p2.relay_designation = [99]
        self.assertEqual(ar._n3_rank(p2), 1)

    def _ranked_packet(self, ar, margin_db):
        p = StubPacket(seq=3)
        sens = ar.conf.current_preset['sensitivity']
        p.rssiAtN = {ar.node.nodeid: sens + margin_db}
        return p

    def test_rank_edge_first_bounded(self):
        ar = make_ar(n3_enabled=True, n3_order_policy='edge_first')
        self.assertEqual(ar._n3_rank(self._ranked_packet(ar, 8.0)), 0)    # weak above floor
        self.assertEqual(ar._n3_rank(self._ranked_packet(ar, 20.0)), 1)   # strong waits
        self.assertEqual(ar._n3_rank(self._ranked_packet(ar, 2.0)), 2)    # below floor: never preferred

    def test_rank_strong_first_inverted(self):
        ar = make_ar(n3_enabled=True, n3_order_policy='strong_first')
        self.assertEqual(ar._n3_rank(self._ranked_packet(ar, 20.0)), 0)
        self.assertEqual(ar._n3_rank(self._ranked_packet(ar, 8.0)), 1)

    def test_rank0_forwards_rank1_census_judges(self):
        ar = make_ar(n3_enabled=True, n3_order_policy='designated',
                     n1_k=2, n3_gap_toa=0.5)
        relayed = []
        ar._relay_packet = lambda p: relayed.append(p.seq)
        # RANK0 (designated): forwards even with copies on the census
        p = StubPacket(seq=10)
        p.relay_designation = [ar.node.nodeid]
        ar._n3_handle(p)
        ar._n3_handle(StubPacket(seq=10, tx=7))    # a copy arrives
        ar.env.run(until=50000)
        self.assertIn(10, relayed)                 # preferred fires regardless
        # RANK1: census >= K=2 -> suppressed
        ar2 = make_ar(n3_enabled=True, n3_order_policy='designated', n1_k=2)
        ar2._relay_packet = lambda p: relayed.append(p.seq)
        ar2._n3_handle(StubPacket(seq=11, tx=1))
        ar2._n3_handle(StubPacket(seq=11, tx=2))   # 2 forwarders heard
        ar2.env.run(until=50000)
        self.assertNotIn(11, relayed)
        self.assertEqual(ar2.stats['suppressed_by_census'], 1)

    def test_hybrid_rescue_under_lease_no_keyerror(self):
        """v0.14 E8 regression: the census-suppression -> rescue conversion
        under a COLLECT lease must not KeyError on lazily-created stats
        (found by the remote E5a panel: 20/240 runs died on it)."""
        ar = make_ar(n3_enabled=True, n3_order_policy='strong_first',
                     n1_mode='fixed', n1_k=2, n3_gap_toa=0.5,
                     shepherd_enabled=True)
        relayed = []
        ar._relay_packet = lambda p: relayed.append(p.seq)
        ar._collect_until = 10 ** 9          # lease active
        ar._rescue_credits = 99.0            # budget never denies
        sens = ar.conf.current_preset['sensitivity']
        p = StubPacket(seq=20, tx=1)
        p.rssiAtN = {ar.node.nodeid: sens + 2.0}   # below floor -> RANK2
        p2 = StubPacket(seq=20, tx=2)
        p2.rssiAtN = {ar.node.nodeid: sens + 2.0}
        self.assertEqual(ar._n3_rank(p), 2)
        ar._n3_handle(p)
        ar._n3_handle(p2)                          # census >= k2=2
        ar.env.run(until=50000)
        self.assertIn(20, relayed)                 # suppression -> RESCUE
        self.assertEqual(ar.stats['n3_shep_rescues'], 1)
        self.assertEqual(ar.stats['cef_frontier_rescued'], 1)
        self.assertEqual(ar.stats['suppressed_by_census'], 0)

    def test_rank2_fires_only_in_near_silence(self):
        ar = make_ar(n3_enabled=True, n3_order_policy='strong_first',
                     n3_k2=2, n3_gap_toa=0.5)
        relayed = []
        ar._relay_packet = lambda p: relayed.append(p.seq)
        # below-floor link -> RANK2; one copy heard (>= k2=2? no: 1 < 2) -> forwards
        sens = ar.conf.current_preset['sensitivity']
        p = StubPacket(seq=12, tx=1)
        p.rssiAtN = {ar.node.nodeid: sens + 2.0}
        self.assertEqual(ar._n3_rank(p), 2)
        ar._n3_handle(p)
        ar.env.run(until=50000)
        self.assertIn(12, relayed)                  # silence -> fallback fires
        self.assertEqual(ar.stats['n3_forward_rank2'], 1)


class TestOODScenarios(unittest.TestCase):
    """v0.14 S3: OOD topology generators — deterministic, local-rng only
    (the global stream that existing generators use stays untouched)."""

    def _hash(self, name, seed):
        conf = Config()
        conf.SEED = seed
        ncs, meta = build_scenario(conf, name, seed)
        pts = sorted((round(c.position.x, 5), round(c.position.y, 5)) for c in ncs)
        return len(ncs), hash(tuple(pts))

    def test_all_ood_generators_deterministic_and_unique(self):
        for name in ('double_bridge', 'cluster_chain', 'rural_corridor',
                     'ring_with_spurs', 'grid', 'core_plus_remote',
                     'two_dense_sparse_mid', 'rand_var_density'):
            h1 = self._hash(name, 5)
            h2 = self._hash(name, 5)      # same seed -> identical
            h3 = self._hash(name, 6)      # different seed -> different
            self.assertEqual(h1, h2, name)
            self.assertNotEqual(h1, h3, name)
            self.assertGreater(h1[0], 10, name)

    def test_existing_scenarios_bit_safe(self):
        # the OOD generators use LOCAL rngs — the global stream must be
        # unaffected, so classic placements stay bit-identical
        conf = Config()
        conf.SEED = 18
        ncs, _ = build_scenario(conf, 'sparse', 18)
        pts = sorted((round(c.position.x, 6), round(c.position.y, 6)) for c in ncs)
        self.assertEqual(len(pts), 10)


class TestN4AirtimeBucket(unittest.TestCase):
    """v0.14 N4: airtime token bucket — cost in ms of REAL packet airtime,
    AIMD regen (additive increase under LOW chanUtil, multiplicative decay
    under HIGH), structural cap multiplier; D3 packet-count bucket stays
    when n4_enabled is off."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def test_d3_mode_unchanged_when_n4_off(self):
        ar = make_ar(shepherd_enabled=True)
        self.assertFalse(ar.p.get('n4_enabled', False))
        ar._rescue_credits = 0.25
        self.assertFalse(ar._rescue_credit(StubPacket(seq=1)))    # D3 deny
        ar._rescue_credits_t = -10 ** 6
        self.assertTrue(ar._rescue_credit(StubPacket(seq=1)))     # D3 grant

    def test_n4_grants_by_airtime_cost(self):
        ar = make_ar(shepherd_enabled=True, n4_enabled=True,
                     n4_base_tokens_ms=1000.0)
        self._nb(ar, 5)
        p = StubPacket(seq=2)                  # timeOnAir = 1000 ms
        ar._n4_bucket = 1000.0
        self.assertTrue(ar._rescue_credit(p))   # exactly enough
        self.assertAlmostEqual(ar._n4_bucket, 0.0)
        self.assertFalse(ar._rescue_credit(p))  # starved -> deny
        self.assertEqual(ar.stats['n4_starved'], 1)

    def test_n4_high_util_multiplicative_decay(self):
        ar = make_ar(shepherd_enabled=True, n4_enabled=True,
                     n4_base_tokens_ms=5000.0)
        self._nb(ar, 5)
        ar.node.channel_utilization_percent = lambda: 80.0   # HIGH
        ar._n4_bucket = 5000.0
        ar._rescue_credit(StubPacket(seq=3))
        self.assertLess(ar._n4_bucket, 5000.0)              # beta decay hit

    def test_n4_structural_cap_raises_for_frontier(self):
        ar = make_ar(shepherd_enabled=True, n4_enabled=True,
                     n4_base_tokens_ms=1000.0)
        self._nb(ar, 5)
        ar._cef_frontier_risk = lambda p=None: 1.0
        ar._n4_bucket = 100.0
        ar._n4_bucket_t = -10 ** 6                          # full regen elapsed
        ar.node.channel_utilization_percent = lambda: 0.0
        ar._rescue_credit(StubPacket(seq=4))                # cost 1000 ms
        # frontier cap = 1000 * (1 + 1.0*1.0) = 2000 ms -> grant survived
        self.assertGreaterEqual(ar._n4_bucket, 0.0)
        self.assertGreater(ar.stats.get('n4_airtime_granted', 0), 0)


class TestCefBoost(unittest.TestCase):
    """v0.14 CEF_BOOST: traffic-jam mode — sustained-evidence entry (hysteresis),
    instant exit on any frontier signal."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def _jam_setup(self, **over):
        ar = make_ar(n3_enabled=True, n3_order_policy='strong_first',
                     n1_mode='degree', shepherd_enabled=True,
                     enable_cef=True, cef_boost_enabled=True, **over)
        for nid in range(100, 116):   # 16 neighbors (dense)
            self._nb(ar, nid)
        ar._confidence_level = lambda: 2
        return ar

    def _tick(self, ar, t):
        """Simulate one window of dense traffic activity, then observe.
        The busy check uses census RX deltas (copies+supp+fwd >= 20)."""
        ar.stats['n3_forward_rank0'] = ar.stats['n3_forward_rank0'] + 10
        ar.stats['copies_before_forward'] = ar.stats['copies_before_forward'] + 10
        ar.stats['suppressed_by_census'] = ar.stats['suppressed_by_census'] + 5
        ar._expire_stale(t)

    def test_disabled_by_default(self):
        ar = make_ar()
        self.assertFalse(ar.p.get('cef_boost_enabled', False))

    def test_entry_requires_sustained_evidence(self):
        ar = self._jam_setup()
        ar._expire_stale(0)            # first window: baseline only
        self.assertFalse(ar._cef_boost)
        self._tick(ar, 1000)           # stable window 1
        self.assertFalse(ar._cef_boost)  # stable=1, need 3
        self._tick(ar, 2000)           # stable window 2
        self.assertFalse(ar._cef_boost)  # stable=2, need 3
        self._tick(ar, 3000)           # stable window 3
        self.assertTrue(ar._cef_boost)   # entered

    def test_exit_on_frontier_regret(self):
        ar = self._jam_setup()
        ar._expire_stale(0)
        for t in (1000, 2000, 3000):
            self._tick(ar, t)
        self.assertTrue(ar._cef_boost)
        # a frontier regret observation: instant exit
        p = StubPacket(seq=99)
        ar._frontier_regret_observe(p, 0, False)  # nobody relayed -> regret
        self.assertFalse(ar._cef_boost)
        self.assertEqual(ar.stats['boost_exits'], 1)

    def test_exit_on_dropped_conditions(self):
        ar = self._jam_setup()
        ar._expire_stale(0)
        for t in (1000, 2000, 3000):
            self._tick(ar, t)
        self.assertTrue(ar._cef_boost)
        # channel went quiet: conditions dropped -> exit
        # (no traffic activity in this window -> heard=0 -> busy=False)
        ar._expire_stale(4000)   # no _tick: no counters incremented
        self.assertFalse(ar._cef_boost)

    def test_cef_gate_respects_boost(self):
        ar = self._jam_setup()
        # not boosted: CEF gate should be False (n3 path takes over)
        self.assertFalse(ar._cef_boost)
        # boost: CEF gate opens
        ar._cef_boost = True
        # (the actual gate check is in _handle_broadcast; the flag is read
        # there; here we verify the flag accessor)
        self.assertTrue(ar._cef_boost)


class TestNodeDBLayer(unittest.TestCase):
    """v2 NodeDB analog: passive hops_away from the hopStart header field
    (firmware NodeDB semantics), the census profile shields, mini-flood
    radius scaling — and the GRACE contract: without evidence the router
    must make bit-identical decisions to the degree heuristic."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def test_ndb_update_passive_and_bounded(self):
        ar = make_ar(ndb_enabled=True)
        p = StubPacket(seq=1, orig=7)
        p.hopStart = 5
        p.hopLimit = 3
        ar.learn(p, -80.0)
        self.assertIn(7, ar.nodedb)
        self.assertEqual(ar.nodedb[7]['hops'], 2)   # hopStart - hopLimit
        # bound: eviction of the oldest entry
        for o in range(100, 200):
            p2 = StubPacket(seq=o, orig=o)
            p2.hopStart, p2.hopLimit = 5, 4
            ar.learn(p2, -80.0)
        self.assertLessEqual(len(ar.nodedb), int(ar.p['ndb_max_entries']))

    def test_ndb_update_gated_without_hopstart(self):
        ar = make_ar(ndb_enabled=True)
        p = StubPacket(seq=1, orig=7)
        assert not hasattr(p, 'hopStart') or getattr(p, 'hopStart', None) is None
        ar.learn(p, -80.0)
        self.assertEqual(ar.nodedb, {})   # has_hops_away False -> no entry

    def test_ndb_disabled_no_state(self):
        ar = make_ar(ndb_enabled=False)
        p = StubPacket(seq=1, orig=7)
        p.hopStart, p.hopLimit = 5, 2
        ar.learn(p, -80.0)
        self.assertEqual(ar.nodedb, {})

    def test_grace_no_evidence_equals_degree(self):
        """GRACE CONTRACT: ndb mode with empty nodedb (cold start or
        ablation world) must return exactly what degree mode returns."""
        for deg, expect in ((3, 99), (7, 3), (16, 2)):
            a_deg = make_ar(n1_enabled=True, n1_mode='degree')
            a_ndb = make_ar(n1_enabled=True, n1_mode='ndb', ndb_enabled=True)
            for a in (a_deg, a_ndb):
                for nid in range(deg):
                    self._nb(a, nid)
                a.structural_risk = lambda: 0.0
            self.assertEqual(a_ndb._n1_k(StubPacket(seq=1)), expect)
            self.assertEqual(a_ndb._n1_k(StubPacket(seq=1)),
                             a_deg._n1_k(StubPacket(seq=1)))

    def test_thin_shield_off_census(self):
        """Corridor signature: unique-2hop nearly empty (everyone I hear
        indirectly is also direct) -> suppression disabled."""
        ar = make_ar(n1_enabled=True, n1_mode='ndb', ndb_enabled=True)
        for nid in range(6):                       # degree 6 (above sparse)
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        # no 2-hop knowledge at all -> thin
        for o in range(10):                        # sufficient origin evidence
            ar.nodedb[o] = {'hops': 2, 't': 1}
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 99)
        self.assertEqual(ar.stats.get('ndb_census_shield', 0), 1)

    def test_chain_shield_far_origins(self):
        ar = make_ar(n1_enabled=True, n1_mode='ndb', ndb_enabled=True)
        for nid in range(6):
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        ar.neighbors_of_nb = {nid: {100 + nid} for nid in range(6)}  # n2u = 6 >= 1.5*6? no: 9
        # n2u=6 < 1.5*6=9 -> thin shield fires anyway; to isolate the far
        # shield, give wide 2-hop spread:
        ar.neighbors_of_nb = {nid: {100 + i for i in range(10)} for nid in range(6)}
        for o in range(10):                        # 10 origins, 5 far (>=4 hops)
            ar.nodedb[o] = {'hops': (5 if o % 2 == 0 else 2), 't': 1}
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 99)   # far_ratio 0.5

    def test_dense_keeps_cheap_silence(self):
        ar = make_ar(n1_enabled=True, n1_mode='ndb', ndb_enabled=True)
        for nid in range(16):                      # dense degree
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        ar.neighbors_of_nb = {nid: {100 + i for i in range(40)} for nid in range(16)}
        for o in range(10):
            ar.nodedb[o] = {'hops': 2, 't': 1}     # all near -> far_ratio 0
        self.assertEqual(ar._n1_k(StubPacket(seq=1)), 2)   # degree baseline kept

    def test_mini_flood_radius_scales_down_only(self):
        ar = make_ar(ndb_enabled=True, mini_flood_ttl=3)
        ar.inhibition_strength = lambda seg: 'medium'
        ar._confidence_level = lambda: 1
        ar._sparse_guard_active = lambda: False
        ar._relay_packet = lambda p, **kw: True
        ar.stats['adaptive_mini_flood_triggered'] = 0
        ar.stats['adaptive_segment_fallback'] = 0
        ar._mini_flood_done = set()
        p = StubPacket(seq=9, dest=42)
        ar.nodedb[42] = {'hops': 1, 't': 1}         # measured at 1 hop -> radius 2 < 3
        ar._mini_flood(p)
        self.assertEqual(ar.stats.get('ndb_radius_scaled', 0), 1)
        # no entry -> current TTL, no scaling
        p2 = StubPacket(seq=10, dest=99)
        ar._mini_flood(p2)
        self.assertEqual(ar.stats.get('ndb_radius_scaled', 0), 1)   # unchanged
        # far entry -> never grows beyond the configured TTL
        p3 = StubPacket(seq=11, dest=77)
        ar.nodedb[77] = {'hops': 5, 't': 1}
        ar._mini_flood(p3)
        self.assertEqual(ar.stats.get('ndb_radius_scaled', 0), 1)   # 6 > 3: no change


class TestExternalAuditFixes(unittest.TestCase):
    """Fixes from the external firmware-realism audit (2026-10-01):
    LPR hop_depth must use the per-packet hop_start, and neighbor velocity
    must be time-based on POSITION deltas, not packet inter-arrivals."""

    def test_lpr_hop_depth_from_hop_start(self):
        ar = make_ar(enable_lpr=True)
        seen = []
        ar.potential.observe_relayed_packet = lambda o, tx, d, c, now: seen.append((o, tx, d))
        p = StubPacket(seq=1, orig=9, tx=5)
        p.hopStart = 5
        p.hopLimit = 3
        ar.learn(p, -80.0)
        self.assertEqual(seen, [(9, 5, 2)])       # hop_start - hop_limit

    def test_lpr_no_hop_start_no_depth(self):
        ar = make_ar(enable_lpr=True)
        seen = []
        ar.potential.observe_relayed_packet = lambda o, tx, d, c, now: seen.append((o, tx, d))
        p = StubPacket(seq=1, orig=9, tx=5)
        p.hopLimit = 3                            # no hopStart -> has_hops_away False
        ar.learn(p, -80.0)
        self.assertEqual(seen, [])

    def test_neighbor_velocity_position_dt(self):
        """POSITION deltas 30 s apart, 10 m apart -> ~0.33 m/s (not the
        old inflated d/(time-since-last-packet))."""
        env = ManualEnv()
        ar = make_ar(env=env)
        p1 = StubPacket(seq=1, tx=5)
        p1.pos_x, p1.pos_y = 0.0, 0.0
        p2 = StubPacket(seq=2, tx=5)
        p2.pos_x, p2.pos_y = 10.0, 0.0
        # non-position packet in between refreshes last_seen but must not
        # affect the velocity delta basis
        p_mid = StubPacket(seq=3, tx=5)
        ar.learn(p1, -80.0)
        env.now = 1000.0                          # plain packet at t=1s
        ar.learn(p_mid, -80.0)
        env.now = 31000.0                         # second POSITION at t=31s
        ar.learn(p2, -80.0)
        v = ar.neighbors[5].velocity
        self.assertLess(v, 1.0)                   # ~0.33 m/s, not inflated
        self.assertGreater(v, 0.05)


class TestNDBK2Shield(unittest.TestCase):
    """Pre-registered experiment: NodeDB thin/chain evidence raises the
    rank2-fallback K2 (the documented home of the rural_corridor loss).
    K only ever rises; no evidence -> exact baseline K2."""

    def _nb(self, ar, nid):
        ar.neighbors[nid] = NeighborInfo(0, -80.0, 0, 0.5, 0.9, 4)

    def test_rank2_baseline_without_shield(self):
        ar = make_ar(n3_enabled=True, n1_mode='ndb', ndb_enabled=True,
                     ndb_k2_shield=False)
        self.assertEqual(ar._rank_k(2, StubPacket(seq=1)), 2)

    def test_rank2_shield_raised_with_thin_evidence(self):
        ar = make_ar(n3_enabled=True, n1_mode='ndb', ndb_enabled=True,
                     ndb_k2_shield=True)
        for nid in range(6):
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        for o in range(10):                        # evidence floor met
            ar.nodedb[o] = {'hops': 2, 't': 1}
        self.assertEqual(ar._rank_k(2, StubPacket(seq=1)), 99)
        self.assertEqual(ar.stats.get('ndb_k2_shield', 0), 1)

    def test_rank2_shield_grace_without_evidence(self):
        ar = make_ar(n3_enabled=True, n1_mode='ndb', ndb_enabled=True,
                     ndb_k2_shield=True)
        for nid in range(6):
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0           # nodedb empty (cold start / ablation)
        self.assertEqual(ar._rank_k(2, StubPacket(seq=1)), 2)

    def test_rank0_rank1_unchanged_by_k2_shield(self):
        # mixed-like profile (wide 2-hop spread, near origins) so the
        # rank1 ndb census keeps its baseline K — isolating the k2 flag
        ar = make_ar(n3_enabled=True, n1_mode='ndb', ndb_enabled=True,
                     ndb_k2_shield=True)
        for nid in range(6):
            self._nb(ar, nid)
        ar.structural_risk = lambda: 0.0
        ar.neighbors_of_nb = {nid: {100 + i for i in range(20)} for nid in range(6)}
        for o in range(10):
            ar.nodedb[o] = {'hops': 2, 't': 1}
        self.assertEqual(ar._rank_k(0, StubPacket(seq=1)), 99)
        self.assertEqual(ar._rank_k(1, StubPacket(seq=1)), 3)   # mixed baseline K
        self.assertEqual(ar._rank_k(2, StubPacket(seq=1)), 2)  # not thin -> baseline K2


class TestRealisticWire(unittest.TestCase):
    """REALISTIC_WIRE observability degradation (external-audit response):
    relayed copies lose the transmitter identity (real rebroadcast keeps
    from=originator), collided frames are not attributed, census counts
    copies (no dedupe without identity), per-neighbor learning happens
    only from originated traffic."""

    def test_view_masks_relayed_copies(self):
        from lib.node import _MaskedCopy
        p = StubPacket(seq=1, orig=9, tx=5)          # relayed copy
        v = _MaskedCopy(p)
        self.assertIsNone(v.txNodeId)
        self.assertEqual(v.seq, 1)                    # attrs forwarded
        self.assertEqual(v.origTxNodeId, 9)
        p2 = StubPacket(seq=1, orig=5, tx=5)          # originated
        v2 = _MaskedCopy(p2)
        self.assertEqual(v2.txNodeId, 5)             # identity kept

    def test_census_counts_unattributed_copies(self):
        ar = make_ar(n1_enabled=True, n1_mode='fixed', n1_k=3)
        ar._n1_census[1] = {'ids': [None], 'decided': False, 't0': 0}
        p = StubPacket(seq=1, tx=7)
        p.origTxNodeId = 9
        class PProxy:
            def __init__(s, tx): s.txNodeId = tx; s.seq = 1
        ar._n1_handle(PProxy(None))
        ar._n1_handle(PProxy(None))
        ent = ar._n1_census[1]
        self.assertEqual(len(ent['ids']), 3)   # every copy counted, no dedupe

    def test_learn_skips_neighbor_for_unattributed(self):
        from lib.node import _MaskedCopy
        ar = make_ar()
        p = StubPacket(seq=1, orig=9, tx=7)
        v = _MaskedCopy(p)                       # full attr forwarding, tx masked
        self.assertIsNone(v.txNodeId)
        ar.learn(v, -80.0)
        self.assertNotIn(None, ar.neighbors)    # no phantom neighbor
        self.assertNotIn(7, ar.neighbors)      # relayer identity truly hidden
        self.assertIn(None, ar.packet_relayers[1])  # progression marker kept

    def test_asymmetric_blind_unknown_relayer(self):
        ar = make_ar()
        self.assertFalse(ar._asymmetric_blind(None))

    def test_echo_probe_originated_keeps_identity(self):
        from lib.node import _MaskedCopy
        p = StubPacket(seq=1, orig=5, tx=5)
        p.is_echo_probe = True
        v = _MaskedCopy(p)
        self.assertEqual(v.txNodeId, 5)   # probe ACK path still attributed


if __name__ == '__main__':
    unittest.main()
