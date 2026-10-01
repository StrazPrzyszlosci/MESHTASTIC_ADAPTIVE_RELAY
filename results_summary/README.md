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
