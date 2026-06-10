// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { MemberStorage } from "../storage/MemberStorage.sol";
import { NotClusterOwner, NotClusterMember, NotInternalCall } from "../errors/Errors.sol";

/// @title ClusterAccess — shared facet access-control modifiers.
/// @notice The cluster-wide config (`clusterOwner`) and membership index live in
///         the shared MemberStorage namespace and are *read* by every facet's
///         access modifiers (contracts spec §4.1, §5.1). Writes to MemberStorage
///         still go exclusively through AttestFacet's internal selectors.
abstract contract ClusterAccess {
    modifier onlyClusterOwner() {
        if (msg.sender != MemberStorage.layout().clusterOwner) revert NotClusterOwner();
        _;
    }

    modifier onlyClusterMember() {
        if (MemberStorage.layout().memberIdOf[msg.sender] == bytes32(0)) {
            revert NotClusterMember();
        }
        _;
    }

    /// @notice Gate for internal facet-to-facet selectors: only the diamond itself
    ///         (a delegatecall-executing sibling facet) may call.
    modifier onlyInternal() {
        if (msg.sender != address(this)) revert NotInternalCall();
        _;
    }

    function _senderMemberId() internal view returns (bytes32) {
        return MemberStorage.layout().memberIdOf[msg.sender];
    }
}
