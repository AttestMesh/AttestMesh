// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { EnumerableSet } from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";

/// @title OperatorStorage — operator-signature attestor state (multi-attestor spec).
/// @notice Owned by OperatorFacet. Holds the cluster-owner-managed set of operator
///         signer addresses whose ECDSA vouchers admit members. TRUST NOTE: members
///         admitted through this namespace are vouched for by a key, not by hardware
///         attestation (see OperatorFacet).
library OperatorStorage {
    /// keccak256(abi.encode(uint256(keccak256("attestmesh.storage.Operator")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 internal constant SLOT =
        0x205db981323e45ca31d5a3fd0354827409c9c954e0978888dcfe2a883515bc00;

    struct Layout {
        EnumerableSet.AddressSet signers;
    }

    function layout() internal pure returns (Layout storage l) {
        bytes32 slot = SLOT;
        assembly {
            l.slot := slot
        }
    }
}
