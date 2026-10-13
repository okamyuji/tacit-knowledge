"""Write synthetic history into temporary directories. No real history is used."""
import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "tacit-knowledge" / "scripts"
sys.path.insert(0, str(SCRIPTS))

CLAUDE_SESSION = "45c391e1-2c4d-4e5f-8a9b-0c1d2e3f4a5b"
CODEX_SESSION = "01a12ca3-2e31-7e63-a2b9-eca2cf61ba57"
TS = "2026-09-24T01:05:30.000Z"


def _encode(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _write_lines(path, records, raw_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for line in raw_lines:
            f.write(line + "\n")


def claude_user(text, ts=TS, cwd="/proj", session=CLAUDE_SESSION, **extra):
    rec = {
        "type": "user",
        "timestamp": ts,
        "sessionId": session,
        "cwd": cwd,
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }
    rec.update(extra)
    return rec


def claude_assistant(text, ts=TS, cwd="/proj", session=CLAUDE_SESSION):
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session,
        "cwd": cwd,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def write_claude(home, project, records, session=CLAUDE_SESSION, raw_lines=()):
    path = Path(home) / "projects" / _encode(project) / f"{session}.jsonl"
    _write_lines(path, records, raw_lines)
    return path


def codex_meta(cwd="/proj", session=CODEX_SESSION, source="cli", ts=TS):
    return {
        "timestamp": ts,
        "type": "session_meta",
        "payload": {"id": session, "cwd": cwd, "source": source, "originator": "codex-tui"},
    }


def codex_user(text, ts=TS):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def codex_assistant(text, ts=TS):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    }


def write_codex(home, records, session=CODEX_SESSION, day="2026/09/24", raw_lines=()):
    path = Path(home) / "sessions" / day / f"rollout-2026-09-24T01-05-30-{session}.jsonl"
    _write_lines(path, records, raw_lines)
    return path
