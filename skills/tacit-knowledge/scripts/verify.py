#!/usr/bin/env python3
"""Check that every evidence line of a tacit-knowledge document quotes a real utterance.

usage: verify.py DOC [--project PATH] [--source claude,codex]

An evidence line looks like `  > 2026-09-24 10:05+09:00 45c391e1-2c4「quoted text」`.
A claim is a top-level `- ` bullet and needs at least one evidence line below it.
This proves the quote exists at that time in that session. It does not prove the
claim above it means what the quote means.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from history import SOURCES, Utterance, load_or_report, minute_key, parse_sources, session_prefix

EVIDENCE = re.compile(
    r"^\s*> (\d{4}-\d{2}-\d{2} \d{2}:\d{2}[+-]\d{2}:\d{2}) ([0-9a-f]{8}-[0-9a-f]{3})「(.+)」\s*$"
)
CLAIM = re.compile(r"^- \S")

Key = Tuple[int, str]
Finding = Tuple[int, str]


def parse_evidence(line: str) -> Optional[Tuple[int, str, str]]:
    m = EVIDENCE.match(line)
    if not m:
        return None
    try:
        local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M%z")
    except ValueError:
        return None
    return minute_key(local), m.group(2), m.group(3)


def build_index(utterances: Sequence[Utterance]) -> Dict[Key, List[str]]:
    index: Dict[Key, List[str]] = {}
    for u in utterances:
        key = (minute_key(u.ts), session_prefix(u.session))
        index.setdefault(key, []).append(u.text)
    return index


def verify(lines: Sequence[str], index: Dict[Key, List[str]]) -> Tuple[List[Finding], List[Finding], List[Finding]]:
    """Return (evidence not found in history, claims without evidence, quoted lines that are not valid evidence)."""
    bad: List[Finding] = []
    unsupported: List[Finding] = []
    malformed: List[Finding] = []
    claim: Optional[Finding] = None
    for no, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if CLAIM.match(line):
            if claim is not None:
                unsupported.append(claim)
            claim = (no, line)
            continue
        evidence = parse_evidence(line)
        if evidence is None:
            # A quoted line that does not parse would otherwise pass as if it had been checked
            if line.lstrip().startswith(">"):
                malformed.append((no, line))
                claim = None
            continue
        claim = None
        ts, session, quote = evidence
        if not any(quote in text for text in index.get((ts, session), [])):
            bad.append((no, line))
    if claim is not None:
        unsupported.append(claim)
    return bad, unsupported, malformed


def _report(total: int, bad: List[Finding], unsupported: List[Finding], malformed: List[Finding]) -> None:
    for label, findings in (("evidence not found in history", bad), ("malformed evidence line", malformed),
                            ("claim without evidence", unsupported)):
        for no, line in findings:
            print(f"NG {label} (line {no}): {line.strip()}")
    print(f"evidence: {total}, verified: {total - len(bad)}, malformed: {len(malformed)}, "
          f"claims without evidence: {len(unsupported)}")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify evidence lines against agent history.")
    parser.add_argument("doc", help="tacit-knowledge document to check")
    parser.add_argument("--project", default=os.getcwd(), help="project directory (default: current directory)")
    parser.add_argument("--source", default=",".join(SOURCES), help="comma-separated: claude,codex")
    parser.add_argument("--include-non-interactive", action="store_true",
                        help="also read claude -p, codex exec, and subagent sessions (usually written by another agent)")
    args = parser.parse_args(argv)
    try:
        args.sources = parse_sources(args.source)
    except ValueError as e:
        parser.error(str(e))
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        lines = Path(args.doc).read_bytes().decode().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        print(f"cannot read {args.doc}: {e}", file=sys.stderr)
        return 2
    result = load_or_report(args.project, args.sources, args.include_non_interactive)
    if result is None:
        return 2
    total = sum(1 for line in lines if parse_evidence(line))
    bad, unsupported, malformed = verify(lines, build_index(result.utterances))
    if total == 0 and not malformed:
        print("no evidence lines found; check the line format in the docstring of verify.py")
        return 1
    _report(total, bad, unsupported, malformed)
    return 1 if bad or unsupported or malformed else 0


if __name__ == "__main__":
    sys.exit(main())
