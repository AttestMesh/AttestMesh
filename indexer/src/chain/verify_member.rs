//! Membership verification on subscribe (spec §8.2 step 2, §8.3).
//!
//! v1 takes the subscribing member's claim at face value: the on-chain MemberStorage
//! already records the result of attestor-facet verification at registration time, so
//! the indexer trusts that `memberId` existing in storage is sufficient proof of
//! membership. We therefore resolve the member's on-chain record to (a) confirm it
//! exists and (b) recover its `registeredAt` block, which is the floor for the
//! delivery cursor (spec §8.2 step 3). The `Hello.attestation` field is ignored in v1
//! (spec §8.3); milestone-B re-verification will dispatch to the member's attestor.

use super::{HttpProvider, IAttest};
use alloy::primitives::{Address, B256};
use anyhow::{Context, Result};

/// The minimal member facts the subscription path needs.
#[derive(Debug, Clone)]
pub struct MemberFacts {
    pub member_contract: Address,
    /// Block at which the member registered — the cursor floor (no rewinding past a
    /// member's join, spec §8.2 step 3).
    pub registered_at: u64,
}

/// Resolve a `(cluster, memberContract)` pair to its on-chain member record via
/// `AttestFacet.memberOf`. Returns `None` if no such member exists (the member record
/// has a zero `memberContract`).
///
/// NB: the on-chain index is by member-contract address, not by 32-byte memberId. The
/// subscribing sidecar presents both in its `Hello` (its `member_id` and, implicitly,
/// the contract that registered it); v1 verifies via the contract address it holds in
/// the cached registration record. For the prototype this helper looks the record up
/// by member-contract address; the caller maps memberId → contract from the cached
/// `MemberRegistered` event (whose `memberContract` is an indexed topic).
pub async fn member_facts(
    provider: &HttpProvider,
    cluster: Address,
    member_contract: Address,
) -> Result<Option<MemberFacts>> {
    let a = IAttest::new(cluster, provider);
    let r = a
        .memberOf(member_contract)
        .call()
        .await
        .context("AttestFacet.memberOf")?
        ._0;
    if r.memberContract == Address::ZERO {
        return Ok(None);
    }
    Ok(Some(MemberFacts {
        member_contract: r.memberContract,
        registered_at: r.registeredAt,
    }))
}

/// `AttestFacet.memberCount()` — used by the registry/observability path (spec §8.2
/// references the same facet). Exposed for completeness and parity with the sidecar.
pub async fn member_count(provider: &HttpProvider, cluster: Address) -> Result<u64> {
    let a = IAttest::new(cluster, provider);
    let c = a
        .memberCount()
        .call()
        .await
        .context("AttestFacet.memberCount")?
        ._0;
    Ok(c.to::<u64>())
}

/// Convenience: a memberId is a 32-byte value; this maps a left-padded contract
/// address into the B256 topic form used when caching `MemberRegistered`.
pub fn member_contract_topic(addr: Address) -> B256 {
    B256::left_padding_from(addr.as_slice())
}
