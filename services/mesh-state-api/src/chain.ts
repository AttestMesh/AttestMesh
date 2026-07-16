// On-chain reader (spec §6). Produces output-shaped snapshots pinned to a single
// block so members are never torn across block boundaries.

import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { createPublicClient, http, keccak256, type PublicClient } from "viem";
import { base } from "viem/chains";

// Canonical Multicall3, same address on every major chain incl. Base.
const MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11" as const;
import { clusterAbi, clusterFactoryAbi } from "./abi.ts";
import type { Config } from "./config.ts";
import {
  appIdOf,
  attestorLabel,
  checksum,
  cidrToString,
  deriveEndpoint,
  ipToDotted,
  nonZeroBytes32,
} from "./format.ts";

export interface MemberOut {
  memberId: string;
  memberContract: `0x${string}`;
  appId: string;
  attestorId: string;
  attestor: string;
  xPubKey: string;
  wgPubKey: string;
  meshIp: string;
  registeredAt: number;
  isOriginator: boolean;
  endpoint: string | null;
  vm: null;
  health: null;
}

export interface Snapshot {
  cluster: `0x${string}`;
  chainId: number;
  atBlock: number;
  meshCidr: string;
  cskCommitment: string | null;
  originatorMemberId: string | null;
  memberCount: number;
  members: MemberOut[];
}

export interface ClusterDeployment {
  cluster: `0x${string}`;
  clusterOwner: `0x${string}`;
  salt: string;
  deployedAtBlock: number;
  deploymentTxHash: string;
  deploymentLogIndex: number;
}

interface DiscoveryState {
  chainId: number;
  clusterFactoryAddr: `0x${string}`;
  scannedToBlock: number;
  deployments: ClusterDeployment[];
}

export interface TimelineEvent {
  type: string;
  block: number;
  txHash: string;
  logIndex: number;
  [k: string]: unknown;
}

export interface Timeline {
  cluster: `0x${string}`;
  fromBlock: number;
  toBlock: number;
  events: TimelineEvent[];
}

const RETRYABLE_RPC_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504]);
let rpcGate: Promise<void> = Promise.resolve();
let nextRpcAt = 0;

function sleep(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => signal?.removeEventListener("abort", abort);
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    const abort = () => {
      clearTimeout(timer);
      cleanup();
      reject(signal?.reason instanceof Error ? signal.reason : new Error("aborted"));
    };
    if (signal?.aborted) {
      abort();
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
  });
}

function retryAfterMs(headers: Headers): number | null {
  const raw = headers.get("retry-after")?.trim();
  if (!raw) return null;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.round(seconds * 1000);
  const dateMs = Date.parse(raw);
  if (Number.isFinite(dateMs)) return Math.max(0, dateMs - Date.now());
  return null;
}

function retryDelayMs(attempt: number, cfg: Config, headers?: Headers): number {
  const retryAfter = headers ? retryAfterMs(headers) : null;
  if (retryAfter !== null) return Math.min(retryAfter, cfg.rpcRetryMaxMs);
  const exponential = cfg.rpcRetryBaseMs * 2 ** attempt;
  const jitter = 0.8 + Math.random() * 0.4;
  return Math.min(Math.round(exponential * jitter), cfg.rpcRetryMaxMs);
}

function isReadRpcBody(body: RequestInit["body"] | null | undefined): boolean {
  if (typeof body !== "string") return false;
  try {
    const parsed = JSON.parse(body) as unknown;
    const calls = Array.isArray(parsed) ? parsed : [parsed];
    return calls.every((call) => {
      if (!call || typeof call !== "object" || !("method" in call)) return false;
      const method = (call as { method?: unknown }).method;
      return typeof method === "string" && !method.startsWith("eth_send") && !method.startsWith("wallet_");
    });
  } catch {
    return false;
  }
}

function timeoutSignal(parent: AbortSignal | null | undefined, timeoutMs: number): {
  signal: AbortSignal | null;
  cleanup: () => void;
} {
  if (timeoutMs <= 0) return { signal: parent ?? null, cleanup: () => {} };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(new Error("rpc request timed out")), timeoutMs);
  const abort = () => controller.abort(parent?.reason);
  if (parent?.aborted) abort();
  else parent?.addEventListener("abort", abort, { once: true });
  return {
    signal: controller.signal,
    cleanup: () => {
      clearTimeout(timer);
      parent?.removeEventListener("abort", abort);
    },
  };
}

