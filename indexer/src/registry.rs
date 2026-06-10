//! IndexerRegistry self-check (spec §6.2).
//!
//! At boot the indexer reads its own `IndexerRegistry.current()` record and compares
//! it against its attested identity. A mismatch is logged loudly and surfaced as an
//! alert flag, but the indexer does NOT refuse to start — the registry is rotated by
//! the org Safe out-of-band from the indexer rollout, so a transient mismatch is
//! expected during deploys (spec §6.2).

use crate::chain::{read_registry, HttpProvider};
use crate::identity::Identity;
use alloy::primitives::{Address, B256};
use anyhow::Result;

/// Outcome of the boot-time self-check.
#[derive(Debug, Clone, Default)]
pub struct SelfCheck {
    pub code_id_matches: bool,
    pub pubkey_matches: bool,
    pub onchain_endpoint: String,
}

impl SelfCheck {
    pub fn ok(&self) -> bool {
        self.code_id_matches && self.pubkey_matches
    }
}

/// Read `IndexerRegistry.current()` and compare to the indexer's identity + expected
/// code id. Logs at warn/error on mismatch (spec §6.2) and returns the result so the
/// caller can raise an alert metric.
pub async fn run(
    provider: &HttpProvider,
    registry_addr: Address,
    identity: &Identity,
    expected_code_id: B256,
) -> Result<SelfCheck> {
    let record = read_registry(provider, registry_addr).await?;
    let our_pubkey = B256::from_slice(&identity.signing_pubkey());

    let code_id_matches = record.code_id == expected_code_id;
    let pubkey_matches = record.pubkey == our_pubkey;

    if !code_id_matches {
        tracing::error!(
            onchain = %record.code_id, expected = %expected_code_id,
            "IndexerRegistry codeId mismatch — registry not yet pointed at this image, or stale image deployed"
        );
    }
    if !pubkey_matches {
        tracing::error!(
            onchain = %record.pubkey, derived = %our_pubkey,
            "IndexerRegistry pubKey mismatch — new subscribers will verify against the old pubkey until the Safe rotates the record"
        );
    }
    if code_id_matches && pubkey_matches {
        tracing::info!(endpoint = %record.endpoint, "IndexerRegistry self-check OK");
    }

    Ok(SelfCheck {
        code_id_matches,
        pubkey_matches,
        onchain_endpoint: record.endpoint,
    })
}
