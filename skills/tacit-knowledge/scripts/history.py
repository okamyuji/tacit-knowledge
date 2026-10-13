"""Read Claude Code and Codex history and keep only what a person typed in an interactive session."""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Prefixes of text that the tools, not the person, write into the user turn
CLAUDE_INJECTED = (
    "<command-",
    "<local-command",
    "<task-notification",
    "<system-reminder",
    "<bash-",
    "<user-prompt-submit-hook",
    "Caveat:",
    "[Request interrupted",
    "This session is being continued",
)
CODEX_INJECTED = ("<environment_context>", "# AGENTS.md instructions", "<recommended_plugins>")

# Codex session IDs are UUIDv7, whose first 8 characters change only about every 65 seconds
SESSION_PREFIX_LEN = 12

SOURCES = ("claude", "codex")

EXCLUDED = "excluded"
NON_INTERACTIVE = "non-interactive"
BROKEN = "broken"
OTHER_PROJECT = "other-project"


@dataclass(frozen=True)
class Utterance:
    ts: datetime
    tool: str
    session: str
    text: str


@dataclass
class LoadResult:
    utterances: List[Utterance] = field(default_factory=list)
    excluded: int = 0
    excluded_sessions: int = 0
    skipped: int = 0
    searched: List[str] = field(default_factory=list)


def default_claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def parse_sources(value: str) -> tuple:
    sources = tuple(s.strip() for s in value.split(",") if s.strip())
    if not sources or any(s not in SOURCES for s in sources):
        raise ValueError(f"source must be a subset of {','.join(SOURCES)}")
    return sources


def encode_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def session_prefix(session: str) -> str:
    return session[:SESSION_PREFIX_LEN]


def resolve_tz(name: Optional[str]) -> Optional[tzinfo]:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown time zone: {name}") from e


def format_ts(ts: datetime, tz: Optional[tzinfo]) -> str:
    local = ts.astimezone(tz)
    offset = local.strftime("%z")
    return f"{local:%Y-%m-%d %H:%M}{offset[:3]}:{offset[3:]}"


