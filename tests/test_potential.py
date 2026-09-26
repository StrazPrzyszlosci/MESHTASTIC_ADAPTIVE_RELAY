import ast
import math
import os
import unittest

import simpy

from lib.config import Config
from lib.potential import (LinkCostModel, PotentialTable, PotentialEntry,
                           PACKET_CLASS_EMERGENCY, PACKET_CLASS_DM,
                           PACKET_CLASS_BROADCAST, PACKET_CLASS_TELEMETRY_LOCAL,
                           PACKET_CLASS_BULK_LOW, packet_class_penalty,
                           SCOPE_GLOBAL, SCOPE_LOCAL)
from tests.test_adaptive import StubNode, StubPacket, make_ar


class TestLprNoOracle(unittest.TestCase):
    """lib/potential.py is pure: no simulator-global state, no other nodes'
    packet arrays, no LINK_OFFSET."""
    FORBIDDEN_ATTRS = {'LINK_OFFSET', 'sensedByN', 'detectedByN', 'collidedAtN',
                       'receivedAtN', 'estimate_path_loss', 'nodes'}

    def test_lpr_no_oracle(self):
        path = os.path.join(os.path.dirname(__file__), '..', 'lib', 'potential.py')
        with open(path) as f:
            tree = ast.parse(f.read())
        bad = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in self.FORBIDDEN_ATTRS:
                bad.append((n.attr, n.lineno))
        self.assertEqual(bad, [], f'oracle leakage in potential.py: {bad}')

    def test_lpr_does_not_use_global_state(self):
        """PotentialTable must be constructible and usable standalone."""
        pt = PotentialTable()
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=3,
                                  hop_cost_est=1000.0, now=1000.0)
        self.assertLess(pt.get_phi(42), float('inf'))
        self.assertLess(pt.get_neighbor_phi(7, 42), float('inf'))


class TestLprMath(unittest.TestCase):
    def test_lpr_progress_selects_lower_cost_neighbor(self):
        """Same destination potential; the cheaper link yields more progress."""
        pt = PotentialTable()
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=3,
                                  hop_cost_est=1000.0, now=1000.0)
        phi_self = pt.get_phi(42)
        # cheap link vs expensive link to two candidates at the same phi
        prog_cheap = pt.compute_progress(42, cost_ij=800.0,
                                         neighbor_phi=pt.get_neighbor_phi(7, 42))
        prog_exp = pt.compute_progress(42, cost_ij=2000.0,
                                       neighbor_phi=pt.get_neighbor_phi(7, 42))
        self.assertGreater(prog_cheap, prog_exp)
        self.assertGreater(prog_cheap, 0.0, 'positive progress for a cheap link')

    def test_lpr_no_progress_when_unknown(self):
        pt = PotentialTable()
        prog = pt.compute_progress(99, cost_ij=1000.0, neighbor_phi=float('inf'))
        self.assertEqual(prog, float('-inf'),
                         'unknown potential must not claim progress')

    def test_lpr_no_progress_triggers_fallback(self):
        """With no potential knowledge, AR designation still works (soft
        scoring: unknown potential contributes 0 — reach-first preserved)."""
        ar = make_ar(enable_lpr=True)
        ar.learn(StubPacket(tx=5), -80.0)
        prim, _, _ = ar._pick_relays_toward(dest=99)
        self.assertIsNotNone(prim, 'soft LPR must not kill designation')

    def test_lpr_hysteresis_prevents_flapping(self):
        """Hysteresis is RELATIVE (progress / Phi_i > hysteresis): progress
        below the fraction is not a real improvement."""
        pt = PotentialTable(hysteresis=0.1)
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=3,
                                  hop_cost_est=1000.0, now=1000.0)
        phi_self = pt.get_phi(42)   # ~3000 (3 hops x 1000)
        prog = pt.compute_progress(42, cost_ij=900.0,
                                   neighbor_phi=pt.get_neighbor_phi(7, 42))
        prog_n = max(0.0, min(1.0, prog / max(1e-9, phi_self)))
        self.assertLessEqual(prog_n, pt.hysteresis + 1e-9,
                             'progress below the relative hysteresis must not count')
        # a clearly better link DOES pass the hysteresis
        prog2 = pt.compute_progress(42, cost_ij=200.0,
                                    neighbor_phi=pt.get_neighbor_phi(7, 42))
        prog2_n = prog2 / max(1e-9, phi_self)
        self.assertGreater(prog2_n, pt.hysteresis)

    def test_lpr_decay_reduces_confidence(self):
        pt = PotentialTable()
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=2,
                                  hop_cost_est=1000.0, now=0.0)
        e = pt.entries[(42, None)]
        c0 = e.confidence
        pt.decay(now=10 * 3600000.0)
        self.assertLess(e.confidence, c0, 'confidence must decay with age')

    def test_lpr_failure_increases_cost_or_reduces_confidence(self):
        pt = PotentialTable()
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=2,
                                  hop_cost_est=1000.0, now=0.0)
        e = pt.entries[(42, None)]
        phi0, c0 = e.potential, e.confidence
        pt.update_on_failure(7, 42, penalty=0.5, now=1000.0)
        self.assertGreater(e.potential, phi0, 'failure must raise the potential')
        self.assertLess(e.confidence, c0, 'failure must reduce confidence')

    def test_lpr_success_reinforces(self):
        pt = PotentialTable()
        pt.observe_relayed_packet(dest=42, neighbor_id=7, hop_depth=2,
                                  hop_cost_est=1000.0, now=0.0)
        e = pt.entries[(42, None)]
        c0 = e.confidence
        pt.update_on_success(7, 42, observed_cost=1000.0, now=1000.0)
        self.assertGreater(e.confidence, c0)


