#!/usr/bin/env python3
"""Meshtasticator panel runner for Kaggle kernels (dataset path).

The code comes from the attached PRIVATE DATASET (meshtasticator-ar-code,
packaged from the local repo's COMMITTED state via `git archive HEAD`,
stamped with CODE_VERSION). The kernel copies it to /kaggle/working
(the /kaggle/input mount is read-only), installs pinned deps, runs ONE
adaptive_bench matrix and leaves raw_*.csv/jsonl in /kaggle/working/output
for `kaggle kernels output` retrieval.

Determinism discipline (B1): same code version + same seeds => identical
results regardless of platform; deps pinned (simpy 4.1.2, numpy 2.5.3).
"""
import json
import os
import shutil
import subprocess
import sys
import glob

WORK = '/kaggle/working'
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = '/kaggle/input/meshtasticator-ar-code'

# params are INJECTED by the driver into this placeholder (the kernel
# payload contains only the code_file); local runs fall back to the file
_PANEL_RAW = '__PANEL_PARAMS__'
try:
    PANEL = json.loads(_PANEL_RAW)
except (json.JSONDecodeError, ValueError):
    PANEL = None


def sh(cmd, **kw):
    print('+', ' '.join(cmd), flush=True)
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        sys.exit(r.returncode)
    return r


def _find_src():
    """Locate the attached code dataset regardless of mount layout
    (new Kaggle kernels mount at /kaggle/input/datasets/<owner>/<slug>)."""
    for root, dirs, files in os.walk('/kaggle/input'):
        if 'CODE_VERSION' in files or 'adaptive_bench.py' in files:
            return root
    raise SystemExit('code dataset not mounted; /kaggle/input contains: %r'
                     % os.listdir('/kaggle/input'))


def main():
    if PANEL is not None:
        params = PANEL
    else:
        params = json.load(open(os.path.join(HERE, 'panel_params.json')))
    print('PANEL PARAMS:', params, flush=True)
    SRC = _find_src()
    print('CODE DATASET at:', SRC, flush=True)
    ver_file = os.path.join(SRC, 'CODE_VERSION')
    if os.path.exists(ver_file):
        print('CODE VERSION:', open(ver_file).read().strip(), flush=True)

    repo = os.path.join(WORK, 'Meshtasticator')
    if os.path.exists(repo):
        shutil.rmtree(repo)
    shutil.copytree(SRC, repo)
    # dataset --dir-mode zip packs directories as *.zip — unpack them
    for z in glob.glob(os.path.join(repo, '*.zip')):
        print('unzip:', os.path.basename(z), flush=True)
        sh([sys.executable, '-m', 'zipfile', '-e', z, repo])
        os.remove(z)
    # vendored simpy wheel (offline kernel — no PyPI, full reproducibility);
    # numpy uses the Kaggle base-image preinstall (verified by bit-identity)
    wheel = glob.glob(os.path.join(SRC, 'simpy-*.whl'))
    if wheel:
        sh([sys.executable, '-m', 'pip', 'install', '--quiet',
            '--no-index', wheel[0]])
    import numpy
    print('numpy version:', numpy.__version__, flush=True)

    outdir = os.path.join(WORK, 'output')
    os.makedirs(outdir, exist_ok=True)
    cmd = [sys.executable, 'adaptive_bench.py',
           '--matrix', params['matrix'],
           '--scenarios', params['scenarios'],
           '--variants', params['variants'],
           '--seeds', params['seeds'],
           '--workers', str(params.get('workers', 4))]
    for opt in ('dms', 'capture_db', 'clock_drift_ppm', 'modem', 'period_s',
                'n_nodes', 'xsize', 'ysize'):
        if params.get(opt) is not None:
            cmd += ['--' + opt.replace('_', '-'), str(params[opt])]
    # boolean flags (store_true in the runner): value '1'/'true' -> bare flag
    for opt in ('realistic_wire', 'no_hopstart'):
        v = params.get(opt)
        if v is not None:
            if str(v).lower() in ('1', 'true', 'on', 'yes'):
                cmd += ['--' + opt.replace('_', '-')]
            elif str(v).lower() not in ('0', 'false', 'off', 'no'):
                cmd += ['--' + opt.replace('_', '-'), str(v)]
    sh(cmd, cwd=repo)

    # expose raw panel outputs for `kaggle kernels output`
    for f in glob.glob(os.path.join(repo, 'results_ar', 'raw_*')):
        if os.path.basename(f).startswith('raw_' + params['matrix']):
            shutil.copy2(f, outdir)
    print('DONE. Output files:', sorted(os.listdir(outdir)), flush=True)


if __name__ == '__main__':
    main()
