// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IMessage — encrypted member-to-member messaging (contracts spec §5.2).
interface IMessage {
    function send(bytes32 recipientMemberId, bytes32 envelopeId, bytes calldata ciphertext) external;

    event MessageSent(
        bytes32 indexed senderMemberId,
        bytes32 indexed recipientMemberId,
        bytes32 indexed envelopeId,
        bytes ciphertext
    );
}
