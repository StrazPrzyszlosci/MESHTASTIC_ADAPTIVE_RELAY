#!/usr/bin/env python3
"""Deterministic scenario generators for the ADAPTIVE_RELAY benchmark.

Every generator is a pure function of (conf, seed): given the same seed it
returns identical node placements and event schedules, so MANAGED_FLOOD
and ADAPTIVE_RELAY always run on bit-identical topologies.

A scenario returns:
    node_configs : [NodeConfig]
    meta : dict    # reporting/harness-only data (NEVER readable by router code)
                   # e.g. enemy/bridge ids, planned failure events, avg neighbors
"""
import math
import random

from lib.common import find_random_position
from lib.node import NodeConfig
from lib.phy import MAXRANGE
from lib.point import Point

DEFAULT_SPACING_FRAC = 0.60   # fraction of MAXRANGE used for chain spacing


# ====================================================================
# v0.14 S3: OOD topology generators (out-of-distribution stress family)
# Doctrine: generators ONLY — no router-side scenario logic; every node
# placement is a pure function of (conf, seed) via a LOCAL rng so the
# global random stream (used by the existing generators) is untouched.
# ====================================================================
def _ood_rng(seed, tag):
    return random.Random(seed * 1_000_003 + tag)


def _chain(conf, rng, x0, y0, x1, y1, count, jitter_frac=0.15):
    """Nodes evenly spaced along a segment with perpendicular jitter."""
    cfgs = []
    for i in range(count):
        t = (i + 1) / (count + 1)
        j = (rng.random() - 0.5) * 2 * MAXRANGE * jitter_frac
        dx, dy = x1 - x0, y1 - y0
        n = math.hypot(dx, dy) or 1.0
        cfgs.append(_mkconf(conf, None, x0 + dx * t - dy * j / n,
                            y0 + dy * t + dx * j / n))
    return cfgs


def scn_double_bridge(conf, seed, n):
    """Two dense clusters joined by TWO independent thin bridge chains
    (alternative paths; killing one bridge leaves the other)."""
    rng = _ood_rng(seed, 17)
    side = 0.30 * MAXRANGE
    ax, ay = -2.0 * MAXRANGE, 0.0
    bx, by = 2.0 * MAXRANGE, 0.0
    cfgs = []
    for i in range(8):
        cfgs.append(_mkconf(conf, None, ax + rng.uniform(-side, side),
                            ay + rng.uniform(-side, side)))
    for i in range(8):
        cfgs.append(_mkconf(conf, None, bx + rng.uniform(-side, side),
                            by + rng.uniform(-side, side)))
    # bridge 1: straight through the middle
    cfgs += _chain(conf, rng, ax, ay, bx, by, 4)
    # bridge 2: a wide detour through the north
    cfgs += _chain(conf, rng, ax, ay, bx, 4.5 * MAXRANGE, 5)
    # two anchor endpoints inside the clusters so chains really connect
    return _assign_ids(cfgs), {}


def scn_cluster_chain(conf, seed, n):
    """6 dense communities in a line, connected by thin 2-3 node corridors."""
    rng = _ood_rng(seed, 29)
    cfgs = []
    x = -6.0 * MAXRANGE
    prev = None
    for c in range(6):
        cy = (rng.random() - 0.5) * 6 * MAXRANGE
        members = [_mkconf(conf, None,
                           x + rng.uniform(-0.3, 0.3) * MAXRANGE,
                           cy + rng.uniform(-0.3, 0.3) * MAXRANGE)
                   for _ in range(6)]
        cfgs += members
        center = (x, cy)
        if prev is not None:
            cfgs += _chain(conf, rng, prev[0], prev[1], center[0], center[1],
                           2 + (c % 2))
        prev = center
        x += 2.4 * MAXRANGE
    return _assign_ids(cfgs), {}