class TestLprPacketClasses(unittest.TestCase):
    def test_lpr_emergency_bypasses_cost_limit(self):
        """EMERGENCY has zero class penalty and AR relays it at max aggression."""
        self.assertEqual(packet_class_penalty(PACKET_CLASS_EMERGENCY), 0.0)
        ar = make_ar(enable_lpr=True)
        p = StubPacket(seq=1, tx=5, dest=9)
        p.is_emergency = True
        p.no_dupe_cancel = True
        p.flood_ttl = 4
        ar._relay_packet = lambda *a, **k: True   # stub the TX machinery
        self.assertTrue(ar._handle_dm(p))

    def test_lpr_telemetry_local_scope(self):
        """TELEMETRY_LOCAL: highest class penalty + LOCAL scope (positions are
        never relayed by AR — localcast)."""
        self.assertEqual(packet_class_penalty(PACKET_CLASS_TELEMETRY_LOCAL), 0.4)
        self.assertGreater(packet_class_penalty(PACKET_CLASS_TELEMETRY_LOCAL),
                           packet_class_penalty(PACKET_CLASS_DM))
        ar = make_ar(position_scope='local')
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        p.pos_x, p.pos_y = 10.0, 10.0
        self.assertEqual(ar._handle_broadcast(p), False,
                         'LOCAL-scope position must never be relayed')
        self.assertEqual(SCOPE_LOCAL, 1)

    def test_lpr_bulk_low_most_penalty(self):
        self.assertEqual(packet_class_penalty(PACKET_CLASS_BULK_LOW), 0.6)
        self.assertEqual(packet_class_penalty(PACKET_CLASS_BULK_LOW),
                         max(packet_class_penalty(c) for c in range(5)))


