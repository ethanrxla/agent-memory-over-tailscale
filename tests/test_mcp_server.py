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
    assert http_client.calls[-1] == (
        "POST",
        "/v1/search",
        {
            "query": "shannon runtime",
            "namespace": "chimera",
            "requester_agent_id": "codex-laptop",
            "agent_id": None,
            "kind": None,
            "tag": None,
            "limit": 3,
        },
    )


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
    assert "Shared Agent Memory Skill" in skills["result"]["contents"][0]["text"]

    info = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "memory://service-info"},
        }
    )
    assert "memory-host.tailnet.ts.net" in info["result"]["contents"][0]["text"]
