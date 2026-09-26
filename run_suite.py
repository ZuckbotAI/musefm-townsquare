#!/usr/bin/env python3
"""Standalone-script suite runner (same shape as the pass-1 worker's sweep).
Runs every test_*.py as a script, parses 'N passed, M failed' tails.
Usage: python3 run_suite.py [outdir]"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "hidden_files", "suite-passD")
os.makedirs(outdir, exist_ok=True)

files = sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
total_p = total_f = 0
rows = []
for f in files:
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, f)],
                           capture_output=True, text=True, timeout=600, cwd=HERE)
        out = (r.stdout or "") + (r.stderr or "")
        open(os.path.join(outdir, f + ".log"), "w").write(out)
        m = re.search(r"(\d+)\s+passed,\s+(\d+)\s+failed", out)
        p, fl = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except subprocess.TimeoutExpired:
        p, fl, r = 0, 0, None
        open(os.path.join(outdir, f + ".log"), "w").write("TIMEOUT after 600s")
    ec = r.returncode if r else -1
    total_p += p
    total_f += fl
    rows.append((f, ec, p, fl))
    print(f"did {f} ec={ec} p={p} f={fl}", flush=True)

with open(os.path.join(outdir, "summary.tsv"), "w") as fh:
    for f, ec, p, fl in rows:
        fh.write(f"{f}\t{ec}\t{p}\t{fl}\n")
print(f"\nTOTAL {total_p} passed, {total_f} failed across {len(files)} files")
