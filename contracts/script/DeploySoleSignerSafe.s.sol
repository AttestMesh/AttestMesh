// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Script } from "forge-std/Script.sol";
import { console2 } from "forge-std/console2.sol";

interface ISafeProxyFactory {
    function createChainSpecificProxyWithNonce(
        address singleton,
        bytes memory initializer,
        uint256 saltNonce
    ) external returns (address proxy);
}

interface ISafeSetup {
    function setup(
        address[] calldata owners,
        uint256 threshold,
        address to,
        bytes calldata data,
        address fallbackHandler,
        address paymentToken,
        uint256 payment,
        address payable paymentReceiver
    ) external;
}

/// Deploys an unmodified SafeL2 1.4.1 proxy with exactly one owner and threshold one.
contract DeploySoleSignerSafe is Script {
    function run() external {
        uint256 pk = vm.envUint("PRIVATE_KEY");
        address signer = vm.addr(pk);
        address singleton = vm.envAddress("SAFE_SINGLETON");
        address factory = vm.envAddress("SAFE_PROXY_FACTORY");
        address fallbackHandler = vm.envAddress("SAFE_FALLBACK_HANDLER");
        uint256 saltNonce = vm.envUint("SAFE_SALT_NONCE");
        address[] memory owners = new address[](1);
        owners[0] = signer;
        bytes memory initializer = abi.encodeCall(
            ISafeSetup.setup,
            (owners, 1, address(0), bytes(""), fallbackHandler, address(0), 0, payable(address(0)))
        );
        vm.startBroadcast(pk);
        address safe = ISafeProxyFactory(factory)
            .createChainSpecificProxyWithNonce(singleton, initializer, saltNonce);
        vm.stopBroadcast();
        console2.log("Safe deployed:", safe);
        console2.log("Safe sole owner:", signer);
    }
}
