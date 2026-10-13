import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import fixtures as fx
import history

PROJECT = "/proj"


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"

    def load(self, project=PROJECT, sources=("claude", "codex")):
        return history.load(project, sources, claude_home=self.claude, codex_home=self.codex)

    def texts(self, result):
        return [u.text for u in result.utterances]


class ClaudeTest(HistoryTestCase):
    def test_reads_string_content_as_utterance(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("公式ドキュメントで確認してください")])
        result = self.load()
        self.assertEqual(self.texts(result), ["公式ドキュメントで確認してください"])
        u = result.utterances[0]
        self.assertEqual(u.tool, "claude")
        self.assertEqual(u.session, fx.CLAUDE_SESSION)
        self.assertEqual(u.ts, datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc))

    def test_joins_text_items_of_list_content(self):
        content = [{"type": "text", "text": "一行目"}, {"type": "image"}, {"type": "text", "text": "二行目"}]
        fx.write_claude(self.claude, PROJECT, [fx.claude_user(content)])
        self.assertEqual(self.texts(self.load()), ["一行目\n二行目"])

    def test_ignores_tool_result_only_content(self):
        content = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
        fx.write_claude(self.claude, PROJECT, [fx.claude_user(content)])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.excluded, 0)

    def test_ignores_meta_sidechain_and_assistant_records(self):
        fx.write_claude(self.claude, PROJECT, [
            fx.claude_user("meta", isMeta=True),
            fx.claude_user("side", isSidechain=True),
            fx.claude_assistant("answer"),
            fx.claude_user("人の発話"),
        ])
        self.assertEqual(self.texts(self.load()), ["人の発話"])

    def test_excludes_injected_texts_and_counts_them(self):
        injected = [
            "<command-name>/compact</command-name>",
            "<local-command-stdout>done</local-command-stdout>",
            "<task-notification>finished</task-notification>",
            "<system-reminder>x</system-reminder>",
            "<bash-input>ls</bash-input>",
            "<user-prompt-submit-hook>x</user-prompt-submit-hook>",
            "Caveat: The messages below were generated",
            "[Request interrupted by user]",
            "This session is being continued from a previous conversation",
            "  <system-reminder>leading spaces</system-reminder>",
        ]
        fx.write_claude(self.claude, PROJECT, [fx.claude_user(t) for t in injected] + [fx.claude_user("残る")])
        result = self.load()
        self.assertEqual(self.texts(result), ["残る"])
        self.assertEqual(result.excluded, len(injected))

    def test_excludes_non_interactive_sdk_session(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("ok とだけ返して", entrypoint="sdk-cli")])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.excluded_sessions, 1)

    def test_keeps_records_without_entrypoint(self):
        rec = fx.claude_user("古い版の記録")
        del rec["entrypoint"]
        fx.write_claude(self.claude, PROJECT, [rec])
        self.assertEqual(self.texts(self.load()), ["古い版の記録"])

    def test_skips_session_whose_cwd_is_another_path_with_same_encoding(self):
        # /a/b_c と /a/b-c は同じディレクトリ名に変換される
        fx.write_claude(self.claude, "/a/b-c", [fx.claude_user("別プロジェクト", cwd="/a/b_c")])
        self.assertEqual(self.load(project="/a/b-c").utterances, [])

    def test_accepts_session_in_subdirectory_of_project(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("配下", cwd="/proj/sub")])
        self.assertEqual(self.texts(self.load()), ["配下"])

    def test_counts_broken_lines_as_skipped(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("正常")], raw_lines=["{not json", "[1, 2]"])
        result = self.load()
        self.assertEqual(self.texts(result), ["正常"])
        self.assertEqual(result.skipped, 2)

    def test_counts_record_with_bad_timestamp_as_skipped(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("壊れた時刻", ts="yesterday")])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.skipped, 1)

    def test_ignores_whitespace_only_text(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("  \n\t ")])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.excluded, 0)


class CodexTest(HistoryTestCase):
    def test_reads_input_text_of_interactive_session(self):
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("このまま進めてください"), fx.codex_assistant("了解")])
        result = self.load()
        self.assertEqual(self.texts(result), ["このまま進めてください"])
        self.assertEqual(result.utterances[0].tool, "codex")
        self.assertEqual(result.utterances[0].session, fx.CODEX_SESSION)

    def test_skips_session_of_other_project(self):
        fx.write_codex(self.codex, [fx.codex_meta(cwd="/other"), fx.codex_user("別")])
        self.assertEqual(self.load().utterances, [])

    def test_excludes_exec_and_subagent_sessions(self):
        fx.write_codex(self.codex, [fx.codex_meta(source="exec"), fx.codex_user("agent")], session="a" * 36)
        fx.write_codex(self.codex, [fx.codex_meta(source={"subagent": {"other": "x"}}), fx.codex_user("sub")], session="b" * 36)
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.excluded_sessions, 2)

    def test_excludes_injected_codex_texts(self):
        injected = ["<environment_context>\n  <cwd>/proj</cwd>", "# AGENTS.md instructions\n\n<INSTRUCTIONS>", "<recommended_plugins>\nHere"]
        fx.write_codex(self.codex, [fx.codex_meta()] + [fx.codex_user(t) for t in injected] + [fx.codex_user("残る")])
        result = self.load()
        self.assertEqual(self.texts(result), ["残る"])
        self.assertEqual(result.excluded, 3)

    def test_counts_file_without_session_meta_as_skipped(self):
        fx.write_codex(self.codex, [fx.codex_user("metaなし")])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.skipped, 1)

    def test_counts_session_meta_without_cwd_as_skipped(self):
        meta = fx.codex_meta()
        del meta["payload"]["cwd"]
        fx.write_codex(self.codex, [meta, fx.codex_user("cwdなし")])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.skipped, 1)

    def test_counts_broken_lines_and_bad_timestamps_as_skipped(self):
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("壊れた時刻", ts="soon")], raw_lines=["{"])
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.skipped, 2)


