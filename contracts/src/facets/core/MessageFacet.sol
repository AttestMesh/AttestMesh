// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { IMessage } from "../../interfaces/IMessage.sol";
import { MemberStorage } from "../../storage/MemberStorage.sol";
import { MessageStorage } from "../../storage/MessageStorage.sol";
import { ClusterAccess } from "../../access/ClusterAccess.sol";
import { DuplicateEnvelope, RecipientNotMember } from "../../errors/Errors.sol";

/// @title MessageFacet — encrypted member-to-member messaging (contracts spec §5.2).
/// @notice Ciphertexts are emitted as event data only; never stored. Per-(recipient,
///         envelopeId) dedup makes sends idempotent.
contract MessageFacet is IMessage, ClusterAccess {
    function send(bytes32 recipientMemberId, bytes32 envelopeId, bytes calldata ciphertext)
        external
        onlyClusterMember
    {
        if (MemberStorage.layout().members[recipientMemberId].memberContract == address(0)) {
            revert RecipientNotMember();
        }

        MessageStorage.Layout storage m = MessageStorage.layout();
        if (m.envelopeNonces[recipientMemberId][envelopeId]) revert DuplicateEnvelope();
        m.envelopeNonces[recipientMemberId][envelopeId] = true;

        emit MessageSent(_senderMemberId(), recipientMemberId, envelopeId, ciphertext);
    }

    /// @notice Encrypted Safe-owner control-plane message. This is deliberately not a generic
    /// owner impersonation path: the event carries a domain-separated pseudo member ID, allowing
    /// application agents to authorize the cluster owner without treating it as a mesh peer.
    function sendOwnerCommand(
        bytes32 recipientMemberId,
        bytes32 envelopeId,
        bytes calldata ciphertext
    ) external onlyClusterOwner {
        if (MemberStorage.layout().members[recipientMemberId].memberContract == address(0)) {
            revert RecipientNotMember();
        }
        MessageStorage.Layout storage m = MessageStorage.layout();
        if (m.envelopeNonces[recipientMemberId][envelopeId]) revert DuplicateEnvelope();
        m.envelopeNonces[recipientMemberId][envelopeId] = true;
        bytes32 ownerSenderId =
            keccak256(abi.encodePacked("attestmesh.cluster-owner.v1", msg.sender));
        emit MessageSent(ownerSenderId, recipientMemberId, envelopeId, ciphertext);
    }
}
