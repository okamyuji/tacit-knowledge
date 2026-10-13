# tacit-knowledge

[日本語](README.ja.md)

A skill for Claude Code and Codex that turns your own conversation history into a written list of your judgment criteria, preferences, recurring procedures, and past mistakes. Every item quotes the exact words you typed, with the time and session, and a bundled script checks that each quote really exists in your history.

Corrections you give an agent ("check the official docs first", "don't merge before CI passes") are domain knowledge you have already put into words. They are scattered across sessions. This skill collects them and keeps the AI's summary separate from your words, so you can see which lines came from the history and which ones the AI added.

## How it works

1. `extract.py` reads the history of one project, keeps only what a person typed in interactive sessions, and writes it into text files with a header per utterance.
2. The agent reads those files and writes a Markdown document. Each claim is followed by evidence lines that quote you.
3. `verify.py` checks every evidence line against the history and fails on quotes that are not there and on claims without evidence.

An evidence line looks like this:

```markdown
- Adoption is decided by measurement in this environment, not by what an article claims.
  > 2026-03-02 14:10+00:00 3f2a9c1e-7b4「only adopt what we measured here, not what the blog post claims」
```

The scripts use only the Python standard library and do not call any model. The agent you are already using writes the summary.

## Requirements

- Python 3.9 or later
- Claude Code, Codex, or both

## Install

### Claude Code

```sh
claude plugin marketplace add okamyuji/tacit-knowledge
claude plugin install tacit-knowledge@tacit-knowledge
```

The plugin contains this one skill and no hooks.

### Codex

Codex loads user skills from `~/.agents/skills/<name>/SKILL.md` ([Codex skills](https://developers.openai.com/codex/skills)).

```sh
git clone https://github.com/okamyuji/tacit-knowledge.git
mkdir -p ~/.agents/skills
cp -R tacit-knowledge/skills/tacit-knowledge ~/.agents/skills/
```

## Usage

Open the project whose history you want to read and ask the agent, for example:

> Extract my judgment criteria, preferences, and procedures from this project's history.

The agent asks for an output directory, runs the extractor, writes the document, and runs the verifier until every line passes.

You can also run the scripts yourself:

```sh
python3 skills/tacit-knowledge/scripts/extract.py --out ~/notes/tacit --project ~/work/my-app
python3 skills/tacit-knowledge/scripts/verify.py ~/notes/tacit/tacit-knowledge.md --project ~/work/my-app
```

| Option | Meaning |
|---|---|
| `--project PATH` | Project directory. Defaults to the current directory. Sessions started in its subdirectories are included, so your home directory covers every project. |
| `--source claude,codex` | Which history to read. Defaults to both. |
| `--tz ZONE` | IANA time zone for the headers, such as `Asia/Tokyo`. Defaults to the local zone. |
| `--chunk-chars N` | Maximum characters per output file of `extract.py`. Defaults to 20000. |
| `--include-non-interactive` | Also read non-interactive sessions (see below). Pass it to both scripts. |

Exit codes are 0 for success, 1 when `verify.py` finds an evidence line that is missing from the history or malformed, a claim without evidence, or no evidence lines at all, and 2 when no history is found, an input cannot be read, the output cannot be written, or an argument is wrong.

## What is left out

- Text that the tools inject into the user turn, such as system reminders, command output, and Codex environment context.
- Non-interactive sessions: `claude -p` and SDK runs, `codex exec`, and subagents. Another agent usually wrote those prompts. If you type prompts into `claude -p` or `codex exec` yourself, pass `--include-non-interactive` to both `extract.py` and `verify.py`.
- History that Claude Code has already deleted. It keeps transcripts for `cleanupPeriodDays` days, 30 by default ([`cleanupPeriodDays`](https://code.claude.com/docs/en/settings-reference#cleanupperioddays)). Raise it in `~/.claude/settings.json` to keep more, for example `"cleanupPeriodDays": 365`.

## Limits

`verify.py` proves that each quote exists at that time in that session. It does not prove that the claim above the quote means the same thing. Read the claims next to their quotes before you rely on them.

## Privacy

The extracted files and the document contain your raw prompts, which may include secrets or customer data. The extractor writes files with mode `0600` and creates new output directories with mode `0700`. Keep the output outside any repository and do not share it.

## Development

```sh
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
