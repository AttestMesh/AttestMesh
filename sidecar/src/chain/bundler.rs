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

/// Which sponsorship dialect the bundler endpoint speaks. Detected from the URL so
/// cutover is a pure env change (`BUNDLER_URL`), no new config knob.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SponsorshipMode {
    /// `alchemy_requestGasAndPaymasterAndData` (policyId keyed).
    Alchemy,
    /// Pimlico: `pimlico_getUserOperationGasPrice` + `pm_sponsorUserOperation`
    /// (sponsorshipPolicyId context). NOTE: Pimlico sponsorships expire ~10 minutes
    /// after issuance — safe here because `submit` builds + sponsors per attempt and
    /// nothing caches a signed op (docs/research/paymaster-provider-selection.md).
    Pimlico,
}

fn detect_mode(url: &str) -> SponsorshipMode {
    if url.contains("pimlico.io") {
        SponsorshipMode::Pimlico
    } else {
        SponsorshipMode::Alchemy
    }
}

pub struct BundlerClient {
    http: reqwest::Client,
    url: String,
    entry_point: Address,
    chain_id: u64,
    /// Sponsorship policy id: Alchemy Gas Manager `policyId` or Pimlico
    /// `sponsorshipPolicyId` (`sp_…`), depending on `mode`.
    gas_policy_id: String,
    mode: SponsorshipMode,
}

