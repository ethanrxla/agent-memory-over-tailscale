#!/usr/bin/env python3
"""Probe NVIDIA NIM chat models for the summariser workflow.

Tests each candidate for: reachability, latency, and -- the thing that actually
matters here -- whether it reliably returns the STRICT JSON the summariser
expects. Reasoning models that are slow or wrap output in prose score poorly for
a background worker even if they are "smarter".

Reads NVIDIA_API_KEY from the environment or --key-file. Uses stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("AGENT_MEMORY_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")

CANDIDATES = [
    ("nvidia/nvidia-nemotron-nano-9b-v2", {}),                       # current default
    ("nvidia/nemotron-3.5-lightning-30b-a3b", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("nvidia/nemotron-3-ultra-550b-a55b", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("deepseek-ai/deepseek-v4-flash-0731", {"chat_template_kwargs": {"thinking": False}}),
    ("deepseek-ai/deepseek-v4-pro-0813", {"chat_template_kwargs": {"thinking": False}}),
    ("moonshotai/kimi-k3", {}),
]

SYSTEM = (
    "You summarise software engineering sessions for other AI coding agents. "
    "Write only what the transcript supports; never invent file names or outcomes. Be terse."
)
INSTRUCTIONS = """Summarise this in-progress coding session.

Respond with STRICT JSON only, no prose outside the object, no markdown fence:
{
  "summary": "<=120 words: what is being worked on, current state, immediate blocker if any>",
  "decisions": ["short statements of choices that were made"],
  "open_threads": ["specific unfinished work items"]
}
If a list has nothing supported by the transcript, use []."""

TRANSCRIPT = """[USER] Set up the telemetry client to talk to the flask server on port 5000
[ASSISTANT] I'll configure the client and point it at the server.
[TOOL] Write /home/x/c3po/telemetry_client.py
[TOOL] $ python telemetry_client.py --port 5000
[USER] great, now decode the beacon interval. also we decided to use protobuf not json for the wire format
[ASSISTANT] Decoded: the beacon interval is 60s. Switching the wire format to protobuf as decided.
[TOOL] Edit /home/x/c3po/beacon.py
[USER] the protobuf schema still needs the auth field added, leave that for next time"""


def post(model: str, key: str, extra: dict, timeout: float) -> tuple[dict | None, str | None]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"{INSTRUCTIONS}\n\nTRANSCRIPT:\n{TRANSCRIPT}"},
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
        "stream": False,
    }
    if extra:
        payload.update(extra)
    request = urllib.request.Request(
        BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:180]}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def extract(data: dict) -> tuple[str, str | None]:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {})
    return msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning")


def strip_reasoning(text: str) -> str:
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip() or text


def parse_json(raw: str):
    import re
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        return json.loads(text[a : b + 1])
    except json.JSONDecodeError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key-file")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--only", help="comma-separated model substrings to include")
    args = ap.parse_args()

    key = os.getenv("NVIDIA_API_KEY", "")
    if not key and args.key_file:
        key = open(os.path.expanduser(args.key_file)).read().strip()
        # accept KEY=value or bare value
        if "=" in key and "\n" not in key:
            key = key.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        print("ERROR: no NVIDIA_API_KEY (env or --key-file)", file=sys.stderr)
        return 2

    models = CANDIDATES
    if args.only:
        wants = [w.strip() for w in args.only.split(",")]
        models = [m for m in CANDIDATES if any(w in m[0] for w in wants)]

    print(f"{'model':<42} {'ok':<4} {'json':<5} {'lat(s)':<8} {'clean_json':<11} notes")
    print("-" * 100)
    results = []
    for model, extra in models:
        t0 = time.monotonic()
        data, err = post(model, key, extra, args.timeout)
        dt = time.monotonic() - t0
        if err:
            print(f"{model:<42} {'no':<4} {'-':<5} {dt:<8.2f} {'-':<11} {err}")
            results.append((model, False, False, dt, False))
            continue
        content, reasoning = extract(data)
        cleaned = strip_reasoning(content)
        parsed = parse_json(cleaned)
        valid = bool(parsed and isinstance(parsed.get("summary"), str) and parsed["summary"].strip())
        # "clean": the model returned JSON with no reasoning leakage / no fence noise
        clean = valid and cleaned.strip().startswith("{") and cleaned.strip().endswith("}")
        usage = data.get("usage", {})
        note = ""
        if valid:
            note = f"dec={len(parsed.get('decisions', []))} threads={len(parsed.get('open_threads', []))}"
            if reasoning:
                note += " +reasoning_field"
        print(f"{model:<42} {'yes':<4} {'yes' if valid else 'NO':<5} {dt:<8.2f} {'yes' if clean else 'no':<11} {note}")
        results.append((model, True, valid, dt, clean))
        if valid and os.getenv("PROBE_VERBOSE"):
            print(f"    summary: {parsed['summary'][:150]}")

    print("\nRecommendation ranking (reachable + valid JSON, fastest first):")
    ok = sorted([r for r in results if r[2]], key=lambda r: r[3])
    for i, (model, _, _, dt, clean) in enumerate(ok, 1):
        print(f"  {i}. {model}  ({dt:.2f}s{', clean JSON' if clean else ', needs cleanup'})")
    if not ok:
        print("  none returned valid JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
