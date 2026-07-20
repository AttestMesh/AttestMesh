#!/usr/bin/env python3
"""MCP tools for safely authoring and sending encrypted PG-HA member commands."""
import datetime as dt
import base64, json, os, subprocess
from mcp.server.fastmcp import FastMCP
from protocol import Command, COMMANDS

mcp = FastMCP("attestmesh-pg-ha-commands")
CLUSTER = os.environ.get("PGHA_CLUSTER_NAME", "andrew-xyn-pg")
SOCKET = os.environ.get("AGENT_GRPC_SOCKET", "/var/run/attestmesh/agent.sock")
MEMBERS = json.loads(os.environ.get("PGHA_MEMBER_IDS_JSON", "{}"))

@mcp.tool()
def list_command_types() -> dict:
    """List the closed command allowlist. No arbitrary shell or SQL is supported."""
    return {"protocol":"attestmesh.pgha.command.v1", "commands":sorted(COMMANDS)}

@mcp.tool()
def draft_command(target: str, command: str, reason: str, mode: str="explain",
                  arguments: dict | None=None) -> dict:
    """Create and validate a five-minute PG-HA command without sending it."""
    now = dt.datetime.now(dt.timezone.utc)
    value = Command(cluster=CLUSTER, target=target, command=command, reason=reason,
                    mode=mode, arguments=arguments or {}, issued_at=now,
                    expires_at=now + dt.timedelta(minutes=5))
    return value.model_dump(mode="json")

@mcp.tool()
def validate_command(command_json: str) -> dict:
    """Validate externally drafted command JSON and return canonical JSON."""
    value = Command.model_validate_json(command_json)
    return {"valid": True, "canonical_json": value.canonical().decode()}

@mcp.tool()
def send_command(command_json: str) -> dict:
    """Encrypt and submit a validated command to its target through MessageFacet."""
    value = Command.model_validate_json(command_json)
    recipient = MEMBERS.get(value.target)
    if not recipient: raise ValueError(f"no sealed member ID configured for {value.target}")
    request = json.dumps({"recipientMemberId": recipient,
                          "payload": base64.b64encode(value.canonical()).decode()})
    proc = subprocess.run(["grpcurl", "-unix", SOCKET, "-plaintext", "-d", request,
        "attestmesh.agent.v1.Agent/SendMessage"], text=True, capture_output=True, timeout=90)
    if proc.returncode: raise RuntimeError(proc.stderr.strip() or "sidecar send failed")
    return json.loads(proc.stdout)

if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
