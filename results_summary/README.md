# Results summary

Raw artifacts of the paired reproduction panels run on this codebase
(official upstream base, identical PHY/MAC for MANAGED_FLOOD and
Adaptive Relay, no parameter tuning on this base).

## Panels

| File | Panel | Composition |
|---|---|---|
| `raw_dev.csv` / `raw_dev.jsonl` | DEV reproduction | 17 topologies (9 classic + 8 OOD) x MF/N3S_SHEP2D x seeds 1-20 = 680 runs, paired per seed |
| `raw_boost_p30/p10/p5.*` | CEF_BOOST load panel (dense) | dense x N3S_SHEP2D/N3S_BOOST x seeds 1-10 at 30/10/5 s period (kernel-computed) |
| `raw_boost_mf10/mf5.*` | MF load baselines (dense) | dense x MF x seeds 1-10 at 10/5 s period (local, cross-platform bit-identity verified) |
| `raw_phy_mf.*` / `raw_phy_ss.*` | PHY robustness (bridge) | bridge x MF/N3S_SHEP2D x seeds 1-10 on MEDIUM_FAST / SHORT_SLOW |
| `raw_validation.csv` / `raw_validation.jsonl` | Frozen holdout | 17 topologies x MF/N3S_SHEP2D x seeds 1001-1020 = 680 runs |
| `raw_d5r_*` / `raw_d5v_*` | D5 node-death campaigns | bridge/hub/linear/dense x MF/N3S_SHEP2D x seeds 1-10; critical node killed at t=300s (relay mode; d5v = revived at t=450s) |
| `raw_d6_*` | D6 watchdog-deafness campaigns | bridge/hub/linear x MF/N3S_SHEP2D x seeds 1-10; RX disabled on the critical node 300-420s (TX unaffected) |
| `raw_ndb_*` | NodeDB-analog layer verdict panel | 5 topologies x N3S_SHEP2D_NDB x seeds 1-10 (paired vs raw_dev baselines). VERDICT: INERT — bit-identical to N3S_SHEP2D in dense/rural/linear, bridge +0.04pp (noise); census shields fired but K was non-binding; the rural_corridor loss lives in rank2-fallback (K2), not rank1 census. Layer kept gated OFF. |
| `raw_ndb_k2.jsonl` | K2-shield pre-registered experiment | 5 topologies x N3S_SHEP2D_NDB(ndb_k2_shield) x seeds 1-10. VERDICT: FAIL — rural_corridor WORSENED to −0.40pp [−0.72,−0.07] vs MF (baseline −0.10), mixed30 regressed −0.75pp [−1.18,−0.32] 0/10. Fallback (rank2) suppressions are NET-BENEFICIAL: releasing weak-margin copies adds collision noise that kills useful copies downstream. Third attempt at the rural loss refuted — accepted as a structural cost of the hybrid. |
| `raw_rw.jsonl` | REALISTIC_WIRE panel (port-thesis experiment) | 5 topologies x N3S_SHEP2D + realistic_wire x seeds 1-10. Firmware-realistic observability: relayed copies lose transmitter identity, collided frames unattributed, census counts copies, per-neighbor learning only from originated traffic. VERDICT: FULL PASS — bridge +8.75pp / hub +7.41pp / linear +4.00pp vs MF (all 10/10, all at or ABOVE the rich-observability numbers; hub/linear BEAT rich by +2.50/+1.45pp); TX cost <= +9.6%. The blind router matches or exceeds the informed one — the core advantage does not depend on richer-than-real observability. |
| `raw_rw_flagdrop.jsonl` | Cross-run determinism check | Accidental rich-mode re-run (the flag was dropped by the panel allowlist before the fix): bit-identical to raw_dev across all 50 runs on the kernel platform. |

| `raw_quiet_bridge/hub` | Quiet-regime pilot (real-traffic load) | 600 s period (real Position/telemetry cadence), 1 h runs. CAUTION: the recorded `reach` metric is ACK-contaminated at quiet loads (see the note in adaptive_run.py) — the pilot verdict uses TRUE distinct (message,receiver) coverage computed from packet state: bridge seeds 1-5, MF vs N3S_SHEP2D, d = −1.00pp [−2.82,+0.82] wins=1/5 → PARITY. The busy-regime advantage is a contention win; at real-network traffic levels the bridge topology delivers ~78% for BOTH routers (residual loss is hop-limit/geometry, not routing). |
| `raw_ndb_k2_ablation.jsonl` | Grace contract, cross-platform | Accidental 50-run Kaggle panel with the hopStart carrier missing from the staged commit: nodedb empty (has-flag guard), shields silent, results BIT-IDENTICAL to N3S_SHEP2D everywhere — the strongest possible confirmation that absence of data cannot change behavior. |

Paired statistics are computed with `panel_stats.py`:

```bash
.venv/bin/python panel_stats.py results_summary/raw_dev.jsonl \
    --baseline MF --candidates N3S_SHEP2D --metrics reach,tx_count
```

DEV verdict (reach, n=20, 95% CI): **12 wins / 5 parities / 0 losses**;
TX savings significant in 11/17 topologies (cost only on bridge, hub and
long chains — the reach-first tradeoff). Full table in the repository
README.

## Provenance

- Kernel-computed panel: `meshtasticator-ar-panel` (Kaggle), dataset
  `meshtasticator-ar-code`, CODE_VERSION `f340050` — tree-content
  identical to the squashed release commit (comment-only differences).
  Cross-platform bit-identity verified on a smoke panel before the run
  (same seeds => identical reach/TX/collisions locally and on the
  kernel).
- Deterministic: same code version + same seed => identical results.
