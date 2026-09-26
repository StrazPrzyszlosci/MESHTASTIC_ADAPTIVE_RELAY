# MESHTASTIC_ADAPTIVE_RELAY

This repository is based on the official
[`meshtastic/Meshtasticator`](https://github.com/meshtastic/Meshtasticator)
simulator. It adds one research layer: the **Adaptive Relay** experimental
router, studied against the simulator's MANAGED_FLOOD reference under
identical PHY/MAC models.

**Adaptive Relay is an experimental routing research project. Results are
simulation-based and do not yet demonstrate equivalent performance on
real Meshtastic hardware.**

## What Adaptive Relay is

MANAGED_FLOOD (the Meshtastic reference behavior) rebroadcasts a received
packet if no duplicate was heard first. Adaptive Relay replaces that blind
redundancy with evidence-ordered, locally-learned relaying:

```text
N3 ranked whisper            ordered designated relays (RANK0/1/2) with
  +                          ToA-scaled response windows: the strongest
                             evidence transmits first, others yield
SHEP2D bounded rescue        a watchdog layer that measures sustained
  +                          regret and COLLECTs a bounded rescue forward
CEF_BOOST congestion mode    a traffic-jam FSM: normal hybrid behavior,
                             pure-CEF forwarding only under sustained
                             busy+redundant+low-frontier evidence
```

The research default variant is **N3S_SHEP2D** (N3 + degree-adaptive
SHEPHERD rescue; CEF_BOOST engages only under real congestion).

Every decision input is a signal a real node can observe: decoded packet
headers, own RSSI, own TX/ACK history, own channel utilization, own
position, explicit protocol additions. No simulator-global knowledge
(audit: [ZERO_ORACLE_AUDIT.md](ZERO_ORACLE_AUDIT.md)). All mechanisms are
gated OFF by default and enabled per experiment.

## Results

All numbers below are produced by this codebase (official upstream base,
identical PHY/MAC for both routers), paired per seed, n=20, 95% CI.
DEV panel: 17 topologies (9 classic + 8 out-of-distribution) x
MANAGED_FLOOD vs N3S_SHEP2D x seeds 1-20 = 680 runs. No parameter was
tuned on this base.

**Verdict: 12 wins / 5 parities / 0 losses (reach)**; TX savings
statistically significant in 11 of 17 topologies, TX cost only where it
buys reach (bridge, hub, long chains).

| Topology | reach d [95% CI] wins | TX d [95% CI] |
|---|---|---|
| bridge | **+8.90pp [+8.26,+9.54] 20/20** | +149 [+97,+200] |
| hub | **+5.02pp [+4.64,+5.40] 20/20** | +58 [+32,+85] |
| linear | **+2.62pp [+2.36,+2.88] 20/20** | +221 [+200,+242] |
| linear100 | **+1.29pp [+1.25,+1.32] 20/20** | +2111 [+2071,+2152] |
| sparse | **+1.53pp [+0.43,+2.64] 13/20** | −16 [−28,−3] |
| two_dense_sparse_mid | **+0.74pp [+0.59,+0.89] 20/20** | +1 [−24,+26] |
| double_bridge | **+0.62pp [+0.39,+0.86] 18/20** | −66 [−95,−37] |
| dense | **+0.52pp [+0.30,+0.74] 17/20** | **−202 [−262,−142]** |
| ring_with_spurs | **+0.40pp [+0.11,+0.69] 16/20** | −53 [−72,−34] |
| mixed100 | **+0.09pp [+0.04,+0.14] 14/20** | −78 [−120,−37] |
| rand_var_density | **+0.09pp [+0.02,+0.15] 13/20** | −56 [−76,−35] |
| random100 | **+0.13pp [+0.06,+0.20] 14/20** | **−188 [−248,−127]** |
| mixed30 | +0.27pp [−0.02,+0.56] 12/20 | −14 [−35,+6] |
| grid | +0.14pp [−0.03,+0.31] 14/20 | −86 [−112,−61] |
| core_plus_remote | +0.34pp [−0.15,+0.83] 13/20 | −32 [−61,−3] |
| cluster_chain | +0.05pp [−0.09,+0.19] 12/20 | **−117 [−149,−84]** |
| rural_corridor | −0.10pp [−0.28,+0.07] 5/20 | −98 [−127,−69] |

Reading: the reach advantage concentrates exactly where blind flooding is
structurally weak — bridged clusters, hubs, long chains — while most other
topologies show parity or small wins with **fewer packets sent** (census
suppression). The chain costs (+TX) are the price of the reach win there
(reach-first). Thin-corridor topologies (rural_corridor) sit at parity.

### CEF_BOOST under load (dense, seeds 1-10, three traffic levels)

| Load | MF reach/TX | N3S_SHEP2D reach/TX | N3S_BOOST reach/TX | boost windows |
|---|---|---|---|---|
| 1x (30 s period) | 25.80% / 7876 | 26.40% / 7702 | **26.66% / 6953 (−11.7% TX vs MF)** | 851 |
| 3x (10 s period) | 20.05% / 9214 | 20.55% / 9111 | **20.60% / 8277 (−10.2% TX vs MF)** | 862 |
| 6x (5 s period) | 18.67% / 9730 | 19.07% / 9549 | **19.10% / 8687 (−10.7% TX vs MF)** | 883 |

The congestion FSM cuts ~10% of transmissions at **every** load level while
keeping reach above both other arms; it enters and exits on rescue signals
(exits ≈ windows — it never gets stuck). On this codebase the RX-busy
threshold is already reachable at normal dense load, so boost engages
early — the effect is purely beneficial: less airtime, no reach cost.

### PHY robustness (bridge, paired seeds 1-10)

| Preset | MF reach | N3S_SHEP2D reach | Δ [95% CI] | wins | TX MF→AR |
|---|---|---|---|---|---|
| LONG_FAST (DEV, n=20) | 37.97% | 46.87% | +8.90pp [+8.26,+9.54] | 20/20 | 2216→2365 |
| MEDIUM_FAST | 54.14% | 72.92% | **+18.78pp [+17.92,+19.65]** | 10/10 | 3181→2874 |
| SHORT_SLOW | 55.23% | 74.66% | **+19.43pp [+18.10,+20.75]** | 10/10 | 3428→2875 |

The advantage **grows on slower PHY presets**: longer airtime makes flooding
redundancy far more destructive, while designated relays with suppression
thrive — ~+19 pp at 10/10 wins, with TX **below** MF. For range-oriented
communities (SHORT_SLOW) this is the largest effect in the study.

### Frozen holdout (seeds 1001-1020 — final confirmation, never seen by the router)

**Verdict: 11 wins / 5 parities / 1 marginal loss** (DEV: 12/5/0).

| Topology | MF reach | N3S_SHEP2D reach | Δ [95% CI] | wins |
|---|---|---|---|---|
| bridge | 38.34% | 46.80% | **+8.46pp [+7.94,+8.99]** | **20/20** |
| hub | 29.22% | 34.06% | **+4.84pp [+4.50,+5.18]** | **20/20** |
| linear | 21.09% | 23.79% | **+2.70pp [+2.50,+2.90]** | **20/20** |
| linear100 | 5.81% | 7.05% | **+1.24pp [+1.19,+1.30]** | **20/20** |
| double_bridge | 29.62% | 30.64% | **+1.03pp [+0.74,+1.32]** | 20/20 |
| dense | 25.50% | 26.11% | **+0.61pp [+0.37,+0.84]** | 18/20 |
| core_plus_remote | 31.54% | 32.09% | **+0.55pp [+0.12,+0.98]** | 13/20 |
| two_dense_sparse_mid | 19.48% | 19.99% | **+0.51pp [+0.35,+0.67]** | 18/20 |
| rand_var_density | 6.61% | 6.75% | **+0.14pp [+0.08,+0.19]** | 18/20 |
| random100 | 10.92% | 11.05% | **+0.13pp [+0.06,+0.20]** | 15/20 |
| mixed100 | 11.29% | 11.37% | **+0.08pp [+0.03,+0.13]** | 15/20 |
| sparse | 67.31% | 68.33% | +1.02pp [−0.26,+2.31] (parity) | 13/20 |
| ring_with_spurs | 21.51% | 21.73% | +0.22pp [−0.02,+0.46] (parity) | 14/20 |
| grid / mixed30 / cluster_chain | — | — | parity (CI includes 0) | 9–10/20 |
| rural_corridor | 17.77% | 17.58% | **−0.19pp [−0.37,−0.00]** (marginal loss) | 7/20 |

Every structural win from the DEV panel replicates on untouched seeds at
matching magnitudes (bridge 8.9→8.5 pp, hub 5.0→4.8, chains and bridged
topologies all 20/20 in DEV and 18-20/20 here). TX: statistically
significant savings in 10 of 17 topologies (e.g. random100 −251, dense
−131). Honest caveats: sparse (a DEV win) is a parity on the holdout —
high per-seed variance; rural_corridor (thin corridor) lands marginally
below zero (−0.19 pp, CI upper bound touches zero) — the documented
cost of census suppression in corridor topologies.

**Campaign closed on this base:** DEV 12/5/0 + holdout 11/5/1 + load
(−10% TX at every level, reach held) + PHY (+19 pp on slower presets,
10/10). High-confidence simulation proof-of-concept; the remaining gap
is hardware (see FIRMWARE_RESOURCE_MODEL.md).

## Failure-scenario campaigns (D5: node death, D6: watchdog deafness)

The port-safety question: is a designated-relay architecture more
fragile than distributed flooding when a critical node dies, and does
a deaf watchdog poison routes? Both routers suffer the identical fault.

### D5 — permanent relay-death of the critical node (t=300s, n=10 paired)

| Scenario (killed node) | MF reach | N3S_SHEP2D reach | Δ [95% CI] | wins | vs no-kill Δ |
|---|---|---|---|---|---|
| bridge (the bridge node) | 38.14% | 46.23% | **+8.09pp [+6.96,+9.23]** | 10/10 | −0.8pp only |
| hub (the hub node) | 29.17% | 34.25% | **+5.07pp [+4.59,+5.56]** | 10/10 | unchanged |
| linear (3 middle nodes) | 20.40% | 23.09% | **+2.69pp [+2.38,+3.01]** | 10/10 | unchanged |
| dense (control) | 25.80% | 26.53% | +0.74pp [+0.44,+1.03] | 9/10 | no effect |

Adaptive Relay is **not more fragile than flooding** under the death of
its structurally critical nodes: the advantage survives (−0.8pp worst
case), failover machinery re-routes without TX storms, and after a
revive (t=450s) deliveries return to pre-kill levels within one
60 s window (re-learning works).

### D6 — watchdog deafness (RX disabled 300–420s on the critical node)

| Scenario (deaf node) | Δ reach [95% CI] | wins | false failures / total | recovery after 420s |
|---|---|---|---|---|
| bridge | **+8.34pp [+7.47,+9.22]** | 10/10 | **1.4%** | full (above pre-deaf) |
| hub | **+4.68pp [+4.04,+5.31]** | 10/10 | **4.7%** | full |
| linear | **+2.17pp [+1.80,+2.54]** | 10/10 | 21.6% | full |

The tri-state watchdog (covered-elsewhere / unknown-second-chance /
failure) protects innocent relays from a deaf observer: false
accusations stay at 1.4-4.7% in bridged topologies (higher in chains
where echo evidence is naturally sparse), and **no false accusation
permanently poisons routes** — reach returns to pre-deaf levels in
every scenario once hearing is restored.

**Port-readiness verdict:** the architecture degrades safely under
both failure modes studied; no death spirals, no permanent route
poisoning. Remaining pre-port work is the C++ resource mapping itself.

These are **high-confidence simulation proof-of-concept** results — not a
proven replacement for Managed Flood on hardware.

## Running

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# single run: Adaptive Relay on the bridge scenario
.venv/bin/python adaptive_run.py --scenario bridge --router ADAPTIVE_RELAY --seed 1 \
    --ar-params <(echo '{"n3_enabled": true, "n3_order_policy": "strong_first",
                          "n1_mode": "degree", "n3_gap_toa": 1.25,
                          "shepherd_enabled": true}')

# paired benchmark panel + statistics
.venv/bin/python adaptive_bench.py --matrix dev --variants MF,N3S_SHEP2D --seeds 1,...,20
.venv/bin/python panel_stats.py results_ar/raw_dev.jsonl --baseline MF \
    --candidates N3S_SHEP2D --metrics reach,tx_count

# full test suite (202 tests: 31 upstream + 171 Adaptive Relay)
.venv/bin/python -m unittest
```

## Verification documents

- [UPSTREAM_BASELINE.md](UPSTREAM_BASELINE.md) — official upstream snapshot,
  its test-suite result, and the MF baseline that must replay bit-identically
  after integration
- [ZERO_ORACLE_AUDIT.md](ZERO_ORACLE_AUDIT.md) — signal-by-signal proof that
  router decisions use only firmware-realistic inputs
- [SIMULATOR_AUDIT.md](SIMULATOR_AUDIT.md) — A/B/C classification of every
  shared-layer change; MF parity proof
- [FIRMWARE_RESOURCE_MODEL.md](FIRMWARE_RESOURCE_MODEL.md) — preliminary
  nRF52840 RAM/flash/airtime budget

## Changes relative to upstream Meshtasticator

Added: `lib/adaptive.py`, `lib/neighborinfo.py`, `lib/potential.py`,
`adaptive_scenarios.py`, `adaptive_run.py`, `adaptive_bench.py`,
`adaptive_agg.py`, `panel_stats.py`, `tests/test_adaptive.py`,
`tests/test_neighborinfo.py`, `tests/test_potential.py`, the audit
documents above and the `kaggle_offload/` remote-compute kit.
Modified (additive, gated; MANAGED_FLOOD runs the untouched upstream path):
`lib/config.py`, `lib/packet.py`, `lib/node.py`, `lib/mac.py`,
`lib/common.py`, `lib/phy.py` — every change classified in
[SIMULATOR_AUDIT.md](SIMULATOR_AUDIT.md).

---

# Meshtasticator
Discrete-event and interactive simulator for [Meshtastic](https://meshtastic.org/). 

## Discrete-event simulator
The discrete-event simulator mimics the radio section of the device software in order to understand its working. It can also be used to assess the performance of your scenario, or the scalability of the protocol. 

See [this document](DISCRETE_EVENT_SIM.md) for a usage guide. 

After a simulation, it plots the placement of nodes and time schedule for each set of overlapping messages that were sent.

![](/img/placement_schedule.png)

It can be used to analyze the network for a set of parameters. For example, these are the results of 100 simulations of 200s with a different hop limit and number of nodes. As expected, the average number of nodes reached for each generated message increases as the hop limit increases. 

![](/img/reachability_hops.png)

However, it comes at the cost of usefulness, i.e., the amount of received packets that contain a new message (not a duplicate due to rebroadcasting) out of all packets received. 

![](/img/usefulness_hops.png)

## Interactive simulator
The interactive simulator uses the [Linux native application of Meshtastic](https://meshtastic.org/docs/development/linux/), i.e. the real device software, while simulating some of the hardware interfaces, including the LoRa chip. Can also be used on a Windows or macOS host with Docker.

See [this document](INTERACTIVE_SIM.md) for a usage guide. 

It allows for debugging multiple communicating nodes without having real devices. 

https://user-images.githubusercontent.com/78759985/209952664-1a571fc8-65d1-4277-8516-2822f60a5dd0.mp4

Furthermore, since the simulator has an 'oracle view' of the network, it allows to visualize the route messages take. 

![](/img/route_plot.png)

# Tests

Unit tests can be executed by running `python3 -m unittest` from the root of the repo. Don't forget to activate your virtual env before running tests.

## License
Part of the source code is based on the work in [1], which eventually stems from [2]. The LoRaSim library from [2] can be found [here](https://www.lancaster.ac.uk/scc/sites/lora/lorasim.html).

This work is licensed under a [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/). 
The Adaptive Relay additions in this repository are modifications of that
work, indicated in the "Changes relative to upstream Meshtasticator"
section above and in the git history.

## References
1. [S. Spinsante, L. Gioacchini and L. Scalise, "A novel experimental-based tool for the design of LoRa networks," 2019 II Workshop on Metrology for Industry 4.0 and IoT (MetroInd4.0&IoT), 2019, pp. 317-322, doi: 10.1109/METROI4.2019.8792833.](https://ieeexplore.ieee.org/document/8792833)
2. [Martin C. Bor, Utz Roedig, Thiemo Voigt, and Juan M. Alonso, "Do LoRa Low-Power Wide-Area Networks Scale?", In Proceedings of the 19th ACM International Conference on Modeling, Analysis and Simulation of Wireless and Mobile Systems (MSWiM '16), 2016. Association for Computing Machinery, New York, NY, USA, 59–67.](https://doi.org/10.1145/2988287.2989163)