class CommonTest(HistoryTestCase):
    def test_merges_duplicates_within_same_session_minute_and_text(self):
        fx.write_claude(self.claude, PROJECT, [
            fx.claude_user("同じ", ts="2026-09-24T01:05:01.000Z"),
            fx.claude_user("同じ", ts="2026-09-24T01:05:59.000Z"),
            fx.claude_user("同じ", ts="2026-09-24T01:06:00.000Z"),
        ])
        self.assertEqual(self.texts(self.load()), ["同じ", "同じ"])

    def test_sorts_utterances_by_time_across_tools(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("二番目", ts="2026-09-24T02:00:00Z")])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("一番目", ts="2026-09-24T01:00:00Z"),
                                    fx.codex_user("三番目", ts="2026-09-24T03:00:00Z")])
        self.assertEqual(self.texts(self.load()), ["一番目", "二番目", "三番目"])

    def test_reads_only_requested_sources(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("claude")])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("codex")])
        self.assertEqual(self.texts(self.load(sources=("codex",))), ["codex"])

    def test_accepts_project_with_trailing_slash(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("末尾")])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("末尾codex")])
        self.assertEqual(self.texts(self.load(project="/proj/")), ["末尾", "末尾codex"])

    def test_accepts_project_given_through_symlink(self):
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real)
        real_path = os.path.realpath(real)
        fx.write_claude(self.claude, real_path, [fx.claude_user("実体", cwd=real_path)])
        fx.write_codex(self.codex, [fx.codex_meta(cwd=str(link)), fx.codex_user("リンク")])
        self.assertEqual(sorted(self.texts(self.load(project=str(link)))), ["リンク", "実体"])

    def test_missing_history_returns_empty_result_with_searched_paths(self):
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertIn(str(self.claude / "projects" / "-proj"), result.searched)
        self.assertIn(str(self.codex / "sessions"), result.searched)

    def test_uses_environment_variables_as_default_homes(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("env claude")])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("env codex")])
        env = {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)}
        with mock.patch.dict(os.environ, env):
            result = history.load(PROJECT, ("claude", "codex"))
        self.assertEqual(self.texts(result), ["env claude", "env codex"])

    def test_falls_back_to_home_directory_without_environment_variables(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(history.default_claude_home(), self.root / ".claude")
            self.assertEqual(history.default_codex_home(), self.root / ".codex")


class FormatTest(unittest.TestCase):
    def test_encode_project_replaces_non_alphanumerics(self):
        self.assertEqual(history.encode_project("/Users/me/devs/_private"), "-Users-me-devs--private")

    def test_session_prefix_keeps_first_twelve_characters(self):
        self.assertEqual(history.session_prefix(fx.CLAUDE_SESSION), "45c391e1-2c4")

    def test_format_ts_writes_offset_with_colon(self):
        ts = datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc)
        self.assertEqual(history.format_ts(ts, timezone(timedelta(hours=9))), "2026-09-24 10:05+09:00")
        self.assertEqual(history.format_ts(ts, timezone(timedelta(hours=-5))), "2026-09-23 20:05-05:00")

    def test_format_ts_uses_local_zone_when_none(self):
        ts = datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc)
        local = ts.astimezone()
        self.assertTrue(history.format_ts(ts, None).startswith(local.strftime("%Y-%m-%d %H:%M")))

    def test_resolve_tz_returns_named_zone(self):
        tz = history.resolve_tz("Asia/Tokyo")
        self.assertEqual(datetime(2026, 1, 1, tzinfo=tz).utcoffset(), timedelta(hours=9))

    def test_resolve_tz_returns_none_for_empty_name(self):
        self.assertIsNone(history.resolve_tz(None))
        self.assertIsNone(history.resolve_tz(""))

    def test_resolve_tz_rejects_unknown_and_malformed_names(self):
        for name in ("Not/AZone", "../etc"):
            with self.assertRaises(ValueError):
                history.resolve_tz(name)


if __name__ == "__main__":
    unittest.main()