async function waitForRpcSlot(minIntervalMs: number, signal?: AbortSignal | null): Promise<void> {
  if (minIntervalMs <= 0) return;
  const turn = rpcGate.then(async () => {
    const waitMs = Math.max(0, nextRpcAt - Date.now());
    nextRpcAt = Date.now() + waitMs + minIntervalMs;
    await sleep(waitMs, signal);
  });
  rpcGate = turn.catch(() => {});
  await turn;
}

async function retryingFetch(cfg: Config, input: string | URL | Request, init?: RequestInit): Promise<Response> {
  const readRpc = isReadRpcBody(init?.body);
  const maxAttempts = readRpc ? cfg.rpcRetryCount + 1 : 1;
  let lastError: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const { signal, cleanup } = timeoutSignal(init?.signal, cfg.rpcTimeoutMs);
    try {
      await waitForRpcSlot(cfg.rpcMinIntervalMs, init?.signal);
      const response = await fetch(input, { ...init, signal });
      if (!readRpc || !RETRYABLE_RPC_STATUSES.has(response.status) || attempt === maxAttempts - 1) {
        return response;
      }
      const delayMs = retryDelayMs(attempt, cfg, response.headers);
      console.warn(JSON.stringify({ msg: "mesh-state-api rpc retry", status: response.status, attempt: attempt + 1, delayMs }));
      await sleep(delayMs, init?.signal);
    } catch (err) {
      lastError = err;
      if (init?.signal?.aborted || attempt === maxAttempts - 1) throw err;
      const delayMs = retryDelayMs(attempt, cfg);
      console.warn(JSON.stringify({ msg: "mesh-state-api rpc retry", error: String(err), attempt: attempt + 1, delayMs }));
      await sleep(delayMs, init?.signal);
    } finally {
      cleanup();
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

interface IndexedMember {
  memberId: string;
  memberContract: `0x${string}`;
  attestorId: string;
  xPubKey: string;
  wgPubKey: string;
  registeredAt: number;
  block: number;
  txHash: string;
  logIndex: number;
}

interface IndexedCluster extends ClusterDeployment {
  meshCidrIp: number;
  meshCidrPrefix: number;
  cskCommitment: string | null;
  scannedToBlock: number;
  members: IndexedMember[];
  events: TimelineEvent[];
}

export interface MeshIndexState {
  version: 1;
  chainId: number;
  clusterFactoryAddr: `0x${string}`;
  scannedToBlock: number;
  clusters: IndexedCluster[];
  updatedAt: string | null;
}

export function makeClient(cfg: Config): PublicClient {
  // Chain is Base for v1; still pin the multicall address explicitly (below) so a
  // custom CHAIN_ID doesn't silently lose batching.
  const chain = cfg.chainId === base.id ? base : undefined;
  return createPublicClient({
    chain,
    transport: http(cfg.rpcUrl, {
      fetchFn: (input, init) => retryingFetch(cfg, input, init),
      retryCount: 0,
      timeout: 0,
    }),
  }) as PublicClient;
}

function sortDeployments(deployments: ClusterDeployment[]): ClusterDeployment[] {
  return deployments.sort((a, b) =>
    a.deployedAtBlock === b.deployedAtBlock
      ? a.deploymentLogIndex - b.deploymentLogIndex
      : a.deployedAtBlock - b.deployedAtBlock,
  );
}

async function readDiscoveryState(cfg: Config): Promise<DiscoveryState | null> {
  try {
    const raw = await readFile(cfg.discoveryStatePath, "utf8");
    const state = JSON.parse(raw) as DiscoveryState;
    if (
      state.chainId !== cfg.chainId ||
      state.clusterFactoryAddr.toLowerCase() !== cfg.clusterFactoryAddr.toLowerCase() ||
      !Number.isInteger(state.scannedToBlock) ||
      !Array.isArray(state.deployments)
    ) {
      return null;
    }
    return state;
  } catch {
    return null;
  }
}

async function writeDiscoveryState(cfg: Config, state: DiscoveryState): Promise<void> {
  await mkdir(dirname(cfg.discoveryStatePath), { recursive: true });
  const tmp = `${cfg.discoveryStatePath}.${process.pid}.tmp`;
  await writeFile(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  await rename(tmp, cfg.discoveryStatePath);
}

function emptyIndexState(cfg: Config): MeshIndexState {
  return {
    version: 1,
    chainId: cfg.chainId,
    clusterFactoryAddr: cfg.clusterFactoryAddr,
    scannedToBlock: Number(cfg.clusterFactoryStartBlock - 1n),
    clusters: [],
    updatedAt: null,
  };
}

async function readIndexState(cfg: Config): Promise<MeshIndexState> {
  try {
    const raw = await readFile(cfg.indexStatePath, "utf8");
    const state = JSON.parse(raw) as MeshIndexState;
    if (
      state.version !== 1 ||
      state.chainId !== cfg.chainId ||
      state.clusterFactoryAddr.toLowerCase() !== cfg.clusterFactoryAddr.toLowerCase() ||
      !Number.isInteger(state.scannedToBlock) ||
      !Array.isArray(state.clusters)
    ) {
      return emptyIndexState(cfg);
    }
    const persistedAt =
      typeof state.updatedAt === "string" && Number.isFinite(Date.parse(state.updatedAt))
        ? state.updatedAt
        : (await stat(cfg.indexStatePath)).mtime.toISOString();
    return { ...state, updatedAt: persistedAt };
  } catch {
    return emptyIndexState(cfg);
  }
}

export async function readMeshIndex(cfg: Config): Promise<MeshIndexState> {
  return readIndexState(cfg);
}

async function writeIndexState(cfg: Config, state: MeshIndexState): Promise<void> {
  await mkdir(dirname(cfg.indexStatePath), { recursive: true });
  const tmp = `${cfg.indexStatePath}.${process.pid}.tmp`;
  state.updatedAt = new Date().toISOString();
  await writeFile(tmp, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  await rename(tmp, cfg.indexStatePath);
}

export function meshIndexProgress(state: MeshIndexState): {
  atBlock: number;
  headBlock: number;
  blockLag: number;
} {
  const headBlock = state.scannedToBlock;
  const atBlock = state.clusters.reduce(
    (oldest, cluster) => Math.min(oldest, cluster.scannedToBlock),
    headBlock,
  );
  return { atBlock, headBlock, blockLag: Math.max(0, headBlock - atBlock) };
}

async function blockTimestamp(
  client: PublicClient,
  blockNumber: bigint,
  cache: Map<string, number>,
): Promise<number> {
  const key = blockNumber.toString();
  const cached = cache.get(key);
  if (cached !== undefined) return cached;
  const block = await client.getBlock({ blockNumber });
  const timestamp = Number(block.timestamp);
  cache.set(key, timestamp);
  return timestamp;
}

function addTimelineEvent(cluster: IndexedCluster, event: TimelineEvent): void {
  cluster.events.push(event);
  cluster.events.sort((a, b) => (a.block === b.block ? a.logIndex - b.logIndex : a.block - b.block));
}

function deriveMeshIp(memberId: string, cidrIp: number, cidrPrefix: number): number {
  const hostCount = 1n << BigInt(32 - cidrPrefix);
  if (hostCount <= 2n) return cidrIp;
  const hash = keccak256(memberId as `0x${string}`);
  const low = BigInt(`0x${hash.slice(-8)}`);
  const offset = Number((low % (hostCount - 2n)) + 1n);
  return cidrIp | offset;
}

function deploymentFromIndexed(cluster: IndexedCluster): ClusterDeployment {
  return {
    cluster: cluster.cluster,
    clusterOwner: cluster.clusterOwner,
    salt: cluster.salt,
    deployedAtBlock: cluster.deployedAtBlock,
    deploymentTxHash: cluster.deploymentTxHash,
    deploymentLogIndex: cluster.deploymentLogIndex,
  };
}

export async function fetchClusterDeployments(client: PublicClient, cfg: Config): Promise<ClusterDeployment[]> {
  const toBlock = await client.getBlockNumber();
  const state = await readDiscoveryState(cfg);
  const byCluster = new Map<string, ClusterDeployment>(
    state?.deployments.map((d) => [d.cluster.toLowerCase(), d]) ?? [],
  );
  const fromStart = state ? BigInt(state.scannedToBlock) + 1n : cfg.clusterFactoryStartBlock;

  for (let from = fromStart; from <= toBlock; from += cfg.logChunkBlocks + 1n) {
    const to = from + cfg.logChunkBlocks > toBlock ? toBlock : from + cfg.logChunkBlocks;
    const logs = await client.getLogs({
      address: cfg.clusterFactoryAddr,
      fromBlock: from,
      toBlock: to,
      events: clusterFactoryAbi as never,
    });
    for (const log of logs as never[]) {
      const l = log as {
        blockNumber: bigint;
        transactionHash: string;
        logIndex: number;
        args?: {
          cluster?: `0x${string}`;
          clusterOwner?: `0x${string}`;
          salt?: string;
        };
      };
      const cluster = l.args?.cluster;
      const clusterOwner = l.args?.clusterOwner;
      if (!cluster || !clusterOwner) continue;
      byCluster.set(cluster.toLowerCase(), {
        cluster: checksum(cluster),
        clusterOwner: checksum(clusterOwner),
        salt: typeof l.args?.salt === "string" ? l.args.salt.toLowerCase() : "0x",
        deployedAtBlock: Number(l.blockNumber),
        deploymentTxHash: l.transactionHash,
        deploymentLogIndex: Number(l.logIndex),
      });
    }
    await writeDiscoveryState(cfg, {
      chainId: cfg.chainId,
      clusterFactoryAddr: cfg.clusterFactoryAddr,
      scannedToBlock: Number(to),
      deployments: sortDeployments([...byCluster.values()]),
    });
  }

  const deployments = sortDeployments([...byCluster.values()]);
  await writeDiscoveryState(cfg, {
    chainId: cfg.chainId,
    clusterFactoryAddr: cfg.clusterFactoryAddr,
    scannedToBlock: Number(toBlock),
    deployments,
  });
  return deployments;
}

export async function updateMeshIndex(client: PublicClient, cfg: Config): Promise<MeshIndexState> {
  const toBlockBig = await client.getBlockNumber();
  const toBlock = Number(toBlockBig);
  const state = await readIndexState(cfg);
  const clustersByAddr = new Map(state.clusters.map((c) => [c.cluster.toLowerCase(), c]));
  const timestampCache = new Map<string, number>();

  for (let from = BigInt(state.scannedToBlock) + 1n; from <= toBlockBig; from += cfg.logChunkBlocks + 1n) {
    const to = from + cfg.logChunkBlocks > toBlockBig ? toBlockBig : from + cfg.logChunkBlocks;
    const logs = await client.getLogs({
      address: cfg.clusterFactoryAddr,
      fromBlock: from,
      toBlock: to,
      events: clusterFactoryAbi as never,
    });
    for (const log of logs as never[]) {
      const l = log as {
        blockNumber: bigint;
        transactionHash: string;
        logIndex: number;
        args?: {
          cluster?: `0x${string}`;
          clusterOwner?: `0x${string}`;
          salt?: string;
        };
      };
      const cluster = l.args?.cluster;
      const clusterOwner = l.args?.clusterOwner;
      if (!cluster || !clusterOwner || clustersByAddr.has(cluster.toLowerCase())) continue;
      const [cidrIp, cidrPrefix] = (await client.readContract({
        address: cluster,
        abi: clusterAbi,
        functionName: "meshCidr",
        blockNumber: l.blockNumber,
      })) as readonly [number, number];
      const indexed: IndexedCluster = {
        cluster: checksum(cluster),
        clusterOwner: checksum(clusterOwner),
        salt: typeof l.args?.salt === "string" ? l.args.salt.toLowerCase() : "0x",
        deployedAtBlock: Number(l.blockNumber),
        deploymentTxHash: l.transactionHash,
        deploymentLogIndex: Number(l.logIndex),
        meshCidrIp: Number(cidrIp),
        meshCidrPrefix: Number(cidrPrefix),
        cskCommitment: null,
        scannedToBlock: Number(l.blockNumber) - 1,
        members: [],
        events: [
          {
            type: "ClusterDeployed",
            block: Number(l.blockNumber),
            txHash: l.transactionHash,
            logIndex: Number(l.logIndex),
            cluster: checksum(cluster),
            clusterOwner: checksum(clusterOwner),
            salt: typeof l.args?.salt === "string" ? l.args.salt.toLowerCase() : "0x",
          },
        ],
      };
      state.clusters.push(indexed);
      clustersByAddr.set(cluster.toLowerCase(), indexed);
    }
    state.scannedToBlock = Number(to);
    state.clusters = sortDeployments(state.clusters) as IndexedCluster[];
    await writeIndexState(cfg, state);
  }
  state.scannedToBlock = toBlock;

  const eventAbi = clusterAbi.filter((e) => e.type === "event");
  for (const cluster of state.clusters) {
    for (let from = BigInt(cluster.scannedToBlock) + 1n; from <= toBlockBig; from += cfg.logChunkBlocks + 1n) {
      const to = from + cfg.logChunkBlocks > toBlockBig ? toBlockBig : from + cfg.logChunkBlocks;
      const logs = await client.getLogs({
        address: cluster.cluster,
        fromBlock: from,
        toBlock: to,
        events: eventAbi as never,
      });
      for (const log of logs as never[]) {
        const l = log as {
          eventName?: string;
          blockNumber: bigint;
          transactionHash: string;
          logIndex: number;
          args?: Record<string, unknown>;
        };
        if (!l.eventName) continue;
        const event: TimelineEvent = {
          type: l.eventName,
          block: Number(l.blockNumber),
          txHash: l.transactionHash,
          logIndex: Number(l.logIndex),
        };
        for (const [k, v] of Object.entries(l.args ?? {})) {
          event[k] = typeof v === "bigint" ? Number(v) : typeof v === "string" ? v.toLowerCase() : v;
        }

        if (l.eventName === "MemberRegistered") {
          const memberId = l.args?.memberId;
          const memberContract = l.args?.memberContract;
          const attestorId = l.args?.attestorId;
          const xPubKey = l.args?.xPubKey;
          const wgPubKey = l.args?.wgPubKey;
          if (
            typeof memberId === "string" &&
            typeof memberContract === "string" &&
            typeof attestorId === "string" &&
            typeof xPubKey === "string" &&
            typeof wgPubKey === "string" &&
            !cluster.members.some((m) => m.memberId === memberId.toLowerCase())
          ) {
            cluster.members.push({
              memberId: memberId.toLowerCase(),
              memberContract: checksum(memberContract as `0x${string}`),
              attestorId: attestorId.toLowerCase(),
              xPubKey: xPubKey.toLowerCase(),
              wgPubKey: wgPubKey.toLowerCase(),
              registeredAt: await blockTimestamp(client, l.blockNumber, timestampCache),
              block: Number(l.blockNumber),
              txHash: l.transactionHash,
              logIndex: Number(l.logIndex),
            });
            cluster.members.sort((a, b) => (a.block === b.block ? a.logIndex - b.logIndex : a.block - b.block));
          }
        } else if (l.eventName === "WgKeyPublished") {
          const memberId = l.args?.memberId;
          const wgPubKey = l.args?.wgPubKey;
          if (typeof memberId === "string" && typeof wgPubKey === "string") {
            const member = cluster.members.find((m) => m.memberId === memberId.toLowerCase());
            if (member) member.wgPubKey = wgPubKey.toLowerCase();
          }
        } else if (l.eventName === "CskCommitmentSet") {
          const commitment = l.args?.commitment;
          if (typeof commitment === "string") cluster.cskCommitment = commitment.toLowerCase();
        }

        addTimelineEvent(cluster, event);
      }
      cluster.scannedToBlock = Number(to);
      await writeIndexState(cfg, state);
    }
  }

  state.clusters = sortDeployments(state.clusters) as IndexedCluster[];
  await writeIndexState(cfg, state);
  return state;
}

export function deploymentsFromIndex(state: MeshIndexState): ClusterDeployment[] {
  return sortDeployments(state.clusters.map(deploymentFromIndexed));
}

export function snapshotsFromIndex(state: MeshIndexState, cfg: Config): Snapshot[] {
  return state.clusters.map((cluster) => {
    const members: MemberOut[] = cluster.members.map((member, i) => {
      const appId = appIdOf(member.memberContract);
      return {
        memberId: member.memberId,
        memberContract: member.memberContract,
        appId,
        attestorId: member.attestorId,
        attestor: attestorLabel(member.attestorId),
        xPubKey: member.xPubKey,
        wgPubKey: member.wgPubKey,
        meshIp: ipToDotted(deriveMeshIp(member.memberId, cluster.meshCidrIp, cluster.meshCidrPrefix)),
        registeredAt: member.registeredAt,
        isOriginator: i === 0,
        endpoint: deriveEndpoint(appId, cfg.gatewayDomain),
        vm: null,
        health: null,
      };
    });
    return {
      cluster: cluster.cluster,
      chainId: state.chainId,
      atBlock: cluster.scannedToBlock,
      meshCidr: cidrToString(cluster.meshCidrIp, cluster.meshCidrPrefix),
      cskCommitment: cluster.cskCommitment,
      originatorMemberId: members[0]?.memberId ?? null,
      memberCount: members.length,
      members,
    };
  });
}

export function timelinesFromIndex(state: MeshIndexState): Timeline[] {
  return state.clusters.map((cluster) => ({
    cluster: cluster.cluster,
    fromBlock: cluster.deployedAtBlock,
    toBlock: cluster.scannedToBlock,
    events: [...cluster.events].sort((a, b) => (a.block === b.block ? a.logIndex - b.logIndex : a.block - b.block)),
  }));
}

export async function fetchSnapshot(
  client: PublicClient,
  cfg: Config,
  cluster: ClusterDeployment,
): Promise<Snapshot> {
  const address = cluster.cluster;
  const atBlock = await client.getBlockNumber();

  const [memberIds, cidr, cskRaw] = await Promise.all([
    client.readContract({ address, abi: clusterAbi, functionName: "listMembers", blockNumber: atBlock }),
    client.readContract({ address, abi: clusterAbi, functionName: "meshCidr", blockNumber: atBlock }),
    client.readContract({ address, abi: clusterAbi, functionName: "cskCommitment", blockNumber: atBlock }),
  ]);

  const ids = memberIds as readonly `0x${string}`[];
  const [cidrIp, cidrPrefix] = cidr as readonly [number, number];

  // One multicall for every per-member read, pinned to the same block.
  const calls = ids.flatMap((id) => [
    { address, abi: clusterAbi, functionName: "memberById", args: [id] } as const,
    { address, abi: clusterAbi, functionName: "meshIpOf", args: [id] } as const,
  ]);
  const results =
    calls.length === 0
      ? []
      : await client.multicall({
          contracts: calls,
          blockNumber: atBlock,
          allowFailure: false,
          multicallAddress: MULTICALL3,
        });

  const members: MemberOut[] = ids.map((id, i) => {
    const rec = results[i * 2] as {
      attestorId: `0x${string}`;
      memberContract: `0x${string}`;
      xPubKey: `0x${string}`;
      wgPubKey: `0x${string}`;
      registeredAt: bigint;
    };
    const meshIpPacked = results[i * 2 + 1] as number | bigint;
    const appId = appIdOf(rec.memberContract);
    return {
      memberId: id.toLowerCase(),
      memberContract: checksum(rec.memberContract),
      appId,
      attestorId: rec.attestorId.toLowerCase(),
      attestor: attestorLabel(rec.attestorId),
      xPubKey: rec.xPubKey.toLowerCase(),
      wgPubKey: rec.wgPubKey.toLowerCase(),
      meshIp: ipToDotted(meshIpPacked),
      registeredAt: Number(rec.registeredAt),
      isOriginator: i === 0,
      endpoint: deriveEndpoint(appId, cfg.gatewayDomain),
      vm: null,
      health: null,
    };
  });

  return {
    cluster: checksum(address),
    chainId: cfg.chainId,
    atBlock: Number(atBlock),
    meshCidr: cidrToString(cidrIp, cidrPrefix),
    cskCommitment: nonZeroBytes32(cskRaw as string),
    originatorMemberId: ids[0] ? ids[0].toLowerCase() : null,
    memberCount: ids.length,
    members,
  };
}

export async function fetchTimeline(
  client: PublicClient,
  cfg: Config,
  cluster: ClusterDeployment,
): Promise<Timeline> {
  const address = cluster.cluster;
  const toBlock = await client.getBlockNumber();
  const events: TimelineEvent[] = [];

  const eventAbi = clusterAbi.filter((e) => e.type === "event");
  const fromBlock = BigInt(cluster.deployedAtBlock);
  for (let from = fromBlock; from <= toBlock; from += cfg.logChunkBlocks + 1n) {
    const to = from + cfg.logChunkBlocks > toBlock ? toBlock : from + cfg.logChunkBlocks;
    // Chunked to stay under the ~10k-block public-RPC getLogs cap (spec §6).
    const logs = await client.getLogs({ address, fromBlock: from, toBlock: to, events: eventAbi as never });
    for (const log of logs as never[]) {
      const l = log as {
        eventName?: string;
        blockNumber: bigint;
        transactionHash: string;
        logIndex: number;
        args?: Record<string, unknown>;
      };
      if (!l.eventName) continue; // skip logs from other (non-tracked) events
      const base: TimelineEvent = {
        type: l.eventName,
        block: Number(l.blockNumber),
        txHash: l.transactionHash,
        logIndex: Number(l.logIndex),
      };
      for (const [k, v] of Object.entries(l.args ?? {})) {
        base[k] = typeof v === "bigint" ? Number(v) : typeof v === "string" ? v.toLowerCase() : v;
      }
      events.push(base);
    }
  }

  events.sort((a, b) => (a.block === b.block ? a.logIndex - b.logIndex : a.block - b.block));
  return { cluster: checksum(address), fromBlock: Number(fromBlock), toBlock: Number(toBlock), events };
}
