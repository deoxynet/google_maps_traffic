## Fuzzing

Install `cargo-fuzz` once:

```bash
cargo install cargo-fuzz
```

Install nightly toolchain (required by libFuzzer sanitizers):

```bash
rustup toolchain install nightly
```

Run a target:

```bash
cargo +nightly fuzz run protobuf_parse
cargo +nightly fuzz run geometry_decode
cargo +nightly fuzz run xhr1_parse
cargo +nightly fuzz run proto_response_parse
cargo +nightly fuzz run traffic_decode_from_proto
cargo +nightly fuzz run osm_matcher
```

Short CI-style smoke run for all targets:

```bash
for t in protobuf_parse geometry_decode xhr1_parse proto_response_parse traffic_decode_from_proto osm_matcher; do
  cargo +nightly fuzz run "$t" -- -max_total_time=20
done
```

Use the repo smoke script (uses `fuzz/corpus_smoke/<target>` when present, then runs on temporary copies so corpora are not mutated; if no corpus exists for a known target, it generates a minimal temporary seed so CI can still run):

```bash
./fuzz/scripts/run_smoke.sh
```

Minimize and refresh the `osm_matcher` smoke corpus snapshot:

```bash
./fuzz/scripts/update_osm_matcher_smoke_corpus.sh
```

Optional environment variables:

```bash
SMOKE_TIME=12 ./fuzz/scripts/run_smoke.sh osm_matcher
KEEP_COUNT=128 CMIN_TIME=180 ./fuzz/scripts/update_osm_matcher_smoke_corpus.sh
```
