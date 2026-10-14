import io
import json
import os
import re
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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


class WalkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))

    def test_follows_relative_symlink_with_parent_reference(self):
        (self.root / "a").mkdir()
        (self.root / "real").mkdir()
        (self.root / "a" / "link").symlink_to("../real")
        self.assertEqual(extract.walk(self.root / "a" / "link" / "out"), self.root / "real" / "out")

    def test_follows_absolute_symlink(self):
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to(self.root / "real")
        self.assertEqual(extract.walk(self.root / "link"), self.root / "real")

    def test_allows_up_to_the_symlink_limit(self):
        (self.root / "real").mkdir()
        previous = self.root / "real"
        for i in range(extract.MAX_SYMLINKS):
            link = self.root / f"l{i}"
            link.symlink_to(previous)
            previous = link
        self.assertEqual(extract.walk(previous), self.root / "real")
        over = self.root / "over"
        over.symlink_to(previous)
        with self.assertRaisesRegex(OSError, "too many symlinks in"):
            extract.walk(over)

    def test_refuses_symlink_loop(self):
        (self.root / "x").symlink_to(self.root / "y")
        (self.root / "y").symlink_to(self.root / "x")
        with self.assertRaisesRegex(OSError, "too many symlinks in"):
            extract.walk(self.root / "x" / "out")

    def test_prepare_refuses_directory_that_resolves_elsewhere_after_creation(self):
        out = self.root / "out"
        with mock.patch.object(extract, "walk", return_value=self.root / "other"):
            with self.assertRaisesRegex(OSError, "changed while it was being created"):
                extract._prepare_dir(self.root, ["out"])

    def test_splits_existing_part_from_missing_names(self):
        self.assertEqual(extract.walk_split(self.root / "a" / "b"), (self.root, ["a", "b"]))

    def test_refuses_parent_reference_after_a_missing_directory(self):
        (self.root / "real").mkdir()
        (self.root / "link").symlink_to("nonexistent/../real")
        with self.assertRaisesRegex(OSError, "'..' after a missing directory"):
            extract.walk(self.root / "link" / "out")


