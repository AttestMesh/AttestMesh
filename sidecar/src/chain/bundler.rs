//! EIP-4337 v0.7 bundler RPC client (sidecar spec §8.2, §2.1). Hand-rolled JSON-RPC
//! (the spec sanctions this where upstream isn't ready). All state-mutating calls go
//! through here; the sidecar holds zero ETH and relies on paymaster sponsorship.
//!
//! **Sponsor-then-sign (premortem F2).** The v0.7 `userOpHash` commits to
//! `paymasterAndData` (it is part of the packed struct the EntryPoint hashes). So the
//! signature MUST be computed over the *final* op — after the paymaster fields are
//! populated. The earlier flow signed first and let the paymaster fill the fields
//! afterwards, which changed the on-chain hash and made `validateUserOp` recover the
//! wrong signer (EntryPoint rejects with AA24). We therefore request gas + paymaster
//! sponsorship via `alchemy_requestGasAndPaymasterAndData`, populate the op, and only
//! then hash + sign. (Flow mirrors TeeSQL/dstackgres's gas_payment/alchemy.rs.)

use super::userop::UserOperation;
use alloy::primitives::{Address, Bytes, B256, U256};
use alloy::signers::local::PrivateKeySigner;
use alloy::signers::Signer;
use anyhow::{bail, Context, Result};
use serde_json::{json, Value};
use std::time::Duration;

/// A valid 65-byte secp256k1 signature used for gas/paymaster estimation before the real
/// signature exists. It must be (a) even-length hex — Alchemy's
/// `alchemy_requestGasAndPaymasterAndData` rejects an odd-length `dummySignature` outright —
/// and (b) actually recoverable with low-s, because ClusterMember.validateUserOp runs OZ
/// `ECDSA.recover`, which reverts on an unrecoverable/high-s signature (which would fail
/// estimation). So we use a real signature (over an arbitrary message by private key 0x1,
/// recovers 0x7E5F…Bdf); the recovered address is irrelevant to estimation.
const DUMMY_SIGNATURE: &str = "0x6f79009346b958973a8481f680a44b5075d56562b39aff2104395323b6bc666a2302f4e19a7da13bef02c866971d5b35ce8c0ad27542a1c476e103b99a7b6cd61b";

pub struct BundlerClient {
    http: reqwest::Client,
    url: String,
    entry_point: Address,
    chain_id: u64,
    /// Alchemy Gas Manager policy id used by `alchemy_requestGasAndPaymasterAndData`.
    gas_policy_id: String,
}

