use std::path::PathBuf;

fn main() {
    // Use a vendored protoc so the build needs no system protobuf install.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc");
    std::env::set_var("PROTOC", protoc);

    let proto_dir = PathBuf::from("proto");
    let protos = [
        proto_dir.join("indexer.proto"),
        proto_dir.join("agent.proto"),
        proto_dir.join("peer.proto"),
    ];

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&protos, &[proto_dir])
        .expect("compile protos");

    for p in &protos {
        println!("cargo:rerun-if-changed={}", p.display());
    }
}
