import io
import os
import re
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import fixtures as fx
import extract
from history import Utterance

EST = timezone(timedelta(hours=-5))


class ChunkTest(unittest.TestCase):
    def test_splits_only_between_blocks(self):
        self.assertEqual(extract.chunk(["a" * 5, "b" * 5, "c" * 5], 10), [["a" * 5, "b" * 5], ["c" * 5]])

    def test_puts_oversized_block_in_its_own_chunk(self):
        self.assertEqual(extract.chunk(["x" * 3, "y" * 20, "z" * 3], 10), [["x" * 3], ["y" * 20], ["z" * 3]])

    def test_returns_no_chunks_for_no_blocks(self):
        self.assertEqual(extract.chunk([], 10), [])


class RenderTest(unittest.TestCase):
    def test_writes_header_with_offset_tool_session_prefix_and_length(self):
        u = Utterance(datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc), "claude", fx.CLAUDE_SESSION, "本文です")
        self.assertEqual(extract.render(u, EST), "=== 2026-09-23 20:05-05:00 claude 45c391e1-2c4 (4)\n本文です\n")


class GitWorktreeTest(unittest.TestCase):
    def test_detects_directory_below_git_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            self.assertTrue(extract.in_git_worktree(Path(tmp) / "a" / "b"))

    def test_does_not_treat_other_case_as_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".GIT_NOT").mkdir()
            self.assertFalse(extract.in_git_worktree(Path(tmp) / "out"))

    def test_returns_false_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(extract.in_git_worktree(Path(tmp) / "out"))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.out = self.root / "out"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = extract.main(["--out", str(self.out), "--project", "/proj", "--tz", "America/New_York", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def write_history(self):
        fx.write_claude(self.claude, "/proj", [
            fx.claude_user("一つ目の発話", ts="2026-09-24T01:05:30Z"),
            fx.claude_user("<system-reminder>注入</system-reminder>"),
            fx.claude_user("三つ目の発話", ts="2026-09-26T03:00:00Z"),
        ])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("二つ目の発話", ts="2026-09-25T02:00:00Z")])
        fx.write_codex(self.codex, [fx.codex_meta(source="exec"), fx.codex_user("agent")], session="e" * 36)

    def test_writes_prompt_file_with_headers_and_texts_in_time_order(self):
        self.write_history()
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual((self.out / "prompts-001.txt").read_text(encoding="utf-8"),
                         "=== 2026-09-23 21:05-04:00 claude 45c391e1-2c4 (6)\n一つ目の発話\n"
                         "=== 2026-09-24 22:00-04:00 codex 01a12ca3-2e3 (6)\n二つ目の発話\n"
                         "=== 2026-09-25 23:00-04:00 claude 45c391e1-2c4 (6)\n三つ目の発話\n")

    def test_restricts_permissions_of_output(self):
        self.write_history()
        self.run_main()
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.out / "prompts-001.txt").stat().st_mode), 0o600)

    def test_keeps_permissions_of_existing_output_directory(self):
        self.write_history()
        self.out.mkdir()
        self.out.chmod(0o755)
        self.run_main()
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.out / "prompts-001.txt").stat().st_mode), 0o600)

    def test_returns_2_when_output_path_is_a_file(self):
        self.write_history()
        self.out.write_text("not a directory", encoding="utf-8")
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(str(self.out), stderr)

    def test_splits_into_numbered_files_by_chunk_size(self):
        self.write_history()
        code, _, _ = self.run_main("--chunk-chars", "10")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["prompts-001.txt", "prompts-002.txt", "prompts-003.txt"])

    def test_removes_stale_prompt_files_but_keeps_other_files(self):
        self.write_history()
        self.out.mkdir()
        (self.out / "prompts-009.txt").write_text("old", encoding="utf-8")
        (self.out / "notes.md").write_text("keep", encoding="utf-8")
        (self.out / "prompts-v2.txt").write_text("keep", encoding="utf-8")
        (self.out / "prompts-2024-notes.txt").write_text("keep", encoding="utf-8")
        (self.out / "prompts-1001.txt").write_text("old", encoding="utf-8")
        self.run_main()
        self.assertFalse((self.out / "prompts-1001.txt").exists())
        self.assertTrue((self.out / "prompts-2024-notes.txt").exists())
        self.assertFalse((self.out / "prompts-009.txt").exists())
        self.assertTrue((self.out / "notes.md").exists())
        self.assertTrue((self.out / "prompts-v2.txt").exists())

    def test_prints_counts_period_and_exclusions(self):
        self.write_history()
        _, stdout, _ = self.run_main()
        self.assertEqual(stdout.splitlines(), [
            "utterances: 3 (claude 2, codex 1)",
            "period: 2026-09-23 21:05-04:00 .. 2026-09-25 23:00-04:00",
            "excluded: 1 injected texts, 1 non-interactive sessions, 0 unreadable records",
            f"files: 1 in {self.out}",
            "note: Claude Code deletes transcripts older than cleanupPeriodDays (default 30 days).",
        ])

    def test_creates_missing_parent_directories(self):
        self.write_history()
        self.out = self.root / "a" / "b" / "out"
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        for directory in (self.root / "a", self.root / "a" / "b", self.out):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)

    def test_accepts_chunk_size_of_one(self):
        self.write_history()
        code, _, _ = self.run_main("--chunk-chars", "1")
        self.assertEqual(code, 0)

    def test_uses_current_directory_as_default_project(self):
        project = self.root / "project"
        project.mkdir()
        real = os.path.realpath(project)
        fx.write_claude(self.claude, real, [fx.claude_user("既定のプロジェクト", cwd=real)])
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(extract.main(["--out", str(self.out)]), 0)

    def test_reads_only_requested_source(self):
        fx.write_claude(self.claude, "/proj", [fx.claude_user("claude だけ")])
        code, _, stderr = self.run_main("--source", "codex")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("no utterances found. searched:\n"))

    def test_help_describes_every_option(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(stdout):
            extract.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(stdout.getvalue().split())
        for phrase in ("Extract human utterances from agent history.", "output directory (do not commit it)",
                       "project directory (default: current directory)", "comma-separated: claude,codex",
                       "IANA time zone such as Asia/Tokyo (default: local)", "max characters per file",
                       "also read claude -p, codex exec, and subagent sessions (usually written by another agent)"):
            self.assertRegex(text, rf"(^| ){re.escape(phrase)}( |$)")

    def test_requires_out(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(stderr):
            extract.main(["--project", "/proj"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--out", stderr.getvalue())

    def test_includes_non_interactive_sessions_when_asked(self):
        self.write_history()
        _, stdout, _ = self.run_main("--include-non-interactive")
        self.assertIn("agent", (self.out / "prompts-001.txt").read_text(encoding="utf-8"))
        self.assertIn("0 non-interactive sessions", stdout)

    def test_warns_when_output_is_inside_git_worktree(self):
        self.write_history()
        (self.root / ".git").mkdir()
        _, _, stderr = self.run_main()
        self.assertIn("git", stderr)

    def test_does_not_warn_outside_git_worktree(self):
        self.write_history()
        _, _, stderr = self.run_main()
        self.assertEqual(stderr, "")

    def test_returns_2_and_lists_searched_paths_when_no_history(self):
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(str(self.claude / "projects" / "-proj"), stderr)
        self.assertIn(str(self.codex / "sessions"), stderr)
        self.assertFalse(self.out.exists())

    def assert_usage_error(self, args, message):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(stderr):
            extract.main(["--out", str(self.out), *args])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(f"error: {message}\n", stderr.getvalue())

    def test_rejects_unknown_time_zone(self):
        self.assert_usage_error(["--tz", "Not/AZone"], "unknown time zone: Not/AZone")

    def test_rejects_unknown_source(self):
        self.assert_usage_error(["--source", "claude,cursor"], "source must be a subset of claude,codex")

    def test_rejects_non_positive_chunk_size(self):
        self.assert_usage_error(["--chunk-chars", "0"], "--chunk-chars must be positive")


if __name__ == "__main__":
    unittest.main()
