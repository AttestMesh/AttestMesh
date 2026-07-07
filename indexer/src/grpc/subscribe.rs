//! Per-subscription helpers (spec §8.2, §11 step 5).
//!
//! The attestation is attached to the FIRST push of each session only; subsequent
//! pushes omit it (the subscriber caches the attestation result for the stream's
//! lifetime). This module owns that "first envelope of the session" decoration and the
//! signing call so both the catch-up replay and the live stream share one path.

use super::envelope;
use crate::identity::Identity;
use crate::pb::{IndexerAttestation, PushEnvelope};
use alloy::primitives::B256;
use std::sync::atomic::{AtomicBool, Ordering};

/// Build the `IndexerAttestation` for this indexer instance (spec §6.1, §8.1).
pub fn attestation_for(identity: &Identity, expected_code_id: B256) -> IndexerAttestation {
    IndexerAttestation {
        quote: identity.quote().to_vec(),
        expected_code_id: expected_code_id.as_slice().to_vec(),
        expected_pubkey: identity.signing_pubkey().to_vec(),
    }
}

/// Tracks whether the session has already emitted its attestation.
pub struct SessionAttestation {
    sent: AtomicBool,
    attestation: IndexerAttestation,
}

impl SessionAttestation {
    pub fn new(attestation: IndexerAttestation) -> Self {
        Self {
            sent: AtomicBool::new(false),
            attestation,
        }
    }

    /// Sign `env` and, if this is the first call for the session, attach the
    /// attestation (spec §11 steps 3–5). Signing always happens over the
    /// signature-and-attestation-excluded view, so attaching the attestation after
    /// signing is correct and intentional.
    pub fn finalize(&self, identity: &Identity, mut env: PushEnvelope) -> PushEnvelope {
        envelope::sign(identity.signing_key(), &mut env);
        // `swap(true)` returns the prior value: only the first caller sees `false`.
        if !self.sent.swap(true, Ordering::SeqCst) {
            env.indexer_attestation = Some(self.attestation.clone());
        }
        env
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::chain::repro::build_stub;
    use crate::chain::watcher::{EventKind, IndexedLog};
    use crate::dstack::MockDstack;
    use alloy::primitives::Address;

    fn fixture_env() -> PushEnvelope {
        let log = IndexedLog {
            cluster_addr: Address::repeat_byte(0xc1),
            block_number: 10,
            tx_hash: B256::repeat_byte(0xbb),
            log_index: 0,
            topics: vec![B256::repeat_byte(0xaa)],
            data: vec![1],
            kind: EventKind::WgKeyPublished {
                member_id: B256::repeat_byte(0x01),
                wg_pub_key: B256::repeat_byte(0x02),
            },
        };
        envelope::build_envelope(&log, &build_stub(&log))
    }

    #[tokio::test]
    async fn attestation_only_on_first_envelope() {
        let id = Identity::derive(&MockDstack::from_label("idx"))
            .await
            .unwrap();
        let att = attestation_for(&id, B256::repeat_byte(0x07));
        let session = SessionAttestation::new(att);

        let first = session.finalize(&id, fixture_env());
        let second = session.finalize(&id, fixture_env());

        assert!(
            first.indexer_attestation.is_some(),
            "first push carries attestation"
        );
        assert!(second.indexer_attestation.is_none(), "later pushes omit it");
        // Both are validly signed regardless.
        assert!(envelope::verify(id.verifying_key(), &first));
        assert!(envelope::verify(id.verifying_key(), &second));
    }
}
