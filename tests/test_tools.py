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
"""tools/list snapshot, error mapping, and per-tool behaviour against a mocked Notion API.

Test ids below are wired from docs/tickets/PRD-21313.ac.json -- that file is
the source of truth for what each AC means; this module is the "how to check
it" side (legs/dev.md S2: design writes the target, dev executes it).
"""

import asyncio
import json

import httpx
import pytest

from notion_mcp import api_client
from notion_mcp.api_client import NotionClient, NotionCredentials, NotionError
from notion_mcp.server import INSTRUCTIONS, _credentials_var, create_mcp_server

ID = {"page_id"}

# Tool names and required params are a public contract: existing agents and
# tenant connections depend on them. Changing this snapshot is a breaking
# change (SOP §13).
EXPECTED_TOOLS: dict[str, set[str]] = {
    "notion_search": {"query"},
    "notion_get_page": {"page_id"},
    "notion_create_page": set(),
    "notion_update_page": {"page_id"},
    "notion_get_database": {"database_id"},
    "notion_query_database": {"database_id"},
    "notion_list_block_children": {"block_id"},
    "notion_append_block_children": {"block_id", "children"},
    "notion_list_users": set(),
}
READ_TOOLS = {
    "notion_search",
    "notion_get_page",
    "notion_get_database",
    "notion_query_database",
    "notion_list_block_children",
    "notion_list_users",
}

CREDS = NotionCredentials("ntn_secret")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(api_client.asyncio, "sleep", instant)


class Recorder:
    """Mock transport: replies by method + path below /v1/."""

    def __init__(self, routes=None, default=None):
        self.requests: list[httpx.Request] = []
        self.routes = routes or {}
        self.default = default if default is not None else {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1/")
        reply = self.routes.get(f"{request.method} {path}", self.routes.get(path, self.default))
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, path: str, method: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == f"/v1/{path}" and (method is None or r.method == method)]


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


async def _call(monkeypatch, recorder: Recorder, tool: str, args: dict):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    monkeypatch.setattr(api_client, "_get_shared_http_client", lambda: http)
    ctx = _credentials_var.set(CREDS)
    try:
        content, _ = await create_mcp_server().call_tool(tool, args)
    finally:
        _credentials_var.reset(ctx)
    return json.loads(content[0].text)


def _client(recorder: Recorder) -> NotionClient:
    return NotionClient(CREDS, httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


# ---- tools/list snapshot (TC-NTN-018) ---------------------------------------


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(create_mcp_server().list_tools())}


def test_tools_list_snapshot_nine_tools_with_hints(tools):
    assert set(tools) == set(EXPECTED_TOOLS)
    assert len(tools) == 9
    for name in sorted(EXPECTED_TOOLS):
        description = (tools[name].description or "").strip()
        assert description, f"{name} has no description"
        assert len(description) <= 500
        assert len(description.splitlines()[0]) <= 100
        assert set(tools[name].inputSchema.get("required", [])) == EXPECTED_TOOLS[name]
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.readOnlyHint is (name in READ_TOOLS)


def test_instructions_budget():
    assert len(INSTRUCTIONS) <= 1500


# ---- error envelope mapping (AC4, AC12, cross-cutting) ----------------------


@pytest.mark.parametrize(
    "status,body_code,code,retryable",
    [
        (401, "unauthorized", "unauthorized", False),
        (404, "object_not_found", "not_found", False),
        (400, "validation_error", "invalid_argument", False),
        (429, "rate_limited", "rate_limited", True),
        (529, None, "rate_limited", True),
        (500, "internal_server_error", "upstream_error", True),
        (0, None, "upstream_error", True),
    ],
)
def test_error_envelope(status, body_code, code, retryable):
    envelope = json.loads(NotionError(status, "boom", body_code=body_code).to_envelope())
    assert envelope == {"error": {"code": code, "message": "boom", "retryable": retryable}}


def test_tool_without_credentials_returns_not_configured():
    content, _ = asyncio.run(create_mcp_server().call_tool("notion_get_page", {"page_id": "p1"}))
    assert json.loads(content[0].text)["error"]["code"] == "not_configured"


# ---- notion_search (TC-NTN-002, TC-NTN-003) ---------------------------------


