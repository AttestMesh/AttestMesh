use std::path::PathBuf;

fn main() {
    // Vendored protoc so the build needs no system protobuf install (same pattern as
    // the sidecar's build.rs). protoc is not on PATH in CI/CVM builds.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc");
    std::env::set_var("PROTOC", protoc);

    // The sidecar owns both canonical wire definitions. Compile the source files in
    // place rather than copying/forking them: indexer.proto is the service we expose,
    // while agent.proto is the co-located sidecar API used only in shared mode.
    let protos = [
        PathBuf::from("../sidecar/proto/indexer.proto"),
        PathBuf::from("../sidecar/proto/agent.proto"),
    ];
    let proto_dir = protos[0].parent().expect("proto parent").to_path_buf();

    tonic_build::configure()
        .build_server(true)
        // The indexer also needs the client types for the in-process integration
        // harness (spec §14.2) and for the round-trip unit coverage.
        .build_client(true)
        .compile_protos(&protos, &[proto_dir])
        .expect("compile canonical sidecar protos");

    for proto in &protos {
        println!("cargo:rerun-if-changed={}", proto.display());
    }
}
