"""Session-aware shared RAG memory for coding agents over Tailscale."""

from .app import __version__, create_app

__all__ = ["create_app", "__version__"]
