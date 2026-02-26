#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FUZZ_DIR="$REPO_ROOT/fuzz"
TARGET="osm_matcher"
SOURCE_CORPUS="$FUZZ_DIR/corpus/$TARGET"
SMOKE_CORPUS="$FUZZ_DIR/corpus_smoke/$TARGET"
KEEP_COUNT="${KEEP_COUNT:-128}"
CMIN_TIME="${CMIN_TIME:-120}"

if [[ ! -d "$SOURCE_CORPUS" ]]; then
  echo "missing source corpus: $SOURCE_CORPUS" >&2
  exit 1
fi
if ! [[ "$KEEP_COUNT" =~ ^[0-9]+$ ]] || [[ "$KEEP_COUNT" -eq 0 ]]; then
  echo "KEEP_COUNT must be a positive integer, got: $KEEP_COUNT" >&2
  exit 1
fi
if ! [[ "$CMIN_TIME" =~ ^[0-9]+$ ]] || [[ "$CMIN_TIME" -eq 0 ]]; then
  echo "CMIN_TIME must be a positive integer, got: $CMIN_TIME" >&2
  exit 1
fi

cd "$REPO_ROOT"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/osm_matcher_cmin.XXXXXX")"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

INPUT_CORPUS="$WORK_DIR/input"
SELECTED_CORPUS="$WORK_DIR/selected"
mkdir -p "$INPUT_CORPUS" "$SELECTED_CORPUS" "$SMOKE_CORPUS"

rsync -a "$SOURCE_CORPUS/" "$INPUT_CORPUS/"

cargo +nightly fuzz cmin "$TARGET" "$INPUT_CORPUS" -- -max_total_time="$CMIN_TIME" -verbosity=0

selected_files=()
while IFS= read -r -d '' file; do
  selected_files+=("$file")
  if [[ "${#selected_files[@]}" -ge "$KEEP_COUNT" ]]; then
    break
  fi
done < <(find "$INPUT_CORPUS" -type f -print0 | LC_ALL=C sort -z)

for i in "${!selected_files[@]}"; do
  cp "${selected_files[$i]}" "$SELECTED_CORPUS/$(printf '%08d' "$i")"
done

rsync -a --delete "$SELECTED_CORPUS/" "$SMOKE_CORPUS/"

source_count="$(find "$SOURCE_CORPUS" -type f | wc -l | tr -d ' ')"
min_count="$(find "$INPUT_CORPUS" -type f | wc -l | tr -d ' ')"
smoke_count="$(find "$SMOKE_CORPUS" -type f | wc -l | tr -d ' ')"

echo "source corpus files: $source_count"
echo "minimized files: $min_count"
echo "smoke corpus files: $smoke_count"
