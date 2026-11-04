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
"""The nine notion_* tools (PRD-21313 design, "Data & API contract")."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import shapes
from ._json import dump_json_capped, error_envelope
from .api_client import NotionClient, NotionError
from .config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

logger = logging.getLogger("notion_mcp.tools")

ClientFactory = Callable[[], NotionClient | None]

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITES_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)

NOT_CONFIGURED = error_envelope(
    "not_configured", "Notion credentials are missing for this request (X-Notion-Api-Key).", False
)

# Notion's own API caps page_size at 100 regardless of what a caller asks
# this service for; a larger `limit` here just means more pages get walked
# with next_cursor, not a bigger single call.
_NOTION_API_PAGE_SIZE_CAP = 100

Cursor = Annotated[str | None, Field(description="Opaque pagination cursor from a previous next_cursor.")]
Limit = Annotated[
    int | None,
    Field(description=f"Max records to return (default {DEFAULT_PAGE_SIZE}, max {MAX_PAGE_SIZE}).", ge=1, le=MAX_PAGE_SIZE),
]
PageId = Annotated[str, Field(description="Notion page id.", min_length=1)]
DatabaseId = Annotated[str, Field(description="Notion database id.", min_length=1)]
BlockId = Annotated[str, Field(description="Notion block or page id (a page id is also a valid block id).", min_length=1)]
Properties = Annotated[
    dict[str, Any] | None,
    Field(description="Flat column -> value map matching the target database (see notion_get_database), or {'title': <text>} for a page under a page."),
]


def _page_size(limit: int | None) -> int:
    return min(limit or DEFAULT_PAGE_SIZE, _NOTION_API_PAGE_SIZE_CAP)


def _paged(results: list[Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "results": results,
        "has_more": bool(payload.get("has_more")),
        "next_cursor": payload.get("next_cursor"),
    }


async def run_tool(
    name: str,
    client_factory: ClientFactory,
    action: Callable[[NotionClient], Awaitable[Any]],
) -> str:
    """Resolve the caller's client, run ``action``, and always return a string.

    Errors never escape as exceptions: they become the standard error envelope
    (PRD-21313 design, "Data & API contract" / SOP A3 error model).
    """
    client = client_factory()
    if client is None:
        return NOT_CONFIGURED
    started = time.monotonic()
    try:
        result = await action(client)
    except NotionError as e:
        logger.info("tool=%s outcome=error status=%s ms=%d", name, e.status_code, _ms(started))
        return e.to_envelope()
    except ValueError as e:
        logger.info("tool=%s outcome=invalid_argument ms=%d", name, _ms(started))
        return error_envelope("invalid_argument", str(e), False)
    except Exception as e:  # noqa: BLE001 - tools must never raise
        logger.warning("tool=%s outcome=unexpected error=%s ms=%d", name, type(e).__name__, _ms(started))
        return error_envelope("upstream_error", f"Unexpected error: {type(e).__name__}", True)
    logger.info("tool=%s outcome=ok ms=%d", name, _ms(started))
    return result if isinstance(result, str) else dump_json_capped(result)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def register(mcp: FastMCP, client_factory: ClientFactory) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def notion_search(
        query: Annotated[str, Field(description="Free-text query matched against page and database titles.", min_length=1)],
        filter_object_type: Annotated[Literal["page", "database"] | None, Field(description="Restrict to pages or databases; omit for both.")] = None,
        cursor: Cursor = None,
        limit: Limit = None,
    ) -> str:
        """Search the workspace for pages and databases shared with this integration."""

        async def action(client: NotionClient) -> dict:
            body: dict[str, Any] = {"query": query, "page_size": _page_size(limit)}
            if filter_object_type:
                body["filter"] = {"property": "object", "value": filter_object_type}
            if cursor:
                body["start_cursor"] = cursor
            payload = await client.search(body)
            return _paged([shapes.collapse_search_result(r) for r in payload.get("results", [])], payload)

        return await run_tool("notion_search", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def notion_get_page(page_id: PageId) -> str:
        """Get one page: id, parent, archived flag, url, and its properties."""

        async def action(client: NotionClient) -> dict:
            return shapes.collapse_page(await client.get_page(page_id))

        return await run_tool("notion_get_page", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def notion_create_page(
        parent_page_id: Annotated[str | None, Field(description="Create under this page. Exactly one of parent_page_id/parent_database_id is required.")] = None,
        parent_database_id: Annotated[str | None, Field(description="Create as a row in this database. Exactly one of parent_page_id/parent_database_id is required.")] = None,
        properties: Properties = None,
    ) -> str:
        """Create a page under a page or as a row in a database."""

        async def action(client: NotionClient) -> dict:
            if bool(parent_page_id) == bool(parent_database_id):
                raise ValueError("Pass exactly one of parent_page_id or parent_database_id.")
            values = properties or {}
            if parent_page_id:
                extra = set(values) - {"title"}
                if extra:
                    raise ValueError(f"A page under a page only has a title; unexpected field(s): {', '.join(sorted(extra))}.")
                body = {"parent": {"page_id": parent_page_id}, "properties": shapes.build_title_property(str(values.get("title", "")))}
            else:
                schema = (await client.get_database(parent_database_id)).get("properties", {})
                body = {"parent": {"database_id": parent_database_id}, "properties": shapes.build_db_properties(values, schema)}
            created = await client.create_page(body)
            return {"id": created.get("id"), "url": created.get("url")}

        return await run_tool("notion_create_page", client_factory, action)

    @mcp.tool(annotations=WRITES_IDEMPOTENT)
    async def notion_update_page(
        page_id: PageId,
        properties: Properties = None,
        archived: Annotated[bool | None, Field(description="true moves the page to Notion's trash, false restores it.")] = None,
    ) -> str:
        """Update a page's properties and/or archive (trash) / restore it."""

        async def action(client: NotionClient) -> dict:
            body: dict[str, Any] = {}
            if archived is not None:
                body["archived"] = archived
            if properties:
                page = await client.get_page(page_id)
                parent = page.get("parent") or {}
                if parent.get("type") == "database_id":
                    schema = (await client.get_database(parent["database_id"])).get("properties", {})
                    body["properties"] = shapes.build_db_properties(properties, schema)
                else:
                    extra = set(properties) - {"title"}
                    if extra:
                        raise ValueError(f"This page is not a database row; only 'title' can be updated, not: {', '.join(sorted(extra))}.")
                    if "title" in properties:
                        body["properties"] = shapes.build_title_property(str(properties["title"]))
            if not body:
                raise ValueError("Nothing to update: pass properties and/or archived.")
            return shapes.collapse_page(await client.update_page(page_id, body))

        return await run_tool("notion_update_page", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def notion_get_database(database_id: DatabaseId) -> str:
        """Get a database's id, title, url and property schema (name, type, options)."""

        async def action(client: NotionClient) -> dict:
            return shapes.collapse_database(await client.get_database(database_id))

        return await run_tool("notion_get_database", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def notion_query_database(
        database_id: DatabaseId,
        filter: Annotated[dict[str, Any] | None, Field(description="Notion filter object, passed through as-is; omit to return every row.")] = None,
        sorts: Annotated[list[dict[str, Any]] | None, Field(description="Notion sort objects, passed through as-is.")] = None,
        cursor: Cursor = None,
        limit: Limit = None,
    ) -> str:
        """List a database's rows (pages), optionally filtered and sorted."""

        async def action(client: NotionClient) -> dict:
            body: dict[str, Any] = {"page_size": _page_size(limit)}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            payload = await client.query_database(database_id, body)
            return _paged([shapes.collapse_page(p) for p in payload.get("results", [])], payload)

        return await run_tool("notion_query_database", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def notion_list_block_children(block_id: BlockId, cursor: Cursor = None, limit: Limit = None) -> str:
        """List a page's or block's direct children (paragraphs, headings, lists, ...)."""

        async def action(client: NotionClient) -> dict:
            params = {"page_size": _page_size(limit)}
            if cursor:
                params["start_cursor"] = cursor
            payload = await client.list_block_children(block_id, params)
            return _paged([shapes.collapse_block(b) for b in payload.get("results", [])], payload)

        return await run_tool("notion_list_block_children", client_factory, action)

    @mcp.tool(annotations=WRITES)
    async def notion_append_block_children(
        block_id: BlockId,
        children: Annotated[
            list[dict[str, Any]],
            Field(
                description="Blocks to append, each {'type': paragraph|heading_1|heading_2|heading_3|bulleted_list_item|numbered_list_item|to_do, 'text': str[, 'checked': bool]}.",
                min_length=1,
            ),
        ],
    ) -> str:
        """Append blocks (common text types only) to the end of a page or block.

        Atomic: nothing is appended if any block type is unsupported.
        """

        async def action(client: NotionClient) -> dict:
            built = [shapes.build_block(c) for c in children]
            created = await client.append_block_children(block_id, {"children": built})
            return {"created": [shapes.collapse_block(b) for b in created.get("results", [])]}

        return await run_tool("notion_append_block_children", client_factory, action)

    @mcp.tool(annotations=READ_ONLY)
    async def notion_list_users(cursor: Cursor = None, limit: Limit = None) -> str:
        """List the workspace's users (id, name, person/bot type).

        For resolving a "person" property's id to a name, not administration.
        """

        async def action(client: NotionClient) -> dict:
            params = {"page_size": _page_size(limit)}
            if cursor:
                params["start_cursor"] = cursor
            payload = await client.list_users(params)
            return _paged([shapes.collapse_user(u) for u in payload.get("results", [])], payload)

        return await run_tool("notion_list_users", client_factory, action)
