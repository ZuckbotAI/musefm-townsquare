#!/usr/bin/env bash
# Sync the rebuilt village.html from the 3D build tree into townsquare's
# served copy, injecting the townsquare bridge script tag.
#
# The parallel 3D worker owns the sources and rebuilds village.html via
# assemble_v2.py. This script copies the REBUILT file (verify freshness
# first) and appends our integration bridge. Re-run after every rebuild.
#
# Usage: scripts/sync-village.sh [--check]
#   --check: only report freshness, don't copy.
set -euo pipefail
SRC="$HOME/workspace/tidepool-3d/round13-test/village/village.html"
DST="$HOME/workspace/musefm-townsquare/village-dist/village.html"
BRIDGE='<script src="/static/js/drift-adopt-bridge.js"></script>'

if [ ! -f "$SRC" ]; then
  echo "SYNC FAIL: $SRC missing"
  exit 1
fi
echo "source: $(stat -c '%y %s bytes' "$SRC")"
if [ -f "$DST" ]; then
  echo "dest:   $(stat -c '%y %s bytes' "$DST")"
fi
if [ "${1:-}" = "--check" ]; then
  exit 0
fi
mkdir -p "$(dirname "$DST")"
cp "$SRC" "$DST"
# Inject the bridge before </body> (idempotent: strip any prior injection).
# Hook the Pet Shop module's adopt() to go through the real backend first.
python3 - "$DST" "$BRIDGE" <<'EOF'
import sys
p, bridge = sys.argv[1], sys.argv[2]
s = open(p).read()
s = s.replace(bridge, "")
hook_old = "function adopt(pet){\n  pet.adoptedAt = Date.now();"
hook_new = ("function adopt(pet){\n"
            "  if (window.__driftAdoptBridge && !pet.__driftDone) {\n"
            "    window.__driftAdoptBridge(pet, function(){ pet.__driftDone = true; adopt(pet); });\n"
            "    return;\n"
            "  }\n"
            "  pet.adoptedAt = Date.now();")
assert s.count(hook_old) == 1, "adopt(pet) hook point not unique: %d" % s.count(hook_old)
s = s.replace(hook_old, hook_new)
assert "</body>" in s, "no </body> in village.html"
s = s.replace("</body>", bridge + "\n</body>")
open(p, "w").write(s)
print("adopt() hooked + bridge injected")
EOF
echo "synced -> $DST"
