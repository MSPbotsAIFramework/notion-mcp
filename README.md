# Notion MCP Server

An [MCP](https://modelcontextprotocol.io) server for the [Notion](https://www.notion.so) API. It
lets an AI agent search a workspace, read and write pages and database rows, read and append page
content (blocks), and look up workspace users -- scoped to whatever the caller's own Notion
internal integration has been shared with.

- Transport: MCP Streamable HTTP at `POST /mcp`, health probe at `GET /health`
- Stateless and multi-tenant: the integration secret arrives on every request as an HTTP header and
  is never stored; every tool is listed from the first `tools/list`
- Python 3.12, [uv](https://docs.astral.sh/uv/), FastMCP, httpx
- License: Apache-2.0, Copyright 2026 MSPbots.ai

<div align="center">

## MSPbots Connector

**The MCP Gateway built for MSPs: visible, controlled, and your data stays where it belongs.**

[**Get started at connect.mspbots.ai →**](https://connect.mspbots.ai)

</div>

---

MSPbots Connector gives your AI clients one MCP URL to reach your entire MSP stack (RMM, PSA, documentation, security, finance), and gives you control over who can call which tool and a record of everything that was called.

**Hosted service: [connect.mspbots.ai](https://connect.mspbots.ai)** · Free to use.

### Why Connector

![MSPbots Connector](https://stgoss.mspbots.ai/files/mcp/mspbots-mcp.png)

| | |
|---|---|
| **Visible** | Every tool call is logged: who, which vendor, when. Usage dashboard, CSV export, SIEM streaming. |
| **Controlled** | Role-based, per-tool allowlist. Deny by default. |
| **Protected** | Credentials encrypted per record. AI clients never see your vendor passwords or tokens. |
| **MSP-native** | Built for the MSP stack only: RMM, PSA, documentation, security, finance. |

### Quick start

1. **Sign up** at [connect.mspbots.ai](https://connect.mspbots.ai).
2. **Connect your vendors.** Pick from 150+ connectors and authorize each one on the vendor's own page, or bring your own API keys. No extra vendor accounts needed.
3. **Set access.** Create roles and decide which tools each role may call. Nothing is allowed until you allow it.
4. **Add your MCP URL to your AI client.** Copy the URL from your Connector dashboard into Claude, ChatGPT, Cursor, VS Code, Gemini, Codex, or any MCP-capable client.
5. **Ask your AI to do real work**, and watch every call appear in the audit log.

### Features

**Unified gateway**
- One MCP URL for all your AI clients
- 150+ connectors across the MSP stack
- Custom MCP servers supported, free
- Authorization happens on each vendor's own page; the AI never sees passwords or tokens
- Bring your own credentials: use the vendor API keys you already have
- Multiple accounts per app; connect once, reuse across AI clients
- Tool search: finds the right action for the task
- One sign-in; vendor credentials are injected behind the scenes and OAuth tokens refresh automatically

**Visibility**
- Audit log for every tool call (who, which vendor, when)
- Usage dashboard: call volume, top vendors, error rate, per-user activity
- Export audit logs to CSV, or stream to Loki, Graylog, or LogScale (SIEM)
- Optional recording of tool arguments and result summaries (off by default, enabled per organization)

**Control**
- Per-role, per-tool allowlist, deny by default
- owner / admin / member roles
- SCIM provisioning with Entra ID, Okta, JumpCloud, and Google Workspace

**Security**
- Credentials encrypted one record at a time (AES-256-GCM, per-record key)
- AI clients authenticate with OAuth 2.1 + PKCE
- Vendor responses are scanned for prompt-injection patterns and returned as structured data. This is one layer of defense, not a guarantee against every attack.
- Static egress IP for customer firewall and API allowlisting

### Pricing

**Free.**

Links: [connect.mspbots.ai](https://connect.mspbots.ai) · [mspbots.ai](https://mspbots.ai)

---

## Authentication headers

| Header | Required | Type | Default | Allowed values | Description | Example |
|---|---|---|---|---|---|---|
| `X-Notion-Api-Key` | Yes | string | — | Notion internal integration secret | The tenant's own Notion integration secret. From Notion → Settings → Connections → Develop or manage integrations → the integration → **Internal Integration Secret**. The integration must also be *shared* with the specific pages/databases you want reachable -- Notion does not grant workspace-wide access by default (share a page or database with it from that item's **···** menu → Connections). | `ntn_xxx` |

Missing header → `401`, body `{"error": "missing_header", "required": ["X-Notion-Api-Key"]}`.

## Tools

| Tool | What it does | Notes |
|---|---|---|
| `notion_search` | Search the workspace for pages and databases by title/content | read-only |
| `notion_get_page` | Get one page (properties, parent, archived flag, url) | read-only |
| `notion_create_page` | Create a page under a page, or as a row in a database | write |
| `notion_update_page` | Update a page's properties, and/or archive (trash) / restore it | write, idempotent |
| `notion_get_database` | Get a database's property schema (names, types, select/status options) | read-only |
| `notion_query_database` | List a database's rows, optionally filtered and sorted | read-only |
| `notion_list_block_children` | Read a page's or block's content (paragraphs, headings, lists, ...) | read-only |
| `notion_append_block_children` | Append content to a page or block (common text block types only) | write |
| `notion_list_users` | List workspace users (for resolving a "person" property's id to a name) | read-only |

Scope (v1): pages, database rows (not schema edits), the common text block types (paragraph,
heading 1-3, bulleted/numbered list items, to-do), and read-only users. No comments API, no
workspace administration, no permanent deletion (Notion's own API only exposes reversible
archive/trash), no OAuth (single static per-tenant API key only). See
`docs/tickets/PRD-21313.md` for the full design and the reasoning behind these choices.

## Errors

Every tool returns a JSON string. On failure: `{"error": {"code", "message", "retryable"}}`, where
`code` is one of `not_configured`, `unauthorized`, `not_found`, `invalid_argument`, `rate_limited`,
`upstream_error`. `not_found` covers both "the id doesn't exist" and "it exists but isn't shared
with this integration" -- Notion's API does not distinguish the two.

## Local development

```bash
uv sync
uv run pytest
docker compose up --build
curl -s localhost:8080/health
curl -s -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'X-Notion-Api-Key: <your test integration secret>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
