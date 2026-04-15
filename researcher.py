#!/usr/bin/env python3
"""
AutoResearcher v2 - Autonomous code improvement agent
Runs Claude Code and Codex in parallel on your top-scored owned GitHub repos.
Supports GitHub Issue inbox commands: focus:/skip:/pause/idea:
"""

import os
import json
import subprocess
import threading
import datetime
import urllib.request
import urllib.parse
import sys
import textwrap
import re
import logging

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("auto-researcher")

# ── Configuration ──────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "")
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "sriv144")
REPO_AGE_DAYS = 180         # Only process repos pushed within this window
QUEUE_COOLDOWN_DAYS = 7     # Skip repos processed in last N days
HOMEWORK_KEYWORDS = ["assignment", "homework", "graded", "course work"]

# Language tier for scoring
SCORED_LANGUAGES = {"Python", "TypeScript", "JavaScript", "Go", "Rust"}


# ── GitHub API helper ──────────────────────────────────────────────────────────

def gh_api(path: str, method: str = "GET", data: dict | None = None) -> dict | list:
    """Make an authenticated GitHub API request."""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, headers=headers, data=body, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        log.warning(f"GitHub API {method} {path} → {e.code}: {e.read().decode()[:200]}")
        return {}
    except Exception as e:
        log.warning(f"GitHub API error on {path}: {e}")
        return {}


def gh_raw_readme(full_name: str) -> str:
    """Fetch raw README content for homework detection."""
    url = f"https://api.github.com/repos/{full_name}/readme"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.raw"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.read().decode(errors="replace")[:3000]
    except Exception:
        return ""


# ── Slack helper ───────────────────────────────────────────────────────────────

def slack(msg: str) -> None:
    """Post a message to Slack. Silently skips if webhook is not configured."""
    if not SLACK_WEBHOOK:
        log.info(f"[Slack skipped] {msg[:80]}")
        return
    try:
        body = json.dumps({"text": msg}).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK, data=body, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req)
    except Exception as e:
        log.warning(f"Slack post failed: {e}")


# ── Inbox command system ───────────────────────────────────────────────────────

def read_inbox() -> dict:
    """
    Read GitHub Issues on the auto-researcher repo with label 'ar-inbox'.
    Returns parsed commands: {pause: bool, focus: [repos], skip: [repos], ideas: [texts], issue_numbers: [int]}
    """
    issues = gh_api(
        f"/repos/{GITHUB_USERNAME}/auto-researcher/issues?labels=ar-inbox&state=open&per_page=20"
    )
    result = {"pause": False, "focus": [], "skip": [], "ideas": [], "issue_numbers": []}

    if not isinstance(issues, list):
        return result

    for issue in issues:
        result["issue_numbers"].append(issue["number"])
        body = (issue.get("body") or "").strip()
        for line in body.splitlines():
            line = line.strip()
            lower = line.lower()
            if lower == "pause":
                result["pause"] = True
            elif lower.startswith("focus:"):
                repo = line[6:].strip()
                if repo:
                    result["focus"].append(repo)
            elif lower.startswith("skip:"):
                repo = line[5:].strip()
                if repo:
                    result["skip"].append(repo)
            elif lower.startswith("idea:"):
                text = line[5:].strip()
                if text:
                    result["ideas"].append(text)

    log.info(f"Inbox: pause={result['pause']}, focus={result['focus']}, skip={result['skip']}, ideas={len(result['ideas'])}")
    return result


def close_inbox_issue(issue_number: int, pr_urls: list[str]) -> None:
    """Comment on and close a processed inbox issue."""
    date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    pr_text = " | ".join(pr_urls) if pr_urls else "N/A"
    comment_body = f"✅ Processed by AutoResearcher v2 — {date_str}\nPRs: {pr_text}"
    gh_api(
        f"/repos/{GITHUB_USERNAME}/auto-researcher/issues/{issue_number}/comments",
        method="POST",
        data={"body": comment_body},
    )
    gh_api(
        f"/repos/{GITHUB_USERNAME}/auto-researcher/issues/{issue_number}",
        method="PATCH",
        data={"state": "closed"},
    )
    log.info(f"Closed inbox issue #{issue_number}")


# ── Repo scoring ───────────────────────────────────────────────────────────────

