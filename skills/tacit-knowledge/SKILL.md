---
name: tacit-knowledge
description: Extracts a developer's own judgment criteria, preferences, recurring procedures, and past mistakes from their Claude Code and Codex conversation history, and backs every item with a dated quote that a bundled script verifies against the history. Use when the user wants to mine their agent history for tacit or domain knowledge.
when_to_use: 'Triggers: "extract my judgment criteria from my history", "what do I keep telling the agent", "make my tacit knowledge explicit", "履歴から暗黙知を抽出", "判断基準を明文化", "作業履歴から私のこだわりをまとめて". Not for summarizing a single session or for analyzing the assistant''s own replies.'
---

# Tacit knowledge from agent history

The user has already taught their agents what they know, one correction at a time. This skill turns those prompts into a document where every claim points to the exact words the user typed, so the user can tell what came from the history and what an AI added.

The scripts live in `scripts/` next to this file. They need only Python 3.9 or later; nothing else is installed or imported.

## What you produce

One Markdown file with four sections. Write the headings in the user's language. In English they are:

- Recurring judgment criteria
- Preferences and points the user insists on
- Frequently used procedures
- Past mistakes and corrections to watch for

Under each heading, every item is one top-level `- ` bullet with a one-sentence claim, followed by one or more evidence lines:

```markdown
- Adoption is decided by measurement in the user's own environment, not by what an article claims.
  > 2026-03-02 14:10+00:00 3f2a9c1e-7b4「only adopt what we measured here, not what the blog post claims」
  > 2026-04-18 09:45+00:00 b81d0e57-2ac「run the benchmark on this machine before we switch」
```

Copy the time and the session prefix from the `===` header of the utterance in the prompt files, and copy the quote as an exact substring of that utterance on one line. `verify.py` treats every top-level `- ` bullet as a claim, so put notes in paragraphs, not bullets.

## Procedure

1. Confirm two things with the user: the project directory (default: the current directory) and an output directory. The output contains the user's raw prompts, which may include secrets, so it must stay outside any repository and must not be committed or shared.
2. Run the extractor and show the user its summary lines:

   ```sh
   python3 <this skill>/scripts/extract.py --out <output dir> --project <project dir>
   ```

   Options: `--source claude` or `--source codex` to read one tool, `--tz Asia/Tokyo` to change the time zone, `--chunk-chars N` to change the file size (default 20000). Non-interactive sessions are skipped by default; if the user types prompts into `claude -p` or `codex exec` by hand, add `--include-non-interactive` here and in step 6. Exit code 2 means no history was found; show the searched paths it printed and ask the user for the right project directory.
3. Read every `prompts-NNN.txt` completely, in order. For each file, write down candidate items with their evidence lines before moving on. Do not skim; a criterion often appears once per file.
4. Merge the candidates into the four sections. Combine items that say the same thing and keep their evidence lines together.
5. Write the document to `<output dir>/tacit-knowledge.md` unless the user named another path.
6. Run the verifier:

   ```sh
   python3 <this skill>/scripts/verify.py <document> --project <project dir>
   ```

   Exit code 1 lists each failing line. Fix it by copying the time, session prefix, or quote again from the prompt file, or by deleting the item. Never edit the scripts or loosen the format to make a line pass. Run the verifier again until it exits 0.
7. Report the document path, the number of items per section, and the verifier result. Tell the user plainly that the verifier proves each quote exists at that time in that session, and does not prove that the claim above it means the same thing as the quote. Ask the user to compare claims and quotes before relying on them.

## Extraction rules

- Use only the user's own words. Pasted articles, posts, logs, error output, and AI answers the user pasted back are not the user's judgment. Quote the user's words around them instead, or skip the item.
- A question stays a question. "Shouldn't we fix it upstream?" becomes "Asks whether the fix belongs upstream", not "Fixes problems upstream".
- A request is not an outcome. Write "Asked to revert the change" unless the user's own words report the result.
- Add nothing the quotes do not say. When unsure, leave the item out.
- An item under "Recurring judgment criteria" needs evidence from at least two different sessions. Items in the other sections need at least one evidence line.

## What the extractor leaves out

- Text that the tools inject into the user turn, such as system reminders, command output, task notifications, and Codex environment context.
- Non-interactive sessions: `claude -p` and SDK runs, `codex exec`, and subagents, unless `--include-non-interactive` is given. Another agent usually wrote those prompts.
- History that Claude Code has already deleted. It keeps transcripts for `cleanupPeriodDays` days (30 by default), so older work is gone unless the user raised that setting.
