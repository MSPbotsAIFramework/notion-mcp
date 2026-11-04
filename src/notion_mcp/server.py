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
"""FastMCP factory and the per-request credential middleware."""

import contextvars
import re
import uuid
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ._arguments import EnvelopeFastMCP
from .api_client import NotionClient, NotionCredentials
from .config import Settings

HEADER_API_KEY = "X-Notion-Api-Key"
REQUIRED_HEADERS = [HEADER_API_KEY]

_PLACEHOLDER = re.compile(r"^\$\{.*\}$")

# Per-request isolation: the middleware sets this for the duration of one
# HTTP request and resets it in `finally`. The credential is never stored at
# module level (PRD-21313 design AC14 / Non-functional "Multi-tenant isolation").
_credentials_var: contextvars.ContextVar[NotionCredentials | None] = contextvars.ContextVar(
    "notion_credentials", default=None
)
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("notion_request_id", default="-")

INSTRUCTIONS = (
    "Notion is a workspace of pages and databases. A database is a table "
    "whose rows are pages with typed properties (its schema); a page's body "
    "is a tree of blocks (paragraphs, headings, lists).\n"
    "Usage:\n"
    "- Find things with notion_search (free text, optionally filtered to "
    "page or database); read a page with notion_get_page, a database's "
    "schema with notion_get_database, and its rows with "
    "notion_query_database.\n"
    "- A page's content is read with notion_list_block_children and "
    "extended with notion_append_block_children (common text blocks only: "
    "paragraph, heading_1-3, bulleted_list_item, numbered_list_item, to_do).\n"
    "- Create a page under a page (parent_page_id, properties={'title': "
    "...}) or under a database (parent_database_id, properties matching "
    "that database's columns -- call notion_get_database first to see "
    "them). notion_update_page changes properties and/or archives "
    "(archived: true moves to trash, false restores).\n"
    "- All list/query tools take cursor + limit (default 50, max 200); "
    "has_more says whether to page on.\n"
    "- The integration only sees pages/databases it has been explicitly "
    "shared with in Notion; not_found can mean either \"does not exist\" or "
    "\"not shared\" -- Notion's API does not distinguish them."
)


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text or _PLACEHOLDER.match(text):
        return None
    return text


def get_client_from_context() -> NotionClient | None:
    """The client for the current request, or None outside a request."""
    credentials = _credentials_var.get()
    if credentials is None:
        return None
    return NotionClient(credentials)


class GatewayCredentialsMiddleware:
    """Reads X-Notion-Api-Key on /mcp requests; 401 when missing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        api_key = _clean(request.headers.get(HEADER_API_KEY))
        if not api_key:
            await JSONResponse(
                {"error": "missing_header", "required": REQUIRED_HEADERS},
                status_code=401,
            )(scope, receive, send)
            return

        request_id = (request.headers.get("x-request-id") or "").strip()[:64] or uuid.uuid4().hex[:12]
        creds_token = _credentials_var.set(NotionCredentials(api_key))
        rid_token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _credentials_var.reset(creds_token)
            request_id_var.reset(rid_token)


def create_mcp_server(settings: Settings | None = None) -> FastMCP:
    """Build the FastMCP server and register every tool."""
    mcp = EnvelopeFastMCP(
        name="notion-mcp",
        instructions=INSTRUCTIONS,
        # The service sits behind the gateway, so Host is never localhost;
        # DNS-rebinding protection would reject those requests with 421.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        stateless_http=True,
    )

    client_factory: Callable[[], NotionClient | None] = get_client_from_context

    from . import tools

    tools.register(mcp, client_factory)

    return mcp