def score_repo(repo: dict, now: datetime.datetime) -> float:
    """
    Score a repo using the 5-factor formula:
      recency    = max(0, 35 - days_since_push × 0.25)   # 0–35
      issues     = min(20, open_issues_count × 5)         # 0–20
      complexity = min(15, size_kb / 2000)                # 0–15
      stars      = min(15, stargazers_count × 3)          # 0–15
      language   = 10 if scored language else 3           # 0–10
    """
    pushed_str = repo.get("pushed_at", "")
    if not pushed_str:
        return 0.0
    pushed_dt = datetime.datetime.strptime(pushed_str, "%Y-%m-%dT%H:%M:%SZ")
    days = (now - pushed_dt).days

    recency = max(0.0, 35.0 - days * 0.25)
    issues = min(20.0, repo.get("open_issues_count", 0) * 5)
    complexity = min(15.0, repo.get("size", 0) / 2000.0)
    stars = min(15.0, repo.get("stargazers_count", 0) * 3)
    language = 10.0 if repo.get("language") in SCORED_LANGUAGES else 3.0

    return recency + issues + complexity + stars + language


def get_owned_repos(focus: list[str], skip: list[str]) -> list[dict]:
    """
    Fetch the user's own repos (non-archived, non-fork, pushed within REPO_AGE_DAYS).
    Applies focus/skip overrides. Returns repos sorted by score descending.
    Skips the auto-researcher repo itself to avoid infinite loops.
    Skips repos whose README contains homework keywords.
    """
    raw = gh_api(f"/users/{GITHUB_USERNAME}/repos?type=owner&sort=pushed&per_page=100")
    if not isinstance(raw, list):
        log.error("Failed to fetch owned repos")
        return []

    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(days=REPO_AGE_DAYS)
    scored = []

    for repo in raw:
        name = repo.get("name", "")
        full_name = repo.get("full_name", "")

        # Hard excludes
        if repo.get("archived"):
            continue
        if repo.get("fork"):
            continue
        if name == "auto-researcher":
            continue  # don't process ourselves
        if name in skip:
            log.info(f"Skipping {name} (inbox skip command)")
            continue

        pushed_str = repo.get("pushed_at", "")
        if not pushed_str:
            continue
        pushed_dt = datetime.datetime.strptime(pushed_str, "%Y-%m-%dT%H:%M:%SZ")
        if pushed_dt < cutoff:
            continue

        # README homework check
        readme = gh_raw_readme(full_name)
        readme_lower = readme.lower()
        if any(kw in readme_lower for kw in HOMEWORK_KEYWORDS):
            log.info(f"Skipping {name} (homework/assignment detected in README)")
            continue

        score = score_repo(repo, now)

        # Focus boost: treated as if pushed today (max recency)
        if name in focus:
            score = min(score + 10.0, 60.0)
            log.info(f"Applied focus boost to {name}")

        scored.append({
            "name": name,
            "full_name": full_name,
            "clone_url": repo.get("clone_url", ""),
            "default_branch": repo.get("default_branch", "main"),
            "score": score,
            "language": repo.get("language", ""),
            "description": repo.get("description", ""),
        })

    scored.sort(key=lambda x: -x["score"])
    log.info(f"Scored {len(scored)} owned repos")
    for r in scored[:6]:
        log.info(f"  {r['name']}: {r['score']:.1f} pts | {r['language']}")
    return scored


# ── Queue management ───────────────────────────────────────────────────────────

def read_queue() -> dict:
    """
    Read QUEUE.md and return structured data.
    Returns: {completed: [{name, date}], ideas: [str]}
    """
    result = {"completed": [], "ideas": []}
    try:
        with open("QUEUE.md") as f:
            content = f.read()
    except FileNotFoundError:
        return result

    # Parse completed repos (format: "- repo_name (YYYY-MM-DD)")
    # and ideas (under "## Ideas" section as "- <text>")
    section = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Ideas"):
            section = "ideas"
        elif stripped.startswith("## "):
            section = None

        if stripped.startswith("- ") and "(" in stripped and ")" in stripped and section != "ideas":
            m = re.match(r"^- ([^\(]+) \((\d{4}-\d{2}-\d{2})\)", stripped)
            if m:
                result["completed"].append({"name": m.group(1).strip(), "date": m.group(2)})
        elif section == "ideas" and stripped.startswith("- "):
            result["ideas"].append(stripped[2:].strip())

    return result


