#![no_main]

use google_maps_traffic::fuzz_support;
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let _ = fuzz_support::parse_protobuf(data);
});
