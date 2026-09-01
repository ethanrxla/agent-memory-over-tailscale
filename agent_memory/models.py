"""Request and response models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["note", "artifact", "message", "decision"]
Visibility = Literal["shared", "private", "direct", "broadcast"]
Scope = Literal["project", "linked", "global"]
Tier = Literal["rolling", "final"]


class AgentRegistration(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    device_name: str | None = Field(default=None, max_length=200)
    tailscale_name: str | None = Field(default=None, max_length=200)
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntryUpsertRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(default="default", min_length=1, max_length=128)
    source_id: str | None = Field(default=None, max_length=200)
    kind: Kind = "note"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    recipient_id: str | None = Field(default=None, max_length=128)
    visibility: Visibility | None = None
    source_uri: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Scoping: pass either an explicit project_key or a cwd for the hub to
    # record verbatim (derivation itself happens on the device that has the
    # filesystem; see agent_memory.projects).
    project_key: str | None = Field(default=None, max_length=400)
    cwd: str | None = Field(default=None, max_length=1000)
    chunk_size: int = Field(default=900, ge=300, le=4000)
    overlap: int = Field(default=120, ge=0, le=500)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    namespace: str | None = Field(default=None, max_length=128)
    requester_agent_id: str | None = Field(default=None, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    kind: Kind | None = None
    tag: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=8, ge=1, le=50)

    # Relevance isolation.
    project_key: str | None = Field(default=None, max_length=400)
    scope: Scope = "project"
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source_types: list[str] | None = None
    include_sidechain: bool = False
    session_id: str | None = Field(default=None, max_length=200)


class SessionUpsertRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    project_key: str = Field(min_length=1, max_length=400)
    display_name: str | None = Field(default=None, max_length=200)
    repo_root: str | None = Field(default=None, max_length=1000)
    git_remote: str | None = Field(default=None, max_length=1000)
    agent_id: str | None = Field(default=None, max_length=128)
    device_name: str | None = Field(default=None, max_length=200)
    tool: str = Field(default="unknown", max_length=64)
    cwd: str | None = Field(default=None, max_length=1000)
    git_branch: str | None = Field(default=None, max_length=200)
    ai_title: str | None = Field(default=None, max_length=400)
    last_prompt: str | None = Field(default=None, max_length=4000)
    status: Literal["active", "idle", "closed"] | None = None
    is_sidechain: bool = False
    started_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionEvent(BaseModel):
    seq: int = Field(ge=0)
    ts: str | None = None
    role: Literal["user", "assistant", "system", "tool"] = "user"
    kind: Literal["prompt", "reply", "tool_use", "tool_result", "meta", "title"] = "prompt"
    content: str = ""
    files: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    is_sidechain: bool = False


class SessionEventsRequest(BaseModel):
    """Idempotent batch ingest. Replays are deduped on (session_id, seq)."""

    session: SessionUpsertRequest
    events: list[SessionEvent] = Field(default_factory=list)


class DecisionRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    project_key: str | None = Field(default=None, max_length=400)
    cwd: str | None = Field(default=None, max_length=1000)
    session_id: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    rationale: str | None = None
    tags: list[str] = Field(default_factory=list)
    supersedes: str | None = Field(default=None, max_length=200)
    namespace: str = Field(default="default", max_length=128)


class ProjectLinkRequest(BaseModel):
    project_key: str = Field(min_length=1, max_length=400)
    linked_project_key: str = Field(min_length=1, max_length=400)
    note: str | None = Field(default=None, max_length=500)
    bidirectional: bool = True