async def test_search_returns_matches(monkeypatch):
    page = {
        "object": "page",
        "id": "p1",
        "url": "https://notion.so/p1",
        "last_edited_time": "2026-10-20T10:00:00.000Z",
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Q3 Roadmap"}]}},
    }
    rec = Recorder({"POST search": {"results": [page], "has_more": False, "next_cursor": None}})
    result = await _call(monkeypatch, rec, "notion_search", {"query": "Q3 roadmap"})
    assert body(rec.requests[0])["query"] == "Q3 roadmap"
    assert result["results"] == [
        {"id": "p1", "object": "page", "title": "Q3 Roadmap", "url": "https://notion.so/p1", "last_edited_time": "2026-10-20T10:00:00.000Z"}
    ]
    assert result["has_more"] is False


async def test_search_paginates(monkeypatch):
    rec = Recorder({"POST search": {"results": [{"object": "page", "id": "p1", "properties": {}}], "has_more": True, "next_cursor": "cur-1"}})
    result = await _call(monkeypatch, rec, "notion_search", {"query": "roadmap", "limit": 1})
    assert body(rec.requests[0])["page_size"] == 1
    assert (result["has_more"], result["next_cursor"]) == (True, "cur-1")


async def test_search_filters_by_object_type(monkeypatch):
    rec = Recorder({"POST search": {"results": [], "has_more": False, "next_cursor": None}})
    await _call(monkeypatch, rec, "notion_search", {"query": "x", "filter_object_type": "database"})
    assert body(rec.requests[0])["filter"] == {"property": "object", "value": "database"}


# ---- notion_get_page (TC-NTN-004, TC-NTN-005) -------------------------------


async def test_get_page_returns_full_shape(monkeypatch):
    page = {
        "id": "p1",
        "parent": {"type": "database_id", "database_id": "db1"},
        "archived": False,
        "url": "https://notion.so/p1",
        "last_edited_time": "2026-10-20T10:00:00.000Z",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Task A"}]},
            "Status": {"type": "select", "select": {"name": "Todo"}},
        },
    }
    rec = Recorder({"GET pages/p1": page})
    result = await _call(monkeypatch, rec, "notion_get_page", {"page_id": "p1"})
    assert result["id"] == "p1"
    assert result["parent"] == {"type": "database_id", "id": "db1"}
    assert result["archived"] is False
    assert result["url"] == "https://notion.so/p1"
    assert result["properties"] == {"Name": "Task A", "Status": "Todo"}


async def test_get_page_not_found_returns_not_found_code(monkeypatch):
    error = {"object": "error", "status": 404, "code": "object_not_found", "message": "Could not find page"}
    rec = Recorder({"GET pages/missing": lambda r: httpx.Response(404, json=error)})
    result = await _call(monkeypatch, rec, "notion_get_page", {"page_id": "missing"})
    assert result["error"]["code"] == "not_found"


# ---- notion_create_page (TC-NTN-006, TC-NTN-007) ----------------------------


async def test_create_page_under_database_succeeds(monkeypatch):
    schema = {"properties": {"Name": {"type": "title"}, "Status": {"type": "select"}}}
    rec = Recorder({
        "GET databases/db123": schema,
        "POST pages": lambda r: httpx.Response(200, json={"id": "pg456", "url": "https://notion.so/pg456"}),
    })
    result = await _call(
        monkeypatch, rec, "notion_create_page",
        {"parent_database_id": "db123", "properties": {"Name": "New task", "Status": "Todo"}},
    )
    assert result == {"id": "pg456", "url": "https://notion.so/pg456"}
    sent = body(rec.calls("pages", "POST")[0])
    assert sent["parent"] == {"database_id": "db123"}
    assert sent["properties"]["Name"] == {"type": "title", "title": [{"type": "text", "text": {"content": "New task"}}]}
    assert sent["properties"]["Status"] == {"type": "select", "select": {"name": "Todo"}}


async def test_create_page_invalid_property_type_returns_invalid_argument(monkeypatch):
    schema = {"properties": {"Name": {"type": "title"}, "Due": {"type": "date"}}}
    rec = Recorder({"GET databases/db123": schema})
    result = await _call(
        monkeypatch, rec, "notion_create_page",
        {"parent_database_id": "db123", "properties": {"Name": "x", "Unknown": "y"}},
    )
    assert result["error"]["code"] == "invalid_argument"
    assert "Unknown" in result["error"]["message"]
    assert rec.calls("pages", "POST") == []


