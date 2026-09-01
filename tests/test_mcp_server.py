from agent_memory_mcp import AgentMemoryMcpServer


class FakeHttpClient:
    def __init__(self) -> None:
        self.base_url = "http://memory-host.tailnet.ts.net:8787"
        self.calls: list[tuple[str, str, dict | None]] = []

    def request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/health":
            return {"status": "ok", "db_path": "/data/agent_memory.db"}
        if method == "POST" and path == "/v1/search":
            return {"results": [{"entry_id": "abc123", "title": "Runtime note"}]}
        if method == "POST" and path == "/v1/agents/register":
            return {"status": "registered", "agent_id": payload["agent_id"]}
        return {"ok": True}


def test_initialize_then_list_tools() -> None:
    server = AgentMemoryMcpServer(http_client=FakeHttpClient())

    initialize = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
    )
    assert initialize["result"]["serverInfo"]["name"] == "agent-memory-mcp"
    assert initialize["result"]["capabilities"]["tools"]["listChanged"] is False

    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools_list = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = [tool["name"] for tool in tools_list["result"]["tools"]]
    assert "search_memory" in tool_names
    assert "send_message" in tool_names


def test_tool_call_forwards_to_backend() -> None:
    http_client = FakeHttpClient()
    server = AgentMemoryMcpServer(http_client=http_client)
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
    )
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_memory",
                "arguments": {
                    "query": "shannon runtime",
                    "namespace": "chimera",
                    "requester_agent_id": "codex-laptop",
                    "limit": 3,
                },
            },
        }
    )

    assert response["result"]["isError"] is False
    assert "Runtime note" in response["result"]["content"][0]["text"]
    method, path, payload = http_client.calls[-1]
    assert (method, path) == ("POST", "/v1/search")
    # Core fields still forwarded verbatim ...
    assert payload["query"] == "shannon runtime"
    assert payload["namespace"] == "chimera"
    assert payload["requester_agent_id"] == "codex-laptop"
    assert payload["limit"] == 3
    # ... plus the new scoping fields. Without a cwd/project_key the wrapper
    # defaults to a global search so behaviour is unchanged for old callers.
    assert payload["scope"] == "global"
    assert payload["project_key"] is None


def test_resource_read_returns_skills_and_service_info() -> None:
    server = AgentMemoryMcpServer(http_client=FakeHttpClient())
    server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
    )
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    skills = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": "memory://skills"},
        }
    )
    assert "Shared Agent Memory" in skills["result"]["contents"][0]["text"]

    info = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "memory://service-info"},
        }
    )
    assert "memory-host.tailnet.ts.net" in info["result"]["contents"][0]["text"]


def _initialized_server():
    server = AgentMemoryMcpServer(http_client=FakeHttpClient())
    server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    })
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def test_new_session_tools_are_advertised() -> None:
    server = _initialized_server()
    listing = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert {"get_project_brief", "list_sessions", "get_session", "record_decision", "open_threads"} <= names


def test_search_derives_project_scope_from_cwd() -> None:
    server = _initialized_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search_memory", "arguments": {"query": "widget", "cwd": "/tmp/some/dir"}},
    })
    assert response["result"]["isError"] is False
    method, path, payload = server.http.calls[-1]
    assert path == "/v1/search"
    # cwd with no git repo derives a path: project key and scopes to it.
    assert payload["project_key"] == "path:/tmp/some/dir"
    assert payload["scope"] == "project"


def test_project_brief_requires_scope() -> None:
    server = _initialized_server()
    response = server.handle_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "get_project_brief", "arguments": {}},
    })
    assert response["result"]["isError"] is True
    assert "cwd or project_key" in response["result"]["content"][0]["text"]
