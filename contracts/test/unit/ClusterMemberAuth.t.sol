// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import { Test } from "forge-std/Test.sol";

import {
    PackedUserOperation
} from "@account-abstraction/contracts/interfaces/PackedUserOperation.sol";

import { ClusterMember } from "../../src/members/ClusterMember.sol";
import { ClusterMemberFactory } from "../../src/members/ClusterMemberFactory.sol";
import { IAppAuth } from "../../src/interfaces/IAppAuth.sol";
import { IClusterMember } from "../../src/interfaces/IClusterMember.sol";
import {
    OnlyEntryPoint,
    OnlyCluster,
    InvalidBootstrapCall,
    NotClusterOwner
} from "../../src/errors/Errors.sol";

/// @notice A stand-in cluster: answers the IAppAuth/IAppAuthBasicManagement forwards
///         and `clusterOwner()` (UUPS upgrade authority) so every ClusterMember
///         pass-through and auth branch is exercisable without a full diamond.
contract MockClusterAuth {
    address public clusterOwner;
    bytes32 public lastComposeHash;
    bytes32 public lastDeviceId;
    bool public lastAllowAny;
    bool public lastRequireTcb;

    constructor(address owner_) {
        clusterOwner = owner_;
    }

    function isAppAllowed(IAppAuth.AppBootInfo calldata info)
        external
        pure
        returns (bool, string memory)
    {
        // Echo a recognizable decision so the forward is observable end-to-end.
        if (info.composeHash == keccak256("good")) return (true, "");
        return (false, "mock-denied");
    }

    function addComposeHash(bytes32 h) external {
        lastComposeHash = h;
    }

    function removeComposeHash(bytes32 h) external {
        lastComposeHash = h;
    }

    function addDevice(bytes32 d) external {
        lastDeviceId = d;
    }

    function removeDevice(bytes32 d) external {
        lastDeviceId = d;
    }

    function setAllowAnyDevice(bool v) external {
        lastAllowAny = v;
    }

    function setRequireTcbUpToDate(bool v) external {
        lastRequireTcb = v;
    }

    function allowedComposeHashes(bytes32 h) external pure returns (bool) {
        return h == keccak256("good");
    }

    function allowedDeviceIds(bytes32 d) external pure returns (bool) {
        return d == keccak256("dev");
    }

    function allowAnyDevice() external pure returns (bool) {
        return true;
    }

    function requireTcbUpToDate() external pure returns (bool) {
        return false;
    }

    function version() external pure returns (uint256) {
        return 7;
    }
}

/// @notice Reverting call target for `execute` bubble-up coverage.
contract Bomb {
    error Boom(uint256 code);

    function explode() external pure {
        revert Boom(42);
    }
}

