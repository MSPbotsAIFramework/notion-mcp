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
"""Token-economical JSON serialization for tool results (PRD-21313 AC13).

Compact (no indent), non-ASCII preserving, and capped at MAX_CHARS so one
tool call can never flood the agent's context.
"""

import json
from typing import Any

MAX_CHARS = 20_000

# Keys that point to the next page. When a list is cut to fit, these are
# replaced with has_more / truncated so the caller never silently skips
# the records that got cut.
_CONTINUATION_KEYS = frozenset({"next_cursor"})
# (max characters per text value, max items per nested list), mildest first.
_SHRINK_STEPS = ((2000, 50), (500, 20), (200, 10), (80, 5))


def compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def error_envelope(code: str, message: str, retryable: bool) -> str:
    return compact({"error": {"code": code, "message": message, "retryable": retryable}})


def dump_json_capped(data: Any, max_chars: int = MAX_CHARS) -> str:
    """Serialize ``data`` within ``max_chars``.

    If the compact form already fits, it is returned as is. Otherwise the
    largest list field (e.g. "results") is shortened step by step (long text
    and nested lists inside each record), and if that is still not enough,
    cut down to the first N records that fit, with has_more / truncated set
    so the caller knows to come back with next_cursor for the rest.
    """
    text = compact(data)
    if len(text) <= max_chars:
        return text

    if not isinstance(data, dict):
        return compact({"truncated": True, "note": f"Result too large to return ({len(text)} chars)."})

    list_keys = [k for k, v in data.items() if isinstance(v, list)]
    if not list_keys:
        return compact({"truncated": True, "note": f"Result too large to return ({len(text)} chars)."})

    key = max(list_keys, key=lambda k: len(compact(data[k])))
    items = data[key]

    # Step 1: shorten long values inside every record, keeping all of them.
    for text_limit, list_limit in _SHRINK_STEPS:
        shortened = [_shrink(item, text_limit, list_limit) for item in items]
        candidate = dict(data) | {key: shortened, "fields_truncated": True}
        result = compact(candidate)
        if len(result) <= max_chars:
            return result

    # Step 2: return only the first N records that fit, dropping onward
    # pagination fields (continuing from them would skip the cut records).
    shortened = [_shrink(item, _SHRINK_STEPS[-1][0], _SHRINK_STEPS[-1][1]) for item in items]

    def build(n: int) -> dict:
        candidate = {k: v for k, v in data.items() if k not in _CONTINUATION_KEYS}
        candidate[key] = shortened[:n]
        if "count" in data:
            candidate["count"] = n
        candidate["has_more"] = True
        candidate["truncated"] = True
        candidate["note"] = (
            f"Only {n} of {len(items)} records fit the response size limit; "
            "narrow the query or use a smaller limit."
        )
        return candidate

    lo, hi = 0, len(items)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len(compact(build(mid))) <= max_chars:
            lo = mid
        else:
            hi = mid - 1
    if lo > 0:
        return compact(build(lo))

    return compact(
        {
            "truncated": True,
            "note": f"Result too large to return even one record ({len(text)} chars); narrow the query.",
        }
    )


def _shrink(value: Any, text_limit: int, list_limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[:text_limit] + "...[truncated]"
    if isinstance(value, list):
        kept = [_shrink(v, text_limit, list_limit) for v in value[:list_limit]]
        return kept + ([f"...[{len(value) - list_limit} more]"] if len(value) > list_limit else [])
    if isinstance(value, dict):
        return {k: _shrink(v, text_limit, list_limit) for k, v in value.items()}
    return value
