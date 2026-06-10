use std::path::PathBuf;

fn main() {
    // Vendored protoc so the build needs no system protobuf install (same pattern as
    // the sidecar's build.rs). protoc is not on PATH in CI/CVM builds.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc");
    std::env::set_var("PROTOC", protoc);

    // The sidecar component owns the canonical indexer.proto (spec §2.2, §4). We
    // compile THAT file directly via its relative path rather than forking it, so the
    // wire format can never drift between the two components.
    let proto = PathBuf::from("../sidecar/proto/indexer.proto");
    let proto_dir = proto.parent().expect("proto parent");

    tonic_build::configure()
        .build_server(true)
        // The indexer also needs the client types for the in-process integration
        // harness (spec §14.2) and for the round-trip unit coverage.
        .build_client(true)
        .compile_protos(std::slice::from_ref(&proto), &[proto_dir])
        .expect("compile indexer.proto");

    println!("cargo:rerun-if-changed={}", proto.display());
}
