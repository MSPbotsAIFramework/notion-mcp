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
"""Collapse Notion's verbose page/property/block JSON into compact shapes,
and the inverse direction (a flat value map -> Notion's typed property JSON)
for notion_create_page / notion_update_page.

Scope matches the PRD-21313 design: pages, databases (rows, not schema
edits), the common text block types, and read-only users.
"""

from typing import Any

# Block types notion_append_block_children accepts in v1 (design "Non-goals" /
# change list: "common text block types").
WRITABLE_BLOCK_TYPES = (
    "paragraph",
    "heading_1",
    "heading_2",
    "heading_3",
    "bulleted_list_item",
    "numbered_list_item",
    "to_do",
)

# Property types notion_create_page / notion_update_page accept (design
# Non-goals: no schema edits, no relation/files/people writes in v1 -- those
# need object lookups this thin layer does not do). Reading supports more
# types than writing; see collapse_property_value.
WRITABLE_PROPERTY_TYPES = (
    "title",
    "rich_text",
    "number",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "select",
    "multi_select",
    "status",
    "date",
)


def _plain_text(rich_text: list[dict[str, Any]] | None) -> str:
    return "".join(item.get("plain_text", "") for item in (rich_text or []))


def _text_run(content: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": content}}


# ---------------------------------------------------------------------------
# Reading: Notion's typed property value -> a plain Python value
# ---------------------------------------------------------------------------


def collapse_property_value(prop: dict[str, Any]) -> Any:
    kind = prop.get("type")
    if kind in ("title", "rich_text"):
        return _plain_text(prop.get(kind))
    if kind == "number":
        return prop.get("number")
    if kind == "checkbox":
        return prop.get("checkbox")
    if kind in ("url", "email", "phone_number"):
        return prop.get(kind)
    if kind == "select":
        value = prop.get("select")
        return value.get("name") if value else None
    if kind == "status":
        value = prop.get("status")
        return value.get("name") if value else None
    if kind == "multi_select":
        return [item.get("name") for item in prop.get("multi_select") or []]
    if kind == "date":
        value = prop.get("date")
        if not value:
            return None
        return value["start"] if value.get("end") is None else {"start": value.get("start"), "end": value.get("end")}
    if kind == "people":
        return [collapse_user(u) for u in prop.get("people") or []]
    if kind == "relation":
        return [item.get("id") for item in prop.get("relation") or []]
    if kind == "files":
        return [_file_ref(f) for f in prop.get("files") or []]
    if kind == "formula":
        inner = prop.get("formula") or {}
        return inner.get(inner.get("type"))
    if kind == "rollup":
        inner = prop.get("rollup") or {}
        inner_type = inner.get("type")
        if inner_type == "array":
            return [collapse_property_value(item) for item in inner.get("array") or []]
        return inner.get(inner_type)
    if kind in ("created_time", "last_edited_time"):
        return prop.get(kind)
    if kind in ("created_by", "last_edited_by"):
        return collapse_user(prop.get(kind) or {})
    if kind == "unique_id":
        value = prop.get("unique_id") or {}
        prefix, number = value.get("prefix"), value.get("number")
        return f"{prefix}-{number}" if prefix else number
    # Unknown/future property type: best effort, return the inner value as is.
    return prop.get(kind)


def _file_ref(file_obj: dict[str, Any]) -> dict[str, Any]:
    kind = file_obj.get("type")
    url = (file_obj.get(kind) or {}).get("url") if kind else None
    return {"name": file_obj.get("name"), "url": url}


def collapse_user(user: dict[str, Any]) -> dict[str, Any]:
    person_or_bot = "bot" if user.get("type") == "bot" else "person"
    return {"id": user.get("id"), "name": user.get("name"), "type": person_or_bot}


def collapse_properties(properties: dict[str, Any] | None) -> dict[str, Any]:
    return {name: collapse_property_value(prop) for name, prop in (properties or {}).items()}


def extract_title(object_: dict[str, Any]) -> str:
    """Best-effort title for a page or database (design AC2: "title (best-effort extraction)")."""
    if object_.get("object") == "database":
        return _plain_text(object_.get("title"))
    for prop in (object_.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain_text(prop.get("title"))
    return ""


def collapse_search_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "object": item.get("object"),
        "title": extract_title(item),
        "url": item.get("url"),
        "last_edited_time": item.get("last_edited_time"),
    }


def collapse_page(page: dict[str, Any]) -> dict[str, Any]:
    parent = page.get("parent") or {}
    return {
        "id": page.get("id"),
        "parent": {"type": parent.get("type"), "id": parent.get(parent.get("type", ""))},
        "archived": page.get("archived"),
        "url": page.get("url"),
        "last_edited_time": page.get("last_edited_time"),
        "properties": collapse_properties(page.get("properties")),
    }


def collapse_schema_property(prop_def: dict[str, Any]) -> dict[str, Any]:
    kind = prop_def.get("type")
    shape: dict[str, Any] = {"type": kind}
    if kind in ("select", "multi_select", "status"):
        options = (prop_def.get(kind) or {}).get("options", [])
        shape["options"] = [o.get("name") for o in options]
    return shape


def collapse_database(database: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": database.get("id"),
        "title": _plain_text(database.get("title")),
        "url": database.get("url"),
        "properties": {
            name: collapse_schema_property(prop_def) for name, prop_def in (database.get("properties") or {}).items()
        },
    }


def collapse_block(block: dict[str, Any]) -> dict[str, Any]:
    kind = block.get("type")
    inner = block.get(kind) or {} if kind else {}
    shape = {
        "id": block.get("id"),
        "type": kind,
        "text": _plain_text(inner.get("rich_text")),
        "has_children": bool(block.get("has_children")),
    }
    if kind == "to_do":
        shape["checked"] = inner.get("checked", False)
    return shape


# ---------------------------------------------------------------------------
# Writing: a flat value -> Notion's typed property JSON
# ---------------------------------------------------------------------------


def build_title_property(text: str) -> dict[str, Any]:
    """Property payload for a page created directly under another page.

    Notion requires the literal key "title" in this case (there is no
    database schema to look the real column name up in).
    """
    return {"title": {"title": [_text_run(text)]}}


def _build_date_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"start": value.get("start"), "end": value.get("end")}
    return {"start": value, "end": None}


