// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Initializable } from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {
    UUPSUpgradeable
} from "@openzeppelin/contracts-upgradeable/proxy/utils/UUPSUpgradeable.sol";
import {
    OwnableUpgradeable
} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";

/// @title MockStockApp — stand-in for dstack's stock `DstackApp` (the contract `phala
///        deploy` mints for a base-KMS app_id).
/// @notice Models exactly the bits Path A depends on: a UUPS proxy, OZ v5 `Initializable`
///         (so its `_initialized` slot aligns with `ClusterMember`'s), a single
///         `initialize` that leaves `_initialized == 1`, and an `onlyOwner` upgrade gate.
///         Used to prove `ClusterMember.reinitializeFromDstackApp` can re-seat such a proxy
///         via `upgradeToAndCall` and that the v1 `initialize` slot is burned afterwards.
contract MockStockApp is Initializable, UUPSUpgradeable, OwnableUpgradeable {
    function initialize(address owner_) external initializer {
        __Ownable_init(owner_);
        __UUPSUpgradeable_init();
    }

    function _authorizeUpgrade(address) internal override onlyOwner { }
}
