# auto-researcher

**AutoResearcher v2** — AI-powered autonomous code improvement system using Claude Code and Codex. Intelligently ranks your own repos by value, reads GitHub inbox for guidance, assesses project completeness, and runs plan-driven implementation workflows.

## Features

### Owned Repo Ranking
Instead of analyzing starred repos from other people, AutoResearcher focuses on **your own repositories**. Each repo is scored on:
- **Recency** (35 pts): Repos updated recently score higher
- **Open Issues** (20 pts): Active projects with engagement score higher
- **Size/Complexity** (15 pts): Larger codebases indicate more opportunity
- **Stars** (15 pts): Community interest is a signal
- **Language Bonus** (10 pts): Python, TypeScript, JavaScript, Go, Rust get full bonus; others get 3 pts

Repos are automatically filtered:
- Homework/assignment repos (detected by README keywords) are skipped
- Trivial repos (<3 files) are skipped
- Archived repos are skipped
- Only repos updated in the last 180 days are considered

### GitHub Inbox (ar-inbox label)
Guide the next run by creating GitHub Issues on `{username}/auto-researcher` with label `ar-inbox`:

**Commands in issue title or body:**
- `focus: owner/repo` — Prioritize this repo at the top of the queue
- `skip: owner/repo` — Skip this repo in the next run
- `idea: <description>` — Log an idea for future runs
- `pause` — Pause AutoResearcher entirely (resume by closing the issue)

Example issue:
```
Title: focus: myuser/my-dashboard

This is a priority — it has important improvements needed.
```

### Completeness Assessment
Before running, AutoResearcher assesses each repo:
- Checks for README, tests, CI, TODOs, error handling, config files
- Scores 0-100; skips trivial or "complete" repos
- Tags each repo as: **mature** (≥75), **moderate** (50-75), **early_stage** (20-50), or **minimal** (<20)
- Skips homework repos and assignment solutions automatically

### Plan-Driven Implementation
Both Claude and Codex now follow a 3-step workflow:

1. **PLANNING** — Analyze the project and identify the single most valuable improvement
2. **IMPLEMENTATION** — Make substantial, user-visible changes (not trivial fixes)
3. **COMMIT & PUSH** — Create feature branch and push to origin

Improved command invocations:
- **Claude**: `claude --model claude-sonnet-4-6 --dangerously-skip-permissions -p <prompt>`
- **Codex**: `npx --yes @openai/codex exec --approval-mode full-auto --model gpt-4.1 <prompt>`

### Inbox Closure
After each run, all processed inbox issues are automatically closed so you can track what's been addressed.

## Setup

### GitHub Personal Access Token
Ensure `GITHUB_TOKEN` env var is set with a token that has `repo` and `issues` scopes.

```bash
export GITHUB_TOKEN="ghp_..."
```

### Slack Webhook (optional)
For status updates, set `SLACK_WEBHOOK_URL`:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

### GitHub Username
Customize the target user (defaults to `sriv144`):

```bash
export GITHUB_USERNAME="myuser"
```

### Create ar-inbox Label
On your `{username}/auto-researcher` repo, create a label called `ar-inbox` for inbox commands.

## Running

```bash
python researcher.py
```

## Queue File

`QUEUE.md` tracks:
- **Completed Repos** — Repos that have been processed
- **Queue** — Repos remaining to process (with score and completeness status)
- **Quota** — API call counts for Claude and Codex

Example:
```markdown
## Queue (Next Run)

- owner/dashboard — score: 87.5 (mature)
- owner/cli-tool — score: 72.3 (moderate)
- owner/utils — score: 45.2 (early_stage)
```

## How It Works

1. Fetch all your repos (type=owner) updated in last 180 days
2. Score each by value (recency, issues, size, stars, language)
3. Filter out homework, trivial, and archived repos
4. Read GitHub inbox for `ar-inbox` issues (focus, skip, pause commands)
5. Apply inbox directives (prioritize focused, remove skipped)
6. Assess completeness of top candidates
7. Run Claude and Codex in parallel (plan → implement → commit → push)
8. Close inbox issues and post results to Slack
9. Update QUEUE.md for next run

## Version History

### v2 (Current)
- Owned repo ranking with 5-factor scoring
- GitHub inbox for user direction
- Completeness assessment to skip homework/trivial repos
- Plan-first implementation prompts
- Feature branch creation and pushing
- Automatic inbox issue closure
- Improved Claude Code and Codex invocations

### v1
- Analyzed starred repos from other people
- No repo ranking or filtering
- Direct implementation without planning
- Manual queue management
