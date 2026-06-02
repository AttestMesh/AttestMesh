//! EIP-4337 v0.7 bundler RPC client (sidecar spec §8.2, §2.1). Hand-rolled JSON-RPC
//! (the spec sanctions this where upstream isn't ready). All state-mutating calls go
//! through here; the sidecar holds zero ETH and relies on paymaster sponsorship.

use super::userop::UserOperation;
use alloy::primitives::{Address, Bytes, B256, U256};
use alloy::signers::local::PrivateKeySigner;
use alloy::signers::Signer;
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::time::Duration;

pub struct BundlerClient {
    http: reqwest::Client,
    url: String,
    entry_point: Address,
    chain_id: u64,
}

impl BundlerClient {
    pub fn new(url: impl Into<String>, entry_point: Address, chain_id: u64) -> Self {
        Self {
            http: reqwest::Client::new(),
            url: url.into(),
            entry_point,
            chain_id,
        }
    }

    async fn rpc(&self, method: &str, params: Value) -> Result<Value> {
        let body = json!({ "jsonrpc": "2.0", "id": 1, "method": method, "params": params });
        let resp: Value = self
            .http
            .post(&self.url)
            .json(&body)
            .send()
            .await
            .with_context(|| format!("bundler {method}"))?
            .json()
            .await
            .context("bundler response decode")?;
        if let Some(err) = resp.get("error") {
            bail!("bundler {method} error: {err}");
        }
        Ok(resp.get("result").cloned().unwrap_or(Value::Null))
    }

    /// Build, sign, submit, and await inclusion. Returns the underlying tx hash.
    pub async fn submit(&self, signer: &PrivateKeySigner, mut op: UserOperation) -> Result<B256> {
        op.nonce = self.get_nonce(op.sender).await.unwrap_or(U256::ZERO);

        // Gas estimation (+20% headroom), best-effort fee defaults.
        if let Ok(est) = self
            .rpc(
                "eth_estimateUserOperationGas",
                json!([userop_json(&op), self.entry_point]),
            )
            .await
        {
            apply_estimate(&mut op, &est);
        }

        // Compute hash with current fields, sign EIP-191 message of it.
        let hash = op.user_op_hash(self.entry_point, self.chain_id);
        let sig = signer
            .sign_message(hash.as_slice())
            .await
            .context("sign userOpHash")?;
        op.signature = Bytes::from(sig.as_bytes().to_vec());

        let sent = self
            .rpc(
                "eth_sendUserOperation",
                json!([userop_json(&op), self.entry_point]),
            )
            .await
            .context("eth_sendUserOperation")?;
        let user_op_hash: B256 =
            serde_json::from_value(sent).context("parse userOpHash from bundler")?;

        self.await_receipt(user_op_hash).await
    }

    async fn get_nonce(&self, sender: Address) -> Result<U256> {
        let r = self
            .rpc("eth_getUserOperationNonce", json!([sender, "0x0"]))
            .await?;
        Ok(serde_json::from_value(r).unwrap_or(U256::ZERO))
    }

    /// Poll `eth_getUserOperationReceipt` up to 60s (sidecar spec §8.2 step 7).
    async fn await_receipt(&self, user_op_hash: B256) -> Result<B256> {
        for _ in 0..30 {
            let r = self
                .rpc("eth_getUserOperationReceipt", json!([user_op_hash]))
                .await?;
            if let Some(tx) = r.get("receipt").and_then(|x| x.get("transactionHash")) {
                return serde_json::from_value(tx.clone()).context("parse txHash");
            }
            tokio::time::sleep(Duration::from_secs(2)).await;
        }
        bail!("userOp not included within 60s: {user_op_hash}")
    }
}

fn apply_estimate(op: &mut UserOperation, est: &Value) {
    let g = |k: &str| -> Option<U256> {
        est.get(k)
            .and_then(|v| v.as_str())
            .and_then(|s| U256::from_str_radix(s.trim_start_matches("0x"), 16).ok())
    };
    let pad = |x: U256| x * U256::from(120u64) / U256::from(100u64);
    if let Some(x) = g("callGasLimit") {
        op.call_gas_limit = pad(x);
    }
    if let Some(x) = g("verificationGasLimit") {
        op.verification_gas_limit = pad(x);
    }
    if let Some(x) = g("preVerificationGas") {
        op.pre_verification_gas = pad(x);
    }
}

/// Serialize a UserOperation to the v0.7 unpacked JSON shape Alchemy expects.
pub fn userop_json(op: &UserOperation) -> Value {
    let hx = |v: U256| format!("0x{:x}", v);
    let mut m = json!({
        "sender": op.sender,
        "nonce": hx(op.nonce),
        "callData": op.call_data,
        "callGasLimit": hx(op.call_gas_limit),
        "verificationGasLimit": hx(op.verification_gas_limit),
        "preVerificationGas": hx(op.pre_verification_gas),
        "maxFeePerGas": hx(op.max_fee_per_gas),
        "maxPriorityFeePerGas": hx(op.max_priority_fee_per_gas),
        "signature": op.signature,
    });
    if let Some(pm) = op.paymaster {
        m["paymaster"] = json!(pm);
        m["paymasterVerificationGasLimit"] = json!(hx(op.paymaster_verification_gas_limit));
        m["paymasterPostOpGasLimit"] = json!(hx(op.paymaster_post_op_gas_limit));
        m["paymasterData"] = json!(op.paymaster_data);
    }
    m
}
