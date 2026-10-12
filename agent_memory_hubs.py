"""Pick a reachable hub from a list of candidates. Stdlib only.

``AGENT_MEMORY_URL`` may name several hubs, comma separated, in order of
preference::

    AGENT_MEMORY_URL=http://pop-os.tailf11891.ts.net:8787,http://windows-pc.tailf11891.ts.net:8787

pop-os is a dual boot: when the PC is in Windows the Linux hub is simply not on
the tailnet. Listing both lets every device follow the machine to whichever OS
it booted, instead of failing until someone edits a config.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8787"
PROBE_TIMEOUT = float(os.getenv("AGENT_MEMORY_PROBE_TIMEOUT", "3"))


def candidate_urls(raw: str | None = None) -> list[str]:
    """Split a comma/whitespace separated URL list, dropping blanks and repeats."""
    raw = raw if raw is not None else os.getenv("AGENT_MEMORY_URL", "")
    urls: list[str] = []
    for part in re.split(r"[,\s]+", raw or ""):
        url = part.strip().rstrip("/")
        if url and url not in urls:
            urls.append(url)
    return urls or [DEFAULT_URL]


def is_healthy(url: str, shared_key: str = "", timeout: float = PROBE_TIMEOUT) -> bool:
    req = urllib.request.Request(url.rstrip("/") + "/health", method="GET")
    if shared_key:
        req.add_header("X-Shared-Key", shared_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pick_hub(raw: str | None = None, shared_key: str | None = None) -> str:
    """Return the first healthy hub, or the first candidate if none answer.

    A single candidate is returned without probing, so the one-hub setup pays
    nothing for this.
    """
    urls = candidate_urls(raw)
    if len(urls) == 1:
        return urls[0]
    key = shared_key if shared_key is not None else os.getenv("AGENT_MEMORY_SHARED_KEY", "")
    for url in urls:
        if is_healthy(url, key):
            return url
    return urls[0]