impl BundlerClient {
    pub fn new(
        url: impl Into<String>,
        entry_point: Address,
        chain_id: u64,
        gas_policy_id: impl Into<String>,
    ) -> Self {
        Self {
            http: reqwest::Client::new(),
            url: url.into(),
            entry_point,
            chain_id,
            gas_policy_id: gas_policy_id.into(),
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

    /// Build, sponsor, sign, submit, and await inclusion. Returns the underlying tx hash.
    pub async fn submit(&self, signer: &PrivateKeySigner, mut op: UserOperation) -> Result<B256> {
        op.nonce = self.get_nonce(op.sender).await.context("EntryPoint.getNonce")?;

        // F2: request gas + paymaster sponsorship and populate the op BEFORE signing.
        self.apply_sponsorship(&mut op).await?;

        // The op is now final (incl. paymasterAndData). Hash it and sign the EIP-191
        // message of the hash, exactly as ClusterMember.validateUserOp will recover.
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

    /// Ask Alchemy for gas limits + paymaster sponsorship for `op` (using a dummy
    /// signature), then populate the op. Must run before signing (F2).
    async fn apply_sponsorship(&self, op: &mut UserOperation) -> Result<()> {
        let partial = json!({
            "sender": op.sender,
            "nonce": format!("0x{:x}", op.nonce),
            "callData": op.call_data,
            "signature": DUMMY_SIGNATURE,
        });
        let params = json!([{
            "policyId": self.gas_policy_id,
            "entryPoint": self.entry_point,
            "dummySignature": DUMMY_SIGNATURE,
            "userOperation": partial,
        }]);
        let resp = self
            .rpc("alchemy_requestGasAndPaymasterAndData", params)
            .await
            .context("alchemy_requestGasAndPaymasterAndData")?;
        apply_sponsorship_response(op, &resp)
    }

    /// EIP-4337 account nonce: `EntryPoint.getNonce(sender, key=0)` via `eth_call`
    /// (there is no bundler RPC for this; the Alchemy endpoint serves both APIs).
    async fn get_nonce(&self, sender: Address) -> Result<U256> {
        let r = self
            .rpc(
                "eth_call",
                json!([{"to": self.entry_point, "data": get_nonce_calldata(sender)}, "latest"]),
            )
            .await?;
        let s: String = serde_json::from_value(r).context("eth_call getNonce result")?;
        U256::from_str_radix(s.trim_start_matches("0x"), 16).context("parse getNonce hex")
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

/// `EntryPoint.getNonce(address,uint192)` calldata: selector `0x35567e1a` plus the
/// two 32-byte-padded args (key fixed to 0). Pure so the encoding is pinned by a
/// unit test — the live-found failure mode here was silent (a wrong nonce source
/// fell back to 0 and every post-registration UserOp died with AA25).
fn get_nonce_calldata(sender: Address) -> String {
    format!("0x35567e1a{:0>64}{:0>64}", hex::encode(sender.as_slice()), "0")
}

/// Populate `op`'s gas + paymaster fields from an `alchemy_requestGasAndPaymasterAndData`
/// result. Pure (no I/O) so the F2 invariant can be unit-tested without a live bundler.
fn apply_sponsorship_response(op: &mut UserOperation, resp: &Value) -> Result<()> {
    let req_u256 = |k: &str| -> Result<U256> {
        resp.get(k)
            .and_then(|v| v.as_str())
            .and_then(|s| U256::from_str_radix(s.trim_start_matches("0x"), 16).ok())
            .with_context(|| format!("sponsorship response missing/invalid {k}"))
    };
    op.call_gas_limit = req_u256("callGasLimit")?;
    op.verification_gas_limit = req_u256("verificationGasLimit")?;
    op.pre_verification_gas = req_u256("preVerificationGas")?;
    op.max_fee_per_gas = req_u256("maxFeePerGas")?;
    op.max_priority_fee_per_gas = req_u256("maxPriorityFeePerGas")?;

    // Paymaster fields are present whenever the policy sponsors this op.
    if let Some(pm) = resp.get("paymaster").and_then(|v| v.as_str()) {
        op.paymaster = Some(pm.parse().context("paymaster address")?);
        op.paymaster_verification_gas_limit = req_u256("paymasterVerificationGasLimit")?;
        op.paymaster_post_op_gas_limit = req_u256("paymasterPostOpGasLimit")?;
        let pd = resp
            .get("paymasterData")
            .and_then(|v| v.as_str())
            .context("sponsorship response has paymaster but no paymasterData")?;
        op.paymaster_data = pd.parse::<Bytes>().context("paymasterData")?;
    }
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_sponsorship() -> Value {
        json!({
            "callGasLimit": "0x186a0",
            "verificationGasLimit": "0x186a0",
            "preVerificationGas": "0xc350",
            "maxFeePerGas": "0x3b9aca00",
            "maxPriorityFeePerGas": "0xf4240",
            "paymaster": "0x1111111111111111111111111111111111111111",
            "paymasterVerificationGasLimit": "0x10000",
            "paymasterPostOpGasLimit": "0x8000",
            "paymasterData": "0xdeadbeef"
        })
    }

    #[test]
    fn sponsorship_response_populates_gas_and_paymaster() {
        let mut op = UserOperation::new(
            Address::repeat_byte(1),
            U256::ZERO,
            Bytes::from(vec![1, 2, 3]),
        );
        apply_sponsorship_response(&mut op, &sample_sponsorship()).unwrap();
        assert_eq!(op.call_gas_limit, U256::from(100_000u64));
        assert_eq!(op.max_fee_per_gas, U256::from(1_000_000_000u64));
        assert!(op.paymaster.is_some());
        assert_eq!(op.paymaster_data.len(), 4); // 0xdeadbeef
    }

    #[test]
    fn sponsorship_response_allows_no_paymaster() {
        let mut op = UserOperation::new(Address::repeat_byte(1), U256::ZERO, Bytes::new());
        let resp = json!({
            "callGasLimit": "0x186a0",
            "verificationGasLimit": "0x186a0",
            "preVerificationGas": "0xc350",
            "maxFeePerGas": "0x1",
            "maxPriorityFeePerGas": "0x1"
        });
        apply_sponsorship_response(&mut op, &resp).unwrap();
        assert!(op.paymaster.is_none());
    }

    /// F2 regression guard: the v0.7 userOpHash MUST commit to paymasterAndData, so a
    /// signature computed before sponsorship would not match the EntryPoint's hash.
    /// Signing must therefore happen AFTER `apply_sponsorship` (see `submit`).
    #[test]
    fn user_op_hash_commits_to_paymaster_fields() {
        let ep = Address::repeat_byte(0xEE);
        let mk = || {
            let mut op = UserOperation::new(
                Address::repeat_byte(1),
                U256::from(7u64),
                Bytes::from(vec![9]),
            );
            op.call_gas_limit = U256::from(100_000u64);
            op.verification_gas_limit = U256::from(100_000u64);
            op.pre_verification_gas = U256::from(50_000u64);
            op.max_fee_per_gas = U256::from(1u64);
            op.max_priority_fee_per_gas = U256::from(1u64);
            op
        };
        let without = mk().user_op_hash(ep, 84532);
        let mut with = mk();
        with.paymaster = Some(Address::repeat_byte(0x22));
        with.paymaster_data = Bytes::from(vec![0xde, 0xad]);
        let with_hash = with.user_op_hash(ep, 84532);
        assert_ne!(
            without, with_hash,
            "paymasterAndData must change the userOpHash; sign AFTER sponsorship (F2)"
        );
    }
}

#[cfg(test)]
mod nonce_tests {
    use super::*;

    /// Regression (live bug 4 in docs/deployment.md): the nonce MUST come from
    /// `EntryPoint.getNonce` via eth_call. Pin the exact calldata encoding so a
    /// drift in selector or padding fails here instead of as a live AA25.
    #[test]
    fn get_nonce_calldata_is_pinned() {
        let sender: Address = "0x54e63929b4d8d09d3c9e3019d54bd20e289ed985"
            .parse()
            .unwrap();
        let data = get_nonce_calldata(sender);
        // selector = first 4 bytes of keccak("getNonce(address,uint192)")
        assert!(data.starts_with("0x35567e1a"));
        // 4-byte selector + 2 × 32-byte args = 2 + 8 + 64 + 64 hex chars
        assert_eq!(data.len(), 2 + 8 + 64 + 64);
        // address left-padded to 32 bytes
        assert!(data[10..74].starts_with("000000000000000000000000"));
        assert!(data[10..74].ends_with("54e63929b4d8d09d3c9e3019d54bd20e289ed985"));
        // key = 0
        assert_eq!(&data[74..], "0".repeat(64));
    }
}
