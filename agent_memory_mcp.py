import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    # Available when the wrapper sits alongside the package (the common case).
    from agent_memory.projects import derive_project
except Exception:  # pragma: no cover - wrapper can run standalone
    derive_project = None  # type: ignore


PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "agent-memory-mcp"
SERVER_VERSION = "0.1.0"


def _json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=True)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AgentMemoryHttpClient:
    def __init__(self, base_url: str | None = None, shared_key: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("AGENT_MEMORY_URL") or "http://127.0.0.1:8787").rstrip("/")
        self.shared_key = shared_key if shared_key is not None else os.getenv("AGENT_MEMORY_SHARED_KEY", "")

    def request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, method=method)
        req.add_header("Content-Type", "application/json")
        if self.shared_key:
            req.add_header("X-Shared-Key", self.shared_key)
        if data is not None:
            req.data = data

        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Request failed: {exc}") from exc


class AgentMemoryMcpServer:
    def __init__(self, http_client: AgentMemoryHttpClient | None = None) -> None:
        self.http = http_client or AgentMemoryHttpClient()
        self.initialized = False
        self.root_dir = Path(__file__).resolve().parent

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "register_agent",
                "description": "Register or refresh an AI agent/device in the shared memory service.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string", "description": "Stable agent identifier on this device."},
                        "display_name": {"type": "string", "description": "Human-readable label for the agent."},
                        "device_name": {"type": "string"},
                        "tailscale_name": {"type": "string"},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["agent_id", "display_name"],
                },
            },
            {
                "name": "list_agents",
                "description": "List recently seen agents registered in shared memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                    },
                },
            },
            {
                "name": "publish_memory",
                "description": "Store a shared note or artifact for later retrieval by other agents.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "namespace": {"type": "string"},
                        "source_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["note", "artifact", "message"]},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "recipient_id": {"type": "string"},
                        "visibility": {"type": "string", "enum": ["shared", "private", "direct", "broadcast"]},
                        "source_uri": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["agent_id", "title", "content"],
                },
            },
            {
                "name": "search_memory",
                "description": (
                    "Semantic + keyword search over shared memory (sessions, decisions, notes). "
                    "Scoped to the current project by default so unrelated work never leaks in. "
                    "Pass cwd (or project_key) to scope; use scope='global' only to search everything."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "cwd": {"type": "string", "description": "Working directory; used to derive the project scope."},
                        "project_key": {"type": "string", "description": "Explicit project key (overrides cwd)."},
                        "scope": {"type": "string", "enum": ["project", "linked", "global"], "default": "project"},
                        "namespace": {"type": "string"},
                        "requester_agent_id": {"type": "string"},
                        "agent_id": {"type": "string"},
                        "kind": {"type": "string", "enum": ["note", "artifact", "message", "decision"]},
                        "tag": {"type": "string"},
                        "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "source_types": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_project_brief",
                "description": (
                    "Call ONCE at the start of a session. Returns a compact, project-scoped brief: "
                    "recent session summaries, open threads, and locked-in decisions for THIS project. "
                    "Far cheaper than re-reading a transcript and keeps you from drifting off track. "
                    "If it reports no prior sessions, DO NOT conclude the work is new: check the "
                    "related_projects it returns (the same project reached from another checkout or "
                    "machine has a different key) by calling this tool again with that project_key, "
                    "or search_memory across all projects."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cwd": {"type": "string", "description": "Working directory; used to derive the project."},
                        "project_key": {"type": "string"},
                        "branch": {"type": "string"},
                        "max_sessions": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                        "token_budget": {"type": "integer", "minimum": 100, "maximum": 4000},
                    },
                },
            },
            {
                "name": "list_sessions",
                "description": "List recent coding sessions, optionally filtered by project, so you can see what other agents are doing.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cwd": {"type": "string"},
                        "project_key": {"type": "string"},
                        "status": {"type": "string", "enum": ["active", "idle", "closed"]},
                        "tool": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    },
                },
            },
            {
                "name": "get_session",
                "description": "Fetch one session: its summaries, decisions, open threads, and (optionally) its full event log.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"},
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "record_decision",
                "description": "Pin a durable decision so future sessions (and other agents) respect it. Include the rationale.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "cwd": {"type": "string"},
                        "project_key": {"type": "string"},
                        "session_id": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "rationale": {"type": "string"},
                        "supersedes": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["agent_id", "title", "content"],
                },
            },
            {
                "name": "open_threads",
                "description": "List unfinished work items (open threads) recorded across this project's sessions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cwd": {"type": "string"},
                        "project_key": {"type": "string"},
                    },
                },
            },
            {
                "name": "get_entry",
                "description": "Fetch a specific memory entry and its full chunked contents.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "entry_id": {"type": "string"},
                        "requester_agent_id": {"type": "string"},
                    },
                    "required": ["entry_id"],
                },
            },
            {
                "name": "send_message",
                "description": "Send a direct or broadcast message to other agents through shared memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "sender_agent_id": {"type": "string"},
                        "recipient_id": {"type": "string"},
                        "namespace": {"type": "string"},
                        "source_id": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "metadata": {"type": "object"},
                    },
                    "required": ["sender_agent_id", "title", "content"],
                },
            },
            {
                "name": "read_inbox",
                "description": "Read recent direct or broadcast messages for a specific agent.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "namespace": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    },
                    "required": ["agent_id"],
                },
            },
        ]

    def resource_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": "memory://skills",
                "name": "Shared Memory Skills",
                "description": "Operational guidance for agents using the shared memory service.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "memory://service-info",
                "name": "Shared Memory Service Info",
                "description": "Current backend URL and health check for the shared memory service.",
                "mimeType": "application/json",
            },
        ]

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "method" not in message:
            return None

        method = message["method"]
        params = message.get("params") or {}
        request_id = message.get("id")
        is_notification = request_id is None

        if method == "initialize":
            return self._response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Use this MCP server to access the shared agent memory backend over Tailscale. "
                        "Search before redoing work, publish durable context, and do not store secrets."
                    ),
                },
            )

        if method == "notifications/initialized":
            self.initialized = True
            return None

        if method == "ping":
            if is_notification:
                return None
            return self._response(request_id, {})

        if not self.initialized:
            if is_notification:
                return None
            return self._error(request_id, -32000, "Server not initialized")

        if method == "tools/list":
            return self._response(request_id, {"tools": self.tool_definitions()})

        if method == "tools/call":
            if not isinstance(params, dict):
                return self._error(request_id, -32602, "Invalid params for tools/call")
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self._error(request_id, -32602, "tools/call requires name and arguments")
            return self._response(request_id, self._call_tool(name, arguments))

        if method == "resources/list":
            return self._response(request_id, {"resources": self.resource_definitions()})

        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str):
                return self._error(request_id, -32602, "resources/read requires a uri")
            return self._response(request_id, {"contents": [self._read_resource(uri)]})

        if method == "resources/templates/list":
            return self._response(request_id, {"resourceTemplates": []})

        if method == "prompts/list":
            return self._response(request_id, {"prompts": []})

        if is_notification:
            return None
        return self._error(request_id, -32601, f"Method not found: {method}")

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "register_agent": self._tool_register_agent,
            "list_agents": self._tool_list_agents,
            "publish_memory": self._tool_publish_memory,
            "search_memory": self._tool_search_memory,
            "get_entry": self._tool_get_entry,
            "send_message": self._tool_send_message,
            "read_inbox": self._tool_read_inbox,
            "get_project_brief": self._tool_get_project_brief,
            "list_sessions": self._tool_list_sessions,
            "get_session": self._tool_get_session,
            "record_decision": self._tool_record_decision,
            "open_threads": self._tool_open_threads,
        }
        handler = handlers.get(name)
        if handler is None:
            raise JsonRpcToolError(f"Unknown tool: {name}")

        try:
            payload = handler(arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}],
                "isError": False,
            }
        except JsonRpcToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Tool execution failed: {exc}"}], "isError": True}

    def _tool_register_agent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        agent_id = self._required_string(arguments, "agent_id")
        display_name = self._required_string(arguments, "display_name")
        payload = {
            "agent_id": agent_id,
            "display_name": display_name,
            "device_name": self._optional_string(arguments, "device_name"),
            "tailscale_name": self._optional_string(arguments, "tailscale_name"),
            "capabilities": self._string_list(arguments.get("capabilities")),
            "metadata": self._object(arguments.get("metadata"), "metadata"),
        }
        return self.http.request_json("POST", "/v1/agents/register", payload)

    def _tool_list_agents(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = self._bounded_int(arguments.get("limit", 50), minimum=1, maximum=200, field="limit")
        query = urllib.parse.urlencode({"limit": limit})
        return self.http.request_json("GET", f"/v1/agents?{query}")

    def _tool_publish_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "agent_id": self._required_string(arguments, "agent_id"),
            "namespace": self._optional_string(arguments, "namespace") or "default",
            "source_id": self._optional_string(arguments, "source_id"),
            "kind": self._optional_string(arguments, "kind") or "note",
            "title": self._required_string(arguments, "title"),
            "content": self._required_string(arguments, "content"),
            "tags": self._string_list(arguments.get("tags")),
            "recipient_id": self._optional_string(arguments, "recipient_id"),
            "visibility": self._optional_string(arguments, "visibility"),
            "source_uri": self._optional_string(arguments, "source_uri"),
            "metadata": self._object(arguments.get("metadata"), "metadata"),
        }
        return self.http.request_json("POST", "/v1/entries/upsert", payload)

    def _resolve_project_key(self, arguments: dict[str, Any]) -> str | None:
        """Prefer an explicit project_key; otherwise derive one from cwd on-device."""
        explicit = self._optional_string(arguments, "project_key")
        if explicit:
            return explicit
        cwd = self._optional_string(arguments, "cwd")
        if cwd and derive_project is not None:
            return derive_project(cwd)["project_key"]
        return None

    def _tool_search_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_key = self._resolve_project_key(arguments)
        scope = self._optional_string(arguments, "scope") or ("project" if project_key else "global")
        payload = {
            "query": self._required_string(arguments, "query"),
            "project_key": project_key,
            "scope": scope,
            "namespace": self._optional_string(arguments, "namespace"),
            "requester_agent_id": self._optional_string(arguments, "requester_agent_id"),
            "agent_id": self._optional_string(arguments, "agent_id"),
            "kind": self._optional_string(arguments, "kind"),
            "tag": self._optional_string(arguments, "tag"),
            "source_types": arguments.get("source_types"),
            "limit": self._bounded_int(arguments.get("limit", 8), minimum=1, maximum=50, field="limit"),
        }
        if "min_score" in arguments and arguments["min_score"] is not None:
            payload["min_score"] = float(arguments["min_score"])
        return self.http.request_json("POST", "/v1/search", payload)

    def _tool_get_project_brief(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_key = self._resolve_project_key(arguments)
        if not project_key:
            raise JsonRpcToolError("Provide cwd or project_key to scope the brief")
        params: dict[str, Any] = {
            "max_sessions": self._bounded_int(arguments.get("max_sessions", 5), minimum=1, maximum=20, field="max_sessions"),
        }
        branch = self._optional_string(arguments, "branch")
        if branch:
            params["branch"] = branch
        if arguments.get("token_budget"):
            params["token_budget"] = self._bounded_int(arguments["token_budget"], minimum=100, maximum=4000, field="token_budget")
        quoted = urllib.parse.quote(project_key, safe="")
        return self.http.request_json("GET", f"/v1/projects/{quoted}/brief?{urllib.parse.urlencode(params)}")

    def _tool_list_sessions(self, arguments: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": self._bounded_int(arguments.get("limit", 20), minimum=1, maximum=100, field="limit"),
        }
        project_key = self._resolve_project_key(arguments)
        if project_key:
            params["project_key"] = project_key
        for field in ("status", "tool"):
            value = self._optional_string(arguments, field)
            if value:
                params[field] = value
        return self.http.request_json("GET", f"/v1/sessions?{urllib.parse.urlencode(params)}")

    def _tool_get_session(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id = urllib.parse.quote(self._required_string(arguments, "session_id"), safe="")
        detail = self._optional_string(arguments, "detail") or "summary"
        return self.http.request_json("GET", f"/v1/sessions/{session_id}?detail={detail}")

    def _tool_record_decision(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "agent_id": self._required_string(arguments, "agent_id"),
            "project_key": self._resolve_project_key(arguments),
            "cwd": self._optional_string(arguments, "cwd"),
            "session_id": self._optional_string(arguments, "session_id"),
            "title": self._required_string(arguments, "title"),
            "content": self._required_string(arguments, "content"),
            "rationale": self._optional_string(arguments, "rationale"),
            "supersedes": self._optional_string(arguments, "supersedes"),
            "tags": self._string_list(arguments.get("tags")),
        }
        return self.http.request_json("POST", "/v1/decisions", payload)

    def _tool_open_threads(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_key = self._resolve_project_key(arguments)
        if not project_key:
            raise JsonRpcToolError("Provide cwd or project_key")
        quoted = urllib.parse.quote(project_key, safe="")
        return self.http.request_json("GET", f"/v1/projects/{quoted}/threads")

    def _tool_get_entry(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entry_id = urllib.parse.quote(self._required_string(arguments, "entry_id"))
        requester = self._optional_string(arguments, "requester_agent_id")
        query = urllib.parse.urlencode({"requester_agent_id": requester}) if requester else ""
        suffix = f"?{query}" if query else ""
        return self.http.request_json("GET", f"/v1/entries/{entry_id}{suffix}")

    def _tool_send_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "agent_id": self._required_string(arguments, "sender_agent_id"),
            "namespace": self._optional_string(arguments, "namespace") or "default",
            "source_id": self._optional_string(arguments, "source_id"),
            "kind": "message",
            "title": self._required_string(arguments, "title"),
            "content": self._required_string(arguments, "content"),
            "tags": self._string_list(arguments.get("tags")),
            "recipient_id": self._optional_string(arguments, "recipient_id"),
            "metadata": self._object(arguments.get("metadata"), "metadata"),
        }
        return self.http.request_json("POST", "/v1/entries/upsert", payload)

    def _tool_read_inbox(self, arguments: dict[str, Any]) -> dict[str, Any]:
        agent_id = urllib.parse.quote(self._required_string(arguments, "agent_id"))
        params = {
            "limit": self._bounded_int(arguments.get("limit", 20), minimum=1, maximum=100, field="limit"),
        }
        namespace = self._optional_string(arguments, "namespace")
        if namespace:
            params["namespace"] = namespace
        return self.http.request_json("GET", f"/v1/messages/inbox/{agent_id}?{urllib.parse.urlencode(params)}")

    def _read_resource(self, uri: str) -> dict[str, Any]:
        if uri == "memory://skills":
            skill_path = self.root_dir / "skill" / "agent-memory" / "SKILL.md"
            source = skill_path if skill_path.exists() else self.root_dir / "SKILLS.md"
            return {
                "uri": uri,
                "mimeType": "text/markdown",
                "text": _read_text(source),
            }
        if uri == "memory://service-info":
            health = self.http.request_json("GET", "/health")
            payload = {
                "backend_url": self.http.base_url,
                "health": health,
            }
            return {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        raise JsonRpcResourceError(f"Resource not found: {uri}")

    def _response(self, request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def _required_string(self, arguments: dict[str, Any], field: str) -> str:
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            raise JsonRpcToolError(f"Field '{field}' must be a non-empty string")
        return value

    def _optional_string(self, arguments: dict[str, Any], field: str) -> str | None:
        value = arguments.get(field)
        if value is None:
            return None
        if not isinstance(value, str):
            raise JsonRpcToolError(f"Field '{field}' must be a string")
        return value

    def _string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise JsonRpcToolError("Expected a list of strings")
        return value

    def _object(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise JsonRpcToolError(f"Field '{field}' must be an object")
        return value

    def _bounded_int(self, value: Any, *, minimum: int, maximum: int, field: str) -> int:
        if not isinstance(value, int):
            raise JsonRpcToolError(f"Field '{field}' must be an integer")
        if value < minimum or value > maximum:
            raise JsonRpcToolError(f"Field '{field}' must be between {minimum} and {maximum}")
        return value


class JsonRpcToolError(RuntimeError):
    pass


class JsonRpcResourceError(RuntimeError):
    pass


def serve_forever(server: AgentMemoryMcpServer | None = None) -> int:
    instance = server or AgentMemoryMcpServer()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            error = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(_json_dumps(error) + "\n")
            sys.stdout.flush()
            continue

        try:
            response = instance.handle_message(message)
        except JsonRpcResourceError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32002, "message": str(exc)},
            }
        except Exception as exc:
            print(f"agent-memory-mcp error: {exc}", file=sys.stderr, flush=True)
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

        if response is not None:
            sys.stdout.write(_json_dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(serve_forever())
