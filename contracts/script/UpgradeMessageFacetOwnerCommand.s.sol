// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";
import { IERC2535DiamondCut } from "@solidstate/contracts/interfaces/IERC2535DiamondCut.sol";
import {
    IERC2535DiamondCutInternal
} from "@solidstate/contracts/interfaces/IERC2535DiamondCutInternal.sol";

import { MessageFacet } from "../src/facets/core/MessageFacet.sol";
import { IMessage } from "../src/interfaces/IMessage.sol";

interface ISoleSignerSafe {
    function nonce() external view returns (uint256);
    function getTransactionHash(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address refundReceiver,
        uint256 nonce_
    ) external view returns (bytes32);
    function execTransaction(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address payable refundReceiver,
        bytes calldata signatures
    ) external payable returns (bool success);
}

/// Installs MessageFacet.sendOwnerCommand on an existing Safe-owned cluster.
/// The deployer must be the sole signer of the threshold-one stock Safe. The
/// facet deployment and Safe-executed diamond cut are broadcast in one script.
///
/// Env: PRIVATE_KEY, CLUSTER, PGHA_SAFE_ADDRESS.
contract UpgradeMessageFacetOwnerCommand is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address cluster = vm.envAddress("CLUSTER");
        ISoleSignerSafe safe = ISoleSignerSafe(vm.envAddress("PGHA_SAFE_ADDRESS"));

        vm.startBroadcast(pk);
        MessageFacet facet = new MessageFacet();

        bytes4[] memory selectors = new bytes4[](1);
        selectors[0] = IMessage.sendOwnerCommand.selector;
        IERC2535DiamondCutInternal.FacetCut[] memory cut =
            new IERC2535DiamondCutInternal.FacetCut[](1);
        cut[0] = IERC2535DiamondCutInternal.FacetCut({
            target: address(facet),
            action: IERC2535DiamondCutInternal.FacetCutAction.ADD,
            selectors: selectors
        });
        bytes memory data = abi.encodeCall(IERC2535DiamondCut.diamondCut, (cut, address(0), ""));

        _safeExec(safe, cluster, data, pk);
        vm.stopBroadcast();

        (bool ok, bytes memory ret) = cluster.staticcall(
            abi.encodeWithSignature("facetAddress(bytes4)", IMessage.sendOwnerCommand.selector)
        );
        require(ok && abi.decode(ret, (address)) == address(facet), "selector install not verified");
        console2.log("cluster:                    ", cluster);
        console2.log("new MessageFacet:           ", address(facet));
        console2.logBytes4(IMessage.sendOwnerCommand.selector);
    }

    function _safeExec(ISoleSignerSafe safe, address cluster, bytes memory data, uint256 pk)
        internal
    {
        bytes32 txHash = safe.getTransactionHash(
            cluster, 0, data, 0, 0, 0, 0, address(0), address(0), safe.nonce()
        );
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, txHash);
        require(
            safe.execTransaction(
                cluster,
                0,
                data,
                0,
                0,
                0,
                0,
                address(0),
                payable(address(0)),
                abi.encodePacked(r, s, v)
            ),
            "Safe diamondCut failed"
        );
    }
}