async def test_create_page_requires_exactly_one_parent(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "notion_create_page", {"properties": {"title": "x"}})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []
    both = await _call(
        monkeypatch, rec, "notion_create_page",
        {"parent_page_id": "p1", "parent_database_id": "db1", "properties": {"title": "x"}},
    )
    assert both["error"]["code"] == "invalid_argument"
    assert rec.requests == []


async def test_create_page_under_page_builds_title(monkeypatch):
    rec = Recorder({"POST pages": lambda r: httpx.Response(200, json={"id": "pg1", "url": "https://notion.so/pg1"})})
    await _call(monkeypatch, rec, "notion_create_page", {"parent_page_id": "parent1", "properties": {"title": "Sub page"}})
    sent = body(rec.calls("pages", "POST")[0])
    assert sent["parent"] == {"page_id": "parent1"}
    assert sent["properties"] == {"title": {"title": [{"type": "text", "text": {"content": "Sub page"}}]}}


# ---- notion_update_page (TC-NTN-008) ----------------------------------------


async def test_update_page_archive_and_restore_roundtrip(monkeypatch):
    rec = Recorder({
        "PATCH pages/pg1": lambda r: httpx.Response(200, json={"id": "pg1", "parent": {}, "archived": json.loads(r.content)["archived"], "properties": {}}),
    })
    archived = await _call(monkeypatch, rec, "notion_update_page", {"page_id": "pg1", "archived": True})
    assert archived["archived"] is True
    restored = await _call(monkeypatch, rec, "notion_update_page", {"page_id": "pg1", "archived": False})
    assert restored["archived"] is False
    assert body(rec.calls("pages/pg1", "PATCH")[0]) == {"archived": True}


async def test_update_page_properties_fetches_schema(monkeypatch):
    rec = Recorder({
        "GET pages/pg1": {"id": "pg1", "parent": {"type": "database_id", "database_id": "db1"}},
        "GET databases/db1": {"properties": {"Status": {"type": "status"}}},
        "PATCH pages/pg1": lambda r: httpx.Response(200, json={"id": "pg1", "parent": {}, "properties": {}}),
    })
    await _call(monkeypatch, rec, "notion_update_page", {"page_id": "pg1", "properties": {"Status": "Done"}})
    sent = body(rec.calls("pages/pg1", "PATCH")[0])
    assert sent["properties"]["Status"] == {"type": "status", "status": {"name": "Done"}}


async def test_update_page_nothing_to_update_is_invalid_argument(monkeypatch):
    rec = Recorder()
    result = await _call(monkeypatch, rec, "notion_update_page", {"page_id": "pg1"})
    assert result["error"]["code"] == "invalid_argument"
    assert rec.requests == []


# ---- notion_get_database (TC-NTN-009) ---------------------------------------


async def test_get_database_returns_schema(monkeypatch):
    db = {
        "id": "db1",
        "title": [{"plain_text": "Tasks"}],
        "url": "https://notion.so/db1",
        "properties": {
            "Name": {"type": "title"},
            "Status": {"type": "select", "select": {"options": [{"name": "Todo"}, {"name": "Done"}]}},
        },
    }
    rec = Recorder({"GET databases/db1": db})
    result = await _call(monkeypatch, rec, "notion_get_database", {"database_id": "db1"})
    assert result["title"] == "Tasks"
    assert result["properties"]["Status"] == {"type": "select", "options": ["Todo", "Done"]}


# ---- notion_query_database (TC-NTN-010) -------------------------------------


async def test_query_database_filter_and_no_filter(monkeypatch):
    page = {"id": "p1", "parent": {}, "properties": {}}
    rec = Recorder({"POST databases/db1/query": {"results": [page], "has_more": False, "next_cursor": None}})
    await _call(monkeypatch, rec, "notion_query_database", {"database_id": "db1"})
    assert "filter" not in body(rec.requests[0])
    await _call(monkeypatch, rec, "notion_query_database", {"database_id": "db1", "filter": {"property": "Status", "select": {"equals": "Done"}}})
    assert body(rec.requests[1])["filter"] == {"property": "Status", "select": {"equals": "Done"}}


# ---- notion_list_block_children / notion_append_block_children (TC-NTN-011..013) --


