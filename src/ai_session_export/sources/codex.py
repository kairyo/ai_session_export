from __future__ import annotations

import json
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple, TextIO

try:
    if sys.version_info >= (3, 14):
        from compression import zstd
    else:
        from backports import zstd
except ImportError:
    zstd = None

from ..markdown import render_markdown
from ..models import MessageTurn, SessionRecord
from ..utils import parse_iso_timestamp, resolve_output_path, should_skip_session, to_local_date, unique_output_path


DEFAULT_CODEX_SESSION_DIRS = (
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
)
DEFAULT_CODEX_SESSION_INDEX = Path.home() / ".codex" / "session_index.jsonl"
SESSION_ID_PATTERN = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(?:\.zst)?$")
ROLLOUT_DATE_PATTERN = re.compile(r"^rollout-(\d{4}-\d{2}-\d{2})T")
ZSTD_INSTALL_HINT = (
    "Codex .jsonl.zst input requires compression.zstd (Python 3.14+) "
    "or the 'codex-zstd' extra on Python 3.11–3.13"
)


class ParsedCodexSession(NamedTuple):
    record: SessionRecord
    latest_timestamp_ms: int


class _MissingZstdDecoder(ImportError):
    pass


def _iter_session_files(session_dirs: tuple[Path, ...]) -> list[Path]:
    # Codex materializes a plain copy before resuming writes. Prefer that
    # representation if both are visible, including across active/archive roots.
    files: dict[str, tuple[bool, int, Path]] = {}
    for root in session_dirs:
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*.jsonl*"):
            logical_name = path.name.removesuffix(".zst")
            if not logical_name.endswith(".jsonl"):
                continue
            try:
                info = path.stat()
            except FileNotFoundError:
                continue  # A rollout can move while its directory is being scanned.
            if not stat.S_ISREG(info.st_mode):
                continue
            key = _session_id_from_path(path) or str(path.with_name(logical_name))
            candidate = (path.suffix == ".jsonl", info.st_mtime_ns, path)
            if key not in files or candidate > files[key]:
                files[key] = candidate
    return sorted(path for _, _, path in files.values())


@contextmanager
def _open_rollout(path: Path) -> Iterator[TextIO]:
    if path.suffix != ".zst":
        with path.open(encoding="utf-8") as stream:
            yield stream
        return
    if zstd is None:
        raise _MissingZstdDecoder(ZSTD_INSTALL_HINT)
    try:
        with zstd.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    except (zstd.ZstdError, EOFError) as exc:
        raise OSError("invalid or incomplete Zstandard rollout") from exc


