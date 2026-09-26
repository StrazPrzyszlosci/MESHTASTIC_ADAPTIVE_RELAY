#!/usr/bin/env python3
"""Local Potential Relay (LPR) for ADAPTIVE_RELAY (v0.8).

Local potential-field routing: every node keeps a LOCAL map of expected
delivery cost (potential) to destinations/zones. A packet "flows down the
potential gradient" — the Bellman equation computed LOCALLY, no global
Dijkstra, no oracle:

    Phi_i(d) = min_j ( c_ij + Phi_hat_j(d) )

Forwarding rule (reach-first):

    progress_ij(d) = Phi_i(d) - (c_ij + Phi_hat_j(d))
    forward via j  iff  progress_ij(d) > hysteresis

Link cost (physically meaningful for LoRa — expected airtime of a SUCCESSFUL
delivery over one link):

    c_ij = airtime_ij * ETX_ij * (1 + kw_q * congestion)
           + kw_e * battery_penalty + kw_u * uncertainty
           + kw_p * packet_class_penalty

Potential estimates come ONLY from locally observable evidence:
  - hop-depth of packets originated at d relayed by neighbor j
    (hop_depth = orig hopLimit - p.hopLimit = hops travelled from origin),
  - watchdog ACK/echo outcomes (update_on_success / update_on_failure),
  - decay with age (confidence),
  - optional trickle beacons (future).

NO-ORACLE RULE: this module is pure — it never reads simulator-global state,
other nodes' state, or packet arrays of other nodes. Enforced by
tests/test_adaptive.py style AST scan (test_lpr_no_oracle).
"""
import math

# packet classes (utility ordering: emergency > DM > broadcast > telemetry)
PACKET_CLASS_EMERGENCY = 0
PACKET_CLASS_DM = 1
PACKET_CLASS_BROADCAST = 2
PACKET_CLASS_TELEMETRY_LOCAL = 3
PACKET_CLASS_BULK_LOW = 4

# scope values
SCOPE_GLOBAL = 0
SCOPE_LOCAL = 1


class LinkCostModel:
    """Local link cost: airtime x ETX with congestion/battery/uncertainty/
    class corrections. All weights configurable (AR_PARAMS passthrough)."""

    def __init__(self, congestion_weight=0.5, battery_weight=0.3,
                 uncertainty_weight=0.3, class_weight=0.2,
                 mobility_weight=0.05):
        self.congestion_weight = congestion_weight
        self.battery_weight = battery_weight
        self.uncertainty_weight = uncertainty_weight
        self.class_weight = class_weight
        self.mobility_weight = mobility_weight

    def link_cost(self, airtime_est, etx, congestion=0.0, battery_penalty=0.0,
                  uncertainty=0.0, packet_class_penalty=0.0, rssi_slope=0.0):
        """Expected airtime needed for a SUCCESSFUL delivery over one link.
        v0.8b: mobility penalty from the RSSI derivative (dRSSI/dt) —
        predicts link expiration (Link Expiration Time) WITHOUT Doppler
        (CFO makes Doppler unusable on SX1262; the RSSI slope is free and
        locally computable from every received packet)."""
        cost = float(airtime_est) * float(etx)
        cost *= 1.0 + self.congestion_weight * max(0.0, congestion)
        cost += self.battery_weight * max(0.0, battery_penalty)
        cost += self.uncertainty_weight * max(0.0, uncertainty)
        cost += self.class_weight * max(0.0, packet_class_penalty)
        # separating link: kappa_mob * max(0, -V_RSSI)^2 (dBm/s scale /10)
        separating = max(0.0, -float(rssi_slope))
        cost += self.mobility_weight * (separating ** 2) / 10.0
        return cost


class PotentialEntry:
    """Local potential estimate for one (destination, zone)."""
    __slots__ = ('destination_id', 'zone_id', 'potential', 'confidence',
                 'next_hop_candidates', 'last_update', 'last_success',
                 'last_failure', 'flap_penalty', 'candidate_count',
                 'observations')

    def __init__(self, destination_id, zone_id=None, now=0.0):
        self.destination_id = destination_id
        self.zone_id = zone_id
        self.potential = float('inf')   # unknown until observed
        self.confidence = 0.0
        self.next_hop_candidates = []
        self.last_update = now
        self.last_success = None
        self.last_failure = None
        self.flap_penalty = 0.0
        self.candidate_count = 0
        self.observations = 0