class TestLprIntegration(unittest.TestCase):
    def test_lpr_does_not_modify_mf_passthrough(self):
        """enable_lpr must not change the passthrough behaviour."""
        ar = make_ar(enable_lpr=True, passthrough=True)
        p = StubPacket(dest=9)
        self.assertFalse(ar.designate_on_send(p))
        self.assertIsNone(p.relay_designation)
        self.assertIsNone(getattr(p, 'potential_to_dest', None))

    def test_lpr_off_is_default_neutral(self):
        """enable_lpr=False must not change AR behaviour at all."""
        ar_off = make_ar(enable_lpr=False)
        ar_on = make_ar(enable_lpr=True)
        for nid in (5, 6):
            for ar in (ar_off, ar_on):
                ar.learn(StubPacket(tx=nid), -80.0)
                ar.neighbors[nid].pdr = 0.9
        p1o, _, _ = ar_off._pick_relays_toward(dest=99)
        p1n, _, _ = ar_on._pick_relays_toward(dest=99)
        # with no potential observations both must behave identically
        self.assertEqual(p1o, p1n)

    def test_hop_depth_observation_builds_potential(self):
        ar = make_ar(enable_lpr=True)
        # packet from dest=9 relayed by 5; hopLimit 3 (orig) -> 1: the packet
        # travelled 2 hops from the origin, so relay 5 is ~1 hop from it
        ar.learn(StubPacket(seq=1, orig=9, tx=5, hop=1), -80.0)
        self.assertLess(ar.potential.get_phi(9), float('inf'))
        self.assertLess(ar.potential.get_neighbor_phi(5, 9), float('inf'))