def scn_rural_corridor(conf, seed, n):
    """Nodes strung along a winding road with occasional side spurs."""
    rng = _ood_rng(seed, 41)
    cfgs = []
    x, y = -7.0 * MAXRANGE, 0.0
    heading = 0.0
    for i in range(30):
        x += MAXRANGE * DEFAULT_SPACING_FRAC * math.cos(heading)
        y += MAXRANGE * DEFAULT_SPACING_FRAC * math.sin(heading)
        j = (rng.random() - 0.5) * 0.2 * MAXRANGE
        cfgs.append(_mkconf(conf, None, x + math.cos(heading + math.pi / 2) * j,
                            y + math.sin(heading + math.pi / 2) * j))
        heading += (rng.random() - 0.5) * 0.9
        if i % 9 == 4:   # a spur off the road
            sh = heading + (math.pi / 2 if rng.random() < 0.5 else -math.pi / 2)
            sx, sy = x, y
            for _ in range(2):
                sx += MAXRANGE * DEFAULT_SPACING_FRAC * math.cos(sh)
                sy += MAXRANGE * DEFAULT_SPACING_FRAC * math.sin(sh)
                cfgs.append(_mkconf(conf, None, sx, sy))
    return _assign_ids(cfgs), {}


def scn_ring_with_spurs(conf, seed, n):
    """A ring of 16 backbone nodes with 2-node spurs every ~3 hops."""
    rng = _ood_rng(seed, 53)
    R = 2.2 * MAXRANGE
    cfgs = []
    for i in range(16):
        a = 2 * math.pi * i / 16
        j = rng.uniform(-0.1, 0.1) * MAXRANGE
        cfgs.append(_mkconf(conf, None,
                            (R + j) * math.cos(a), (R + j) * math.sin(a)))
        if i % 3 == 2:
            nx, ny = (R + j) * math.cos(a), (R + j) * math.sin(a)
            for k in (1, 2):
                d = (R + 1.2 * k * MAXRANGE * DEFAULT_SPACING_FRAC)
                cfgs.append(_mkconf(conf, None, d * math.cos(a + 0.25),
                                    d * math.sin(a + 0.25)))
    return _assign_ids(cfgs), {}


def scn_grid(conf, seed, n):
    """Urban 7x7 grid, spacing ~0.7 MAXRANGE (diagonals out of range)."""
    rng = _ood_rng(seed, 67)
    s = 0.70 * MAXRANGE
    cfgs = []
    for r in range(7):
        for c in range(7):
            jx = (rng.random() - 0.5) * 0.08 * MAXRANGE
            jy = (rng.random() - 0.5) * 0.08 * MAXRANGE
            cfgs.append(_mkconf(conf, None, c * s + jx, r * s + jy))
    return _assign_ids(cfgs), {}


def scn_core_plus_remote(conf, seed, n):
    """Dense city core plus remote single nodes hanging off relay spokes."""
    rng = _ood_rng(seed, 79)
    cfgs = []
    side = 0.35 * MAXRANGE
    for _ in range(16):
        cfgs.append(_mkconf(conf, None, rng.uniform(-side, side),
                            rng.uniform(-side, side)))
    for k in range(4):
        a = 2 * math.pi * (k + 0.5) / 4
        relay = _mkconf(conf, None,
                        1.3 * MAXRANGE * math.cos(a),
                        1.3 * MAXRANGE * math.sin(a))
        cfgs.append(relay)
        d = 2.5 * MAXRANGE
        cfgs.append(_mkconf(conf, None, d * math.cos(a), d * math.sin(a)))
    return _assign_ids(cfgs), {}


def scn_two_dense_sparse_middle(conf, seed, n):
    """Two dense blobs with a wide sparse scatter bridging them."""
    rng = _ood_rng(seed, 83)
    cfgs = []
    for cx in (-3.2, 3.2):
        for _ in range(12):
            cfgs.append(_mkconf(conf, None,
                                cx * MAXRANGE + rng.uniform(-0.35, 0.35) * MAXRANGE,
                                rng.uniform(-0.35, 0.35) * MAXRANGE))
    for _ in range(10):
        cfgs.append(_mkconf(conf, None,
                            rng.uniform(-2.4, 2.4) * MAXRANGE,
                            rng.uniform(-2.2, 2.2) * MAXRANGE))
    return _assign_ids(cfgs), {}


