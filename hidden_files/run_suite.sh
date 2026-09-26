#!/usr/bin/env bash
# Parallel full-suite runner. Usage: run_suite.sh <site_dir> <out_dir>
SITE="$1"; OUT="$2"
mkdir -p "$OUT"
cd "$SITE" || exit 1
run_one() {
  t="$1"
  log="$OUT/$t.log"
  timeout 600 python3 "$t" >"$log" 2>&1
  ec=$?
  passes=$(grep -c "^  PASS " "$log")
  fails=$(grep -c "^  FAIL " "$log")
  tb=0; grep -q "Traceback (most recent call last)" "$log" && tb=1
  fnames=$(grep "^  FAIL " "$log" | sed 's/^  FAIL //; s/ -- .*//' | head -8 | tr '\n' '|')
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$t" "$ec" "$passes" "$fails" "$tb" "$fnames"
  echo "did $t ec=$ec p=$passes f=$fails" >&2
}
export -f run_one
export OUT
ls test_*.py | sort | xargs -P 6 -I{} bash -c 'run_one "$@"' _ {} > "$OUT/summary.tsv" 2> "$OUT/progress.log"
echo "DONE"
