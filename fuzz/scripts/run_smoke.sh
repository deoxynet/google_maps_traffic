#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FUZZ_DIR="$REPO_ROOT/fuzz"
SMOKE_TIME="${SMOKE_TIME:-8}"
has_files() {
  local dir="$1"
  find "$dir" -type f -print -quit | grep -q .
}

if ! [[ "$SMOKE_TIME" =~ ^[0-9]+$ ]] || [[ "$SMOKE_TIME" -eq 0 ]]; then
  echo "SMOKE_TIME must be a positive integer, got: $SMOKE_TIME" >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=(
    protobuf_parse
    geometry_decode
    xhr1_parse
    proto_response_parse
    traffic_decode_from_proto
    osm_matcher
  )
fi

cd "$REPO_ROOT"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fuzz_smoke.XXXXXX")"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

for target in "${TARGETS[@]}"; do
  target_source="$FUZZ_DIR/fuzz_targets/$target.rs"
  if [[ ! -f "$target_source" ]]; then
    echo "unknown fuzz target '$target' (missing $target_source)" >&2
    exit 1
  fi

  smoke_corpus="$FUZZ_DIR/corpus_smoke/$target"
  default_corpus="$FUZZ_DIR/corpus/$target"
  corpus=""
  corpus_mode=""
  if [[ -d "$smoke_corpus" ]] && has_files "$smoke_corpus"; then
    corpus="$smoke_corpus"
    corpus_mode="smoke"
  elif [[ -d "$default_corpus" ]] && has_files "$default_corpus"; then
    corpus="$default_corpus"
    corpus_mode="default"
  fi

  run_corpus="$WORK_DIR/$target"
  mkdir -p "$run_corpus"
  if [[ -n "${corpus:-}" ]]; then
    rsync -a "$corpus/" "$run_corpus/"
    echo "=== $target (corpus: ${corpus#$REPO_ROOT/}, mode: temp-copy/$corpus_mode) ==="
  else
    printf '\x00' > "$run_corpus/seed"
    echo "=== $target (corpus: generated, mode: temp-copy/synthetic-seed) ==="
  fi
  cargo +nightly fuzz run "$target" "$run_corpus" -- -max_total_time="$SMOKE_TIME" -verbosity=0 -print_final_stats=1
  echo
done