/// @notice ClusterMember auth + forwarding branches not reachable from the
///         happy-path 4337 tests: execute gating and revert bubbling, the
///         cluster-mediated owner install, the bootstrap-shape rejections,
///         prefund payment, the IAppAuth(BasicManagement) forwards, and the
///         UUPS upgrade authority.
contract ClusterMemberAuthTest is Test {
    ClusterMemberFactory internal factory;
    ClusterMember internal member;
    MockClusterAuth internal clusterMock;
    address internal entryPoint;
    address internal clusterOwner = address(0xC1A55);

    function setUp() public {
        clusterMock = new MockClusterAuth(clusterOwner);
        address impl = address(new ClusterMember());
        factory = new ClusterMemberFactory(impl, address(0xA11CE));
        member = ClusterMember(payable(factory.deployMember(address(clusterMock), keccak256("m"))));
        entryPoint = member.ENTRY_POINT();
    }

    // ── execute ───────────────────────────────────────────────────────────────

    function test_executeOnlyEntryPoint() public {
        vm.expectRevert(OnlyEntryPoint.selector);
        member.execute(address(clusterMock), 0, "");
    }

    function test_executeForwardsCall() public {
        vm.prank(entryPoint);
        member.execute(
            address(clusterMock), 0, abi.encodeCall(MockClusterAuth.addComposeHash, (bytes32("h")))
        );
        assertEq(clusterMock.lastComposeHash(), bytes32("h"));
    }

    function test_executeBubblesRevert() public {
        Bomb bomb = new Bomb();
        vm.prank(entryPoint);
        vm.expectRevert(abi.encodeWithSelector(Bomb.Boom.selector, 42));
        member.execute(address(bomb), 0, abi.encodeCall(Bomb.explode, ()));
    }

    // ── validateUserOp gating + malformed signatures ──────────────────────────

    function test_validateOnlyEntryPoint() public {
        PackedUserOperation memory op;
        vm.expectRevert(OnlyEntryPoint.selector);
        member.validateUserOp(op, bytes32(0), 0);
    }

    function test_malformedSignatureFailsValidation() public {
        PackedUserOperation memory op;
        op.signature = hex"deadbeef"; // not a 65-byte ECDSA signature
        vm.prank(entryPoint);
        uint256 v = member.validateUserOp(op, keccak256("h"), 0);
        assertEq(v, 1, "SIG_VALIDATION_FAILED");
    }

    function test_prefundIsPaidEvenOnFailedValidation() public {
        vm.deal(address(member), 1 ether);
        PackedUserOperation memory op;
        op.signature = hex"deadbeef";
        uint256 epBefore = entryPoint.balance;
        vm.prank(entryPoint);
        member.validateUserOp(op, keccak256("h"), 0.5 ether);
        assertEq(entryPoint.balance, epBefore + 0.5 ether, "missingAccountFunds forwarded");
    }

    // ── bootstrap-shape rejections (owner == 0 path) ──────────────────────────

    function _signedOp(bytes memory callData) internal returns (PackedUserOperation memory op) {
        // A structurally valid 65-byte signature so tryRecover succeeds and the
        // bootstrap parser (not the recover) is what rejects.
        (, bytes32 r, bytes32 s) = vm.sign(0xB17D, keccak256("anything"));
        op.sender = address(member);
        op.callData = callData;
        op.signature = abi.encodePacked(r, s, uint8(27));
    }

    function test_bootstrapRejectsNonExecuteCalldata() public {
        PackedUserOperation memory op = _signedOp(abi.encodeWithSignature("notExecute()"));
        vm.prank(entryPoint);
        vm.expectRevert(InvalidBootstrapCall.selector);
        member.validateUserOp(op, keccak256("h"), 0);
    }

    function test_bootstrapRejectsWrongTarget() public {
        bytes memory callData = abi.encodeWithSelector(
            ClusterMember.execute.selector, address(0xBEEF), uint256(0), bytes("")
        );
        PackedUserOperation memory op = _signedOp(callData);
        vm.prank(entryPoint);
        vm.expectRevert(InvalidBootstrapCall.selector);
        member.validateUserOp(op, keccak256("h"), 0);
    }

    function test_bootstrapRejectsNonzeroValue() public {
        bytes memory callData = abi.encodeWithSelector(
            ClusterMember.execute.selector, address(clusterMock), uint256(1), bytes("")
        );
        PackedUserOperation memory op = _signedOp(callData);
        vm.prank(entryPoint);
        vm.expectRevert(InvalidBootstrapCall.selector);
        member.validateUserOp(op, keccak256("h"), 0);
    }

    function test_bootstrapRejectsWrongInnerSelector() public {
        bytes memory inner = abi.encodeWithSignature("send(bytes32,bytes32,bytes)");
        bytes memory callData = abi.encodeWithSelector(
            ClusterMember.execute.selector, address(clusterMock), uint256(0), inner
        );
        PackedUserOperation memory op = _signedOp(callData);
        vm.prank(entryPoint);
        vm.expectRevert(InvalidBootstrapCall.selector);
        member.validateUserOp(op, keccak256("h"), 0);
    }

    // ── cluster-mediated owner install ────────────────────────────────────────

    function test_setOwnerOnlyCluster() public {
        vm.expectRevert(OnlyCluster.selector);
        member.__setOwnerFromCluster(address(1));
    }

    /// Skip-if-set (multi-attestor spec): a second owner-set succeeds, keeps the
    /// existing owner, and emits OwnerSetSkipped so the ignored key is observable.
    function test_setOwnerSkipIfSet() public {
        vm.prank(address(clusterMock));
        member.__setOwnerFromCluster(address(1));
        vm.prank(address(clusterMock));
        vm.expectEmit(true, true, true, true, address(member));
        emit IClusterMember.OwnerSetSkipped(address(1), address(2));
        member.__setOwnerFromCluster(address(2));
        assertEq(member.owner(), address(1), "existing owner must be untouched");
    }

    // ── IAppAuth / IAppAuthBasicManagement forwards ───────────────────────────

    function test_isAppAllowedForwards() public view {
        IAppAuth.AppBootInfo memory info;
        info.composeHash = keccak256("good");
        (bool ok,) = member.isAppAllowed(info);
        assertTrue(ok);
        info.composeHash = keccak256("bad");
        (bool ok2, string memory reason) = member.isAppAllowed(info);
        assertFalse(ok2);
        assertEq(reason, "mock-denied");
    }

    function test_managementForwards() public {
        member.addComposeHash(bytes32("a"));
        assertEq(clusterMock.lastComposeHash(), bytes32("a"));
        member.removeComposeHash(bytes32("b"));
        assertEq(clusterMock.lastComposeHash(), bytes32("b"));
        member.addDevice(bytes32("d1"));
        assertEq(clusterMock.lastDeviceId(), bytes32("d1"));
        member.removeDevice(bytes32("d2"));
        assertEq(clusterMock.lastDeviceId(), bytes32("d2"));
        member.setAllowAnyDevice(true);
        assertTrue(clusterMock.lastAllowAny());
        member.setRequireTcbUpToDate(true);
        assertTrue(clusterMock.lastRequireTcb());
    }

    function test_viewForwards() public view {
        assertTrue(member.allowedComposeHashes(keccak256("good")));
        assertFalse(member.allowedComposeHashes(keccak256("bad")));
        assertTrue(member.allowedDeviceIds(keccak256("dev")));
        assertTrue(member.allowAnyDevice());
        assertFalse(member.requireTcbUpToDate());
        assertEq(member.version(), 7);
    }

    // ── UUPS upgrade authority ────────────────────────────────────────────────

    function test_upgradeRejectsNonClusterOwner() public {
        address newImpl = address(new ClusterMember());
        vm.prank(address(0xBEEF));
        vm.expectRevert(NotClusterOwner.selector);
        member.upgradeToAndCall(newImpl, "");
    }

    function test_upgradeAllowsClusterOwner() public {
        address newImpl = address(new ClusterMember());
        vm.prank(clusterOwner);
        member.upgradeToAndCall(newImpl, "");
    }

    // ── receive ───────────────────────────────────────────────────────────────

    function test_receivesEth() public {
        vm.deal(address(this), 1 ether);
        (bool ok,) = address(member).call{ value: 0.1 ether }("");
        assertTrue(ok);
        assertEq(address(member).balance, 0.1 ether);
    }
}
