#!/usr/bin/env python3
"""
Todo Tool Module - Planning & Task Management

Provides an in-memory task list the agent uses to decompose complex tasks,
track progress, and maintain focus across long conversations. The state
lives on the AIAgent instance (one per session) and is re-injected into
the conversation after context compression events.

Design:
- Single `todo` tool: provide `todos` param to write, omit to read
- Every call returns the full current list
- No system prompt mutation, no tool response modification
- Behavioral guidance lives entirely in the tool schema description
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


logger = logging.getLogger(__name__)


# Valid status values for todo items
VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

# Bounds on persisted todo state. The todo list is a planning aid the model
# re-reads after every context-compression event (see format_for_injection),
# so unbounded item content or count defeats the compression it rides through.
# These caps keep a single oversized item (whether authored by the model or
# replayed from caller-supplied history on the API server) from inflating the
# re-injection block. Generous relative to real plans — a todo item is a short
# task description, and active lists are a handful of items, not hundreds.
MAX_TODO_CONTENT_CHARS = 4000
MAX_TODO_ITEMS = 256
# Upper bound on a single todo tool-result payload accepted during history
# hydration. The gateway/API server replays caller-supplied conversation
# history to rebuild the store, so an oversized forged result is dropped
# before it is parsed and re-injected (see AIAgent._hydrate_todo_store).
MAX_TODO_RESULT_CHARS = 512_000
_TRUNCATION_MARKER = "… [truncated]"
# Persisted as ordinary message content. ContextCompressor uses this stable
# header to distinguish the synthetic post-compaction row from a real user.
TODO_INJECTION_HEADER = (
    "[Your active task list was preserved across context compression]"
)

# --- P3a: per-session disk persistence -------------------------------------
# One JSON file per session: <HERMES_HOME>/state/todos/<session_id>.json,
# written atomically (tmp + fsync + rename) via utils.atomic_json_write.
# A failed write never breaks the tool -- the in-memory list stays the
# source of truth for the current turn.
TODO_STATE_SUBDIR = ("state", "todos")          # relative to get_hermes_home()
TODO_STATE_VERSION = 1
# Sanity cap for a state file: legit content is bounded by MAX_TODO_ITEMS *
# MAX_TODO_CONTENT_CHARS (~1 MB worst case), so anything far beyond that is
# forged/corrupt and rejected before parsing.
_TODO_MAX_STATE_FILE_BYTES = 2 * 1024 * 1024
# session_id -> filename whitelist. Blocks path traversal ("../", absolute
# paths, backslashes, null bytes) for ids supplied by external callers
# (spec-falhas F7). A trailing ".json" is always appended, so even "." or
# ".." would resolve to a plain filename inside the todos dir.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _safe_state_path(
    session_id: Optional[str], base_dir: Optional[Path]
) -> Optional[Path]:
    """Resolve the state file for *session_id*, or None when persistence is off.

    Persistence is disabled when session_id is empty/None or fails the
    filename whitelist. ``base_dir`` defaults to ``get_hermes_home() / state /
    todos`` -- resolved at call time, never cached at import time (tests
    isolate HERMES_HOME per test; see tests/conftest.py).
    """
    if not session_id:
        return None
    if not _SESSION_ID_RE.fullmatch(session_id):
        logger.warning(
            "TodoStore: session_id %r rejected for persistence (invalid characters)",
            session_id,
        )
        return None
    if base_dir is None:
        from hermes_constants import get_hermes_home

        base_dir = get_hermes_home().joinpath(*TODO_STATE_SUBDIR)
    return base_dir / f"{session_id}.json"


class TodoStore:
    """
    In-memory todo list. One instance per AIAgent (one per session).

    Items are ordered -- list position is priority. Each item has:
      - id: unique string identifier (agent-chosen)
      - content: task description
      - status: pending | in_progress | completed | cancelled
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self._items: List[Dict[str, str]] = []
        self._session_id = session_id
        self._base_dir = Path(base_dir) if base_dir is not None else None
        # P3a: hydrate from disk when a session id is set (spec 3.2). Any
        # failure is logged inside load(); the store simply stays empty.
        if self._state_path() is not None:
            self.load()

    def write(self, todos: List[Dict[str, Any]], merge: bool = False) -> List[Dict[str, str]]:
        """
        Write todos. Returns the full current list after writing.

        Args:
            todos: list of {id, content, status} dicts
            merge: if False, replace the entire list. If True, update
                   existing items by id and append new ones.
        """
        if not merge:
            # Replace mode: new list entirely
            self._items = [self._validate(t) for t in self._dedupe_by_id(todos)]
        else:
            # Merge mode: update existing items by id, append new ones
            existing = {item["id"]: item for item in self._items}
            for t in self._dedupe_by_id(todos):
                item_id = str(t.get("id", "")).strip()
                if not item_id:
                    continue  # Can't merge without an id

                if item_id in existing:
                    # Update only the fields the LLM actually provided
                    if "content" in t and t["content"]:
                        existing[item_id]["content"] = self._cap_content(str(t["content"]).strip())
                    if "status" in t and t["status"]:
                        status = str(t["status"]).strip().lower()
                        if status in VALID_STATUSES:
                            existing[item_id]["status"] = status
                else:
                    # New item -- validate fully and append to end
                    validated = self._validate(t)
                    existing[validated["id"]] = validated
                    self._items.append(validated)
            # Rebuild _items preserving order for existing items
            seen = set()
            rebuilt = []
            for item in self._items:
                current = existing.get(item["id"], item)
                if current["id"] not in seen:
                    rebuilt.append(current)
                    seen.add(current["id"])
            self._items = rebuilt
        # Bound total item count so a replayed/oversized list can't grow the
        # re-injection block without limit. Keep the highest-priority head
        # (list order is priority).
        if len(self._items) > MAX_TODO_ITEMS:
            self._items = self._items[:MAX_TODO_ITEMS]
        # P3a: persist every successful mutation (replace and merge modes).
        # Failures are logged inside save() and never break the tool.
        self.save()
        return self.read()

    def read(self) -> List[Dict[str, str]]:
        """Return a copy of the current list."""
        return [item.copy() for item in self._items]

    def has_items(self) -> bool:
        """Check if there are any items in the list."""
        return bool(self._items)

    def format_for_injection(self) -> Optional[str]:
        """
        Render the todo list for post-compression injection.

        Returns a human-readable string to append to the compressed
        message history, or None if the list is empty.
        """
        if not self._items:
            return None

        # Status markers for compact display
        markers = {
            "completed": "[x]",
            "in_progress": "[>]",
            "pending": "[ ]",
            "cancelled": "[~]",
        }

        # Only inject pending/in_progress items — completed/cancelled ones
        # cause the model to re-do finished work after compression.
        active_items = [
            item for item in self._items
            if item["status"] in {"pending", "in_progress"}
        ]
        if not active_items:
            return None

        lines = [TODO_INJECTION_HEADER]
        for item in active_items:
            marker = markers.get(item["status"], "[?]")
            lines.append(f"- {marker} {item['id']}. {item['content']} ({item['status']})")

        return "\n".join(lines)

    @staticmethod
    def _cap_content(content: str) -> str:
        """Truncate oversized todo content to MAX_TODO_CONTENT_CHARS.

        A single huge item would otherwise inflate the post-compression
        re-injection block (format_for_injection) without bound. Keep the
        head — the actionable part of a task description — plus a marker.
        """
        if len(content) > MAX_TODO_CONTENT_CHARS:
            keep = MAX_TODO_CONTENT_CHARS - len(_TRUNCATION_MARKER)
            return content[:keep] + _TRUNCATION_MARKER
        return content

    @staticmethod
    def _validate(item: Dict[str, Any]) -> Dict[str, str]:
        """
        Validate and normalize a todo item.

        Ensures required fields exist and status is valid.
        Returns a clean dict with only {id, content, status}.
        """
        if not isinstance(item, dict):
            return {"id": "?", "content": "(invalid item)", "status": "pending"}

        item_id = str(item.get("id", "")).strip()
        if not item_id:
            item_id = "?"

        content = str(item.get("content", "")).strip()
        if not content:
            content = "(no description)"
        else:
            content = TodoStore._cap_content(content)

        status = str(item.get("status", "pending")).strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"

        return {"id": item_id, "content": content, "status": status}

    @staticmethod
    def _dedupe_by_id(todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Collapse duplicate ids, keeping the last occurrence in its position."""
        last_index: Dict[str, int] = {}
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                # Non-dict items get a synthetic key so _validate can handle them
                last_index[f"__invalid_{i}"] = i
                continue
            item_id = str(item.get("id", "")).strip() or "?"
            last_index[item_id] = i
        return [todos[i] for i in sorted(last_index.values())]

    def _state_path(self) -> Optional[Path]:
        """State file path for this instance, or None when persistence is off."""
        return _safe_state_path(self._session_id, self._base_dir)

    def save(self) -> bool:
        """Persist a full snapshot of _items to <base>/state/todos/<session_id>.json.

        Atomic write via utils.atomic_json_write (tmp + fsync + rename).
        Returns True on success; False on any failure (logged, never raised).
        No-op (returns True) when persistence is disabled (no valid session_id).
        """
        path = self._state_path()
        if path is None:
            return True
        try:
            from utils import atomic_json_write

            atomic_json_write(
                path,
                {
                    "version": TODO_STATE_VERSION,
                    "session_id": self._session_id,
                    "updated_at": datetime.now().isoformat(),
                    "items": self._items,
                },
                indent=2,
            )
            return True
        except Exception as exc:
            logger.warning(
                "TodoStore.save failed for session=%s: %s",
                self._session_id or "none",
                exc,
            )
            return False

    def load(self) -> bool:
        """Hydrate _items from the session's state file, if present.

        - Missing file -> True (silent no-op).
        - Invalid JSON / wrong schema / OSError -> warning + empty store + False.
        - Valid content: every item passes through _validate/_cap_content and
          the list is truncated to MAX_TODO_ITEMS (same validation path as
          write(), so a forged/tampered file is sanitized identically).
        Returns True when hydrated or when there was nothing to load.
        """
        path = self._state_path()
        if path is None:
            return True  # persistence disabled for this instance
        try:
            if not path.exists():
                return True
            if path.stat().st_size > _TODO_MAX_STATE_FILE_BYTES:
                logger.warning(
                    "TodoStore.load: state file too large for session=%s (%d bytes), ignoring",
                    self._session_id or "none",
                    path.stat().st_size,
                )
                return False
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "TodoStore.load: cannot read state file %s: %s", path, exc
            )
            # Preserve the corrupt file for debugging without breaking anything.
            try:
                backup = path.with_name(
                    f"{path.name}.corrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                path.rename(backup)
            except Exception:
                pass
            self._items = []
            return False

        try:
            if not isinstance(raw, dict) or raw.get("version") != TODO_STATE_VERSION:
                logger.warning(
                    "TodoStore.load: unsupported schema for session=%s (version=%r), ignoring",
                    self._session_id or "none",
                    raw.get("version") if isinstance(raw, dict) else None,
                )
                self._items = []
                return False
            items = raw.get("items")
            if not isinstance(items, list):
                logger.warning(
                    "TodoStore.load: 'items' missing or not a list for session=%s, ignoring",
                    self._session_id or "none",
                )
                self._items = []
                return False
            # Same validation path as write(): _validate (+ _cap_content) +
            # _dedupe_by_id + MAX_TODO_ITEMS truncation.
            self._items = [self._validate(t) for t in self._dedupe_by_id(items)]
            if len(self._items) > MAX_TODO_ITEMS:
                self._items = self._items[:MAX_TODO_ITEMS]
            return True
        except Exception as exc:
            logger.warning(
                "TodoStore.load: validation failed for session=%s: %s",
                self._session_id or "none",
                exc,
            )
            self._items = []
            return False

    @staticmethod
    def purge(session_id: str, base_dir: Optional[Path] = None) -> bool:
        """Delete the session's state file (best-effort, never raises).

        No-op when the session id is invalid or no file exists.
        Returns True only if a file was actually removed.
        """
        path = _safe_state_path(session_id, base_dir)
        if path is None:
            return False
        try:
            if path.exists():
                path.unlink()
                return True
        except OSError as exc:
            logger.warning(
                "TodoStore.purge failed for session=%s: %s", session_id, exc
            )
        return False


def todo_tool(
    todos: Optional[List[Dict[str, Any]]] = None,
    merge: bool = False,
    store: Optional[TodoStore] = None,
) -> str:
    """
    Single entry point for the todo tool. Reads or writes depending on params.

    Args:
        todos: if provided, write these items. If None, read current list.
        merge: if True, update by id. If False (default), replace entire list.
        store: the TodoStore instance from the AIAgent.

    Returns:
        JSON string with the full current list and summary metadata.
    """
    if store is None:
        return tool_error("TodoStore not initialized")

    if todos is not None:
        # Guard: LLM sometimes sends todos as a JSON string instead of a list
        if isinstance(todos, str):
            try:
                todos = json.loads(todos)
            except (json.JSONDecodeError, TypeError):
                return tool_error("todos must be a list of objects, got unparseable string")
        if not isinstance(todos, list):
            return tool_error(
                f"todos must be a list, got {type(todos).__name__}"
            )
        items = store.write(todos, merge)
    else:
        items = store.read()

    # Build summary counts
    pending = sum(1 for i in items if i["status"] == "pending")
    in_progress = sum(1 for i in items if i["status"] == "in_progress")
    completed = sum(1 for i in items if i["status"] == "completed")
    cancelled = sum(1 for i in items if i["status"] == "cancelled")

    return json.dumps({
        "todos": items,
        "summary": {
            "total": len(items),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
            "cancelled": cancelled,
        },
    }, ensure_ascii=False)


def check_todo_requirements() -> bool:
    """Todo tool has no external requirements -- always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schema
# =============================================================================
# Behavioral guidance is baked into the description so it's part of the
# static tool schema (cached, never changes mid-conversation).

TODO_SCHEMA = {
    "name": "todo",
    "description": (
        "Manage your task list for the current session. Use for complex tasks "
        "with 3+ steps or when the user provides multiple tasks. "
        "Call with no parameters to read the current list.\n\n"
        "Writing:\n"
        "- Provide 'todos' array to create/update items\n"
        "- merge=false (default): replace the entire list with a fresh plan\n"
        "- merge=true: update existing items by id, add any new ones\n\n"
        "Each item: {id: string, content: string, "
        "status: pending|in_progress|completed|cancelled}\n"
        "List order is priority. Only ONE item in_progress at a time.\n"
        "Mark items completed immediately when done. If something fails, "
        "cancel it and add a revised item.\n\n"
        "Always returns the full current list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "Task items to write. Omit to read current list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique item identifier"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "cancelled"],
                            "description": "Current status"
                        }
                    },
                    "required": ["id", "content", "status"]
                }
            },
            "merge": {
                "type": "boolean",
                "description": (
                    "true: update existing items by id, add new ones. "
                    "false (default): replace the entire list."
                ),
                "default": False
            }
        },
        "required": []
    }
}


# --- Registry ---
from tools.registry import registry, tool_error

registry.register(
    name="todo",
    toolset="todo",
    schema=TODO_SCHEMA,
    handler=lambda args, **kw: todo_tool(
        todos=args.get("todos"), merge=args.get("merge", False), store=kw.get("store")),
    check_fn=check_todo_requirements,
    emoji="📋",
)
