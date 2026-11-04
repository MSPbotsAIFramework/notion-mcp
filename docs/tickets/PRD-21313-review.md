# PRD-21313 · Code review

## Round 1 · 2026-11-04 · reviewed dev/PRD-21313 @ fa03bc6 (base main @ 1ef5429)

Status: frozen @ fa03bc6

Verdict: approved

### Scope

`git diff origin/main...fa03bc6` in `MSPbotsAIFramework/notion-mcp` (first version of this repo; base `1ef5429` is the single README-only initial commit). 24 files, +3609/-2. Read in full: `server.py`, `api_client.py`, `tools.py`, `shapes.py`, `config.py`, `_json.py`, `_arguments.py`, `__main__.py`, `tests/test_tools.py`, `tests/test_middleware.py`, `Dockerfile`, `README.md`, `pyproject.toml`, `docs/tickets/PRD-21313.md`, `docs/tickets/PRD-21313.ac.json`.

### Verification performed

- `uv sync` → 0, clean install.
- `uv run pytest -q` → **39 passed**, matches the `[DEV]` stub's claim.
- `grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .` → no output (no Chinese).
- `grep -rniE '(ntn_[a-z0-9]{20,}|secret_[a-z0-9]{20,})' --exclude-dir=.git --exclude-dir=.venv .` → no output (no committed secret-shaped strings).
- `gitleaks` not installed in this sandbox (same as dev leg's note); the manual secret-pattern grep above substitutes.
- `docker build` not run: `docker info` confirms the daemon is unusable in this sandbox, same environment limitation the dev leg already flagged in Handoff. `Dockerfile` read manually against SOP §8/§14: multi-stage, non-root (`USER app`), `curl` installed in the production stage, `EXPOSE 8080`, `ENV MCP_HTTP_PORT=8080 MCP_HTTP_HOST=0.0.0.0`, `HEALTHCHECK` against `/health`. Not a blocker (REVIEW.md §1 "environment failure, not a code problem"); `/api/vendor-register` builds the image server-side as part of registration, so this gets exercised for real at R-4.8/4.4.
- README "MSPbots Connector" section (`README.md:14-84`) diffed byte-for-byte against `vendor-mcp-development/reference/readme-connector-section.md` → identical.
- Commit authorship on all 3 commits in the diff: `Dev01.MBAgent <dev01.mbagent@mspbots.ai>` (author and committer) — correct for the MCP track (`mcp-track.md` §4.7).

### Findings

No must-fix findings.

1. [suggestion] `src/notion_mcp/api_client.py:39,56-60` — the tech design ("Change list" / "Non-functional" in `docs/tickets/PRD-21313.md`) describes "one `httpx.AsyncClient` per request (built from the context credential, not shared/cached across requests)" and "each request has its own credential and its own `httpx.AsyncClient`". The actual implementation uses one process-wide shared `httpx.AsyncClient` (`_shared_http_client`), with the `Authorization` header built per call in `_headers()` rather than stored on the client. This is a legitimate implementation choice (connection reuse, no cookie jar cross-contamination since `_RejectAllCookies` is installed, no client-level default headers that could leak between tenants) and satisfies SOP §14's "reuse the same HTTP client within one request" more strongly than required — it is not a security or correctness problem. It is a documentation/implementation mismatch against the frozen main doc, which this leg has no authority to edit. Noting it so a future reader of the tech design isn't misled about the actual request-sharing model; no action required.
2. [suggestion] `tests/test_tools.py:241-250` (`test_create_page_invalid_property_type_returns_invalid_argument`, TC-NTN-007) — the test name and the AC5/test-case wording ("invalid property values... wrong type for the target database's schema, e.g. a string where a date is expected") suggest a value-type-mismatch scenario, but the test actually exercises a different local check: a property name (`"Unknown"`) that does not exist in the database's schema at all. The ac.json `evidence` field for this case accurately describes what was tested, so this is not a false "passed" claim — but the genuine "wrong type for a known column" path (e.g. a string where Notion's API expects a `date`) is not built-in-validated by this code (`shapes.build_db_properties` only checks column existence and whether the type is writable, not the value's shape) and relies entirely on Notion's own 400 `validation_error` being passed through via `api_client.classify()`, which is covered only by the generic parametrized `test_error_envelope` mapping test, not by a `create_page`-specific one. Behavior is correct (AC5's "Notion's validation message passed through" is exactly what happens), but the test naming overstates what TC-NTN-007 covers. No fix required before merge; worth tightening the test name/description if this file is touched again.

### Conclusion

Code matches the tech design's AC1–AC14, the SOP's contact contract (§1, §3: header-only credentials, no env fallback, no cross-request state, `contextvars` isolation with `finally` reset verified by `test_concurrent_requests_do_not_leak_context`), the error-envelope model (§4), retry/backoff with `Retry-After` honored and a 100s call budget under the gateway's 120s tool-timeout (§5), and the response-size cap (AC13, exercised with a 150k-char payload). README Header table and Connector section both correct. No schema/migration concerns (stateless passthrough, not applicable to this repo). No UI (`@mspbots/ui` requirement not applicable). 18/19 test cases pass with evidence; the 19th (TC-NTN-019) is correctly deferred as "needs a real Notion account," which is not a blocker per SOP/`mcp-track.md`. Proceeding to merge into `main`, first-time vendor registration (`/api/vendor-register`, not `/api/deploy-int`), and the connector wiki.