def write_queue(completed: list[dict], ideas: list[str]) -> None:
    """Write updated QUEUE.md with timestamps."""
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# AutoResearcher Queue",
        f"_Last updated: {ts}_",
        "",
        "## Completed Repos",
        "_(Format: repo-name (YYYY-MM-DD) — repos processed in last 7 days are skipped)_",
        "",
    ]
    for entry in completed:
        lines.append(f"- {entry['name']} ({entry['date']})")

    if ideas:
        lines.append("")
        lines.append("## Ideas (from ar-inbox)")
        for idea in ideas:
            lines.append(f"- {idea}")

    lines.extend(["", f"_AutoResearcher v2 — {ts}_"])

    with open("QUEUE.md", "w") as f:
        f.write("\n".join(lines) + "\n")


def get_recently_done_repos(queue_data: dict) -> set[str]:
    """Return set of repo names completed within the last QUEUE_COOLDOWN_DAYS days (inclusive)."""
    today = datetime.datetime.utcnow().date()
    cutoff_date = today - datetime.timedelta(days=QUEUE_COOLDOWN_DAYS)
    recent = set()
    for entry in queue_data.get("completed", []):
        try:
            done_date = datetime.datetime.strptime(entry["date"], "%Y-%m-%d").date()
            if done_date >= cutoff_date:
                recent.add(entry["name"])
        except ValueError:
            continue
    return recent


# ── GitHub PR helper ───────────────────────────────────────────────────────────

def create_pr(repo_name: str, branch: str, default_branch: str, title: str, body: str) -> str:
    """Create a pull request and return its HTML URL."""
    resp = gh_api(
        f"/repos/{GITHUB_USERNAME}/{repo_name}/pulls",
        method="POST",
        data={"title": title, "body": body, "head": branch, "base": default_branch},
    )
    url = resp.get("html_url", "")
    if url:
        log.info(f"Created PR: {url}")
    else:
        log.warning(f"PR creation failed for {repo_name}: {resp}")
    return url


def create_report_issue(title: str, body: str) -> str:
    """Create a GitHub Issue on the auto-researcher repo and return its URL."""
    resp = gh_api(
        f"/repos/{GITHUB_USERNAME}/auto-researcher/issues",
        method="POST",
        data={"title": title, "body": body},
    )
    return resp.get("html_url", "")


# ── Implementation runners ─────────────────────────────────────────────────────

