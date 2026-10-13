#!/usr/bin/env python3
"""Write the human utterances of a project's Claude Code / Codex history into chunked text files.

usage: extract.py --out DIR [--project PATH] [--source claude,codex] [--tz ZONE] [--chunk-chars N]
                  [--include-non-interactive]
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from datetime import tzinfo
from pathlib import Path
from typing import List, Optional, Sequence

from history import SOURCES, Utterance, dedup, format_ts, load_or_report, parse_sources, resolve_tz, session_prefix

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
    parser.add_argument("--out", required=True, type=lambda s: Path(os.path.abspath(os.path.expanduser(s))), help="output directory (do not commit it)")
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


MAX_SYMLINKS = 40
DOCUMENT = "tacit-knowledge.md"
UNSAFE_CHARS = re.compile(r"['\x00-\x1f\x7f]")


def _owned_by_other(st: os.stat_result) -> bool:
    # Windows has no uid; there only the mode checks run
    return hasattr(os, "getuid") and st.st_uid not in (0, os.getuid())


def _check_entry(entry: Path, st: os.stat_result) -> None:
    if _owned_by_other(st):
        raise OSError(f"{entry} is owned by another user")
    if stat.S_ISLNK(st.st_mode):
        return
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{entry} is not a directory")
    if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
        raise OSError(f"{entry} is writable by other users")


def walk(path: Path) -> Path:
    """Resolve an absolute path one entry at a time, refusing anything another user could swap.

    Every entry must belong to the user or root, and every directory must be writable only by them unless
    it is sticky, where others cannot rename or remove our entries. What passes cannot change under us.
    Returns the real path; missing trailing parts are appended unchecked.
    """
    current, parts, links = Path(path.anchor), list(path.parts[1:]), 0
    while parts:
        name = parts.pop(0)
        if name == "..":
            current = current.parent
            continue
        entry = current / name
        try:
            st = entry.lstat()
        except FileNotFoundError:
            return entry.joinpath(*parts)
        _check_entry(entry, st)
        if stat.S_ISLNK(st.st_mode):
            links += 1
            if links > MAX_SYMLINKS:
                raise OSError(f"too many symlinks in {path}")
            # join() keeps an absolute target as is and puts a relative one under the link's directory
            target = Path(os.path.join(current, os.readlink(entry)))
            current, parts = Path(target.anchor), list(target.parts[1:]) + parts
            continue
        current = entry
    return current


def check_name(path: Path) -> None:
    # The agent pastes this path into shell commands in single quotes
    if UNSAFE_CHARS.search(str(path)):
        raise OSError(f"{path!r} contains a quote or a control character")


def _prepare_dir(out: Path) -> None:
    """`out` comes from walk(); walk it again after creating it, since another user may have raced us."""
    # One level at a time: mkdir never follows a symlink at the name it creates, so a name planted after
    # walk() makes it fail instead of creating a directory wherever that symlink points
    for missing in [p for p in (*reversed(out.parents), out) if not os.path.lexists(p)]:
        try:
            os.mkdir(missing, 0o700)
        except FileExistsError:
            raise OSError(f"{missing} appeared while the output directory was being created") from None
    if walk(out) != out:
        raise OSError(f"{out} changed while it was being created")
    st = out.lstat()
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        raise OSError(f"{out} is owned by another user")
    # The agent's editor may save through a temporary file, so others must not even list or read this directory
    if st.st_mode & 0o077:
        raise OSError(f"{out} is accessible to other users; run chmod 700 on it or choose another directory")


def _prepare_document(out: Path) -> Path:
    doc = out / DOCUMENT
    try:
        os.close(os.open(doc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        st = doc.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            raise OSError(f"{doc} is not a regular file of yours with a single link") from None
        # Only the user can write to `out`, so the file checked above is the one changed here
        doc.chmod(0o600)
    return doc


def _write_chunks(out: Path, chunks: List[List[str]]) -> None:
    _prepare_dir(out)
    for stale in out.glob("prompts-*.txt"):
        if re.fullmatch(r"prompts-[0-9]{3,}\.txt", stale.name):
            stale.unlink()
    for i, blocks in enumerate(chunks, 1):
        # O_EXCL refuses an existing name, including a symlink, so the prompts never land in someone else's file
        fd = os.open(out / f"prompts-{i:03d}.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            # JSON allows lone surrogates, which UTF-8 cannot encode
            f.write("".join(blocks).encode(errors="backslashreplace"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    result = load_or_report(args.project, args.sources, args.include_non_interactive)
    if result is None:
        return 2
    try:
        out = walk(args.out)
        check_name(out)
    except OSError as e:
        print(f"refusing output directory {args.out}: {e}", file=sys.stderr)
        return 2
    if in_git_worktree(out):
        print(f"refusing to write raw prompts inside a git worktree: {out}", file=sys.stderr)
        return 2
    utts = dedup(result.utterances)
    chunks = chunk([render(u, args.zone) for u in utts], args.chunk_chars)
    try:
        _write_chunks(out, chunks)
        doc = _prepare_document(out)
    except OSError as e:
        print(f"cannot write to {out}: {e}", file=sys.stderr)
        return 2
    by_tool = ", ".join(f"{t} {sum(1 for u in utts if u.tool == t)}" for t in args.sources)
    print(f"utterances: {len(utts)} ({by_tool})")
    print(f"period: {format_ts(utts[0].ts, args.zone)} .. {format_ts(utts[-1].ts, args.zone)}")
    print(f"excluded: {result.excluded} injected texts, {result.excluded_sessions} non-interactive sessions, "
          f"{result.skipped} unreadable records")
    print(f"files: {len(chunks)} in {out}")
    print(f"document: {doc}")
    print("note: Claude Code deletes transcripts older than cleanupPeriodDays (default 30 days).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
