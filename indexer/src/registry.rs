//! IndexerRegistry self-check and cluster-shared gRPC admission (spec §6.2).
//!
//! At boot the indexer reads its own `IndexerRegistry.current()` record and compares
//! it against its attested identity. Instance mode retains the historical non-fatal
//! check. Cluster-shared mode may warm its HTTP/read model as a candidate, but may
//! bind gRPC only while both the v1 registry pubkey and code ID match.

use crate::chain::{read_registry, HttpProvider};
use crate::identity::Identity;
use alloy::primitives::{Address, B256};
use anyhow::{Context, Result};
use std::time::Duration;

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

    pub fn authorized_shared(&self) -> bool {
        self.ok()
    }
}

/// Poll policy for the shared-mode admission boundary. One registry read already
/// contains the chain module's bounded RPC retries; three failed reads therefore
/// represent a sustained outage rather than a single transient request failure.
#[derive(Debug, Clone, Copy)]
pub struct AdmissionPolicy {
    pub poll_interval: Duration,
    pub max_consecutive_errors: usize,
}

impl Default for AdmissionPolicy {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_secs(5),
            max_consecutive_errors: 3,
        }
    }
}

#[derive(Debug)]
struct ErrorBudget {
    consecutive: usize,
    limit: usize,
}

impl ErrorBudget {
    fn new(limit: usize) -> Self {
        Self {
            consecutive: 0,
            limit: limit.max(1),
        }
    }

    fn success(&mut self) {
        self.consecutive = 0;
    }

    fn failure(&mut self) -> bool {
        self.consecutive += 1;
        self.consecutive >= self.limit
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

/// Wait for the v1 registry to authorize this exact shared identity and measured
/// compose hash. Mismatches are an expected candidate window and wait indefinitely;
/// sustained read failures return an error so the process remains fail closed.
pub async fn wait_for_shared_authorization(
    provider: &HttpProvider,
    registry_addr: Address,
    identity: &Identity,
    expected_code_id: B256,
    policy: AdmissionPolicy,
) -> Result<SelfCheck> {
    let mut errors = ErrorBudget::new(policy.max_consecutive_errors);
    loop {
        match run(provider, registry_addr, identity, expected_code_id).await {
            Ok(check) if check.authorized_shared() => {
                tracing::info!("shared-mode registry admission granted");
                return Ok(check);
            }
            Ok(_) => {
                errors.success();
                tracing::warn!(
                    retry_ms = policy.poll_interval.as_millis(),
                    "shared-mode candidate warming; gRPC remains closed until registry pubkey and codeId match"
                );
            }
            Err(error) => {
                if errors.failure() {
                    return Err(error).context(format!(
                        "IndexerRegistry unreadable for {} consecutive admission checks",
                        errors.consecutive
                    ));
                }
                tracing::warn!(
                    error = %error,
                    consecutive_errors = errors.consecutive,
                    retry_ms = policy.poll_interval.as_millis(),
                    "shared-mode registry admission read failed; gRPC remains closed"
                );
            }
        }
        tokio::time::sleep(policy.poll_interval).await;
    }
}

/// Once gRPC is open, keep checking authorization. A registry mismatch revokes the
/// listener immediately. Transient read errors consume the same bounded budget;
/// sustained errors revoke it fail closed.
pub async fn monitor_shared_authorization(
    provider: &HttpProvider,
    registry_addr: Address,
    identity: &Identity,
    expected_code_id: B256,
    policy: AdmissionPolicy,
) -> Result<SelfCheck> {
    let mut errors = ErrorBudget::new(policy.max_consecutive_errors);
    loop {
        tokio::time::sleep(policy.poll_interval).await;
        match run(provider, registry_addr, identity, expected_code_id).await {
            Ok(check) if check.authorized_shared() => errors.success(),
            Ok(check) => {
                tracing::error!(
                    "shared-mode registry authorization changed; closing gRPC listener"
                );
                return Ok(check);
            }
            Err(error) => {
                if errors.failure() {
                    return Err(error).context(format!(
                        "IndexerRegistry unreadable for {} consecutive serving checks",
                        errors.consecutive
                    ));
                }
                tracing::warn!(
                    error = %error,
                    consecutive_errors = errors.consecutive,
                    "shared-mode registry serving check failed"
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_admission_requires_both_matches() {
        let admitted = SelfCheck {
            code_id_matches: true,
            pubkey_matches: true,
            ..Default::default()
        };
        assert!(admitted.authorized_shared());
        assert!(!SelfCheck {
            code_id_matches: false,
            ..admitted.clone()
        }
        .authorized_shared());
        assert!(!SelfCheck {
            pubkey_matches: false,
            ..admitted
        }
        .authorized_shared());
    }

    #[test]
    fn successful_read_resets_sustained_error_budget() {
        let mut budget = ErrorBudget::new(3);
        assert!(!budget.failure());
        assert!(!budget.failure());
        budget.success();
        assert!(!budget.failure());
        assert!(!budget.failure());
        assert!(budget.failure());
    }

    #[test]
    fn zero_error_limit_still_fails_closed() {
        let mut budget = ErrorBudget::new(0);
        assert!(budget.failure());
    }
}
