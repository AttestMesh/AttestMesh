// Minimal ABI for the AttestMesh cluster diamond — AttestFacet views + events
// (spec §6). Signatures verbatim from contracts/src/facets/core/AttestFacet.sol
// and interfaces/INetwork.sol / IAttest.sol.

export const clusterAbi = [
  {
    type: "function",
    name: "listMembers",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "bytes32[]" }],
  },
  {
    type: "function",
    name: "memberCount",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "uint256" }],
  },
  {
    // returns MemberStorage.MemberRecord
    type: "function",
    name: "memberById",
    stateMutability: "view",
    inputs: [{ name: "memberId", type: "bytes32" }],
    outputs: [
      {
        type: "tuple",
        components: [
          { name: "attestorId", type: "bytes32" },
          { name: "memberContract", type: "address" },
          { name: "xPubKey", type: "bytes32" },
          { name: "wgPubKey", type: "bytes32" },
          { name: "registeredAt", type: "uint64" },
        ],
      },
    ],
  },
  {
    type: "function",
    name: "meshIpOf",
    stateMutability: "view",
    inputs: [{ name: "memberId", type: "bytes32" }],
    outputs: [{ type: "uint32" }],
  },
  {
    type: "function",
    name: "meshCidr",
    stateMutability: "view",
    inputs: [],
    outputs: [
      { name: "ip", type: "uint32" },
      { name: "prefix", type: "uint8" },
    ],
  },
  {
    type: "function",
    name: "cskCommitment",
    stateMutability: "view",
    inputs: [],
    outputs: [{ type: "bytes32" }],
  },
  {
    type: "event",
    name: "MemberRegistered",
    inputs: [
      { name: "memberId", type: "bytes32", indexed: true },
      { name: "memberContract", type: "address", indexed: true },
      { name: "attestorId", type: "bytes32", indexed: true },
      { name: "xPubKey", type: "bytes32", indexed: false },
      { name: "wgPubKey", type: "bytes32", indexed: false },
    ],
  },
  {
    type: "event",
    name: "CskCommitmentSet",
    inputs: [{ name: "commitment", type: "bytes32", indexed: false }],
  },
  {
    type: "event",
    name: "WgKeyPublished",
    inputs: [
      { name: "memberId", type: "bytes32", indexed: true },
      { name: "wgPubKey", type: "bytes32", indexed: false },
    ],
  },
] as const;

export const clusterFactoryAbi = [
  {
    type: "event",
    name: "ClusterDeployed",
    inputs: [
      { name: "cluster", type: "address", indexed: true },
      { name: "clusterOwner", type: "address", indexed: true },
      { name: "salt", type: "bytes32", indexed: false },
    ],
  },
] as const;
