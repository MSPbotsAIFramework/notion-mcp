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
"""Async client for the Notion REST API."""

import asyncio
import logging
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

from ._json import error_envelope
from .config import API_BASE_URL, NOTION_VERSION

logger = logging.getLogger("notion_mcp.api")

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS = 20.0
# Whole-call budget including retries; the gateway times a tool call out at 120s.
_CALL_BUDGET_SECONDS = 100.0

# One connection pool for the process. It never holds credentials: the
# Authorization header is built per request from the caller's own key, so
# sharing the pool across tenants is safe (PRD-21313 design, "Non-functional").
_shared_http_client: httpx.AsyncClient | None = None


class _RejectAllCookies(DefaultCookiePolicy):
    """Never store or send a cookie: the HTTP client is shared by every tenant."""

    def set_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False

    def return_ok(self, cookie, request) -> bool:  # noqa: ANN001 - stdlib signature
        return False


def _no_cookies() -> CookieJar:
    return CookieJar(policy=_RejectAllCookies())


def _get_shared_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(cookies=_no_cookies(), timeout=_TIMEOUT)
    return _shared_http_client


# Notion's own error codes (the body's "code") map onto our envelope more
# reliably than the bare HTTP status for a couple of cases (403 can be either
# "restricted_resource" or an auth problem); fall back to the status table
# when the body doesn't have a recognised code.
_NOTION_CODE_TO_ENVELOPE: dict[str, tuple[str, bool]] = {
    "unauthorized": ("unauthorized", False),
    "restricted_resource": ("unauthorized", False),
    "object_not_found": ("not_found", False),
    "validation_error": ("invalid_argument", False),
    "invalid_json": ("invalid_argument", False),
    "invalid_request_url": ("invalid_argument", False),
    "invalid_request": ("invalid_argument", False),
    "rate_limited": ("rate_limited", True),
    "internal_server_error": ("upstream_error", True),
    "service_unavailable": ("upstream_error", True),
    "database_connection_unavailable": ("upstream_error", True),
    "gateway_timeout": ("upstream_error", True),
}
# status_code -> (error code, retryable), used when the body has no usable "code".
# 0 means no HTTP response at all (connection error or timeout). 529 is Notion's
# service-overload status, called out alongside 429 in the design (AC12).
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    409: ("invalid_argument", False),
    429: ("rate_limited", True),
    529: ("rate_limited", True),
}


def classify(status_code: int, body_code: str | None) -> tuple[str, bool]:
    if body_code and body_code in _NOTION_CODE_TO_ENVELOPE:
        return _NOTION_CODE_TO_ENVELOPE[body_code]
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    if status_code >= 400:
        return "invalid_argument", False
    return "upstream_error", True


class NotionError(Exception):
    def __init__(self, status_code: int, message: str, body_code: str | None = None):
        self.status_code = status_code
        self.message = message
        self.body_code = body_code
        super().__init__(f"Notion API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = classify(self.status_code, self.body_code)
        return error_envelope(code, self.message, retryable)


@dataclass(frozen=True)
class NotionCredentials:
    api_key: str

    def __repr__(self) -> str:  # never leak the key into logs or tracebacks
        return "NotionCredentials(api_key=<redacted>)"


class NotionClient:
    """Thin wrapper over the Notion REST API for one caller's integration token."""

    def __init__(self, credentials: NotionCredentials, http_client: httpx.AsyncClient | None = None):
        self._credentials = credentials
        self._http = http_client
        self.base_url = API_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._credentials.api_key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    # ---- resource-shaped calls (PRD-21313 "Data & API contract") ----------

    async def search(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "search", body=body)

    async def get_page(self, page_id: str) -> dict[str, Any]:
        return await self._request("GET", f"pages/{page_id}")

    async def create_page(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "pages", body=body)

    async def update_page(self, page_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"pages/{page_id}", body=body)

    async def get_database(self, database_id: str) -> dict[str, Any]:
        return await self._request("GET", f"databases/{database_id}")

    async def query_database(self, database_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"databases/{database_id}/query", body=body)

    async def list_block_children(self, block_id: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", f"blocks/{block_id}/children", params=params)

    async def append_block_children(self, block_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", f"blocks/{block_id}/children", body=body)

    async def list_users(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("GET", "users", params=params)

    # ---- transport ----------------------------------------------------------

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None
    ) -> dict[str, Any]:
        http = self._http or _get_shared_http_client()
        url = f"{self.base_url}/{path}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        headers = self._headers()
        started = time.monotonic()
        attempt = 0
        while True:
            t0 = time.monotonic()
            resp: httpx.Response | None = None
            transport_error: NotionError | None = None
            try:
                resp = await http.request(method, url, params=query, json=body, headers=headers)
                status = resp.status_code
            except httpx.TimeoutException:
                status = 0
                transport_error = NotionError(0, f"Timed out calling Notion ({method} {path})")
            except httpx.RequestError as e:
                status = 0
                transport_error = NotionError(0, f"Network error calling Notion ({method} {path}): {type(e).__name__}")

            logger.info(
                "notion_api method=%s path=%s status=%s ms=%d attempt=%d",
                method, path, status, (time.monotonic() - t0) * 1000, attempt,
            )

            retryable = status in (429, 529) or status >= 500 or status == 0
            if retryable and attempt < _MAX_RETRIES:
                delay = _retry_delay(resp, attempt)
                if time.monotonic() - started + delay < _CALL_BUDGET_SECONDS:
                    attempt += 1
                    await asyncio.sleep(delay)
                    continue

            if transport_error is not None:
                raise transport_error
            assert resp is not None
            if resp.status_code >= 400:
                raise _to_error(resp, path)
            if not resp.content or not resp.content.strip():
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw_response": resp.text[:2000]}


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_BACKOFF_SECONDS))
            except ValueError:
                pass
    return min(float(2**attempt), _MAX_BACKOFF_SECONDS)


def _to_error(resp: httpx.Response, path: str) -> NotionError:
    """Notion errors: {"object": "error", "status": ..., "code": ..., "message": ...}."""
    fallback = f"Notion returned HTTP {resp.status_code} for {path}"
    try:
        payload = resp.json()
    except ValueError:
        text = resp.text.strip()
        return NotionError(resp.status_code, f"{fallback}: {text[:300]}" if text else fallback)
    if not isinstance(payload, dict):
        return NotionError(resp.status_code, fallback)
    message = str(payload.get("message") or "").strip()
    code = payload.get("code") if isinstance(payload.get("code"), str) else None
    text = f"{fallback}: {message}" if message else fallback
    return NotionError(resp.status_code, text, body_code=code)
