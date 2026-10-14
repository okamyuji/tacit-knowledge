import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import fixtures as fx
import verify
from history import Utterance

SID = "45c391e1-2c4"
GOOD = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「公式ドキュメントで確認してください」"


def utterance(text, ts=datetime(2026, 9, 24, 1, 5, 30, tzinfo=timezone.utc), session=fx.CLAUDE_SESSION):
    return Utterance(ts, "claude", session, text)


class ParseEvidenceTest(unittest.TestCase):
    def test_parses_time_as_minute_key_session_and_quote(self):
        minute = int(datetime(2026, 9, 24, 1, 5, tzinfo=timezone.utc).timestamp()) // 60
        self.assertEqual(verify.parse_evidence(GOOD), (minute, SID, "公式ドキュメントで確認してください"))

    def test_keeps_inner_closing_bracket_in_quote(self):
        line = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「「割れる」って直訳ですよね」"
        self.assertEqual(verify.parse_evidence(line)[2], "「割れる」って直訳ですよね")

    def test_ignores_trailing_spaces_and_crlf(self):
        self.assertEqual(verify.parse_evidence(GOOD + "  \r\n"), verify.parse_evidence(GOOD))

    def test_returns_none_for_other_lines(self):
        for line in ("- 主張", "  > 引用だけ", "2026-09-24 10:05+09:00 45c391e1-2c4「行頭に > がない」",
                     "  > 2026-13-40 10:05+09:00 45c391e1-2c4「存在しない日付」"):
            self.assertIsNone(verify.parse_evidence(line), line)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.index = verify.build_index([utterance("公式ドキュメントで確認してください")])

    def run_verify(self, text):
        return verify.verify(text.splitlines(keepends=True), self.index)

    def test_accepts_evidence_that_exists(self):
        self.assertEqual(self.run_verify(f"- 主張\n{GOOD}\n"), ([], [], []))

    def test_reports_quoted_lines_that_are_not_valid_evidence(self):
        broken = ["  > 2026-09-24 10:05+09:00 45c391e1-2c4 「空白」", "  > 2026-09-24 10:05 45c391e1-2c4「オフセットなし」",
                  "  > 2026-09-24 10:05+09:00 45C391E1-2C4「大文字」", "> メモ"]
        result = self.run_verify("- 主張\n" + "\n".join(broken) + "\n")
        self.assertEqual(result, ([], [], [(i + 2, line) for i, line in enumerate(broken)]))

    def test_accepts_same_instant_written_in_other_offset(self):
        line = "  > 2026-09-23 20:05-05:00 45c391e1-2c4「公式ドキュメントで確認してください」"
        self.assertEqual(self.run_verify(f"- 主張\n{line}\n"), ([], [], []))

    def test_rejects_quote_not_in_utterance(self):
        line = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「公式ドキュメントは見なくてよい」"
        self.assertEqual(self.run_verify(f"- 主張\n{line}\n"), ([(2, line)], [], []))

    def test_rejects_shifted_time(self):
        line = "  > 2026-09-24 01:05+09:00 45c391e1-2c4「公式ドキュメントで確認してください」"
        self.assertEqual(self.run_verify(f"- 主張\n{line}\n")[0], [(2, line)])

    def test_rejects_other_session(self):
        line = "  > 2026-09-24 10:05+09:00 deadbeef-000「公式ドキュメントで確認してください」"
        self.assertEqual(self.run_verify(f"- 主張\n{line}\n")[0], [(2, line)])

    def test_reports_claims_without_evidence_including_last_one(self):
        text = f"- 根拠なし1\n- 根拠あり\n{GOOD}\n- 根拠なし2\n"
        self.assertEqual(self.run_verify(text)[1], [(1, "- 根拠なし1"), (4, "- 根拠なし2")])

    def test_matches_utterance_with_fractional_seconds(self):
        index = verify.build_index([utterance("小数秒", ts=datetime(2026, 9, 24, 1, 5, 30, 500000, tzinfo=timezone.utc))])
        line = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「小数秒」"
        self.assertEqual(verify.verify([f"- 主張\n", line], index), ([], [], []))

    def test_accepts_evidence_after_blank_line(self):
        self.assertEqual(self.run_verify(f"- 主張\n\n{GOOD}\n"), ([], [], []))


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        self.codex = self.root / "codex"
        self.doc = self.root / "doc.md"
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude), "CODEX_HOME": str(self.codex)})
        env.start()
        self.addCleanup(env.stop)

    def write_history(self):
        fx.write_claude(self.claude, "/proj", [fx.claude_user("公式ドキュメントで確認してください")])
        fx.write_codex(self.codex, [fx.codex_meta(), fx.codex_user("このまま進めてください", ts="2026-09-25T02:00:10Z")])

    def run_main(self, text):
        self.doc.write_text(text, encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = verify.main([str(self.doc), "--project", "/proj"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_expands_tilde_in_document_path(self):
        self.write_history()
        self.doc.write_text(f"- 主張\n{GOOD}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main(["~/doc.md", "--project", "/proj"]), 0)

    def test_returns_0_when_all_evidence_verifies_across_tools(self):
        self.write_history()
        codex_line = "  > 2026-09-25 11:00+09:00 01a12ca3-2e3「このまま進めてください」"
        code, stdout, _ = self.run_main(f"- 主張\n{GOOD}\n{codex_line}\n")
        self.assertEqual(code, 0)
        self.assertIn("evidence: 2, verified: 2, malformed: 0, claims without evidence: 0", stdout)

    def test_returns_1_and_prints_failing_lines(self):
        self.write_history()
        bad = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「偽の引用」"
        code, stdout, _ = self.run_main(f"- 主張\n{bad}\n- 根拠なし\n")
        self.assertEqual(code, 1)
        self.assertIn("NG evidence not found in history (line 2)", stdout)
        self.assertIn("NG claim without evidence (line 3)", stdout)

    def test_returns_1_and_prints_malformed_evidence(self):
        self.write_history()
        code, stdout, _ = self.run_main(f"- 主張\n{GOOD}\n  > 2026-09-24 10:05 45c391e1-2c4「オフセットなし」\n")
        self.assertEqual(code, 1)
        self.assertIn("NG malformed evidence line (line 3)", stdout)
        self.assertIn("evidence: 1, verified: 1, malformed: 1, claims without evidence: 0", stdout)

    def test_verifies_quotes_from_non_interactive_sessions_only_when_asked(self):
        fx.write_codex(self.codex, [fx.codex_meta(source="exec"), fx.codex_user("手で打った exec の指示")])
        line = "  > 2026-09-24 10:05+09:00 01a12ca3-2e3「手で打った exec の指示」"
        self.doc.write_text(f"- 主張\n{line}\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(verify.main([str(self.doc), "--project", "/proj"]), 2)
            self.assertEqual(verify.main([str(self.doc), "--project", "/proj", "--include-non-interactive"]), 0)

    def test_verifies_evidence_from_either_copy_of_a_forked_record(self):
        rec = fx.claude_user("どちらの複製でも通る", uuid="11111111-2222-3333-4444-555555555555")
        fx.write_claude(self.claude, "/proj", [rec], session="bbbbbbbb-0000")
        fx.write_claude(self.claude, "/proj", [rec], session="aaaaaaaa-0000")
        line = "  > 2026-09-24 10:05+09:00 bbbbbbbb-000「どちらの複製でも通る」"
        code, stdout, _ = self.run_main(f"- 主張\n{line}\n")
        self.assertEqual(code, 0, stdout)

    def test_prints_malformed_lines_when_no_line_is_valid_evidence(self):
        self.write_history()
        code, stdout, _ = self.run_main("- 主張\n  > 2026-09-24 10:05 45c391e1-2c4「オフセットなし」\n")
        self.assertEqual(code, 1)
        self.assertIn("NG malformed evidence line (line 2)", stdout)

    def test_returns_1_when_document_has_no_evidence_lines(self):
        self.write_history()
        code, stdout, _ = self.run_main("# 見出しだけの文書\n")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "no evidence lines found; check the line format in the docstring of verify.py\n")

    def test_returns_1_when_only_evidence_fails(self):
        self.write_history()
        bad = "  > 2026-09-24 10:05+09:00 45c391e1-2c4「偽の引用」"
        code, stdout, _ = self.run_main(f"- 主張\n{bad}\n")
        self.assertEqual(code, 1)
        self.assertIn("evidence: 1, verified: 0, malformed: 0, claims without evidence: 0", stdout)

    def test_returns_1_when_only_a_claim_lacks_evidence(self):
        self.write_history()
        code, _, _ = self.run_main(f"- 主張\n{GOOD}\n- 根拠なし\n")
        self.assertEqual(code, 1)

    def test_returns_2_when_document_is_not_utf8(self):
        self.write_history()
        self.doc.write_bytes(b"- \xff\xfe\n")
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = verify.main([str(self.doc), "--project", "/proj"])
        self.assertEqual(code, 2)
        self.assertIn(f"cannot read {self.doc}", stderr.getvalue())

    def test_help_describes_every_option(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stdout(stdout):
            verify.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(stdout.getvalue().split())
        for phrase in ("Verify evidence lines against agent history.", "tacit-knowledge document to check",
                       "project directory (default: current directory)", "comma-separated: claude,codex",
                       "also read claude -p, codex exec, and subagent sessions (usually written by another agent)"):
            self.assertRegex(text, rf"(^| ){re.escape(phrase)}( |$)")

    def test_uses_current_directory_as_default_project(self):
        project = self.root / "project"
        project.mkdir()
        real = os.path.realpath(project)
        fx.write_claude(self.claude, real, [fx.claude_user("公式ドキュメントで確認してください", cwd=real)])
        self.doc.write_text(f"- 主張\n{GOOD}\n", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, cwd)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(verify.main([str(self.doc)]), 0)

    def test_returns_2_when_history_is_missing(self):
        code, _, stderr = self.run_main(f"- 主張\n{GOOD}\n")
        self.assertEqual(code, 2)
        self.assertIn(str(self.codex / "sessions"), stderr)

    def test_returns_2_when_document_cannot_be_read(self):
        self.write_history()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = verify.main([str(self.root / "missing.md"), "--project", "/proj"])
        self.assertEqual(code, 2)
        self.assertIn("missing.md", stderr.getvalue())

    def test_rejects_unknown_source(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm, redirect_stderr(stderr):
            verify.main([str(self.doc), "--source", "cursor"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("error: source must be a subset of claude,codex\n", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
