"""Environment-driven configuration for the agent memory hub."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved runtime configuration.

    Every field falls back to a working default so the hub runs with zero
    environment set up. NVIDIA credentials are optional by design: without
    them the service degrades to FTS5-only retrieval and extractive summaries.
    """

    db_path: str = field(default_factory=lambda: _env_str("AGENT_MEMORY_DB_PATH", "/data/agent_memory.db"))
    shared_key: str = field(default_factory=lambda: _env_str("AGENT_MEMORY_SHARED_KEY", ""))

    # NVIDIA NIM (https://integrate.api.nvidia.com/v1) -- OpenAI compatible.
    nvidia_api_key: str = field(default_factory=lambda: _env_str("NVIDIA_API_KEY", ""))
    nvidia_base_url: str = field(
        default_factory=lambda: _env_str("AGENT_MEMORY_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
    )
    embed_model: str = field(
        default_factory=lambda: _env_str("AGENT_MEMORY_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")
    )
    embed_dim: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_EMBED_DIM", 2048))
    embed_batch: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_EMBED_BATCH", 32))
    summary_model: str = field(
        default_factory=lambda: _env_str("AGENT_MEMORY_SUMMARY_MODEL", "nvidia/nvidia-nemotron-nano-9b-v2")
    )
    # Free tier is ~40 requests/minute/model; stay well under it.
    nim_rpm: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_NIM_RPM", 30))
    nim_timeout: float = field(default_factory=lambda: _env_float("AGENT_MEMORY_NIM_TIMEOUT", 60.0))

    # Retrieval tuning.
    recency_half_life_days: float = field(
        default_factory=lambda: _env_float("AGENT_MEMORY_RECENCY_HALF_LIFE_DAYS", 14.0)
    )
    default_min_score: float = field(default_factory=lambda: _env_float("AGENT_MEMORY_MIN_SCORE", 0.12))
    rrf_k: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_RRF_K", 60))
    brief_token_budget: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_BRIEF_TOKENS", 500))

    # Background worker.
    worker_enabled: bool = field(default_factory=lambda: _env_bool("AGENT_MEMORY_WORKER", True))
    worker_interval: float = field(default_factory=lambda: _env_float("AGENT_MEMORY_WORKER_INTERVAL", 15.0))
    # A session with no new events for this long is eligible for a final summary.
    session_idle_minutes: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_IDLE_MINUTES", 30))
    rolling_summary_every: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_ROLLING_EVERY", 25))

    # Retention (0 disables).
    retention_days: int = field(default_factory=lambda: _env_int("AGENT_MEMORY_RETENTION_DAYS", 0))

    @property
    def nim_enabled(self) -> bool:
        return bool(self.nvidia_api_key)


def load_config(**overrides: object) -> Config:
    cfg = Config()
    for key, value in overrides.items():
        if value is not None and hasattr(cfg, key):
            setattr(cfg, key, value)
    return cfg