def scn_random_variable_density(conf, seed, n):
    """Random geometric graph with LOCALLY variable density: 3 dense hot
    patches + a large sparse hinterland; counts per patch drawn per seed."""
    rng = _ood_rng(seed, 97)
    cfgs = []
    patches = [(rng.uniform(-3, 3), rng.uniform(-3, 3)) for _ in range(3)]
    hot = rng.sample(range(n), k=rng.randint(12, 18))
    for i in range(n):
        if i in hot:
            px, py = patches[i % len(patches)]
            cfgs.append(_mkconf(conf, None,
                                px * MAXRANGE + rng.uniform(-0.4, 0.4) * MAXRANGE,
                                py * MAXRANGE + rng.uniform(-0.4, 0.4) * MAXRANGE))
        else:
            cfgs.append(_mkconf(conf, None,
                                rng.uniform(-5, 5) * MAXRANGE,
                                rng.uniform(-5, 5) * MAXRANGE))
    return _assign_ids(cfgs), {}


def _assign_ids(cfgs):
    for i, c in enumerate(cfgs):
        c.node_id = i
    return cfgs


def _mkconf(conf, node_id, x, y, z=None):
    return NodeConfig(node_id, Point(x, y, z if z is not None else conf.HM),
                      conf.PERIOD, conf.PTX, conf.FREQ,
                      antenna_gain=conf.GL, hop_limit=conf.hopLimit)


def _avg_neighbors(conf, node_configs):
    """Reporting-only: mean number of in-range neighbors at t=0 (oracle, report)."""
    from lib.phy import estimate_path_loss
    pts = [nc.position for nc in node_configs]
    counts = []
    sens = conf.current_preset["sensitivity"]
    for i, a in enumerate(pts):
        c = 0
        for j, b in enumerate(pts):
            if i == j:
                continue
            d = a.euclidean_distance(b)
            pl = estimate_path_loss(conf, d, conf.FREQ, a.z, b.z)
            if conf.PTX + 2 * conf.GL - pl >= sens:
                c += 1
        counts.append(c)
    return sum(counts) / max(1, len(counts))


# ----------------------------------------------------------------------
# individual topologies
# ----------------------------------------------------------------------

def scn_random(conf, seed, n):
    """Baseline: default Meshtasticator placement (every node >= 1 link)."""
    rng = random.Random(seed * 7919 + 13)
    old_state = random.getstate()
    random.seed(seed * 7919 + 13)
    node_configs = []
    for i in range(n):
        x, y = find_random_position(conf, node_configs)
        node_configs.append(_mkconf(conf, i, x, y))
    random.setstate(old_state)
    return node_configs, {}


def scn_sparse(conf, seed, n):
    """Sparse: n nodes over the full default area (15x15 km)."""
    return scn_random(conf, seed, n)


def scn_dense(conf, seed, n, side=3500.0):
    """Cluster: n nodes in a side x side km square, always >=1 link each."""
    rng = random.Random(seed * 104729 + 7)
    node_configs = []
    tries = 0
    while len(node_configs) < n and tries < 20000:
        tries += 1
        x = rng.uniform(-side / 2, side / 2)
        y = rng.uniform(-side / 2, side / 2)
        cand = Point(x, y, conf.HM)
        if all(cand.euclidean_distance(nc.position) >= conf.MINDIST for nc in node_configs):
            if not node_configs or any(
                    conf.PTX + 2 * conf.GL - __import__('lib.phy', fromlist=['estimate_path_loss'])
                    .estimate_path_loss(conf, cand.euclidean_distance(nc.position), conf.FREQ, conf.HM, nc.position.z)
                    >= conf.current_preset["sensitivity"] for nc in node_configs):
                node_configs.append(_mkconf(conf, len(node_configs), x, y))
    return node_configs, {'area_side': side}


