# auto-researcher

AutoResearcher v2 — AI-powered autonomous code improvement system using Claude Code (claude-sonnet-4-6) and Codex (gpt-5.4).

Runs daily via GitHub Actions. Picks your top-scored owned repos, implements a meaningful improvement on each (in parallel), and opens pull requests for your review.

## How it works

1. **Inbox check** — reads GitHub Issues with label `ar-inbox` for commands
2. **Repo scoring** — ranks your owned repos using a 5-factor formula
3. **Implementation** — Claude Code improves Repo 1, Codex improves Repo 2 (in parallel)
4. **PR creation** — opens PRs for review (never auto-merges)
5. **Reporting** — posts results to Slack and creates a summary GitHub Issue

## Repo scoring formula

```
recency    = max(0, 35 - days_since_push × 0.25)   # 0–35 pts
issues     = min(20, open_issues_count × 5)          # 0–20 pts
complexity = min(15, size_kb / 2000)                 # 0–15 pts
stars      = min(15, stargazers_count × 3)           # 0–15 pts
language   = 10 if Python/TS/JS/Go/Rust else 3       # 0–10 pts
```

Repos processed in the last 7 days are skipped (cooldown window).
Repos with "assignment/homework/graded" in the README are skipped.

## Inbox commands

Create a GitHub Issue on **this repo** with label `ar-inbox`. Write commands in the body:

| Command | Effect |
|---------|--------|
| `focus: <repo>` | Boost a repo's score for next run |
| `skip: <repo>` | Exclude a repo from next run |
| `pause` | Skip the next scheduled run |
| `idea: <text>` | Queue an idea for future runs (logged to QUEUE.md) |

Issues are automatically closed after processing.

## Setup

### 1. Repository secrets

| Secret | Value |
|--------|-------|
| `AR_GITHUB_TOKEN` | Personal access token with `repo` + `issues` scope |
| `AR_SLACK_WEBHOOK` | Slack incoming webhook URL |

### 2. GitHub Actions

The workflow runs automatically at 08:00 IST (02:30 UTC) daily.
Trigger manually via **Actions → AutoResearcher → Run workflow**.

### 3. Environment variables

```bash
export GITHUB_TOKEN="your_pat"
export SLACK_WEBHOOK_URL="https://hooks.slack.com/..."
export GITHUB_USERNAME="sriv144"   # default
```

### 4. Run locally

```bash
python researcher.py
```

### 5. Run tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## QUEUE.md

Tracks which repos were processed and when. Repos done in the last 7 days are automatically skipped. After all repos have been processed, the cycle resets.

## Architecture

```
researcher.py
├── read_inbox()          — GitHub Issues → parsed commands
├── score_repo()          — 5-factor scoring formula
├── get_owned_repos()     — fetch + filter + score your repos
├── read_queue()          — parse QUEUE.md (with timestamps)
├── write_queue()         — update QUEUE.md
├── run_claude_on_repo()  — Claude Code implementation thread
├── run_codex_on_repo()   — Codex implementation thread
├── push_and_pr()         — git push + create PR
└── main()                — orchestration
```
