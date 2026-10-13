"""extract.py から verify.py までを、利用者と同じく CLI として通しで実行する。"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import fixtures as fx

HEADER = re.compile(r"^=== (\S+ \S+) (claude|codex) (\S+) \(\d+\)$")


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.root / "claude"), CODEX_HOME=str(self.root / "codex"))

    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(fx.SCRIPTS / name), *args], env=self.env,
                              cwd=self.root, capture_output=True, text=True)

    def write_history(self):
        fx.write_claude(self.root / "claude", "/proj", [
            fx.claude_user("事実は公式ドキュメントで確認してください", ts="2026-09-24T01:05:30Z"),
        ])
        fx.write_codex(self.root / "codex", [
            fx.codex_meta(), fx.codex_user("結論から提示してください", ts="2026-09-25T02:00:10Z"),
        ])

    def evidence_lines(self):
        lines = (self.out / "prompts-001.txt").read_text(encoding="utf-8").splitlines()
        found = []
        for header, text in zip(lines, lines[1:]):
            m = HEADER.match(header)
            if m:
                found.append(f"  > {m.group(1)} {m.group(3)}「{text[2:10]}」")
        return found

    def test_extracted_quotes_pass_verification_and_fakes_fail(self):
        self.write_history()
        extracted = self.run_script("extract.py", "--out", str(self.out), "--project", "/proj", "--tz", "America/New_York")
        self.assertEqual(extracted.returncode, 0, extracted.stderr)
        self.assertIn("utterances: 2 (claude 1, codex 1)", extracted.stdout)

        doc = self.root / "tacit-knowledge.md"
        body = "## 繰り返し出てくる判断基準\n\n- 事実と結論の出し方にこだわります。\n" + "\n".join(self.evidence_lines()) + "\n"
        doc.write_text(body, encoding="utf-8")
        verified = self.run_script("verify.py", str(doc), "--project", "/proj")
        self.assertEqual(verified.returncode, 0, verified.stdout)
        self.assertIn("evidence: 2, verified: 2", verified.stdout)

        doc.write_text(body + "  > 2026-09-23 21:05-04:00 45c391e1-2c4「書いていない文」\n", encoding="utf-8")
        self.assertEqual(self.run_script("verify.py", str(doc), "--project", "/proj").returncode, 1)

    def test_both_commands_exit_2_without_history(self):
        doc = self.root / "doc.md"
        doc.write_text("- 主張\n", encoding="utf-8")
        self.assertEqual(self.run_script("extract.py", "--out", str(self.out), "--project", "/proj").returncode, 2)
        self.assertEqual(self.run_script("verify.py", str(doc), "--project", "/proj").returncode, 2)


if __name__ == "__main__":
    unittest.main()