impl BundlerClient {
    pub fn new(
        url: impl Into<String>,
        entry_point: Address,
        chain_id: u64,
        gas_policy_id: impl Into<String>,
    ) -> Self {
        let url = url.into();
        let mode = detect_mode(&url);
        Self {
            http: reqwest::Client::new(),
            url,
            entry_point,
            chain_id,
            gas_policy_id: gas_policy_id.into(),
            mode,
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

    /// Request gas limits + paymaster sponsorship for `op` (using a dummy signature),
    /// then populate the op. Must run before signing (F2). Dispatches on the detected
    /// provider dialect.
    async fn apply_sponsorship(&self, op: &mut UserOperation) -> Result<()> {
        match self.mode {
            SponsorshipMode::Alchemy => self.apply_sponsorship_alchemy(op).await,
            SponsorshipMode::Pimlico => self.apply_sponsorship_pimlico(op).await,
        }
    }

    /// Pimlico flow: fees FIRST (the paymaster signature commits to them), then one
    /// `pm_sponsorUserOperation` call returns gas limits + paymaster fields.
    async fn apply_sponsorship_pimlico(&self, op: &mut UserOperation) -> Result<()> {
        let prices = self
            .rpc("pimlico_getUserOperationGasPrice", json!([]))
            .await
            .context("pimlico_getUserOperationGasPrice")?;
        apply_pimlico_gas_price(op, &prices)?;

        let mut partial = op.clone();
        partial.signature = DUMMY_SIGNATURE
            .parse::<Bytes>()
            .expect("static dummy signature parses");
        let params = if self.gas_policy_id.is_empty() {
            json!([userop_json(&partial), self.entry_point])
        } else {
            json!([
                userop_json(&partial),
                self.entry_point,
                { "sponsorshipPolicyId": self.gas_policy_id }
            ])
        };
        let resp = self
            .rpc("pm_sponsorUserOperation", params)
            .await
            .context("pm_sponsorUserOperation")?;
        apply_pimlico_sponsorship_response(op, &resp)
    }

    /// Alchemy Gas Manager flow: one proprietary call returns fees, gas limits, and
    /// paymaster fields together.
    async fn apply_sponsorship_alchemy(&self, op: &mut UserOperation) -> Result<()> {
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

/// Set `op`'s fee fields from a `pimlico_getUserOperationGasPrice` result ("standard"
/// tier). Pure so it is unit-testable without a live endpoint.
fn apply_pimlico_gas_price(op: &mut UserOperation, resp: &Value) -> Result<()> {
    let tier = resp
        .get("standard")
        .context("gas price response missing 'standard' tier")?;
    let req_u256 = |k: &str| -> Result<U256> {
        tier.get(k)
            .and_then(|v| v.as_str())
            .and_then(|s| U256::from_str_radix(s.trim_start_matches("0x"), 16).ok())
            .with_context(|| format!("gas price response missing/invalid standard.{k}"))
    };
    op.max_fee_per_gas = req_u256("maxFeePerGas")?;
    op.max_priority_fee_per_gas = req_u256("maxPriorityFeePerGas")?;
    Ok(())
}

/// Populate `op`'s gas limits + paymaster fields from a `pm_sponsorUserOperation`
/// (v0.7) result. Fee fields were set beforehand and are NOT in this response.
/// Pure (no I/O) so the F2 invariant stays unit-testable.
fn apply_pimlico_sponsorship_response(op: &mut UserOperation, resp: &Value) -> Result<()> {
    let req_u256 = |k: &str| -> Result<U256> {
        resp.get(k)
            .and_then(|v| v.as_str())
            .and_then(|s| U256::from_str_radix(s.trim_start_matches("0x"), 16).ok())
            .with_context(|| format!("pm_sponsorUserOperation missing/invalid {k}"))
    };
    op.call_gas_limit = req_u256("callGasLimit")?;
    op.verification_gas_limit = req_u256("verificationGasLimit")?;
    op.pre_verification_gas = req_u256("preVerificationGas")?;

    // Unlike Alchemy's method, a pm_sponsorUserOperation success ALWAYS sponsors:
    // missing paymaster fields mean a malformed response, not "not sponsored".
    let pm = resp
        .get("paymaster")
        .and_then(|v| v.as_str())
        .context("pm_sponsorUserOperation response missing paymaster")?;
    op.paymaster = Some(pm.parse().context("paymaster address")?);
    op.paymaster_verification_gas_limit = req_u256("paymasterVerificationGasLimit")?;
    op.paymaster_post_op_gas_limit = req_u256("paymasterPostOpGasLimit")?;
    let pd = resp
        .get("paymasterData")
        .and_then(|v| v.as_str())
        .context("pm_sponsorUserOperation response missing paymasterData")?;
    op.paymaster_data = pd.parse::<Bytes>().context("paymasterData")?;
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
mod pimlico_tests {
    use super::*;

    #[test]
    fn mode_detected_from_url() {
        assert_eq!(
            detect_mode("https://api.pimlico.io/v2/8453/rpc?apikey=x"),
            SponsorshipMode::Pimlico
        );
        assert_eq!(
            detect_mode("https://base-mainnet.g.alchemy.com/v2/key"),
            SponsorshipMode::Alchemy
        );
    }

    #[test]
    fn gas_price_applies_standard_tier() {
        let mut op = UserOperation::new(Address::repeat_byte(1), U256::ZERO, Bytes::new());
        let resp = json!({
            "slow": {"maxFeePerGas": "0x1", "maxPriorityFeePerGas": "0x1"},
            "standard": {"maxFeePerGas": "0x3b9aca00", "maxPriorityFeePerGas": "0xf4240"},
            "fast": {"maxFeePerGas": "0x77359400", "maxPriorityFeePerGas": "0x1e8480"}
        });
        apply_pimlico_gas_price(&mut op, &resp).unwrap();
        assert_eq!(op.max_fee_per_gas, U256::from(1_000_000_000u64));
        assert_eq!(op.max_priority_fee_per_gas, U256::from(1_000_000u64));
    }

    #[test]
    fn sponsorship_response_populates_limits_and_paymaster() {
        let mut op = UserOperation::new(Address::repeat_byte(1), U256::ZERO, Bytes::new());
        op.max_fee_per_gas = U256::from(7u64); // set beforehand; must survive untouched
        let resp = json!({
            "callGasLimit": "0x186a0",
            "verificationGasLimit": "0x186a0",
            "preVerificationGas": "0xc350",
            "paymaster": "0x2222222222222222222222222222222222222222",
            "paymasterVerificationGasLimit": "0x10000",
            "paymasterPostOpGasLimit": "0x8000",
            "paymasterData": "0xdeadbeef"
        });
        apply_pimlico_sponsorship_response(&mut op, &resp).unwrap();
        assert_eq!(op.call_gas_limit, U256::from(100_000u64));
        assert_eq!(op.max_fee_per_gas, U256::from(7u64), "fees set pre-sponsorship survive");
        assert!(op.paymaster.is_some());
        assert_eq!(op.paymaster_data.len(), 4);
    }

    #[test]
    fn sponsorship_response_without_paymaster_is_an_error() {
        let mut op = UserOperation::new(Address::repeat_byte(1), U256::ZERO, Bytes::new());
        let resp = json!({
            "callGasLimit": "0x186a0",
            "verificationGasLimit": "0x186a0",
            "preVerificationGas": "0xc350"
        });
        assert!(apply_pimlico_sponsorship_response(&mut op, &resp).is_err());
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
