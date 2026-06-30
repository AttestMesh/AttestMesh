//! Solidity ABI bindings (sidecar spec §2.2). Generated from the contracts spec's
//! external surface via alloy's `sol!` macro. Read interfaces carry `#[sol(rpc)]`
//! so they can be called against a provider; the submit interfaces are used purely
//! to ABI-encode inner calldata for UserOperations.

use alloy::sol;

sol! {
    #[sol(rpc)]
    #[derive(Debug)]
    interface IAttest {
        struct MemberRecord {
            bytes32 attestorId;
            address memberContract;
            bytes32 xPubKey;
            bytes32 wgPubKey;
            uint64 registeredAt;
        }
        function memberOf(address account) external view returns (MemberRecord memory);
        function memberById(bytes32 memberId) external view returns (MemberRecord memory);
        function memberIdOf(address account) external view returns (bytes32);
        function memberCount() external view returns (uint256);
        function cskCommitment() external view returns (bytes32);
        function xPubKeyOf(bytes32 memberId) external view returns (bytes32);
        function listMembers() external view returns (bytes32[] memory);
        function meshCidr() external view returns (uint32 ip, uint8 prefix);
        function meshIpOf(bytes32 memberId) external view returns (uint32);
    }

    #[sol(rpc)]
    interface IClusterMemberView {
        function cluster() external view returns (address);
    }

    // MessageFacet event, decoded from `eth_getLogs` during mesh bring-up.
    #[sol(rpc)]
    #[derive(Debug)]
    interface IMessageEvents {
        event MessageSent(
            bytes32 indexed senderMemberId,
            bytes32 indexed recipientMemberId,
            bytes32 indexed envelopeId,
            bytes ciphertext
        );
    }

    #[sol(rpc)]
    interface IndexerRegistryView {
        function current()
            external
            view
            returns (string endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt);
    }

    // ── Submit calls (encoded as inner UserOp calldata) ───────────────────────

    // Real dstack KMS sig-chain proof shape (ported from TeeSQL/dstackgres). Field
    // order MUST match contracts' IDstackFacet.DstackProof exactly (ABI + selector).
    #[derive(Debug)]
    struct DstackProof {
        bytes32 codeId;
        bytes32 messageHash;
        bytes messageSignature;
        bytes appSignature;
        bytes kmsSignature;
        bytes derivedCompressedPubkey;
        bytes appCompressedPubkey;
        string purpose;
    }

    function dstack_register(
        DstackProof proof,
        address memberContract,
        bytes32 xPubKey,
        bytes32 wgPubKey
    ) external returns (bytes32);

    function publishWgKey(bytes32 wgPubKey) external;
    function send(bytes32 recipientMemberId, bytes32 envelopeId, bytes ciphertext) external;
    function setCskCommitment(bytes32 commitment) external;

    // ClusterMember.execute — the outer wrapper the gas webhook gates on.
    function execute(address target, uint256 value, bytes data) external;
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy::sol_types::SolCall;

    /// Cross-language ABI guard (audit AUDIT_1780519998 finding 2). The
    /// `dstack_register` selector is keccak256 of its canonical signature, which
    /// expands the full `DstackProof` tuple — so pinning the same literal here
    /// (alloy `sol!`), in the contracts (`IDstackFacet.dstack_register.selector`,
    /// `test/unit/Selectors.t.sol`), and in the gas-webhook (`selectors.spec.ts`)
    /// makes any field-order/type drift in one encoder fail that language's test.
    #[test]
    fn dstack_register_selector_is_pinned() {
        assert_eq!(dstack_registerCall::SELECTOR, [0x53, 0x7d, 0x49, 0x1c]);
    }
}
