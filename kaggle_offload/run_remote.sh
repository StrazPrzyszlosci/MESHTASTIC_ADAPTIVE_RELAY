#!/usr/bin/env bash
# Meshtasticator -> Kaggle offload driver (dataset path, no GitHub needed).
#
# Usage:
#   ./kaggle_offload/run_remote.sh <matrix> <scenarios> <variants> <seeds> [key=val ...]
#   extra args: dms=1 capture_db=0 clock_drift_ppm=40 modem=SHORT_SLOW
#
# Prerequisites (ONE TIME, both DONE):
#   - kaggle CLI authenticated (OAuth: ~/.kaggle/credentials.json)
#   - dataset `strazprzyszlosci/meshtasticator-ar-code` created (first run)
#
# Flow: stage COMMITTED code (git archive HEAD, no results_ar/.venv) with a
# CODE_VERSION stamp -> dataset version push -> pin panel params -> kernel
# push -> poll -> pull raw_*.csv/jsonl into results_ar/ (bench resume
# semantics merge them on re-run).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
KAGGLE="$REPO/.venv/bin/kaggle"
KERNEL_ID="strazprzyszlosci/meshtasticator-ar-panel"
DATASET_ID="strazprzyszlosci/meshtasticator-ar-code"

MATRIX="${1:?matrix name, e.g. e4_remote}"
SCENARIOS="${2:?scenarios csv}"
VARIANTS="${3:?variants csv}"
SEEDS="${4:?seeds csv}"
shift 4 || true

# --- 1. stage the committed code -------------------------------------------
STAGE="$(mktemp -d /tmp/kilo/kaggle_stage.XXXXXX)"
git -C "$REPO" archive HEAD | tar -x -C "$STAGE"
# keep the dataset lean: routing layer + panel infra only
rm -rf "$STAGE/results_ar" "$STAGE/.git" "$STAGE/ga_results" \
       "$STAGE/final_results" "$STAGE/results" "$STAGE/results_v2" "$STAGE/img"
git -C "$REPO" rev-parse HEAD > "$STAGE/CODE_VERSION"
cp "$HERE"/vendor/*.whl "$STAGE/" 2>/dev/null || true
printf '%s\n' '{"id": "'"${DATASET_ID}"'", "title": "meshtasticator-ar-code", "is_private": true, "licenses": [{"name": "CC-BY-4.0"}]}' > "$STAGE/dataset-metadata.json"
cp "$HERE/panel.py" "$HERE/panel_params.json" "$STAGE/"
# panel_params pinned to the packaged commit
"$REPO/.venv/bin/python" - "$STAGE/panel_params.json" "$MATRIX" "$SCENARIOS" "$VARIANTS" "$SEEDS" "$@" <<'PYEOF'
import json, os, subprocess, sys
path, matrix, scen, var, seeds = sys.argv[1:6]
commit = open(os.path.join(os.path.dirname(path), 'CODE_VERSION')).read().strip()
p = json.load(open(path))
p.update(commit=commit, matrix=matrix, scenarios=scen, variants=var, seeds=seeds)
for extra in sys.argv[6:]:
    k, v = extra.split('=', 1)
    p[k] = v if v != 'null' else None
json.dump(p, open(path, 'w'), indent=1)
print('panel_params:', {k: p[k] for k in ('commit', 'matrix', 'scenarios', 'variants', 'seeds')})
PYEOF

# --- 2. dataset create-or-version ------------------------------------------
if ! "$KAGGLE" datasets status "$DATASET_ID" >/dev/null 2>&1; then
    echo "creating dataset $DATASET_ID (first time)..."
    (cd "$STAGE" && "$KAGGLE" datasets create -p . --dir-mode zip)
else
    (cd "$STAGE" && "$KAGGLE" datasets version -p . --dir-mode zip -m "code $(cat CODE_VERSION)")
fi
# dataset processing is async server-side — wait until it is ready
echo "waiting for dataset processing..."
for i in $(seq 1 20); do
    sleep 30
    if "$KAGGLE" datasets status "$DATASET_ID" 2>&1 | grep -qi "ready\|available"; then
        echo "dataset ready."
        break
    fi
done

# --- 3. kernel push + poll --------------------------------------------------
# the kernel payload contains ONLY the code_file — inject the params into
# a stage copy of panel.py (__PANEL_PARAMS__ placeholder) and push from a
# dedicated kernel dir
KDIR="$(mktemp -d /tmp/kilo/kaggle_kernel.XXXXXX)"
PARAMS_ONELINE="$(tr -d '\n' < "$STAGE/panel_params.json")"
sed "s|__PANEL_PARAMS__|${PARAMS_ONELINE}|g" "$HERE/panel.py" > "$KDIR/panel.py"
if grep -q '__PANEL_PARAMS__' "$KDIR/panel.py"; then
    echo "FATAL: placeholder not injected"; exit 1
fi
cp "$HERE/kernel-metadata.json" "$KDIR/"
"$KAGGLE" kernels push -p "$KDIR"
echo "kernel pushed ($KERNEL_ID); polling status..."
while true; do
    sleep 90
    ST="$("$KAGGLE" kernels status "$KERNEL_ID" 2>&1 | tail -1)"
    ST_LC="$(printf '%s' "$ST" | tr '[:upper:]' '[:lower:]')"
    echo "  $(date +%H:%M:%S) $ST"
    case "$ST_LC" in
        *complete*) break ;;
        *error*|*cancel*) echo "KERNEL FAILED — logs:"; "$KAGGLE" kernels output "$KERNEL_ID" -p /tmp/kilo/kaggle_fail >/dev/null 2>&1 || true; exit 1 ;;
    esac
done

# --- 4. pull outputs --------------------------------------------------------
OUT="$REPO/results_ar"
PULL="$(mktemp -d /tmp/kilo/kaggle_pull.XXXXXX)"
"$KAGGLE" kernels output "$KERNEL_ID" -p "$PULL"
for f in "$PULL"/output/raw_* "$PULL"/Meshtasticator/output/raw_*; do
    [ -e "$f" ] && cp "$f" "$OUT/"
done
rm -rf "$STAGE" "$KDIR"
echo "outputs pulled into $OUT:"
ls -la "$OUT" | grep "raw_${MATRIX}" || true
