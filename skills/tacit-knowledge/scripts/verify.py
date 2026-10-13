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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from history import SOURCES, Utterance, load_or_report, parse_sources, session_prefix

EVIDENCE = re.compile(
    r"^\s*> (\d{4}-\d{2}-\d{2} \d{2}:\d{2}[+-]\d{2}:\d{2}) ([0-9a-f]{8}-[0-9a-f]{3})「(.+)」\s*$"
)
CLAIM = re.compile(r"^- \S")

Key = Tuple[datetime, str]
Finding = Tuple[int, str]


def parse_evidence(line: str) -> Optional[Tuple[datetime, str, str]]:
    m = EVIDENCE.match(line)
    if not m:
        return None
    try:
        local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M%z")
    except ValueError:
        return None
    return local.astimezone(timezone.utc), m.group(2), m.group(3)


def build_index(utterances: Sequence[Utterance]) -> Dict[Key, List[str]]:
    index: Dict[Key, List[str]] = {}
    for u in utterances:
        key = (u.ts.replace(second=0, microsecond=0), session_prefix(u.session))
        index.setdefault(key, []).append(u.text)
    return index


def verify(lines: Sequence[str], index: Dict[Key, List[str]]) -> Tuple[List[Finding], List[Finding]]:
    """Return (evidence lines not found in history, claims without evidence)."""
    bad: List[Finding] = []
    unsupported: List[Finding] = []
    claim: Optional[Finding] = None
    for no, raw in enumerate(lines, 1):
        line = raw.rstrip()
        if CLAIM.match(line):
            if claim:
                unsupported.append(claim)
            claim = (no, line)
            continue
        evidence = parse_evidence(line)
        if evidence is None:
            continue
        claim = None
        ts, session, quote = evidence
        if not any(quote in text for text in index.get((ts, session), [])):
            bad.append((no, line))
    if claim:
        unsupported.append(claim)
    return bad, unsupported


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify evidence lines against agent history.")
    parser.add_argument("doc", help="tacit-knowledge document to check")
    parser.add_argument("--project", default=os.getcwd(), help="project directory (default: current directory)")
    parser.add_argument("--source", default=",".join(SOURCES), help="comma-separated: claude,codex")
    args = parser.parse_args(argv)
    try:
        args.sources = parse_sources(args.source)
    except ValueError as e:
        parser.error(str(e))
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        with open(args.doc, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"cannot read {args.doc}: {e}", file=sys.stderr)
        return 2
    result = load_or_report(args.project, args.sources)
    if result is None:
        return 2
    total = sum(1 for line in lines if parse_evidence(line.rstrip()))
    if total == 0:
        print("no evidence lines found; check the line format in the docstring of verify.py")
        return 1
    bad, unsupported = verify(lines, build_index(result.utterances))
    for no, line in bad:
        print(f"NG evidence not found in history (line {no}): {line.strip()}")
    for no, line in unsupported:
        print(f"NG claim without evidence (line {no}): {line.strip()}")
    print(f"evidence: {total}, verified: {total - len(bad)}, claims without evidence: {len(unsupported)}")
    return 1 if bad or unsupported else 0


if __name__ == "__main__":
    sys.exit(main())
