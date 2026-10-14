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
from typing import List, Optional, Sequence, Tuple

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
    return st.st_uid not in (0, os.getuid())


def _private_group(gid: int) -> bool:
    """True for a group of the user's own, which Ubuntu and others pair with umask 002: its bits let no one else in."""
    # Imported here: Windows lacks both, and main() must get as far as saying it does not run there
    import grp
    import pwd
    try:
        me = pwd.getpwuid(os.getuid())
    except KeyError:
        # A uid without a passwd entry, as in `docker run --user`, has no group of its own to vouch for
        return False
    # groupadd -o can give one gid several names, and the kernel grants by gid, so every name must be the user's own
    groups = [g for g in grp.getgrall() if g.gr_gid == gid]
    return (bool(groups) and all(g.gr_name == me.pw_name and set(g.gr_mem) <= {me.pw_name} for g in groups)
            # shortcut: getpwall() sees only enumerable users (local files; LDAP only with enumeration on), check the
            # group's members another way if a directory service hides users
            and not any(p.pw_gid == gid and p.pw_uid != me.pw_uid for p in pwd.getpwall()))


def _others_bits(st: os.stat_result, bits: int) -> int:
    """`bits` of the group and others, leaving out the group's when the group is the user's own."""
    group, other = st.st_mode & bits & 0o070, st.st_mode & bits & 0o007
    return other | (group if group and not _private_group(st.st_gid) else 0)


def _check_entry(entry: Path, st: os.stat_result) -> None:
    if _owned_by_other(st):
        raise OSError(f"{entry} is owned by another user")
    if stat.S_ISLNK(st.st_mode):
        return
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{entry} is not a directory")
    if _others_bits(st, 0o022) and not st.st_mode & stat.S_ISVTX:
        raise OSError(f"{entry} is writable by other users")


def walk_split(path: Path) -> Tuple[Path, List[str]]:
    """Resolve an absolute path one entry at a time, refusing anything another user could swap.

    Every entry must belong to the user or root, and every directory must be writable only by them unless
    it is sticky, where others cannot rename or remove our entries. What passes cannot change under us.
    Returns the real path of the part that exists and the names below it that do not.
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
            missing = [name, *parts]
            if ".." in missing:
                raise OSError(f"{path} has '..' after a missing directory") from None
            return current, missing
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
    return current, []


def walk(path: Path) -> Path:
    existing, missing = walk_split(path)
    return existing.joinpath(*missing)


def check_name(path: Path) -> None:
    # The agent pastes this path into shell commands in single quotes
    if UNSAFE_CHARS.search(str(path)):
        raise OSError(f"{path!r} contains a quote or a control character")


def _prepare_dir(existing: Path, missing: List[str]) -> Path:
    """Create the names walk_split() found missing, then walk again, since another user may have raced us."""
    out = existing.joinpath(*missing)
    # One level at a time, from the list taken when the path was checked: mkdir never follows a symlink at the
    # name it creates, so a name planted since then makes it fail instead of creating where that symlink points
    for i in range(1, len(missing) + 1):
        level = existing.joinpath(*missing[:i])
        try:
            os.mkdir(level, 0o700)
        except FileExistsError:
            raise OSError(f"{level} appeared while the output directory was being created") from None
    if walk(out) != out:
        raise OSError(f"{out} changed while it was being created")
    st = out.lstat()
    if st.st_uid != os.getuid():
        raise OSError(f"{out} is owned by another user")
    # The agent's editor may save through a temporary file, so others must not even list or read this directory
    if _others_bits(st, 0o077):
        raise OSError(f"{out} is accessible to other users; run chmod 700 on it or choose another directory")
    return out


def _prepare_document(out: Path) -> Path:
    doc = out / DOCUMENT
    try:
        os.close(os.open(doc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        st = doc.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid():
            raise OSError(f"{doc} is not a regular file of yours with a single link") from None
        # Only the user can write to `out`, so the file checked above is the one changed here
        doc.chmod(0o600)
    return doc


def _write_chunks(out: Path, chunks: List[List[str]]) -> None:
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
    if sys.platform == "win32":
        # The checks below read POSIX owners and mode bits, which Windows does not report
        print("extract.py runs on macOS and Linux only", file=sys.stderr)
        return 2
    result = load_or_report(args.project, args.sources, args.include_non_interactive)
    if result is None:
        return 2
    try:
        existing, missing = walk_split(args.out)
        out = existing.joinpath(*missing)
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
        _write_chunks(_prepare_dir(existing, missing), chunks)
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