class PotentialTable:
    """Per-node table of local potential estimates."""

    def __init__(self, decay_lambda=0.05, hysteresis=0.1,
                 cost_model=None):
        self.entries = {}   # (dest, zone) -> PotentialEntry
        self.decay_lambda = decay_lambda
        self.hysteresis = hysteresis
        self.cost_model = cost_model or LinkCostModel()

    # ------------------------------------------------------------------
    def _entry(self, dest, zone_id=None, now=0.0):
        key = (dest, zone_id)
        e = self.entries.get(key)
        if e is None:
            e = PotentialEntry(dest, zone_id, now)
            self.entries[key] = e
        return e

    def get_phi(self, dest, zone_id=None, now=0.0):
        """My estimated minimal cost from ME to dest (inf when unknown)."""
        e = self.entries.get((dest, zone_id))
        if e is None:
            return float('inf')
        # decay: stale knowledge loses confidence but keeps its estimate
        return e.potential

    def get_neighbor_phi(self, neighbor_id, dest, zone_id=None):
        """Phi_hat_j(d): the neighbor's estimated cost to dest.
        UNKNOWN (inf) when never observed — no progress, fallback."""
        e = self.entries.get((dest, zone_id))
        if e is None:
            return float('inf')
        nbc = e.next_hop_candidates or {}
        return nbc.get(neighbor_id, float('inf'))

    # ------------------------------------------------------------------
    def observe_relayed_packet(self, dest, neighbor_id, hop_depth, hop_cost_est, now):
        """Passive observation: a packet originated at `dest` was relayed by
        `neighbor_id` and reached me at `hop_depth` hops from the origin.
        Local distance-vector estimate:
            my cost to dest      ~ hop_depth * hop_cost
            neighbor cost to dest ~ (hop_depth - 1) * hop_cost
        """
        e = self._entry(dest, None, now)
        nb_phi = max(0.0, (hop_depth - 1)) * hop_cost_est
        e.next_hop_candidates = getattr(e, 'next_hop_candidates', None) or {}
        prev = e.next_hop_candidates.get(neighbor_id, float('inf'))
        # EWMA on the neighbor's potential (fresh observations dominate)
        new = _phi_ewma(prev, nb_phi, 0.3)
        e.next_hop_candidates[neighbor_id] = new
        # my own potential: one hop beyond the best neighbor
        best = min(e.next_hop_candidates.values())
        my_phi = best + hop_cost_est
        e.potential = _phi_ewma(e.potential, my_phi, 0.3) \
            if e.potential != float('inf') else my_phi
        e.observations += 1
        e.confidence = min(1.0, e.observations / 10.0)
        e.last_update = now

    def update_on_success(self, neighbor_id, dest, observed_cost, now):
        """Watchdog confirmed the relay forwarded: reinforce, raise confidence."""
        e = self.entries.get((dest, None))
        if e is None:
            e = self._entry(dest, None, now)
        e.last_success = now
        e.confidence = min(1.0, e.confidence + 0.1)
        e.flap_penalty = max(0.0, e.flap_penalty - 0.1)
        if observed_cost is not None and e.potential != float('inf'):
            e.potential = _phi_ewma(e.potential, observed_cost, 0.2)

    def update_on_failure(self, neighbor_id, dest, penalty, now):
        """Watchdog failure: confidence drops, potential rises (route worse)."""
        e = self.entries.get((dest, None))
        if e is None:
            return
        e.last_failure = now
        e.confidence = max(0.0, e.confidence - 0.2)
        e.flap_penalty = min(1.0, e.flap_penalty + penalty)
        e.potential = e.potential * (1.0 + 0.1 * penalty) \
            if e.potential != float('inf') else e.potential

    def decay(self, now):
        """Slow decay of confidence with age; potential uncertainty grows."""
        for e in self.entries.values():
            dt_h = max(0.0, now - e.last_update) / 3600000.0
            if dt_h <= 0:
                continue
            f = math.exp(-self.decay_lambda * dt_h)
            e.confidence *= f
            if e.potential != float('inf'):
                e.potential = e.potential * (1.0 + (1.0 - f) * 0.1)

    def detect_residual(self, dest, neighbor_phis, my_phi):
        """Inconsistency sensor: r_i = Phi_i - min_j(c_ij + Phi_hat_j).
        Large residual -> stale/inconsistent knowledge -> probe/fallback."""
        if my_phi == float('inf'):
            return float('inf')
        best = min(neighbor_phis) if neighbor_phis else float('inf')
        if best == float('inf'):
            return float('inf')
        return my_phi - best

    def compute_progress(self, dest, cost_ij, neighbor_phi, now=0.0):
        """progress_ij(d) = Phi_i(d) - (c_ij + Phi_hat_j(d)).
        Units: same as the cost model (ms-scale). The HYSTERESIS is applied
        by the caller as a RELATIVE threshold (progress / Phi_i > hysteresis),
        because absolute thresholds are meaningless across cost scales."""
        phi_self = self.get_phi(dest, now=now)
        if phi_self == float('inf') or neighbor_phi == float('inf'):
            return float('-inf')   # unknown potential -> no progress claim
        return phi_self - (cost_ij + neighbor_phi)


def _phi_ewma(old, sample, alpha):
    if old == float('inf'):
        return sample
    return old * (1.0 - alpha) + sample * alpha


def packet_class_penalty(packet_class):
    """Higher penalty for lower-priority classes (cost-sensitivity)."""
    return {PACKET_CLASS_EMERGENCY: 0.0,
            PACKET_CLASS_DM: 0.1,
            PACKET_CLASS_BROADCAST: 0.2,
            PACKET_CLASS_TELEMETRY_LOCAL: 0.4,
            PACKET_CLASS_BULK_LOW: 0.6}.get(packet_class, 0.2)