def scn_linear(conf, seed, n, spacing_frac=DEFAULT_SPACING_FRAC):
    """Linear chain: node i hears ~i+-1. Deterministic layout (gentle sine)."""
    from lib.phy import MAXRANGE
    s = MAXRANGE * spacing_frac
    node_configs = []
    for i in range(n):
        x = (i - (n - 1) / 2) * s
        y = math.sin(i * 0.8) * s * 0.08   # not a perfect straight ruler
        node_configs.append(_mkconf(conf, i, x, y))
    return node_configs, {'spacing_m': s, 'endpoints': (0, n - 1)}


def scn_bridge(conf, seed, n_per_cluster=12, n_bridges=1):
    """Two dense clusters connected only via dedicated bridge node(s)."""
    rng = random.Random(seed * 31 + 5)
    from lib.phy import MAXRANGE
    r_cluster = MAXRANGE * 0.38
    d_center = MAXRANGE * 1.5          # cluster center distance
    node_configs = []
    nid = 0
    centers = [(-d_center / 2, 0.0), (d_center / 2, 0.0)]
    for cx, cy in centers:
        placed = 0
        while placed < n_per_cluster:
            x = cx + rng.uniform(-r_cluster, r_cluster)
            y = cy + rng.uniform(-r_cluster, r_cluster)
            cand = Point(x, y, conf.HM)
            if all(cand.euclidean_distance(nc.position) >= conf.MINDIST for nc in node_configs):
                node_configs.append(_mkconf(conf, nid, x, y))
                nid += 1
                placed += 1
    # bridge nodes on the connecting line
    bridge_ids = []
    for k in range(n_bridges):
        off = (k - (n_bridges - 1) / 2) * MAXRANGE * 0.15
        x = 0.0 + off
        y = rng.uniform(-MAXRANGE * 0.05, MAXRANGE * 0.05)
        node_configs.append(_mkconf(conf, nid, x, y))
        bridge_ids.append(nid)
        nid += 1
    return node_configs, {'bridge_ids': bridge_ids, 'n_per_cluster': n_per_cluster,
                          'clusters': (list(range(0, n_per_cluster)),
                                       list(range(n_per_cluster, 2 * n_per_cluster)))}


