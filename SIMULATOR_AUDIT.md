# SIMULATOR AUDIT

Classification of every modification this repository makes to the shared
simulator layers (PHY/MAC/packet/scenario generator) relative to official
`meshtastic/Meshtasticator` @ `17ceb82`.

Rule enforced: **MANAGED_FLOOD and Adaptive Relay run on identical
PHY/MAC/collision/timing models.** No modification may act only in AR's
favor. Proof: the official MF baseline (`loraMesh.py 100 --no-gui
--simtime-seconds 600`, SEED=44) replays **bit-identically** after the
full integration (see UPSTREAM_BASELINE.md; verified 2026-09-25).

## A. Instrumentation / behavior-neutral (defaults = upstream)

| Change | File | Effect on MF/AR runs |
|---|---|---|
| `ROUTER_TYPE.ADAPTIVE_RELAY` enum entry | config.py | none (new enum value) |
| `AR_PARAMS` block (~210 params, all mechanisms OFF by default) | config.py | none unless router is ADAPTIVE_RELAY |
| 22 inert packet extension fields (default no-ops) | packet.py | none; MF code never reads/sets them |
| 12 node hooks, all gated on `self.adaptive is not None` | node.py | none for MF (adaptive=None); see hooks table below |
| `failed` test hook (kill switch for failure tests) | node.py | default False; inert |
| `fast_cw` short-CW branch in `set_transmit_delay` | mac.py | packet flag never set by MF; MF draws unchanged |
| `CAPTURE_THRESHOLD_DB` parametrization of `power_collision` | phy.py | default 6 dB = the upstream hardcoded value; identical behavior |
| `CLOCK_DRIFT_PPM` parametrization (`node_clock_scale`) | node.py | default 0 → scale exactly 1.0; all divisions by 1.0 are value-preserving |
| control-packet exclusions in stats (delays/usefulPackets/droppedByDelay) | node.py | gated on AR-only control-packet markers; MF never produces those packets |
| ACK `fast_cw=True` | node.py | gated on `self.adaptive is not None` |

## B. Bugfix / realism improvement

| Change | File | Class | Notes |
|---|---|---|---|
| `frequency_collision`: `p2.freq == 500` → `p2.bw == 500` (x2) | phy.py | **BUGFIX** (behavior-changing only for mixed-bandwidth setups) | upstream compares bandwidth against `freq` in two of three branches; all campaign scenarios use one modem preset → inert here; affects MF and AR identically |
| `find_random_position` per-candidate `foundMin` reset + deterministic fallback placement | common.py | **BUGFIX** (scenario generator) | old sticky-flag bug crashed tight layouts (position=None → `.x` AttributeError); the fallback uses no RNG (reproducible); affects all routers identically |
| per-node clock drift (±ppm) | node.py | REALISM (opt-in) | default 0 = perfect clocks; campaign D1 measured it as mildly positive; not enabled in the reproduction panels |

## C. Behavior-changing — full disclosure

1. **Adaptive Relay itself** is a new ROUTER_TYPE; it changes forwarding
   decisions for its own nodes only. It does not alter the shared
   PHY/MAC. MF nodes in the same simulation run the upstream code path.
2. **`frequency_collision` bugfix** (B above) — strictly more correct;
   both routers equally affected; inert for single-preset campaigns.
