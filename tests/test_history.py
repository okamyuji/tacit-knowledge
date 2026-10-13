import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
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

    def test_uses_file_name_as_session_id(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("本文", session="not-the-file-name")])
        self.assertEqual(self.load().utterances[0].session, fx.CLAUDE_SESSION)

    def test_ignores_text_items_without_text(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user([{"type": "text"}, {"type": "text", "text": "本文"}])])
        self.assertEqual(self.texts(self.load()), ["本文"])

    def test_ignores_message_without_content(self):
        rec = fx.claude_user("x")
        rec["message"] = {"role": "user"}
        fx.write_claude(self.claude, PROJECT, [rec])
        result = self.load()
        self.assertEqual((result.utterances, result.excluded, result.skipped), ([], 0, 0))

    def test_decides_project_by_first_record_with_cwd(self):
        fx.write_claude(self.claude, PROJECT, [
            {"type": "summary", "summary": "no cwd here"},
            fx.claude_user("最初", cwd="/proj"),
            fx.claude_user("移動後", cwd="/other", ts="2026-09-24T02:00:00Z"),
        ])
        self.assertEqual(self.texts(self.load()), ["最初", "移動後"])

    def test_counts_each_non_interactive_session(self):
        for i in range(2):
            fx.write_claude(self.claude, PROJECT, [fx.claude_user("ok", entrypoint="sdk-cli")], session=f"s{i}")
        self.assertEqual(self.load().excluded_sessions, 2)

    def test_sums_injected_texts_across_sessions(self):
        for i in range(2):
            fx.write_claude(self.claude, PROJECT, [fx.claude_user("<system-reminder>x</system-reminder>")], session=f"s{i}")
        self.assertEqual(self.load().excluded, 2)

    def test_counts_each_bad_timestamp(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("a", ts="x"), fx.claude_user("b", ts="y")])
        self.assertEqual(self.load().skipped, 2)

    def test_counts_undecodable_line_as_skipped(self):
        path = fx.write_claude(self.claude, PROJECT, [fx.claude_user("読める")])
        with open(path, "ab") as f:
            f.write(b"\xff\xfe broken\n")
        result = self.load()
        self.assertEqual(self.texts(result), ["読める"])
        self.assertEqual(result.skipped, 1)

    def test_reads_records_after_blank_line(self):
        later = json.dumps(fx.claude_user("後", ts="2026-09-24T03:00:00Z"), ensure_ascii=False)
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("前")], raw_lines=["", later])
        self.assertEqual(self.texts(self.load()), ["前", "後"])

    def test_counts_message_that_is_not_an_object_as_skipped(self):
        rec = fx.claude_user("x")
        rec["message"] = "oops"
        fx.write_claude(self.claude, PROJECT, [rec, fx.claude_user("残る")])
        result = self.load()
        self.assertEqual((self.texts(result), result.skipped), (["残る"], 1))

    def test_counts_timestamp_without_offset_as_skipped(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("オフセットなし", ts="2026-09-24T01:06:30"),
                                               fx.claude_user("残る")])
        result = self.load()
        self.assertEqual((self.texts(result), result.skipped), (["残る"], 1))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can read files with mode 000")
    def test_counts_unreadable_file_as_skipped(self):
        path = fx.write_claude(self.claude, PROJECT, [fx.claude_user("読めない")], session="locked")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("読める")])
        result = self.load()
        self.assertEqual((self.texts(result), result.skipped), (["読める"], 1))

    def test_keeps_human_text_that_starts_with_caveat(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("Caveat: this API is flaky, retry twice")])
        self.assertEqual(self.texts(self.load()), ["Caveat: this API is flaky, retry twice"])

    def test_load_keeps_every_copy_of_a_forked_record(self):
        rec = fx.claude_user("複製", uuid="11111111-2222-3333-4444-555555555555")
        fx.write_claude(self.claude, PROJECT, [rec], session="b-copy")
        fx.write_claude(self.claude, PROJECT, [rec], session="a-copy")
        self.assertEqual([u.session for u in self.load().utterances], ["a-copy", "b-copy"])

    def test_dedup_keeps_one_copy_of_a_forked_record(self):
        rec = fx.claude_user("複製", uuid="11111111-2222-3333-4444-555555555555")
        fx.write_claude(self.claude, PROJECT, [rec], session="b-copy")
        fx.write_claude(self.claude, PROJECT, [rec], session="a-copy")
        self.assertEqual([u.session for u in history.dedup(self.load().utterances)], ["a-copy"])

    def test_does_not_count_broken_lines_of_a_neighbouring_project(self):
        fx.write_claude(self.claude, "/proj-x", [fx.claude_user("隣", cwd="/proj-x")], raw_lines=["{", "{"])
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("自分")], session="own")
        result = self.load()
        self.assertEqual((self.texts(result), result.skipped), (["自分"], 0))

    def test_counts_human_records_of_a_file_without_cwd_as_skipped(self):
        rec = fx.claude_user("どこで打ったか分からない")
        del rec["cwd"]
        fx.write_claude(self.claude, PROJECT, [rec])
        result = self.load()
        self.assertEqual((result.utterances, result.skipped), ([], 1))

    def test_ignores_file_without_cwd_outside_the_projects_own_directory(self):
        rec = fx.claude_user("どこの発話か分からない")
        del rec["cwd"]
        fx.write_claude(self.claude, "/proj/sub", [rec], raw_lines=["{"])
        result = self.load()
        self.assertEqual((result.utterances, result.skipped), ([], 0))

    def test_lists_subdirectory_search_pattern(self):
        self.assertIn(str(self.claude / "projects" / "-proj-*"), self.load().searched)

    def test_reads_sessions_below_the_root_directory(self):
        fx.write_claude(self.claude, "/proj/sub", [fx.claude_user("ルート配下", cwd="/proj/sub")])
        self.assertEqual(self.texts(self.load(project="/")), ["ルート配下"])

    def test_lists_search_pattern_for_shortened_names(self):
        project = "/" + "a" * 250
        expected = self.claude / "projects" / (history.encode_project(project)[:200] + "-*")
        self.assertIn(str(expected), self.load(project=project).searched)

    def test_keeps_same_text_from_records_with_different_uuids(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("同じ", uuid="u1")], session="s1")
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("同じ", uuid="u2")], session="s2")
        self.assertEqual(self.texts(self.load()), ["同じ", "同じ"])

    def test_ignores_uuid_that_is_not_a_string(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("一つ目", uuid=7), fx.claude_user("二つ目", uuid=7)])
        self.assertEqual([u.text for u in history.dedup(self.load().utterances)], ["一つ目", "二つ目"])

    def test_dedup_keeps_distinct_codex_utterances(self):
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("一つ目"), fx.codex_user("二つ目")])
        self.assertEqual([u.text for u in history.dedup(self.load().utterances)], ["一つ目", "二つ目"])

    def test_sums_skipped_records_across_files(self):
        for i in range(2):
            fx.write_claude(self.claude, PROJECT, [fx.claude_user(f"発話{i}")], session=f"s{i}", raw_lines=["{"])
        self.assertEqual(self.load().skipped, 2)

    def test_sums_skipped_records_of_files_without_cwd(self):
        for i in range(2):
            rec = fx.claude_user(f"cwdなし{i}")
            del rec["cwd"]
            fx.write_claude(self.claude, PROJECT, [rec], session=f"s{i}")
        self.assertEqual(self.load().skipped, 2)

    def test_reads_subdirectory_sessions_of_a_project_ending_in_x(self):
        fx.write_claude(self.claude, "/projX/sub", [fx.claude_user("配下", cwd="/projX/sub")])
        self.assertEqual(self.texts(self.load(project="/projX")), ["配下"])

    def test_reads_project_whose_directory_name_is_exactly_at_the_limit(self):
        project = "/" + "a" * 199
        self.assertEqual(len(history.encode_project(project)), history.CLAUDE_NAME_LIMIT)
        fx.write_claude(self.claude, project, [fx.claude_user("ちょうど200字", cwd=project)])
        result = self.load(project=project)
        self.assertEqual(self.texts(result), ["ちょうど200字"])
        self.assertIn(str(self.claude / "projects" / history.encode_project(project)), result.searched)

    def test_reads_sessions_started_in_a_subdirectory(self):
        fx.write_claude(self.claude, "/proj/sub", [fx.claude_user("配下で起動", cwd="/proj/sub")])
        fx.write_claude(self.claude, "/proj-x", [fx.claude_user("隣のプロジェクト", cwd="/proj-x")], session="sib")
        self.assertEqual(self.texts(self.load()), ["配下で起動"])

    def test_reads_project_whose_directory_name_was_shortened(self):
        project = "/" + "a" * 250
        name = history.encode_project(project)[:200] + "-1a2b3c"
        path = self.claude / "projects" / name / f"{fx.CLAUDE_SESSION}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(fx.claude_user("長いパス", cwd=project)) + "\n", encoding="utf-8")
        self.assertEqual(self.texts(self.load(project=project)), ["長いパス"])

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

    def test_includes_sdk_session_when_asked(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("手で打った claude -p", entrypoint="sdk-cli")])
        result = history.load(PROJECT, ("claude",), claude_home=self.claude, codex_home=self.codex,
                              include_non_interactive=True)
        self.assertEqual(self.texts(result), ["手で打った claude -p"])
        self.assertEqual(result.excluded_sessions, 0)

    def test_keeps_records_without_entrypoint(self):
        rec = fx.claude_user("古い版の記録")
        del rec["entrypoint"]
        fx.write_claude(self.claude, PROJECT, [rec])
        self.assertEqual(self.texts(self.load()), ["古い版の記録"])

    def test_skips_session_whose_cwd_is_another_path_with_same_encoding(self):
        # /a/b_c and /a/b-c map to the same directory name
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

    def test_ignores_developer_messages_and_non_response_records(self):
        developer = fx.codex_user("developer の指示")
        developer["payload"]["role"] = "developer"
        event = fx.codex_user("event の記録")
        event["type"] = "event_msg"
        fx.write_codex(self.codex, [fx.codex_meta(), developer, event, fx.codex_user("人の発話")])
        self.assertEqual(self.texts(self.load()), ["人の発話"])

    def test_reads_only_input_text_items(self):
        rec = fx.codex_user("本文")
        rec["payload"]["content"].append({"type": "input_image", "text": "代替テキスト"})
        rec["payload"]["content"].append({"type": "input_text"})
        fx.write_codex(self.codex, [fx.codex_meta(), rec])
        result = self.load()
        self.assertEqual(self.texts(result), ["本文"])
        self.assertEqual(result.skipped, 0)

    def test_counts_empty_file_as_skipped(self):
        fx.write_codex(self.codex, [])
        self.assertEqual(self.load().skipped, 1)

    def test_counts_session_meta_without_id_as_skipped(self):
        meta = fx.codex_meta()
        del meta["payload"]["id"]
        fx.write_codex(self.codex, [meta, fx.codex_user("idなし")])
        result = self.load()
        self.assertEqual((result.utterances, result.skipped), ([], 1))

    def test_counts_each_broken_file(self):
        for i in range(2):
            fx.write_codex(self.codex, [fx.codex_user("metaなし")], session=f"s{i}")
        self.assertEqual(self.load().skipped, 2)

    def test_keeps_reading_after_broken_line(self):
        later = json.dumps(fx.codex_user("後", ts="2026-09-24T03:00:00Z"), ensure_ascii=False)
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("前")], raw_lines=["{", later])
        result = self.load()
        self.assertEqual(self.texts(result), ["前", "後"])
        self.assertEqual(result.skipped, 1)

    def test_counts_records_whose_payload_is_not_an_object_as_skipped(self):
        meta = fx.codex_meta()
        meta["payload"] = "x"
        fx.write_codex(self.codex, [meta, fx.codex_user("metaが壊れている")], session="m" * 36)
        fx.write_codex(self.codex, [fx.codex_meta(), {"type": "response_item", "payload": ["x"]}, fx.codex_user("残る")])
        result = self.load()
        self.assertEqual((self.texts(result), result.skipped), (["残る"], 2))

    def test_skips_session_of_other_project(self):
        fx.write_codex(self.codex, [fx.codex_meta(cwd="/other"), fx.codex_user("別")])
        self.assertEqual(self.load().utterances, [])

    def test_excludes_exec_and_subagent_sessions(self):
        fx.write_codex(self.codex, [fx.codex_meta(source="exec"), fx.codex_user("agent")], session="a" * 36)
        fx.write_codex(self.codex, [fx.codex_meta(source={"subagent": {"other": "x"}}), fx.codex_user("sub")], session="b" * 36)
        result = self.load()
        self.assertEqual(result.utterances, [])
        self.assertEqual(result.excluded_sessions, 2)

    def test_includes_exec_and_subagent_sessions_when_asked(self):
        fx.write_codex(self.codex, [fx.codex_meta(source="exec", session="a" * 36), fx.codex_user("手で打った exec")],
                       session="a" * 36)
        fx.write_codex(self.codex, [fx.codex_meta(source={"subagent": {}}, session="b" * 36), fx.codex_user("sub")],
                       session="b" * 36)
        result = history.load(PROJECT, ("codex",), claude_home=self.claude, codex_home=self.codex,
                              include_non_interactive=True)
        self.assertEqual(sorted(self.texts(result)), ["sub", "手で打った exec"])
        self.assertEqual({u.session for u in result.utterances}, {"a" * 36, "b" * 36})

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
        self.assertEqual([u.text for u in history.dedup(self.load().utterances)], ["同じ", "同じ"])

    def test_merges_duplicates_with_fractional_seconds(self):
        fx.write_claude(self.claude, PROJECT, [
            fx.claude_user("同じ", ts="2026-09-24T01:05:01.250Z"),
            fx.claude_user("同じ", ts="2026-09-24T01:05:59.750Z"),
        ])
        self.assertEqual([u.text for u in history.dedup(self.load().utterances)], ["同じ"])

    def test_load_or_report_reads_only_requested_sources_and_reports_paths(self):
        fx.write_claude(self.claude, PROJECT, [fx.claude_user("claude だけ")])
        stderr = io.StringIO()
        env = {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)}
        with mock.patch.dict(os.environ, env), redirect_stderr(stderr):
            self.assertIsNone(history.load_or_report(PROJECT, ("codex",), False))
        self.assertEqual(stderr.getvalue(), f"no utterances found. searched:\n  {self.codex / 'sessions'}\n")

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

    def test_parse_sources_accepts_subset_with_spaces(self):
        self.assertEqual(history.parse_sources(" codex , claude "), ("codex", "claude"))

    def test_parse_sources_rejects_unknown_or_empty(self):
        for value in ("cursor", "claude,cursor", "", " , "):
            with self.assertRaisesRegex(ValueError, "^source must be a subset of claude,codex$"):
                history.parse_sources(value)

    def test_minute_key_counts_whole_minutes_since_epoch(self):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(history.minute_key(epoch + timedelta(seconds=59.9)), 0)
        self.assertEqual(history.minute_key(epoch + timedelta(minutes=1)), 1)
        self.assertEqual(history.minute_key(datetime(1970, 1, 1, 9, 1, tzinfo=timezone(timedelta(hours=9)))), 1)

    def test_is_under_handles_root_and_sibling_prefix(self):
        self.assertTrue(history.is_under("/proj", "/"))
        self.assertTrue(history.is_under("/proj/sub", "/proj/"))
        self.assertFalse(history.is_under("/projX", "/proj"))

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
            with self.assertRaisesRegex(ValueError, f"^unknown time zone: {name}$"):
                history.resolve_tz(name)


if __name__ == "__main__":
    unittest.main()