def run_claude_on_repo(repo: dict, branch: str, results: dict, key: str) -> None:
    """Run claude-sonnet-4-6 (via Claude Code) to implement an improvement on the repo."""
    name = repo["name"]
    workdir = f"/tmp/ar_impl_{name}"
    log.info(f"[Claude] Starting on {name}")

    try:
        auth_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{name}.git"
        subprocess.run(
            ["git", "clone", "--quiet", auth_url, workdir],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(["git", "config", "user.email", "autoresearcher@bot.local"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "AutoResearcher"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True, capture_output=True)

        prompt = textwrap.dedent(f"""
            You are a senior engineer improving the GitHub repo: {name} ({repo.get('description','')})
            Language: {repo.get('language','unknown')}
            Working directory: {workdir}

            Your task: identify and implement the SINGLE most valuable improvement missing from this project.
            Focus on: real user-visible behavior — a new API endpoint, persistent storage, a CLI command,
            test coverage, or observability. NOT minor refactors or doc fixes.

            Rules:
            1. Read all relevant source files before writing code
            2. Write production-quality code: type hints, docstrings, error handling
            3. Add tests for any new logic
            4. Update README if the user interface changes
            5. Commit with: git add -A && git commit -m 'feat: [what you built and why it matters]'
            6. Do NOT push. Commit locally only.
        """).strip()

        result = subprocess.run(
            [
                "claude",
                "--model", "claude-sonnet-4-6",
                "--dangerously-skip-permissions",
                "-p", prompt,
            ],
            cwd=workdir, capture_output=True, text=True, timeout=900,
        )
        output = (result.stdout or result.stderr or "").strip()
        commit = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            cwd=workdir, capture_output=True, text=True,
        ).stdout.strip()
        diff_stat = subprocess.run(
            ["git", "diff", repo["default_branch"] + "...HEAD", "--stat"],
            cwd=workdir, capture_output=True, text=True,
        ).stdout.strip()

        results[key] = {
            "repo": repo, "workdir": workdir, "status": "done",
            "summary": output[:1000], "commit": commit, "diff_stat": diff_stat,
            "branch": branch,
        }
        log.info(f"[Claude] Done on {name} — commits: {commit[:60]}")
    except Exception as e:
        log.error(f"[Claude] Error on {name}: {e}")
        results[key] = {"repo": repo, "workdir": None, "status": "error", "summary": str(e), "branch": branch}


def run_codex_on_repo(repo: dict, branch: str, results: dict, key: str) -> None:
    """Run Codex (gpt-5.4 via npx @openai/codex) to implement an improvement."""
    name = repo["name"]
    workdir = f"/tmp/ar_codex_{name}"
    log.info(f"[Codex] Starting on {name}")

    try:
        auth_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{name}.git"
        subprocess.run(
            ["git", "clone", "--quiet", auth_url, workdir],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(["git", "config", "user.email", "autoresearcher@bot.local"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "AutoResearcher"], cwd=workdir, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True, capture_output=True)

        prompt = textwrap.dedent(f"""
            Improve the GitHub repo at {workdir} (repo: {name}, language: {repo.get('language','unknown')}).

            Find and implement the SINGLE most valuable missing feature — something that gives users
            new capability they don't have today.

            Rules:
            1. Read source files first to understand the codebase
            2. Write production-quality code
            3. Add tests for new logic
            4. git add -A && git commit -m 'feat: [what you built]'
            5. Do NOT push.
        """).strip()

        result = subprocess.run(
            [
                "npx", "--yes", "@openai/codex", "exec",
                "--approval-mode", "full-auto",
                "--model", "gpt-5.4",
                "--reasoning-effort", "high",
                prompt,
            ],
            cwd=workdir, capture_output=True, text=True, timeout=900,
        )
        output = (result.stdout or result.stderr or "").strip()
        commit = subprocess.run(
            ["git", "log", "--oneline", "-3"],
            cwd=workdir, capture_output=True, text=True,
        ).stdout.strip()
        diff_stat = subprocess.run(
            ["git", "diff", repo["default_branch"] + "...HEAD", "--stat"],
            cwd=workdir, capture_output=True, text=True,
        ).stdout.strip()

        results[key] = {
            "repo": repo, "workdir": workdir, "status": "done",
            "summary": output[:1000], "commit": commit, "diff_stat": diff_stat,
            "branch": branch,
        }
        log.info(f"[Codex] Done on {name}")
    except Exception as e:
        log.error(f"[Codex] Error on {name}: {e}")
        results[key] = {"repo": repo, "workdir": None, "status": "error", "summary": str(e), "branch": branch}


def push_and_pr(result_data: dict, queue_entry: dict, tool_label: str) -> str:
    """Push branch and create a PR. Returns PR URL or empty string."""
    repo = result_data.get("repo", {})
    workdir = result_data.get("workdir")
    branch = result_data.get("branch", "")
    commit = result_data.get("commit", "")
    diff_stat = result_data.get("diff_stat", "")
    name = repo.get("name", "")
    default_branch = repo.get("default_branch", "main")

    if not workdir or result_data.get("status") == "error":
        log.warning(f"Skipping PR for {name} — implementation failed")
        return ""

    # Check if there are commits on the branch
    if not commit:
        log.warning(f"No commits found for {name} — skipping PR")
        return ""

    # Push
    auth_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_USERNAME}/{name}.git"
    push_result = subprocess.run(
        ["git", "push", auth_url, branch],
        cwd=workdir, capture_output=True, text=True,
    )
    if push_result.returncode != 0:
        log.error(f"Push failed for {name}: {push_result.stderr[:200]}")
        return ""

    date_str = queue_entry["date"]
    title = f"AutoResearch {date_str}: {commit.splitlines()[0][:75]}" if commit else f"AutoResearch {date_str}"

    body = f"""## AutoResearch {date_str} — {tool_label}

### What was built
{result_data.get('summary', '')[:600]}

### Changes
```
{diff_stat}
```

### Commits
```
{commit}
```

---
🤖 AutoResearcher v2 | {tool_label} | Do NOT auto-merge — review before merging
"""

    return create_pr(name, branch, default_branch, title, body)


# ── Main orchestration ─────────────────────────────────────────────────────────

def main() -> None:
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    branch = f"auto-research/{today}"
    log.info(f"=== AutoResearcher v2 starting — {today} ===")

    # Step 1: Read inbox
    inbox = read_inbox()

    if inbox["pause"]:
        msg = f"⏸️ AutoResearcher paused by inbox command — {today}"
        log.info(msg)
        slack(msg)
        return

    # Step 2: Read queue and get repos done recently
    queue_data = read_queue()
    recently_done = get_recently_done_repos(queue_data)
    log.info(f"Repos done in last {QUEUE_COOLDOWN_DAYS} days: {recently_done}")

    # Step 3: Score owned repos
    repos = get_owned_repos(focus=inbox["focus"], skip=inbox["skip"])
    if not repos:
        slack(f"⚠️ AutoResearcher: no eligible owned repos found — {today}")
        return

    # Filter out recently done repos
    eligible = [r for r in repos if r["name"] not in recently_done]
    if not eligible:
        log.info("All repos done recently. Resetting cooldown for a fresh cycle.")
        eligible = repos  # Reset: process top repos again

    # Pick top 2
    to_process = eligible[:2]
    log.info(f"Selected repos: {[r['name'] for r in to_process]}")
    slack(
        f"🔬 *AutoResearcher v2 starting* — {today}\n"
        f"• Claude (sonnet-4-6) → `{to_process[0]['name']}`\n"
        + (f"• Codex (gpt-5.4) → `{to_process[1]['name']}`" if len(to_process) > 1 else "")
    )

    # Step 4: Run Claude and Codex in parallel
    results: dict = {}
    threads = []

    t1 = threading.Thread(
        target=run_claude_on_repo, args=(to_process[0], branch, results, "claude")
    )
    threads.append(t1)

    if len(to_process) > 1:
        t2 = threading.Thread(
            target=run_codex_on_repo, args=(to_process[1], branch, results, "codex")
        )
        threads.append(t2)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Step 5: Push + create PRs
    pr_urls: list[str] = []
    date_entry = today

    if "claude" in results:
        queue_entry = {"name": to_process[0]["name"], "date": date_entry}
        pr_url = push_and_pr(results["claude"], queue_entry, "Claude Code (claude-sonnet-4-6)")
        if pr_url:
            pr_urls.append(pr_url)

    if "codex" in results and len(to_process) > 1:
        queue_entry = {"name": to_process[1]["name"], "date": date_entry}
        pr_url = push_and_pr(results["codex"], queue_entry, "Codex (gpt-5.4, reasoning-effort: high)")
        if pr_url:
            pr_urls.append(pr_url)

    # Step 6: Update QUEUE.md
    new_completed = list(queue_data.get("completed", []))
    for repo in to_process:
        new_completed.append({"name": repo["name"], "date": today})

    # Merge ideas from inbox
    new_ideas = list(queue_data.get("ideas", [])) + inbox["ideas"]
    write_queue(new_completed, new_ideas)
    log.info("Updated QUEUE.md")

    # Step 7: Close inbox issues
    for issue_num in inbox["issue_numbers"]:
        close_inbox_issue(issue_num, pr_urls)

    # Step 8: Post Slack summary
    pr_lines = "\n".join(f"• {url}" for url in pr_urls) if pr_urls else "• No PRs created"
    slack_msg = (
        f"✅ *AutoResearcher v2 done!* — {today}\n"
        f"{'✅' if results.get('claude',{}).get('status')=='done' else '❌'} Claude → `{to_process[0]['name']}`\n"
        + (
            f"{'✅' if results.get('codex',{}).get('status')=='done' else '❌'} Codex → `{to_process[1]['name']}`\n"
            if len(to_process) > 1 else ""
        )
        + f"\n{pr_lines}\n\n"
        "*Control next run via GitHub Issues on `sriv144/auto-researcher` with label `ar-inbox`:*\n"
        "`focus: <repo>` · `skip: <repo>` · `pause` · `idea: <text>`"
    )
    slack(slack_msg)

    # Step 9: Create report issue
    report_body = f"""# AutoResearcher v2 Run — {today}

## Repos Selected
{chr(10).join(f"- `{r['name']}` (score={r['score']:.1f})" for r in to_process)}

## PRs Created
{chr(10).join(f"- {url}" for url in pr_urls) if pr_urls else "- None"}

## Claude Summary
**Repo:** `{results.get('claude',{}).get('repo',{}).get('name','N/A')}`
{results.get('claude',{}).get('summary','N/A')[:500]}

## Codex Summary
**Repo:** `{results.get('codex',{}).get('repo',{}).get('name','N/A') if 'codex' in results else 'N/A'}`
{results.get('codex',{}).get('summary','N/A')[:500] if 'codex' in results else 'N/A'}

---
🤖 Reply with inbox commands by creating an issue with label `ar-inbox`:
`focus: <repo>` · `skip: <repo>` · `pause` · `idea: <description>`
"""
    issue_url = create_report_issue(f"AutoResearcher v2 Run — {today}", report_body)
    if issue_url:
        log.info(f"Report issue: {issue_url}")

    log.info("=== AutoResearcher v2 complete ===")


if __name__ == "__main__":
    main()