async def test_list_block_children_paginates(monkeypatch):
    blocks = {
        "results": [
            {"id": "b1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hello"}]}, "has_children": False},
            {"id": "b2", "type": "to_do", "to_do": {"rich_text": [{"plain_text": "Buy milk"}], "checked": True}, "has_children": False},
        ],
        "has_more": True,
        "next_cursor": "cur-2",
    }
    rec = Recorder({"GET blocks/page1/children": blocks})
    result = await _call(monkeypatch, rec, "notion_list_block_children", {"block_id": "page1"})
    assert result["results"] == [
        {"id": "b1", "type": "paragraph", "text": "Hello", "has_children": False},
        {"id": "b2", "type": "to_do", "text": "Buy milk", "has_children": False, "checked": True},
    ]
    assert (result["has_more"], result["next_cursor"]) == (True, "cur-2")


async def test_append_block_children_supported_types(monkeypatch):
    created = {"results": [{"id": "new1", "type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Hi"}]}, "has_children": False}]}
    rec = Recorder({"PATCH blocks/page1/children": lambda r: httpx.Response(200, json=created)})
    result = await _call(
        monkeypatch, rec, "notion_append_block_children",
        {"block_id": "page1", "children": [{"type": "paragraph", "text": "Hi"}]},
    )
    sent = body(rec.calls("blocks/page1/children", "PATCH")[0])
    assert sent["children"] == [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": "Hi"}}]}}]
    assert result["created"][0]["id"] == "new1"


async def test_append_block_children_rejects_unsupported_type(monkeypatch):
    rec = Recorder()
    result = await _call(
        monkeypatch, rec, "notion_append_block_children",
        {"block_id": "page1", "children": [{"type": "paragraph", "text": "ok"}, {"type": "table", "text": "nope"}]},
    )
    assert result["error"]["code"] == "invalid_argument"
    assert "table" in result["error"]["message"]
    assert rec.requests == []  # nothing partially appended


# ---- notion_list_users (TC-NTN-014) -----------------------------------------


async def test_list_users_returns_members(monkeypatch):
    users = {"results": [{"id": "u1", "name": "Jane", "type": "person"}, {"id": "u2", "name": "Bot", "type": "bot"}], "has_more": False, "next_cursor": None}
    rec = Recorder({"GET users": users})
    result = await _call(monkeypatch, rec, "notion_list_users", {})
    assert result["results"] == [{"id": "u1", "name": "Jane", "type": "person"}, {"id": "u2", "name": "Bot", "type": "bot"}]


# ---- rate limiting / retry (TC-NTN-015) -------------------------------------


async def test_rate_limit_retry_then_exhausted_returns_rate_limited(monkeypatch):
    rec = Recorder({"GET pages/p1": lambda r: httpx.Response(429, headers={"Retry-After": "1"}, json={"object": "error", "status": 429, "code": "rate_limited", "message": "slow down"})})
    result = await _call(monkeypatch, rec, "notion_get_page", {"page_id": "p1"})
    assert result["error"] == {"code": "rate_limited", "message": "Notion returned HTTP 429 for pages/p1: slow down", "retryable": True}
    # 1 initial + 3 retries (api_client._MAX_RETRIES) = 4 attempts, never a raw 429 surfaced
    assert len(rec.calls("pages/p1", "GET")) == api_client._MAX_RETRIES + 1


async def test_rate_limit_retry_then_success(monkeypatch):
    replies = iter([httpx.Response(429, headers={"Retry-After": "1"}, json={}), httpx.Response(200, json={"id": "p1", "parent": {}, "properties": {}})])
    rec = Recorder({"GET pages/p1": lambda r: next(replies)})
    result = await _call(monkeypatch, rec, "notion_get_page", {"page_id": "p1"})
    assert result["id"] == "p1"
    assert len(rec.calls("pages/p1", "GET")) == 2


# ---- response size cap (TC-NTN-016) -----------------------------------------


async def test_large_result_truncated_with_cursor(monkeypatch):
    big_page = {
        "object": "page",
        "id": "p",
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "x" * 3000}]}},
    }
    pages = [{**big_page, "id": f"p{i}"} for i in range(50)]
    rec = Recorder({"POST search": {"results": pages, "has_more": False, "next_cursor": None}})
    result = await _call(monkeypatch, rec, "notion_search", {"query": "x"})
    raw = json.dumps(result, separators=(",", ":"))
    assert len(raw) <= 20_000
    assert result.get("has_more") is True or result.get("fields_truncated") is True
