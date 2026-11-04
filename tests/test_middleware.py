# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Credential middleware: 401 on a missing header, isolation (PRD-21313 TC-NTN-001, TC-NTN-017)."""

import asyncio

import pytest
from starlette.testclient import TestClient

from notion_mcp.__main__ import build_http_app
from notion_mcp.server import (
    GatewayCredentialsMiddleware,
    _credentials_var,
    create_mcp_server,
    get_client_from_context,
)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
CREDS = {"X-Notion-Api-Key": "ntn_secret"}


def _client() -> TestClient:
    return TestClient(build_http_app(create_mcp_server()))


def test_health_is_local():
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"X-Notion-Api-Key": ""}, {"X-Notion-Api-Key": "${KEY}"}])
def test_missing_header_returns_401(headers):
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | headers)
    assert resp.status_code == 401
    assert resp.json()["required"] == list(CREDS)


def test_tools_list_over_http():
    with _client() as client:
        resp = client.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS | CREDS)
    assert resp.status_code == 200
    for name in ("notion_search", "notion_get_page", "notion_append_block_children"):
        assert name in resp.text


def _run_middleware(headers: list[tuple[bytes, bytes]]) -> dict:
    seen: dict = {}

    async def app(scope, receive, send):
        seen["credentials"] = _credentials_var.get()
        seen["client"] = get_client_from_context()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    asyncio.run(GatewayCredentialsMiddleware(app)({"type": "http", "path": "/mcp", "headers": headers}, receive, send))
    return seen


def test_credentials_reach_request_context_and_are_reset():
    seen = _run_middleware([(b"x-notion-api-key", b" ntn_abc123 ")])
    client = seen["client"]
    assert client._headers()["Authorization"] == "Bearer ntn_abc123"
    assert _credentials_var.get() is None


def test_concurrent_requests_do_not_leak_context():
    """PRD-21313 TC-NTN-017: two tenants in flight at once never see each other's credential."""
    observed: list[tuple[str, str]] = []

    async def app(scope, receive, send):
        before = _credentials_var.get().api_key
        await asyncio.sleep(0.01)
        observed.append((before, _credentials_var.get().api_key))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = GatewayCredentialsMiddleware(app)

    def scope(key: bytes) -> dict:
        return {"type": "http", "path": "/mcp", "headers": [(b"x-notion-api-key", key)]}

    async def both():
        await asyncio.gather(middleware(scope(b"tenant-a-key"), receive, send), middleware(scope(b"tenant-b-key"), receive, send))

    asyncio.run(both())
    assert sorted(observed) == [("tenant-a-key", "tenant-a-key"), ("tenant-b-key", "tenant-b-key")]


def test_credentials_repr_hides_key():
    seen = _run_middleware([(b"x-notion-api-key", b"very-secret-key")])
    assert "very-secret-key" not in repr(seen["credentials"])