def parse_ts(value: str) -> datetime:
    # Python 3.9 and 3.10 cannot parse a trailing "Z"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def minute_key(ts: datetime) -> int:
    """Whole minutes since the epoch, so the same instant matches whatever offset it was written in."""
    return int(ts.timestamp()) // 60


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def is_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _read_jsonl(path: Path) -> Iterator[Optional[dict]]:
    """Yield each record, or None for a line that is not a JSON object in UTF-8."""
    with open(path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                yield None
                continue
            yield rec if isinstance(rec, dict) else None


def _utterance(tool: str, session: str, ts: object, text: str, injected: Sequence[str]) -> Union[Utterance, str, None]:
    if not text.strip():
        return None
    if text.lstrip().startswith(injected):
        return EXCLUDED
    try:
        return Utterance(parse_ts(str(ts)), tool, session, text)
    except ValueError:
        return BROKEN


def _tally(result: LoadResult, outcome: Union[Utterance, str, None], into: List[Utterance]) -> None:
    if isinstance(outcome, Utterance):
        into.append(outcome)
    elif outcome == EXCLUDED:
        result.excluded += 1
    elif outcome == BROKEN:
        result.skipped += 1


def _claude_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c["text"] for c in content
                         if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
    return ""


def _claude_outcome(rec: dict, session: str) -> Union[Utterance, str, None]:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    text = _claude_text((rec.get("message") or {}).get("content"))
    return _utterance("claude", session, rec.get("timestamp"), text, CLAUDE_INJECTED)


def _load_claude_file(path: Path, project: str, result: LoadResult, include_non_interactive: bool) -> None:
    found: List[Utterance] = []
    pending = LoadResult()
    cwd_matches: Optional[bool] = None
    entrypoints = set()
    for rec in _read_jsonl(path):
        if rec is None:
            result.skipped += 1
            continue
        if cwd_matches is None and rec.get("cwd"):
            cwd_matches = is_under(_real(str(rec["cwd"])), project)
        entrypoints.add(str(rec.get("entrypoint")))
        # Claude Code names each transcript after its session ID
        _tally(pending, _claude_outcome(rec, path.stem), found)
    if cwd_matches is False:
        return
    if any(e.startswith("sdk") for e in entrypoints) and not include_non_interactive:
        result.excluded_sessions += 1
        return
    result.excluded += pending.excluded
    result.skipped += pending.skipped
    result.utterances.extend(found)


def load_claude(project: str, home: Path, result: LoadResult, include_non_interactive: bool) -> None:
    real = _real(project)
    dirs = sorted({encode_project(os.path.abspath(os.path.expanduser(project))), encode_project(real)})
    for name in dirs:
        directory = home / "projects" / name
        result.searched.append(str(directory))
        for path in sorted(directory.glob("*.jsonl")):
            _load_claude_file(path, real, result, include_non_interactive)


def _codex_texts(rec: dict) -> List[str]:
    payload = rec.get("payload") or {}
    if rec.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return []
    return [c["text"] for c in payload.get("content") or []
            if isinstance(c, dict) and c.get("type") == "input_text" and isinstance(c.get("text"), str)]


def _codex_status(meta: Optional[dict], project: str) -> Optional[str]:
    """Return None when the file should be read, otherwise why it is not."""
    if not meta or meta.get("type") != "session_meta":
        return BROKEN
    payload = meta.get("payload") or {}
    if not payload.get("cwd") or not payload.get("id"):
        return BROKEN
    if not is_under(_real(str(payload["cwd"])), project):
        return OTHER_PROJECT
    source = payload.get("source")
    if not isinstance(source, str) or source == "exec":
        return NON_INTERACTIVE
    return None


def _load_codex_file(path: Path, project: str, result: LoadResult, include_non_interactive: bool) -> None:
    records = _read_jsonl(path)
    meta = next(records, None)
    status = _codex_status(meta, project)
    if status == NON_INTERACTIVE and include_non_interactive:
        status = None
    if status == BROKEN:
        result.skipped += 1
    elif status == NON_INTERACTIVE:
        result.excluded_sessions += 1
    if status is not None:
        return
    session = str(meta["payload"]["id"])
    for rec in records:
        if rec is None:
            result.skipped += 1
            continue
        for text in _codex_texts(rec):
            _tally(result, _utterance("codex", session, rec.get("timestamp"), text, CODEX_INJECTED), result.utterances)


def load_codex(project: str, home: Path, result: LoadResult, include_non_interactive: bool) -> None:
    root = home / "sessions"
    result.searched.append(str(root))
    real = _real(project)
    for path in sorted(root.glob("**/*.jsonl")):
        _load_codex_file(path, real, result, include_non_interactive)


def _dedup(utterances: Sequence[Utterance]) -> List[Utterance]:
    seen = set()
    out = []
    for u in utterances:
        key = (u.session, minute_key(u.ts), u.text)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def load(project: str, sources: Sequence[str] = SOURCES, claude_home: Optional[Path] = None,
         codex_home: Optional[Path] = None, include_non_interactive: bool = False) -> LoadResult:
    result = LoadResult()
    if "claude" in sources:
        load_claude(project, claude_home or default_claude_home(), result, include_non_interactive)
    if "codex" in sources:
        load_codex(project, codex_home or default_codex_home(), result, include_non_interactive)
    result.utterances = _dedup(sorted(result.utterances, key=lambda u: (u.ts, u.tool, u.session)))
    return result


def load_or_report(project: str, sources: Sequence[str], include_non_interactive: bool) -> Optional[LoadResult]:
    """Load history, or print where it looked and return None when nothing was found."""
    result = load(project, sources, include_non_interactive=include_non_interactive)
    if result.utterances:
        return result
    print("no utterances found. searched:", file=sys.stderr)
    for path in result.searched:
        print(f"  {path}", file=sys.stderr)
    return None
