#!/usr/bin/env python3
"""Consume decrypted on-chain commands and return bounded Patroni reasoning results."""
import base64, datetime as dt, json, os, sqlite3, subprocess, urllib.request
from eth_hash.auto import keccak
from protocol import Command

SOCKET=os.environ.get("AGENT_GRPC_SOCKET", "/var/run/attestmesh/agent.sock")
NODE=os.environ["PGHA_NODE_NAME"]; CLUSTER=os.environ["PGHA_CLUSTER_NAME"]
SAFE=bytes.fromhex(os.environ["PGHA_SAFE_ADDRESS"].lower().removeprefix("0x"))
OWNER_SENDER=keccak(b"attestmesh.cluster-owner.v1"+SAFE).hex()
DB=sqlite3.connect("/var/lib/pgha-command-agent/replay.sqlite3")
DB.execute("create table if not exists seen(id text primary key, at text not null)"); DB.commit()

def local_status(command):
    if command.command.startswith("patroni."):
        with urllib.request.urlopen("http://sidecar:8008/cluster", timeout=5) as r: return json.load(r)
    with open("/pgha-status/walg", encoding="utf-8") as f: return {"tail":f.readlines()[-30:]}

def explain(command, status):
    prompt=("You are a PostgreSQL 16 and Patroni 4 incident analyst. Interpret only the supplied "
            "diagnostics. Never emit shell commands, SQL, credentials, URLs, or instructions to "
            "bypass quorum. Explain evidence, uncertainty, and a safe operator plan.\n"+json.dumps(status))
    body=json.dumps({"model":os.environ["LLM_MODEL"], "messages":[{"role":"user","content":prompt}],
                     "temperature":0}).encode()
    req=urllib.request.Request(os.environ["LLM_BASE_URL"].rstrip("/")+"/chat/completions", body,
        {"Authorization":"Bearer "+os.environ["LLM_API_KEY"], "Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)["choices"][0]["message"]["content"]

def send(recipient, payload):
    request=json.dumps({"recipientMemberId":recipient,
        "payload":base64.b64encode(json.dumps(payload, separators=(",",":")).encode()).decode()})
    subprocess.run(["grpcurl","-unix",SOCKET,"-plaintext","-d",request,
        "attestmesh.agent.v1.Agent/SendMessage"], check=True, timeout=90)

def handle(event):
    sender=base64.b64decode(event["senderMemberId"]).hex()
    if sender != OWNER_SENDER: return
    command=Command.model_validate_json(base64.b64decode(event["payload"]))
    now=dt.datetime.now(dt.timezone.utc)
    if command.cluster != CLUSTER or command.target != NODE or not(command.issued_at <= now <= command.expires_at): return
    try: DB.execute("insert into seen values (?,?)",(str(command.command_id),now.isoformat())); DB.commit()
    except sqlite3.IntegrityError: return
    status=local_status(command)
    result={"protocol":"attestmesh.pgha.result.v1","command_id":str(command.command_id),
            "node":NODE,"observed_at":now.isoformat(),"status":status}
    if command.mode == "explain" or command.command.endswith(("explain","plan")):
        result["interpretation"]=explain(command,status)
    DB.execute("create table if not exists results(id text primary key, result text not null)")
    DB.execute("insert or replace into results values (?,?)",(str(command.command_id),json.dumps(result)))
    DB.commit()

def main():
    proc=subprocess.Popen(["grpcurl","-unix",SOCKET,"-plaintext",
        "attestmesh.agent.v1.Agent/SubscribeMessages"],stdout=subprocess.PIPE,text=True)
    for line in proc.stdout:
        try: handle(json.loads(line))
        except Exception as exc: print(f"rejected: {type(exc).__name__}: {exc}",flush=True)

if __name__ == "__main__": main()
