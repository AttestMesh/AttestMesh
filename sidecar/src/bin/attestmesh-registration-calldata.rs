use alloy::primitives::{Address, B256};
use anyhow::{ensure, Context, Result};
use axum::{routing::get, Json, Router};
use cluster_mesh_agent::chain::dstack_facet;
use cluster_mesh_agent::dstack::{DstackRuntime, UnixSocketDstack};
use cluster_mesh_agent::keys;
use serde_json::{json, Value};
use std::env;
use std::net::SocketAddr;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<()> {
    let dstack_socket = env::var("DSTACK_SOCKET").unwrap_or_else(|_| "/var/run/dstack.sock".into());
    let listen: SocketAddr = env::var("REGISTRATION_HELPER_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:9092".into())
        .parse()
        .context("parse REGISTRATION_HELPER_ADDR")?;
    let cluster: Address = env::var("CLUSTER")
        .context("CLUSTER env is required")?
        .parse()
        .context("parse CLUSTER")?;
    let dstack = UnixSocketDstack::new(dstack_socket);

    let mut last_err = None;
    for _ in 0..90 {
        match build_payload(&dstack, cluster).await {
            Ok(payload) => {
                println!("ATTESTMESH_DIRECT_REGISTER {}", payload);
                let app = Router::new().route(
                    "/registration-calldata",
                    get(move || async move { Json(payload.clone()) }),
                );
                let listener = tokio::net::TcpListener::bind(listen)
                    .await
                    .context("bind registration helper")?;
                axum::serve(listener, app)
                    .await
                    .context("serve registration helper")?;
                return Ok(());
            }
            Err(err) => {
                last_err = Some(err);
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
    }

    Err(last_err.expect("retry loop ran without an error"))
}

async fn build_payload(dstack: &UnixSocketDstack, cluster: Address) -> Result<Value> {
    let info = dstack.info().await.context("dstack /Info")?;
    ensure!(
        info.app_id.len() == 20,
        "dstack app_id is {} bytes, expected 20",
        info.app_id.len()
    );
    let member = Address::from_slice(&info.app_id);
    let keys = keys::derive_all(dstack).await.context("derive node keys")?;
    let x_pub = B256::from(keys.x_pub);
    let wg_pub = B256::from(keys.wg_pub);
    let (proof, owner) =
        dstack_facet::build_proof_from_runtime(dstack, cluster, member, x_pub, wg_pub)
            .await
            .context("build dstack registration proof")?;
    let code_id = proof.codeId;
    let calldata = dstack_facet::build_register_calldata(proof, member, x_pub, wg_pub);

    Ok(json!({
        "cluster": format!("{cluster:#x}"),
        "member": format!("{member:#x}"),
        "owner": format!("{:#x}", owner.address()),
        "codeId": format!("{code_id:#x}"),
        "xPubKey": format!("{x_pub:#x}"),
        "wgPubKey": format!("{wg_pub:#x}"),
        "calldata": format!("0x{}", hex::encode(calldata)),
    }))
}
