//! `mesh-voucher` — mint OperatorProof registration vouchers (multi-attestor spec).
//!
//! Agent-friendly and scriptable: flags + env, hex output on stdout, no wallet GUI.
//! Because the voucher signs over the node's pubkeys, provisioning is one step:
//! by default this generates the node's seed file AND the voucher together, so a
//! single run yields both `ATTESTOR_SEED_PATH` and `OPERATOR_VOUCHER`. Alternatively
//! pass `--x-pub/--wg-pub/--owner` from a node's first-boot log to sign for a seed
//! that already exists elsewhere.
//!
//! TRUST DISCLOSURE: a voucher admits a member on the operator's signature alone —
//! NOT hardware attestation. The voucher itself is not secret (useless without the
//! node's seed), but the signer key and the seed file are.
//!
//! Usage:
//!   OPERATOR_SIGNER_KEY=0x<32-byte-hex> mesh-voucher \
//!       --cluster 0x<diamond> --member 0x<cluster-member> \
//!       [--seed-path ./attestor-seed]            # default mode: seed + voucher
//!       [--x-pub 0x.. --wg-pub 0x.. --owner 0x..] # external-pubkeys mode
//!       [--expiry-secs 604800]                    # voucher lifetime (default 7d)
//!       [--json]                                  # emit JSON instead of hex CBOR

use alloy::primitives::{Address, B256};
use alloy::signers::local::PrivateKeySigner;
use alloy::signers::SignerSync;
use anyhow::{bail, Context, Result};
use cluster_mesh_agent::attestor::operator::{
    encode_voucher_hex, key_material_from_seed, load_or_create_seed, operator_bind_hash,
    owner_signer_from_seed, OperatorVoucher,
};
use std::collections::HashMap;

const DEFAULT_EXPIRY_SECS: u64 = 7 * 24 * 3600;

fn parse_flags() -> Result<HashMap<String, String>> {
    let mut flags = HashMap::new();
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        let Some(key) = a.strip_prefix("--") else {
            bail!("unexpected argument {a:?} (flags are --key value)");
        };
        if key == "json" {
            flags.insert("json".to_string(), "true".to_string());
            continue;
        }
        let v = args
            .next()
            .with_context(|| format!("--{key} needs a value"))?;
        flags.insert(key.to_string(), v);
    }
    Ok(flags)
}

fn parse_b256(label: &str, s: &str) -> Result<[u8; 32]> {
    let b: B256 = s
        .trim()
        .parse()
        .with_context(|| format!("--{label}: bad 32-byte hex"))?;
    Ok(b.0)
}

fn main() -> Result<()> {
    let flags = parse_flags()?;

    let cluster: Address = flags
        .get("cluster")
        .context("--cluster is required")?
        .parse()
        .context("--cluster: bad address")?;
    let member: Address = flags
        .get("member")
        .context("--member is required")?
        .parse()
        .context("--member: bad address")?;

    let signer_key = std::env::var("OPERATOR_SIGNER_KEY")
        .context("OPERATOR_SIGNER_KEY env var is required (allowlisted operator key)")?;
    let signer = PrivateKeySigner::from_slice(
        &hex::decode(signer_key.trim().trim_start_matches("0x"))
            .context("OPERATOR_SIGNER_KEY: bad hex")?,
    )
    .context("OPERATOR_SIGNER_KEY: not a valid secp256k1 key")?;

    let expiry_secs: u64 = match flags.get("expiry-secs") {
        Some(s) => s.parse().context("--expiry-secs: bad number")?,
        None => DEFAULT_EXPIRY_SECS,
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .context("system clock before epoch")?
        .as_secs();
    let expiry = now + expiry_secs;

    // Pubkeys: either supplied (a node's first-boot log) or derived from a seed
    // file this run creates/loads — the single-provisioning-step path.
    let external =
        flags.contains_key("x-pub") || flags.contains_key("wg-pub") || flags.contains_key("owner");
    let (x_pub, wg_pub, owner_key, seed_note) = if external {
        let x = parse_b256(
            "x-pub",
            flags
                .get("x-pub")
                .context("--x-pub required with --owner")?,
        )?;
        let w = parse_b256(
            "wg-pub",
            flags
                .get("wg-pub")
                .context("--wg-pub required with --owner")?,
        )?;
        let o: Address = flags
            .get("owner")
            .context("--owner required with --x-pub/--wg-pub")?
            .parse()
            .context("--owner: bad address")?;
        (x, w, o, None)
    } else {
        let seed_path = flags
            .get("seed-path")
            .cloned()
            .unwrap_or_else(|| "./attestor-seed".to_string());
        let seed = load_or_create_seed(&seed_path)?;
        let km = key_material_from_seed(&seed);
        let owner = owner_signer_from_seed(&seed)?;
        (km.x_pub, km.wg_pub, owner.address(), Some(seed_path))
    };

    let bind = operator_bind_hash(
        cluster,
        member,
        B256::from(x_pub),
        B256::from(wg_pub),
        owner_key,
        expiry,
    );
    let sig = signer
        .sign_message_sync(bind.as_slice())
        .context("sign voucher bind hash")?;

    let voucher = OperatorVoucher {
        cluster: cluster.to_string(),
        member_contract: member.to_string(),
        x_pub: format!("0x{}", hex::encode(x_pub)),
        wg_pub: format!("0x{}", hex::encode(wg_pub)),
        signer: signer.address().to_string(),
        owner_key: owner_key.to_string(),
        expiry,
        signature: format!("0x{}", hex::encode(sig.as_bytes())),
    };

    eprintln!("mesh-voucher: TRUST NOTE — this voucher admits the member on your");
    eprintln!(
        "signature alone (no hardware attestation). Signer: {}",
        voucher.signer
    );
    if let Some(path) = seed_note {
        eprintln!("seed file: {path} (deploy as ATTESTOR_SEED_PATH, keep mode 0600)");
    }
    eprintln!("expiry: {expiry} (unix)");

    if flags.contains_key("json") {
        println!("{}", serde_json::to_string_pretty(&voucher)?);
    } else {
        println!("{}", encode_voucher_hex(&voucher)?);
    }
    Ok(())
}