class PrivateGroupTest(unittest.TestCase):
    ME = SimpleNamespace(pw_name="dev", pw_uid=os.getuid(), pw_gid=1001)

    def check(self, group, users=()):
        groups = group if isinstance(group, list) else [group]
        with mock.patch("grp.getgrall", return_value=groups), mock.patch("pwd.getpwuid", return_value=self.ME), \
                mock.patch("pwd.getpwall", return_value=[self.ME, *users]):
            return extract._private_group(1001)

    def test_accepts_group_named_after_the_user_with_no_other_member(self):
        self.assertTrue(self.check(SimpleNamespace(gr_name="dev", gr_gid=1001, gr_mem=[])))
        self.assertTrue(self.check(SimpleNamespace(gr_name="dev", gr_gid=1001, gr_mem=["dev"])))

    def test_refuses_group_with_another_member(self):
        self.assertFalse(self.check(SimpleNamespace(gr_name="dev", gr_gid=1001, gr_mem=["dev", "eve"])))

    def test_refuses_group_named_otherwise(self):
        self.assertFalse(self.check(SimpleNamespace(gr_name="staff", gr_gid=1001, gr_mem=[])))

    def test_refuses_group_that_is_another_users_primary_group(self):
        eve = SimpleNamespace(pw_name="eve", pw_uid=os.getuid() + 1, pw_gid=1001)
        self.assertFalse(self.check(SimpleNamespace(gr_name="dev", gr_gid=1001, gr_mem=[]), [eve]))

    def test_refuses_unknown_group(self):
        self.assertFalse(self.check([SimpleNamespace(gr_name="dev", gr_gid=1002, gr_mem=[])]))

    def test_refuses_when_the_user_has_no_passwd_entry(self):
        with mock.patch("pwd.getpwuid", side_effect=KeyError(os.getuid())):
            self.assertFalse(extract._private_group(1001))

    def test_refuses_gid_that_another_group_name_shares(self):
        # groupadd -o lets root give one gid several names; the kernel grants by gid, not by name
        self.assertFalse(self.check([SimpleNamespace(gr_name="dev", gr_gid=1001, gr_mem=[]),
                                     SimpleNamespace(gr_name="shared", gr_gid=1001, gr_mem=["bob"])]))

    def test_asks_about_the_group_of_the_entry(self):
        st = os.stat_result((stat.S_IFDIR | 0o770, 0, 0, 0, 0, 1001, 0, 0, 0, 0))
        with mock.patch.object(extract, "_private_group", return_value=True) as private:
            self.assertEqual(extract._others_bits(st, 0o077), 0)
        private.assert_called_once_with(1001)

    def test_looks_up_groups_only_when_group_bits_are_set(self):
        st = os.stat_result((stat.S_IFDIR | 0o705, 0, 0, 0, 0, 1001, 0, 0, 0, 0))
        with mock.patch.object(extract, "_private_group") as private:
            self.assertEqual(extract._others_bits(st, 0o077), 0o005)
        private.assert_not_called()



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

    def test_writes_one_copy_of_a_forked_record(self):
        rec = fx.claude_user("複製された発話", uuid="11111111-2222-3333-4444-555555555555")
        fx.write_claude(self.claude, "/proj", [rec], session="b-copy")
        fx.write_claude(self.claude, "/proj", [rec], session="a-copy")
        _, stdout, _ = self.run_main()
        self.assertEqual((self.out / "prompts-001.txt").read_text(encoding="utf-8").count("複製された発話"), 1)
        self.assertIn("utterances: 1 (claude 1, codex 0)", stdout)

    def test_restricts_permissions_of_output(self):
        self.write_history()
        self.run_main()
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.out / "prompts-001.txt").stat().st_mode), 0o600)

    def test_refuses_existing_output_directory_others_can_open(self):
        self.write_history()
        self.out.mkdir()
        for mode in (0o755, 0o750, 0o701):
            self.out.chmod(mode)
            # a group of the user's own counts as the user; Linux runners have one, macOS's staff is shared
            with mock.patch.object(extract, "_private_group", return_value=False):
                code, _, stderr = self.run_main()
            self.assertEqual(code, 2, oct(mode))
            self.assertIn("is accessible to other users; run chmod 700 on it", stderr)
            self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), mode)
            self.assertEqual(list(self.out.iterdir()), [])

    def test_accepts_existing_private_output_directory(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        self.assertEqual(self.run_main()[0], 0)
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
        self.assertEqual(sorted(p.name for p in self.out.glob("prompts-*")), ["prompts-001.txt", "prompts-002.txt", "prompts-003.txt"])

    def test_removes_stale_prompt_files_but_keeps_other_files(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        (self.out / "prompts-009.txt").write_text("old", encoding="utf-8")
        (self.out / "notes.md").write_text("keep", encoding="utf-8")
        (self.out / "prompts-v2.txt").write_text("keep", encoding="utf-8")
        (self.out / "prompts-2024-notes.txt").write_text("keep", encoding="utf-8")
        (self.out / "prompts-1001.txt").write_text("old", encoding="utf-8")
        (self.out / "prompts-\u0661\u0662\u0663.txt").write_text("keep", encoding="utf-8")
        self.run_main()
        self.assertFalse((self.out / "prompts-1001.txt").exists())
        self.assertTrue((self.out / "prompts-2024-notes.txt").exists())
        self.assertTrue((self.out / "prompts-\u0661\u0662\u0663.txt").exists())
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
            f"files: 1 in {self.out.resolve()}",
            f"document: {self.out.resolve() / 'tacit-knowledge.md'}",
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

    def test_refuses_output_inside_git_worktree(self):
        self.write_history()
        (self.root / ".git").mkdir()
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("inside a git worktree", stderr)
        self.assertFalse(self.out.exists())

    def test_refuses_existing_directory_writable_by_others(self):
        self.write_history()
        for mode in (0o777, 0o775):
            self.out.mkdir(exist_ok=True)
            self.out.chmod(mode)
            # a group of the user's own counts as the user; Linux runners have one, macOS's staff is shared
            with mock.patch.object(extract, "_private_group", return_value=False):
                code, _, stderr = self.run_main()
            self.assertEqual(code, 2, oct(mode))
            self.assertIn("writable by other users", stderr)
            self.assertEqual(list(self.out.iterdir()), [])

    def test_refuses_existing_directory_owned_by_another_user(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        with mock.patch.object(extract.os, "getuid", return_value=os.getuid() + 1):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("owned by another user", stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_refuses_new_output_directory_under_a_parent_owned_by_another_user(self):
        self.write_history()
        with mock.patch.object(extract.os, "getuid", return_value=os.getuid() + 1):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is owned by another user", stderr)
        self.assertFalse(self.out.exists())

    def test_refuses_existing_output_directory_owned_by_root(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        out, real_lstat = self.out.resolve(), Path.lstat
        root_owned = os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        def fake_lstat(path):
            return root_owned if path == out else real_lstat(path)

        with mock.patch.object(extract.Path, "lstat", autospec=True, side_effect=fake_lstat):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(f"{out} is owned by another user", stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_refuses_parent_directory_writable_by_others_unless_sticky(self):
        self.write_history()
        shared = self.root / "shared"
        shared.mkdir()
        self.out = shared / "out"
        shared.chmod(0o777)
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(f"{shared.resolve()} is writable by other users", stderr)
        self.assertFalse(self.out.exists())
        shared.chmod(0o1777)
        self.assertEqual(self.run_main()[0], 0)

    def test_refuses_to_run_on_windows(self):
        self.write_history()
        with mock.patch.object(extract.sys, "platform", "win32"):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "extract.py runs on macOS and Linux only\n")
        self.assertFalse(self.out.exists())

    def test_accepts_group_access_when_the_group_is_private(self):
        self.write_history()
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o775)
        self.out = shared / "out"
        self.out.mkdir()
        self.out.chmod(0o770)
        with mock.patch.object(extract, "_private_group", return_value=True):
            self.assertEqual(self.run_main()[0], 0)
            self.out.chmod(0o705)
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is accessible to other users", stderr)
        self.out.chmod(0o770)
        with mock.patch.object(extract, "_private_group", return_value=False):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn(f"{shared.resolve()} is writable by other users", stderr)

    def test_refuses_name_planted_after_the_path_was_checked(self):
        self.write_history()
        victim = self.root / "victim"
        victim.mkdir(mode=0o700)
        self.out = self.root / "a" / "out"
        real_dedup = extract.dedup

        def planting_dedup(utts):
            os.symlink(victim, self.root / "a")
            return real_dedup(utts)

        with mock.patch.object(extract, "dedup", side_effect=planting_dedup):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("appeared while the output directory was being created", stderr)
        self.assertEqual(list(victim.iterdir()), [])

    def test_refuses_sticky_output_directory_writable_by_others(self):
        self.write_history()
        self.out.mkdir()
        self.out.chmod(0o1777)
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is accessible to other users", stderr)

    def test_resolves_symlinked_output_directory_and_prints_real_path(self):
        self.write_history()
        real = self.root / "real"
        real.mkdir(mode=0o700)
        self.out.symlink_to(real)
        code, stdout, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(f"files: 1 in {real.resolve()}", stdout)
        self.assertTrue((real / "prompts-001.txt").exists())

    def test_expands_tilde_in_output_directory(self):
        self.write_history()
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            code, stdout, _ = self.run_main("--out", "~/notes")
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "notes" / "prompts-001.txt").exists())
        self.assertIn(f"files: 1 in {(self.root / 'notes').resolve()}", stdout)

    def test_refuses_symlink_on_the_path_owned_by_another_user(self):
        self.write_history()
        (self.root / "real").mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(self.root / "real")
        self.out = link / "out"
        real_lstat = Path.lstat
        foreign = os.stat_result((stat.S_IFLNK | 0o755, 0, 0, 0, os.getuid() + 1, 0, 0, 0, 0, 0))

        def fake_lstat(path):
            return foreign if path == link.resolve().parent / "link" else real_lstat(path)

        with mock.patch.object(extract.Path, "lstat", autospec=True, side_effect=fake_lstat):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("link is owned by another user", stderr)
        self.assertFalse((self.root / "real" / "out").exists())

    def test_refuses_output_path_with_a_quote_or_control_character(self):
        self.write_history()
        for name in ("it's", "line\nbreak"):
            real = self.root / name
            real.mkdir(mode=0o700)
            self.out = self.root / "link"
            if self.out.is_symlink():
                self.out.unlink()
            # a symlink keeps the typed path clean while the resolved one is not
            self.out.symlink_to(real)
            code, _, stderr = self.run_main()
            self.assertEqual(code, 2, name)
            self.assertIn("contains a quote or a control character", stderr)
            self.assertEqual(list(real.iterdir()), [])

    def test_refuses_path_whose_component_is_a_file(self):
        self.write_history()
        (self.root / "file").write_text("x", encoding="utf-8")
        self.out = self.root / "file" / "out"
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not a directory", stderr)

    def test_creates_private_empty_document(self):
        self.write_history()
        self.assertEqual(self.run_main()[0], 0)
        doc = self.out / "tacit-knowledge.md"
        self.assertEqual((doc.read_text(encoding="utf-8"), stat.S_IMODE(doc.stat().st_mode)), ("", 0o600))

    def test_refuses_name_planted_while_creating_the_output_directory(self):
        self.write_history()
        victim = self.root / "victim"
        victim.mkdir(mode=0o700)
        self.out = self.root / "a" / "out"
        real_mkdir = os.mkdir

        def racing_mkdir(path, *args, **kwargs):
            if not os.path.lexists(self.root / "a"):
                os.symlink(victim, self.root / "a")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(extract.os, "mkdir", side_effect=racing_mkdir):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("appeared while the output directory was being created", stderr)
        self.assertEqual(list(victim.iterdir()), [])

    def test_refuses_document_with_another_hard_link(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        other = self.root / "other.txt"
        other.write_text("keep", encoding="utf-8")
        other.chmod(0o644)
        os.link(other, self.out / "tacit-knowledge.md")
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not a regular file of yours with a single link", stderr)
        self.assertEqual(stat.S_IMODE(other.stat().st_mode), 0o644)

    def test_refuses_document_owned_by_another_user(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        doc = self.out / "tacit-knowledge.md"
        doc.write_text("", encoding="utf-8")
        real_lstat = Path.lstat
        foreign = os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, os.getuid() + 1, 0, 0, 0, 0, 0))

        def fake_lstat(path):
            return foreign if path.name == "tacit-knowledge.md" else real_lstat(path)

        with mock.patch.object(extract.Path, "lstat", autospec=True, side_effect=fake_lstat):
            code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not a regular file of yours with a single link", stderr)

    def test_keeps_existing_document_and_makes_it_private(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        doc = self.out / "tacit-knowledge.md"
        doc.write_text("# 以前の文書\n", encoding="utf-8")
        doc.chmod(0o644)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual((doc.read_text(encoding="utf-8"), stat.S_IMODE(doc.stat().st_mode)), ("# 以前の文書\n", 0o600))

    def test_refuses_document_that_is_a_symlink(self):
        self.write_history()
        self.out.mkdir(mode=0o700)
        target = self.root / "elsewhere.md"
        target.write_text("keep", encoding="utf-8")
        target.chmod(0o644)
        (self.out / "tacit-knowledge.md").symlink_to(target)
        code, _, stderr = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("is not a regular file of yours with a single link", stderr)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_does_not_follow_a_symlink_at_an_output_name(self):
        self.out.mkdir(mode=0o700)
        victim = self.root / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        (self.out / "prompts-001.txt").symlink_to(victim)
        with mock.patch.object(Path, "glob", return_value=iter([])), self.assertRaises(FileExistsError):
            extract._write_chunks(extract.walk(self.out), [["本文"]])
        self.assertEqual(victim.read_text(encoding="utf-8"), "keep")

    def test_writes_utterance_with_a_lone_surrogate(self):
        rec = fx.claude_user("bad \ud800 surrogate")
        fx.write_claude(self.claude, "/proj", [], raw_lines=[json.dumps(rec)])
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("bad \\ud800 surrogate", (self.out / "prompts-001.txt").read_text(encoding="utf-8"))

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