_PROPERTY_BUILDERS = {
    "title": lambda v: {"title": [_text_run(str(v))]},
    "rich_text": lambda v: {"rich_text": [_text_run(str(v))]},
    "number": lambda v: {"number": v},
    "checkbox": lambda v: {"checkbox": bool(v)},
    "url": lambda v: {"url": v},
    "email": lambda v: {"email": v},
    "phone_number": lambda v: {"phone_number": v},
    "select": lambda v: {"select": {"name": v} if v is not None else None},
    "status": lambda v: {"status": {"name": v} if v is not None else None},
    "multi_select": lambda v: {"multi_select": [{"name": n} for n in (v or [])]},
    "date": lambda v: {"date": _build_date_value(v) if v is not None else None},
}


def build_db_properties(values: dict[str, Any], schema: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Map a flat {column name: value} map to Notion's typed property JSON.

    Raises ValueError (-> invalid_argument, design AC5) naming the first
    field that is not a column in this database, or whose column type this
    v1 does not support writing.
    """
    built: dict[str, Any] = {}
    for name, value in values.items():
        column = schema.get(name)
        if column is None:
            raise ValueError(f"'{name}' is not a property of this database.")
        kind = column.get("type")
        builder = _PROPERTY_BUILDERS.get(kind)
        if builder is None:
            raise ValueError(f"'{name}' is a '{kind}' property; v1 cannot write that type.")
        built[name] = {"type": kind, **builder(value)}
    return built


def build_block(simplified: dict[str, Any]) -> dict[str, Any]:
    """One child block for notion_append_block_children.

    Raises ValueError (-> invalid_argument, design AC10) naming the first
    unsupported block type.
    """
    kind = simplified.get("type")
    if kind not in WRITABLE_BLOCK_TYPES:
        raise ValueError(f"'{kind}' is not a supported block type in v1 ({', '.join(WRITABLE_BLOCK_TYPES)}).")
    text = str(simplified.get("text", ""))
    body: dict[str, Any] = {"rich_text": [_text_run(text)]}
    if kind == "to_do":
        body["checked"] = bool(simplified.get("checked", False))
    return {"object": "block", "type": kind, kind: body}
