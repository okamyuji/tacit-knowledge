"""Claude Code と Codex の履歴から、人が対話で書いた発話だけを取り出す。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 人ではなくツールが user 発話として書き込む文の接頭辞
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

# Codex の session ID は UUIDv7 で、先頭8文字は約65秒ごとにしか変わらない
SESSION_PREFIX_LEN = 12

EXCLUDED = "excluded"
NON_INTERACTIVE = "non-interactive"
BROKEN = "broken"


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
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def is_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _read_jsonl(path: Path) -> Iterator[Optional[dict]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
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
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _claude_outcome(rec: dict) -> Union[Utterance, str, None]:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    if str(rec.get("entrypoint", "")).startswith("sdk"):
        return NON_INTERACTIVE
    text = _claude_text((rec.get("message") or {}).get("content"))
    return _utterance("claude", str(rec.get("sessionId", "")), rec.get("timestamp"), text, CLAUDE_INJECTED)


def _load_claude_file(path: Path, project: str, result: LoadResult) -> None:
    found: List[Utterance] = []
    pending = LoadResult()
    cwd_matches: Optional[bool] = None
    interactive = True
    for rec in _read_jsonl(path):
        if rec is None:
            result.skipped += 1
            continue
        if cwd_matches is None and rec.get("cwd"):
            cwd_matches = is_under(_real(str(rec["cwd"])), project)
        outcome = _claude_outcome(rec)
        if outcome == NON_INTERACTIVE:
            interactive = False
        _tally(pending, outcome, found)
    if cwd_matches is False:
        return
    if not interactive:
        result.excluded_sessions += 1
        return
    result.excluded += pending.excluded
    result.skipped += pending.skipped
    result.utterances.extend(found)


def load_claude(project: str, home: Path, result: LoadResult) -> None:
    real = _real(project)
    dirs = sorted({encode_project(os.path.abspath(os.path.expanduser(project))), encode_project(real)})
    for name in dirs:
        directory = home / "projects" / name
        result.searched.append(str(directory))
        for path in sorted(directory.glob("*.jsonl")):
            _load_claude_file(path, real, result)


def _codex_texts(rec: dict) -> List[str]:
    payload = rec.get("payload") or {}
    if rec.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
        return []
    return [c.get("text", "") for c in payload.get("content") or [] if isinstance(c, dict) and c.get("type") == "input_text"]


def _codex_session(meta: Optional[dict], project: str) -> tuple:
    """(状態, session ID) を返す。状態は None（読む）、BROKEN、NON_INTERACTIVE、"other"（別プロジェクト）。"""
    if not meta or meta.get("type") != "session_meta":
        return BROKEN, ""
    payload = meta.get("payload") or {}
    cwd = payload.get("cwd")
    if not cwd:
        return BROKEN, ""
    if not is_under(_real(str(cwd)), project):
        return "other", ""
    source = payload.get("source")
    if not isinstance(source, str) or source == "exec":
        return NON_INTERACTIVE, ""
    return None, str(payload.get("id", ""))


def _load_codex_file(path: Path, project: str, result: LoadResult) -> None:
    records = _read_jsonl(path)
    status, session = _codex_session(next(records, None), project)
    if status == BROKEN:
        result.skipped += 1
    elif status == NON_INTERACTIVE:
        result.excluded_sessions += 1
    if status is not None:
        return
    for rec in records:
        if rec is None:
            result.skipped += 1
            continue
        for text in _codex_texts(rec):
            _tally(result, _utterance("codex", session, rec.get("timestamp"), text, CODEX_INJECTED), result.utterances)


def load_codex(project: str, home: Path, result: LoadResult) -> None:
    root = home / "sessions"
    result.searched.append(str(root))
    real = _real(project)
    for path in sorted(root.glob("**/*.jsonl")):
        _load_codex_file(path, real, result)


def _dedup(utterances: Sequence[Utterance]) -> List[Utterance]:
    seen = set()
    out = []
    for u in utterances:
        key = (u.session, u.ts.replace(second=0, microsecond=0), u.text)
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def load(project: str, sources: Sequence[str] = ("claude", "codex"),
         claude_home: Optional[Path] = None, codex_home: Optional[Path] = None) -> LoadResult:
    result = LoadResult()
    if "claude" in sources:
        load_claude(project, claude_home or default_claude_home(), result)
    if "codex" in sources:
        load_codex(project, codex_home or default_codex_home(), result)
    result.utterances = _dedup(sorted(result.utterances, key=lambda u: (u.ts, u.tool, u.session)))
    return result
