//! `cluster-mesh-agent` entrypoint. The bring-up state machine is driven from here.

fn main() -> anyhow::Result<()> {
    cluster_mesh_agent::run()
}
