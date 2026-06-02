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
        function memberIdOf(address account) external view returns (bytes32);
        function memberCount() external view returns (uint256);
        function cskCommitment() external view returns (bytes32);
        function xPubKeyOf(bytes32 memberId) external view returns (bytes32);
        function listMembers() external view returns (bytes32[] memory);
        function meshIpOf(bytes32 memberId) external view returns (uint32);
    }

    #[sol(rpc)]
    interface IClusterMemberView {
        function cluster() external view returns (address);
    }

    #[sol(rpc)]
    interface IndexerRegistryView {
        function current()
            external
            view
            returns (string endpoint, bytes32 codeId, bytes32 pubKey, uint64 updatedAt);
    }

    // ── Submit calls (encoded as inner UserOp calldata) ───────────────────────

    #[derive(Debug)]
    struct DstackProof {
        bytes kmsRootPubKey;
        bytes appKey;
        bytes appKeySig;
        bytes32 appComposeHash;
        bytes derivedPubKey;
        bytes derivedKeySig;
        bytes32 derivedInstanceId;
        bytes32 derivedDeviceId;
        string tcbStatus;
        string[] advisoryIds;
        bytes bindingSig;
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