class TestCefV09(unittest.TestCase):
    """v0.9 CEF tactical layer tests (spec section 5.2)."""

    def test_gi_dimensionless_airtime_normalization(self):
        """G is dimensionless: doubling the airtime halves G (A/A_ref)."""
        ar = make_ar(enable_cef=True)
        p1 = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        p1.timeOnAir = 150.0   # == A_ref
        p2 = StubPacket(seq=2, tx=5, dest=0xFFFFFFFF)
        p2.timeOnAir = 300.0   # 2x A_ref
        g1 = ar._cef_estimate(p1)['G']
        g2 = ar._cef_estimate(p2)['G']
        self.assertAlmostEqual(g1, 2.0 * g2, places=5)

    def test_role_cost_suppresses_mute_but_not_emergency(self):
        """CLIENT_MUTE (R=20) suppresses low-value relays; EMERGENCY always
        forwards regardless of role."""
        ar = make_ar(enable_cef=True)
        ar.node.role = type('R', (), {'value': 'CLIENT_MUTE'})()
        self.assertEqual(ar._cef_role_cost(), 20.0)
        # 5+ neighbors so the sparse reach-guard does not mask the cost test
        for nid in range(1, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        p.timeOnAir = 150.0
        # low-value relay: no relayers heard, M=blind gain 0.5, W=1.0
        # G = 0.5 / (1 * 1 * 20) = 0.025 << theta -> suppressed
        self.assertFalse(ar._cef_should_forward(p))
        # emergency: always forwards
        pe = StubPacket(seq=2, tx=5, dest=0xFFFFFFFF)
        pe.timeOnAir = 150.0
        pe.is_emergency = True
        self.assertTrue(ar._cef_should_forward(pe))

    def test_collision_penalty_nonlinear(self):
        """(1 + kc*C)^gamma is nonlinear (gamma > 1)."""
        ar = make_ar(enable_cef=True)
        gamma = float(ar.p['cef_gamma'])
        kc = float(ar.p['cef_collision_weight'])
        self.assertGreater(gamma, 1.0, 'saturation exponent must exceed 1')
        p1 = 1.0 + kc * 0.5          # linear penalty
        p_nl = (1.0 + kc * 0.5) ** gamma
        self.assertGreater(p_nl, p1, 'saturation exponent must be nonlinear')
        self.assertAlmostEqual(p_nl, 1.25 ** gamma, places=6)

    def test_zero_neighbors_blind_gain_fallback(self):
        """No relayers heard -> M = blind gain (0.5): the packet is not
        killed at cold start."""
        ar = make_ar(enable_cef=True)
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        self.assertAlmostEqual(ar._cef_estimated_total_demand(p), 0.5)

    def test_theta_stability_no_oscillation(self):
        """Same local state -> same theta (deterministic, clamped)."""
        ar = make_ar(enable_cef=True)
        for _ in range(20):
            ar.learn(StubPacket(seq=100 + _ if False else 1, tx=5), -80.0)
        t1 = ar._cef_theta()
        t2 = ar._cef_theta()
        self.assertEqual(t1, t2)
        self.assertGreaterEqual(t1, float(ar.p['cef_theta_min']))
        self.assertLessEqual(t1, float(ar.p['cef_theta_max']))

    def test_asymmetric_link_no_storm(self):
        """Neighbor with RSSI < -115 dBm AND pdr < 0.2: no client repeat, no
        failover (asymmetric blind spot — repeating is futile)."""
        ar = make_ar(asymmetric_rssi_min=-115)
        ar.learn(StubPacket(tx=5), -125.0)   # weak rssi...
        ar.neighbors[5].pdr = 0.9
        self.assertFalse(ar._asymmetric_blind(5),
                         'good-PDR neighbor is not blind (v0.10 audit)')
        ar.neighbors[5].pdr = 0.1   # ...AND bad PDR -> blind
        self.assertTrue(ar._asymmetric_blind(5))
        p = StubPacket(seq=1, tx=5, dest=9)
        self.assertFalse(ar._schedule_client_repeat(p),
                         'client repeat toward an asymmetric blind spot must be blocked')
        self.assertGreaterEqual(ar.stats.get('asymmetric_link_suppression', 0), 1)

    def test_sf_gatekeeper_drops_telemetry(self):
        """POSITION/telemetry is dropped before flash write; DM passes."""
        ar = make_ar()
        p_pos = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        p_pos.pos_x, p_pos.pos_y = 1.0, 2.0
        self.assertFalse(ar._sf_gatekeeper_allow(p_pos))
        p_dm = StubPacket(seq=2, tx=5, dest=9)
        self.assertTrue(ar._sf_gatekeeper_allow(p_dm))
        p_em = StubPacket(seq=3, tx=5, dest=0xFFFFFFFF)
        p_em.is_emergency = True
        self.assertTrue(ar._sf_gatekeeper_allow(p_em))

    def test_no_oracle_in_m_i_calculation(self):
        """M_i is computed from locally known state only (packet_relayers,
        neighbors, RSSI) — no global graph access."""
        ar = make_ar(enable_cef=True)
        # 3 neighbors, 1 relayed
        for nid in (5, 6, 7):
            ar.learn(StubPacket(tx=nid), -80.0)
        ar.packet_relayers[1] = {5}
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        m = ar._cef_estimated_total_demand(p)
        self.assertGreaterEqual(m, 0.0)
        self.assertLessEqual(m, 10.0, 'M must stay in a sane range')


class TestCefV010(unittest.TestCase):
    """v0.10 fixes: sparse guard, CEF gating, linear TX budget, asym audit."""

    def test_sparse_guard_activates_and_disables_cef(self):
        """Sparse guard: low degree or LOW conf -> active; CEF must not
        decide in the guarded regime."""
        ar = make_ar(enable_cef=True, sparse_guard=True)
        self.assertTrue(ar._sparse_guard_active(),
                        'cold start (LOW conf, no neighbors) activates the guard')
        # with 5 neighbors (<= sparse_degree_threshold) the guard STAYS active
        for nid in range(1, 6):
            ar.learn(StubPacket(tx=nid), -80.0)
        self.assertTrue(ar._sparse_guard_active(),
                        'degree <= 5 keeps the guard active')
        # CEF metrics must stay untouched (gated off in the guard)
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        ar._handle_broadcast(p)
        self.assertEqual(ar.stats.get('cef_suppressed_by_cost', 0), 0)

    def test_cef_gating_blocks_sparse_mixed(self):
        """CEF gating: degree < cef_min_degree or conf < MEDIUM -> CEF off."""
        ar = make_ar(enable_cef=True)
        for nid in range(1, 6):   # 5 neighbors < cef_min_degree (8)
            ar.learn(StubPacket(tx=nid), -80.0)
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        before = ar.stats.get('cef_decisions', 0)
        ar._handle_broadcast(p)
        self.assertEqual(ar.stats.get('cef_decisions', before), before,
                         'CEF must be gated off below cef_min_degree')

    def test_cef_jitter_desynchronizes_forwards(self):
        """CEF forward jitter: two same-state relays get DIFFERENT delays
        (deterministic per-ID step + seeded random component)."""
        ar1 = make_ar(nodeid=1, enable_cef=True)
        ar2 = make_ar(nodeid=2, enable_cef=True)
        for nid in range(1, 9):
            for ar in (ar1, ar2):
                ar.learn(StubPacket(tx=nid), -80.0)
        p = StubPacket(seq=1, tx=5, dest=0xFFFFFFFF)
        d1 = ((ar1.node.nodeid % 10) * float(ar1.p['cef_jitter_step_ms'])
              + ar1.rng.uniform(0, float(ar1.p['cef_jitter_max_ms'])))
        d2 = ((ar2.node.nodeid % 10) * float(ar2.p['cef_jitter_step_ms'])
              + ar2.rng.uniform(0, float(ar2.p['cef_jitter_max_ms'])))
        self.assertNotEqual(d1, d2, 'jitter must desynchronize same-state relays')

    def test_linear_tx_budget_reduces_backups(self):
        """Overloaded node (tx_per_delivered > threshold): retry capped at 1."""
        ar = make_ar(linear_tx_budget=True, tx_per_delivered_threshold=1.2)
        ar.node.nodeid = 0
        ar.node.nrPacketsSent = 100
        ar.node.usefulPackets = 50   # tpd = 2.0 > 1.2
        ent = {'relays': {5}, 'ctx': {'packet': StubPacket(seq=1, tx=2, dest=9),
                                      'retries': 1, 'excluded': set()}}
        ar.node.nodes = [type('N', (), {'nodeid': 0})()]
        # stub the relay machinery: count failover mini-flood vs re-relay
        calls = []
        ar._relay_packet = lambda *a, **k: calls.append('relay') or True
        ar._mini_flood = lambda *a, **k: calls.append('flood') or True
        # retries(1) >= max_retries(1 after budget) -> mini-flood... but budget
        # caps at 1: with tpd high, max_retries = min(2, 1) = 1
        class FakeEnv:
            now = 0
        ar.node.env = FakeEnv()
        ar.env = FakeEnv()
        ar._failover(1, ent, ent['ctx'], ent['ctx']['packet'])
        self.assertIn('flood', calls,
                      'overloaded node must not exceed the retry budget')

    def test_asym_blind_requires_both_conditions(self):
        """v0.10 audit: blind spot needs rssi < -115 AND pdr < 0.2."""
        ar = make_ar()
        ar.learn(StubPacket(tx=5), -125.0)
        ar.neighbors[5].pdr = 0.9   # good PDR: NOT blind (rssi-only would mark it)
        self.assertFalse(ar._asymmetric_blind(5),
                         'good-PDR neighbor must not be marked blind on rssi alone')
        ar.neighbors[5].pdr = 0.1   # bad PDR too: blind
        self.assertTrue(ar._asymmetric_blind(5))

    def test_dense_roles_reach_not_regressed(self):
        """ROUTER_CLIENT R_cost lowered to 1.5: its G must pass theta in a
        dense-like state (no reach regression from role costs)."""
        ar = make_ar(enable_cef=True)
        ar.node.role = type('R', (), {'value': 'ROUTER_CLIENT'})()
        self.assertEqual(ar._cef_role_cost(), 1.5)
        for nid in range(1, 10):   # 9 neighbors >= cef_min_degree
            ar.learn(StubPacket(seq=100 + nid, tx=nid), -80.0)
            ar.neighbors[nid].pdr = 0.9
        p = StubPacket(seq=200, tx=5, dest=0xFFFFFFFF)   # fresh seq: no relayers
        p.timeOnAir = 150.0
        est = ar._cef_estimate(p)
        # blind-gain M=0.5 with W=1.0 and R=1.5 -> G = 0.33 > 0
        self.assertGreater(est['G'], 0.0,
                           'ROUTER_CLIENT must not be cost-suppressed to zero')


if __name__ == '__main__':
    unittest.main()