def _item_content_text(item: dict) -> str:
    """Join the text parts of an item_completed message.

    Content entries are `{"type": "text"|"Text", "text": ...}`; the casing differs
    between user and agent items, so match on the payload key instead of the type.
    """
    parts = []
    for entry in item.get("content") or []:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _load_session_titles(index_file: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not index_file.is_file():
        return titles

    with index_file.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            session_id = str(entry.get("id") or "").strip()
            title = str(entry.get("thread_name") or "").strip()
            if session_id and title:
                titles[session_id] = title
    return titles


def _session_id_from_path(file_path: Path) -> str:
    match = SESSION_ID_PATTERN.search(file_path.name)
    return match.group(1) if match else ""


def _date_from_path(file_path: Path) -> date | None:
    match = ROLLOUT_DATE_PATTERN.match(file_path.name)
    return date.fromisoformat(match.group(1)) if match else None


def parse_codex_session_file(file_path: Path, titles: dict[str, str]) -> ParsedCodexSession | None:
    session_id = ""
    cwd = ""
    models: set[str] = set()
    messages: list[MessageTurn] = []
    current_model: str | None = None
    pending_user_turns: list[int] = []
    first_user_text = ""
    started_at: date | None = None
    latest_timestamp_ms = 0

    with _open_rollout(file_path) as handle:
        for raw_line in handle:
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                # Active Codex rollouts may have an incomplete final line.
                continue

            timestamp = parse_iso_timestamp(str(event.get("timestamp") or ""))
            event_ts_ms = None
            if timestamp is not None:
                event_ts_ms = int(timestamp.timestamp() * 1000)
                latest_timestamp_ms = max(latest_timestamp_ms, event_ts_ms)
                if started_at is None:
                    started_at = to_local_date(timestamp)

            event_type = event.get("type")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue

            if event_type == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                cwd = str(payload.get("cwd") or cwd)
                continue

            if event_type == "turn_context":
                model = str(payload.get("model") or "").strip()
                if model:
                    current_model = model
                    models.add(model)
                    for index in pending_user_turns:
                        messages[index] = messages[index]._replace(model=model)
                    pending_user_turns.clear()
                cwd = str(payload.get("cwd") or cwd)
                continue

            if event_type != "event_msg":
                continue

            payload_type = payload.get("type")
            role: str | None = None
            text = ""

            if payload_type == "user_message":
                role, text = "user", str(payload.get("message") or "").strip()
            elif payload_type == "agent_message":
                role, text = "assistant", str(payload.get("message") or "").strip()
            elif payload_type == "item_completed":
                # Current rollout format: turns arrive as completed items rather than
                # user_message/agent_message events. Anything other than the two
                # message kinds (Reasoning, command output, ...) stays private.
                item = payload.get("item")
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                if item_type == "UserMessage":
                    role = "user"
                elif item_type == "AgentMessage":
                    role = "assistant"
                else:
                    continue
                text = _item_content_text(item)

            if role is None:
                continue

            if role == "user":
                if not text:
                    continue
                if not first_user_text:
                    first_user_text = text
                messages.append(
                    MessageTurn(role="user", content=text, time_created=event_ts_ms, model=current_model)
                )
                pending_user_turns.append(len(messages) - 1)
            else:
                if text:
                    messages.append(
                        MessageTurn(role="assistant", content=text, time_created=event_ts_ms, model=current_model)
                    )
                pending_user_turns.clear()

    if not session_id or not first_user_text or not messages or started_at is None:
        return None

    title = titles.get(session_id) or first_user_text.splitlines()[0][:120]
    if should_skip_session(title):
        return None

    return ParsedCodexSession(
        record=SessionRecord(
            source="codex",
            session_id=session_id,
            title=title,
            date=started_at.isoformat(),
            messages=messages,
            project_directory=cwd,
            models_used=sorted(models),
        ),
        latest_timestamp_ms=latest_timestamp_ms,
    )


def export_codex(
    output_dir: Path,
    state: dict[str, Any],
    *,
    full: bool,
    dry_run: bool,
    since_date: date | None,
    session_dirs: tuple[Path, ...] = DEFAULT_CODEX_SESSION_DIRS,
    session_index: Path = DEFAULT_CODEX_SESSION_INDEX,
) -> dict[str, Any]:
    session_files = _iter_session_files(session_dirs)
    titles = _load_session_titles(session_index)
    if dry_run:
        session_state = state.get("codex", {}).get("sessions", {})
    else:
        session_state = state.setdefault("codex", {}).setdefault("sessions", {})

    exported = 0
    scanned = 0
    missing_decoder = 0
    warnings: list[dict[str, str]] = []
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for file_path in session_files:
        scanned += 1
        path_date = _date_from_path(file_path)
        if since_date and path_date and path_date < since_date:
            continue
        path_session_id = _session_id_from_path(file_path)
        path_state = session_state.get(path_session_id, {})
        path_output = str(path_state.get("output_file") or "")
        path_output_exists = bool(path_output) and (output_dir / path_output).is_file()
        try:
            info = file_path.stat()
        except OSError as exc:
            warnings.append({"path": file_path.name, "error": str(exc)})
            continue
        if (
            not full
            and path_session_id
            and int(path_state.get("source_mtime_ns", 0)) == info.st_mtime_ns
            and path_state.get("source_suffix", ".jsonl") == file_path.suffix
            and path_state.get("source_size", info.st_size) == info.st_size
            and path_output_exists
        ):
            continue

        try:
            parsed = parse_codex_session_file(file_path, titles)
        except _MissingZstdDecoder:
            missing_decoder += 1
            continue
        except (OSError, UnicodeError) as exc:
            warnings.append({"path": file_path.name, "error": str(exc)})
            continue
        if parsed is None:
            continue
        if since_date and date.fromisoformat(parsed.record.date) < since_date:
            continue

        previous = session_state.get(parsed.record.session_id, {})
        source_stamp = {
            "source_mtime_ns": info.st_mtime_ns,
            "source_size": info.st_size,
            "source_suffix": file_path.suffix,
        }
        previous_timestamp = int(previous.get("latest_timestamp", 0))
        previous_output = str(previous.get("output_file") or "")
        output_path = output_dir / Path(previous_output).name if previous_output else None
        output_exists = output_path is not None and output_path.is_file()
        if not full and parsed.latest_timestamp_ms <= previous_timestamp and output_exists:
            if not dry_run:
                previous.update(source_stamp)
                session_state[parsed.record.session_id] = previous
            continue

        if previous_output and not dry_run:
            output_path = resolve_output_path(
                output_dir, previous_output, parsed.record.date, parsed.record.title
            )
        elif output_path is None:
            output_path = unique_output_path(output_dir, parsed.record.date, parsed.record.title)
        if not dry_run:
            output_path.write_text(render_markdown(parsed.record), encoding="utf-8")
            session_state[parsed.record.session_id] = {
                "latest_timestamp": parsed.latest_timestamp_ms,
                "output_file": output_path.name,
                **source_stamp,
            }
        exported += 1

    result: dict[str, Any] = {"source": "codex", "scanned": scanned, "exported": exported}
    if warnings or missing_decoder:
        result["failed"] = len(warnings) + missing_decoder
        if missing_decoder:
            warnings.append({
                "path": "*.jsonl.zst",
                "error": f"{missing_decoder} compressed rollout(s): {ZSTD_INSTALL_HINT}",
            })
        result["warnings"] = warnings
    return result