def scn_hub(conf, seed, n_ring=10, n_leaves=19):
    """Hub / mountain node: center + ring + chains of leaves behind some ring nodes."""
    from lib.phy import MAXRANGE
    rng = random.Random(seed * 83 + 11)
    node_configs = [_mkconf(conf, 0, 0.0, 0.0)]
    nid = 1
    ring_r = MAXRANGE * 0.55
    ring_ids = []
    for i in range(n_ring):
        a = 2 * math.pi * i / n_ring
        x = ring_r * math.cos(a)
        y = ring_r * math.sin(a)
        node_configs.append(_mkconf(conf, nid, x, y))
        ring_ids.append(nid)
        nid += 1
    # leaves: chains extending outward behind a subset of ring nodes
    per_chain = max(1, n_leaves // max(1, n_ring // 2))
    step = MAXRANGE * 0.5
    leaf_source = ring_ids[::2]
    li = 0
    for src in leaf_source:
        a = 2 * math.pi * ring_ids.index(src) / n_ring
        for k in range(1, per_chain + 1):
            if nid >= 1 + n_ring + n_leaves:
                break
            jitter = rng.uniform(-0.05, 0.05)
            x = (ring_r + k * step) * math.cos(a + jitter)
            y = (ring_r + k * step) * math.sin(a + jitter)
            node_configs.append(_mkconf(conf, nid, x, y))
            nid += 1
            li += 1
    return node_configs, {'hub_id': 0, 'ring_ids': ring_ids}


def scn_topo_distributed(conf, seed):
    """X.13: A-B-C, B-D-E-F, F-G-H — nobody sees everything."""
    from lib.phy import MAXRANGE
    s = MAXRANGE * 0.55
    coords = {
        'A': (-2.5 * s, 0.0), 'B': (-1.5 * s, 0.0), 'C': (-0.5 * s, 0.0),
        'D': (-1.5 * s, -0.9 * s), 'E': (-0.5 * s, -1.7 * s), 'F': (0.5 * s, -2.3 * s),
        'G': (1.5 * s, -2.8 * s), 'H': (2.5 * s, -3.3 * s),
    }
    node_configs = []
    for i, (name, (x, y)) in enumerate(sorted(coords.items())):
        node_configs.append(_mkconf(conf, i, x, y))
    return node_configs, {'labels': coords}


def scn_dense_core(conf, seed, n_core=20, n_edge=10):
    """Dense center + sparse chains on the rim."""
    rng = random.Random(seed * 131 + 3)
    from lib.phy import MAXRANGE
    core, _ = scn_dense(conf, seed, n_core, side=MAXRANGE * 0.8)
    node_configs = list(core)
    nid = len(node_configs)
    n_dirs = min(4, n_edge)
    per = max(1, n_edge // n_dirs)
    for k in range(n_dirs):
        a = 2 * math.pi * k / n_dirs + rng.uniform(-0.2, 0.2)
        for j in range(1, per + 1):
            if nid >= n_core + n_edge:
                break
            r = MAXRANGE * 0.6 + j * MAXRANGE * 0.5
            node_configs.append(_mkconf(conf, nid, r * math.cos(a), r * math.sin(a)))
            nid += 1
    return node_configs, {}


def scn_three_clusters(conf, seed, n_per=8):
    """Three clusters pairwise joined by 1-node relays (two bridges per pair not needed)."""
    from lib.phy import MAXRANGE
    rng = random.Random(seed * 17 + 29)
    node_configs = []
    nid = 0
    R = MAXRANGE * 1.4
    centers = [(R * math.cos(2 * math.pi * i / 3), R * math.sin(2 * math.pi * i / 3)) for i in range(3)]
    cluster_ids = []
    for cx, cy in centers:
        ids = []
        placed = 0
        while placed < n_per:
            x = cx + rng.uniform(-MAXRANGE * 0.3, MAXRANGE * 0.3)
            y = cy + rng.uniform(-MAXRANGE * 0.3, MAXRANGE * 0.3)
            cand = Point(x, y, conf.HM)
            if all(cand.euclidean_distance(nc.position) >= conf.MINDIST for nc in node_configs):
                node_configs.append(_mkconf(conf, nid, x, y))
                ids.append(nid)
                nid += 1
                placed += 1
        cluster_ids.append(ids)
    # one relay midway on each pair edge
    relay_ids = []
    for i in range(3):
        j = (i + 1) % 3
        mx = (centers[i][0] + centers[j][0]) / 2
        my = (centers[i][1] + centers[j][1]) / 2
        node_configs.append(_mkconf(conf, nid, mx, my))
        relay_ids.append(nid)
        nid += 1
    return node_configs, {'relay_ids': relay_ids, 'clusters': cluster_ids}


def scn_moving(conf, seed, n):
    """Random layout; movement comes from conf.MOVEMENT_ENABLED (set by runner)."""
    return scn_random(conf, seed, n)


def _assign_roles(node_configs, seed):
    """v0.9 tactical MT roles, assigned deterministically (same roles for
    every router on the same seed — fair comparison):
    20% ROUTER, 30% CLIENT_BASE, 50% CLIENT_MUTE."""
    from lib.node import MESHTASTIC_ROLE
    rng = random.Random(seed * 977 + 31)
    for nc in node_configs:
        r = rng.random()
        if r < 0.20:
            nc.role = MESHTASTIC_ROLE.ROUTER
        elif r < 0.50:
            nc.role = MESHTASTIC_ROLE.CLIENT_BASE
        else:
            nc.role = MESHTASTIC_ROLE.CLIENT_MUTE
    return node_configs


def scn_mixed_roles(conf, seed, n, base_fn=None):
    """Random layout + mixed MT roles (v0.9)."""
    if base_fn is None:
        base_fn = scn_random
    node_configs, meta = base_fn(conf, seed, n)
    node_configs = _assign_roles(node_configs, seed)
    meta['roles_mixed'] = True
    return node_configs, meta


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------
SCENARIOS = {
    # name -> (generator, kwargs, recommended n, hop_limit, dms)
    'sparse':             (scn_sparse,           {}, 10, 7, False),
    'linear':             (scn_linear,           {}, 25, 7, True),
    'dense':              (scn_dense,            {}, 50, 3, False),
    'very_dense':         (scn_dense,            {}, 100, 3, False),
    'moving':             (scn_moving,           {}, 30, 3, False),
    'bridge':             (scn_bridge,           {}, 25, 5, True),
    'hub':                (scn_hub,              {}, 30, 5, True),
    'topo_distributed':   (scn_topo_distributed, {}, 8, 7, True),
    'dense_core_edge':    (scn_dense_core,       {}, 30, 5, True),
    'three_clusters':     (scn_three_clusters,   {}, 27, 5, True),
    'random30':           (scn_random,           {}, 30, 3, False),
    # v0.9: mixed MT roles (20% ROUTER / 30% CLIENT_BASE / 50% CLIENT_MUTE)
    'dense_roles':        (scn_mixed_roles,      {'base_fn': scn_dense}, 50, 3, False),
    'mixed30':            (scn_mixed_roles,      {'base_fn': scn_random}, 30, 3, False),
    # v0.12b B6 scale panel: larger device counts. random100/mixed100 keep
    # the default 15x15 km area (density stress, same placement rules as
    # random30/mixed30); ultra_dense packs 200 into the 3.5 km cluster
    # square (extreme density); linear100 is a 100-node chain (long-haul
    # reach/latency under firmware hop limits).
    'random100':          (scn_random,           {}, 100, 3, False),
    'mixed100':           (scn_mixed_roles,      {'base_fn': scn_random}, 100, 3, False),
    'linear100':          (scn_linear,           {}, 100, 7, True),
    'ultra_dense':        (scn_dense,            {}, 200, 3, False),
    # v0.14 S3: OOD stress family (out-of-distribution topologies; the
    # router NEVER sees the scenario name — local signals only)
    'double_bridge':      (scn_double_bridge,    {}, 25, 5, False),
    'cluster_chain':      (scn_cluster_chain,    {}, 49, 7, False),
    'rural_corridor':     (scn_rural_corridor,   {}, 36, 7, False),
    'ring_with_spurs':    (scn_ring_with_spurs,  {}, 26, 6, False),
    'grid':               (scn_grid,             {}, 49, 5, False),
    'core_plus_remote':   (scn_core_plus_remote, {}, 24, 6, False),
    'two_dense_sparse_mid': (scn_two_dense_sparse_middle, {}, 34, 6, False),
    'rand_var_density':   (scn_random_variable_density, {}, 60, 5, False),
}


def build_scenario(conf, name, seed, n_override=None):
    fn, kwargs, n_def, hop_def, dms_def = SCENARIOS[name]
    kwargs = dict(kwargs)
    import inspect
    if 'n' in inspect.signature(fn).parameters:
        kwargs.setdefault('n', n_override or n_def)
    node_configs, meta = fn(conf, seed, **kwargs)
    meta.update({
        'scenario': name, 'seed': seed, 'n_nodes': len(node_configs),
        'hop_limit': hop_def, 'dms': dms_def,
        'avg_neighbors_t0': round(_avg_neighbors(conf, node_configs), 2),
    })
    return node_configs, meta
