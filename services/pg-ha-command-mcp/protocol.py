import datetime as dt
import json
import random, time, uuid
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

COMMANDS = {"patroni.status", "patroni.explain", "backup.status", "patroni.switchover.plan"}

def uuid7() -> uuid.UUID:
    millis = int(time.time() * 1000)
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= random.SystemRandom().getrandbits(12) << 64
    value |= 0b10 << 62
    value |= random.SystemRandom().getrandbits(62)
    return uuid.UUID(int=value)

class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["attestmesh.pgha.command.v1"] = "attestmesh.pgha.command.v1"
    command_id: uuid.UUID = Field(default_factory=uuid7)
    cluster: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9-]+$")
    target: str = Field(pattern=r"^pg[1-9][0-9]*$")
    issued_at: dt.datetime
    expires_at: dt.datetime
    mode: Literal["explain", "execute"] = "explain"
    command: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def safe(self):
        if self.command_id.version != 7: raise ValueError("command_id must be UUIDv7")
        if self.command not in COMMANDS: raise ValueError("command is not allowlisted")
        if self.command in {"patroni.explain", "patroni.switchover.plan"} and self.mode != "explain":
            raise ValueError("command is advisory-only")
        if self.expires_at <= self.issued_at or self.expires_at - self.issued_at > dt.timedelta(minutes=5):
            raise ValueError("validity window must be between 0 and 5 minutes")
        return self

    def canonical(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False).encode()
