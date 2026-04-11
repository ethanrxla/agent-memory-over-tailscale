import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_URL = os.getenv("AGENT_MEMORY_URL", "http://127.0.0.1:8787")
DEFAULT_KEY = os.getenv("AGENT_MEMORY_SHARED_KEY", "")


def request_json(method: str, path: str, payload: dict | None = None) -> dict:
    url = DEFAULT_URL.rstrip("/") + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method=method)
    req.add_header("Content-Type", "application/json")
    if DEFAULT_KEY:
        req.add_header("X-Shared-Key", DEFAULT_KEY)
    if data is not None:
        req.data = data

    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc}") from exc


def cmd_register(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent_id,
        "display_name": args.display_name,
        "device_name": args.device_name,
        "tailscale_name": args.tailscale_name,
        "capabilities": args.capability or [],
        "metadata": parse_json(args.metadata),
    }
    print(json.dumps(request_json("POST", "/v1/agents/register", payload), indent=2))


def cmd_publish(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent_id,
        "namespace": args.namespace,
        "source_id": args.source_id,
        "kind": args.kind,
        "title": args.title,
        "content": read_content(args),
        "tags": args.tag or [],
        "recipient_id": args.recipient_id,
        "visibility": args.visibility,
        "source_uri": args.source_uri,
        "metadata": parse_json(args.metadata),
    }
    print(json.dumps(request_json("POST", "/v1/entries/upsert", payload), indent=2))


def cmd_search(args: argparse.Namespace) -> None:
    payload = {
        "query": args.query,
        "namespace": args.namespace,
        "requester_agent_id": args.requester_agent_id,
        "agent_id": args.agent_id,
        "kind": args.kind,
        "tag": args.tag,
        "limit": args.limit,
    }
    print(json.dumps(request_json("POST", "/v1/search", payload), indent=2))


def cmd_send(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.sender_agent_id,
        "namespace": args.namespace,
        "source_id": args.source_id,
        "kind": "message",
        "title": args.title,
        "content": read_content(args),
        "tags": args.tag or [],
        "recipient_id": args.recipient_id,
        "metadata": parse_json(args.metadata),
    }
    print(json.dumps(request_json("POST", "/v1/entries/upsert", payload), indent=2))


def cmd_inbox(args: argparse.Namespace) -> None:
    params = {"limit": str(args.limit)}
    if args.namespace:
        params["namespace"] = args.namespace
    path = f"/v1/messages/inbox/{urllib.parse.quote(args.agent_id)}?{urllib.parse.urlencode(params)}"
    print(json.dumps(request_json("GET", path), indent=2))


def parse_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON metadata: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("Metadata must be a JSON object")
    return parsed


def read_content(args: argparse.Namespace) -> str:
    if args.content is not None:
        return args.content
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            return handle.read()
    raise SystemExit("Provide either --content or --file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CLI for the local agent memory service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register", help="Register an agent/device")
    register.add_argument("--agent-id", required=True)
    register.add_argument("--display-name", required=True)
    register.add_argument("--device-name")
    register.add_argument("--tailscale-name")
    register.add_argument("--capability", action="append")
    register.add_argument("--metadata")
    register.set_defaults(func=cmd_register)

    publish = subparsers.add_parser("publish", help="Store a shared note or artifact")
    publish.add_argument("--agent-id", required=True)
    publish.add_argument("--namespace", default="default")
    publish.add_argument("--source-id")
    publish.add_argument("--kind", default="note", choices=["note", "artifact", "message"])
    publish.add_argument("--title", required=True)
    publish.add_argument("--content")
    publish.add_argument("--file")
    publish.add_argument("--tag", action="append")
    publish.add_argument("--recipient-id")
    publish.add_argument("--visibility", choices=["shared", "private", "direct", "broadcast"])
    publish.add_argument("--source-uri")
    publish.add_argument("--metadata")
    publish.set_defaults(func=cmd_publish)

    search = subparsers.add_parser("search", help="Search shared memory")
    search.add_argument("--query", required=True)
    search.add_argument("--namespace")
    search.add_argument("--requester-agent-id")
    search.add_argument("--agent-id")
    search.add_argument("--kind", choices=["note", "artifact", "message"])
    search.add_argument("--tag")
    search.add_argument("--limit", type=int, default=8)
    search.set_defaults(func=cmd_search)

    send = subparsers.add_parser("send", help="Send a direct or broadcast message")
    send.add_argument("--sender-agent-id", required=True)
    send.add_argument("--recipient-id")
    send.add_argument("--namespace", default="default")
    send.add_argument("--source-id")
    send.add_argument("--title", required=True)
    send.add_argument("--content")
    send.add_argument("--file")
    send.add_argument("--tag", action="append")
    send.add_argument("--metadata")
    send.set_defaults(func=cmd_send)

    inbox = subparsers.add_parser("inbox", help="Read messages for an agent")
    inbox.add_argument("--agent-id", required=True)
    inbox.add_argument("--namespace")
    inbox.add_argument("--limit", type=int, default=20)
    inbox.set_defaults(func=cmd_inbox)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
