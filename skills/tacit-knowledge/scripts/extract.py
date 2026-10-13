#!/usr/bin/env python3
"""Write the human utterances of a project's Claude Code / Codex history into chunked text files.

usage: extract.py --out DIR [--project PATH] [--source claude,codex] [--tz ZONE] [--chunk-chars N]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import tzinfo
from pathlib import Path
from typing import List, Optional, Sequence

from history import SOURCES, Utterance, format_ts, load_or_report, parse_sources, resolve_tz, session_prefix

DEFAULT_CHUNK_CHARS = 20000


def render(u: Utterance, tz: Optional[tzinfo]) -> str:
    return f"=== {format_ts(u.ts, tz)} {u.tool} {session_prefix(u.session)} ({len(u.text)})\n{u.text}\n"


def chunk(blocks: Sequence[str], limit: int) -> List[List[str]]:
    """Split only between blocks: a quote must stay intact to be verifiable."""
    chunks: List[List[str]] = []
    for block in blocks:
        if chunks and sum(map(len, chunks[-1])) + len(block) <= limit:
            chunks[-1].append(block)
        else:
            chunks.append([block])
    return chunks


def _has_git(directory: Path) -> bool:
    # Compare names instead of calling exists(): ".GIT" exists too on case-insensitive file systems
    try:
        return ".git" in os.listdir(directory)
    except OSError:
        return False


def in_git_worktree(path: Path) -> bool:
    return any(_has_git(p) for p in [path, *path.parents])


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract human utterances from agent history.")
    parser.add_argument("--out", required=True, type=Path, help="output directory (do not commit it)")
    parser.add_argument("--project", default=os.getcwd(), help="project directory (default: current directory)")
    parser.add_argument("--source", default=",".join(SOURCES), help="comma-separated: claude,codex")
    parser.add_argument("--include-non-interactive", action="store_true",
                        help="also read claude -p, codex exec, and subagent sessions (usually written by another agent)")
    parser.add_argument("--tz", help="IANA time zone such as Asia/Tokyo (default: local)")
    parser.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS, help="max characters per file")
    args = parser.parse_args(argv)
    if args.chunk_chars <= 0:
        parser.error("--chunk-chars must be positive")
    try:
        args.sources = parse_sources(args.source)
        args.zone = resolve_tz(args.tz)
    except ValueError as e:
        parser.error(str(e))
    return args


def _write_chunks(out: Path, chunks: List[List[str]]) -> None:
    # An existing directory belongs to the user, so leave its mode alone; the files are always 0600.
    if not out.is_dir():
        out.mkdir(parents=True)
        out.chmod(0o700)
    for stale in out.glob("prompts-[0-9][0-9][0-9].txt"):
        stale.unlink()
    for i, blocks in enumerate(chunks, 1):
        fd = os.open(out / f"prompts-{i:03d}.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write("".join(blocks).encode())


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    result = load_or_report(args.project, args.sources, args.include_non_interactive)
    if result is None:
        return 2
    if in_git_worktree(args.out.resolve()):
        print(f"warning: {args.out} is inside a git worktree; do not commit these files", file=sys.stderr)
    utts = result.utterances
    chunks = chunk([render(u, args.zone) for u in utts], args.chunk_chars)
    try:
        _write_chunks(args.out, chunks)
    except OSError as e:
        print(f"cannot write to {args.out}: {e}", file=sys.stderr)
        return 2
    by_tool = ", ".join(f"{t} {sum(1 for u in utts if u.tool == t)}" for t in args.sources)
    print(f"utterances: {len(utts)} ({by_tool})")
    print(f"period: {format_ts(utts[0].ts, args.zone)} .. {format_ts(utts[-1].ts, args.zone)}")
    print(f"excluded: {result.excluded} injected texts, {result.excluded_sessions} non-interactive sessions, "
          f"{result.skipped} unreadable records")
    print(f"files: {len(chunks)} in {args.out}")
    print("note: Claude Code deletes transcripts older than cleanupPeriodDays (default 30 days).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
