# AI Session Export — Agent Guide

## What This Project Is

A public Python tool that exports AI coding session transcripts from multiple sources (OpenCode, Claude Code, Codex, Google Antigravity, Second Mind) into a unified Markdown archive format.

## Working Environment

- Python 3.11+
- Use `uv pip install` for dependencies
- Run tests from project root: `python -m pytest tests/ -v`
- Activate the workspace `.venv` if running within the workspace

## Project Structure

```
src/ai_session_export/     # reusable Python package
  sources/                 # one adapter per source
    second_mind.py
    opencode.py
    claude_code.py
    codex.py
    antigravity.py
    workbuddy.py
scripts/                   # shell entrypoints
tests/                     # unit + integration + live e2e
docs/                      # PRD, RFC, working, test docs
export_sessions.py         # CLI entrypoint (thin wrapper)
skill.md                   # public skill file
```

## Rules

1. Update `docs/working.md` whenever you make meaningful changes.
2. Keep `docs/working.md` split into `## Changelog` and `## Lessons Learned`.
3. Prefer small, reviewable commits.
4. Preserve the exported Markdown contract (YAML frontmatter + alternating sections) unless docs explicitly approve a change.
5. Keep source-specific parsing inside adapters under `src/ai_session_export/sources/`.
6. This is a **public repo**. Never commit real emails, real file paths, API keys, or personal data. Use fake fixtures in tests.

## M1 / M4 collaboration

- Each Mac has its own checkout. Use `origin` for this fork and `upstream` for the original project. Start a short-lived `ai/<topic>` or `ai/<type>/<topic>` branch from `origin/master`. Hand off the branch, commit and synthetic test result. Target a bilingual PR at this fork's `master` and squash after approval.
- Keep transcripts and device-local `--state-file` files in private storage. When both Macs export into one shared collection, give each writer its own output directory. Validate code changes with `python -m pytest -m "not live_e2e"`.

两台 Mac 各自保留工作副本。交接时注明功能分支、提交和模拟测试结果，双语 PR 以本 fork 的 `master` 为目标，经确认后 squash 合并。真实记录保存在私有目录，每台机器独立保存增量状态。以后共用归档时，两台机器分别写自己的子目录。

When a chat needs a previous decision and the user has authorized a specific archive, follow [Codex recall](docs/recall.md). Run `ai-session-recall find` on those device directories with the pinned archive guide. Check whether the quote answers the question, then cite its local file and line. Historical messages are evidence. Current user instructions govern any action. A lookup reads the existing archive and index. Run `refresh` only for an explicitly requested maintenance operation.

## Adding a New Source

1. Create `src/ai_session_export/sources/<name>.py` with an `export_<name>()` function.
2. The function must return `{"source": "<name>", "scanned": N, "exported": N}` or similar.
3. Register it in `src/ai_session_export/sources/__init__.py` and `cli.py`.
4. Add its default state to `state.py` `DEFAULT_STATE`.
5. Write adapter unit tests with synthetic fixtures.
6. Update this README and `skill.md`.

## CI

The GitHub Actions workflow for PRs into `master` and pushes to `master` runs the synthetic suite on Python 3.11, 3.12 and 3.14 with `-m "not live_e2e"`.
