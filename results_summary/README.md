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
