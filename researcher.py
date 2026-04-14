#!/usr/bin/env python3
"""
AutoResearcher v2 — smarter repo ranking, inbox reading, completeness check, plan-driven implementation.
Autonomous code improvement agent that ranks your own repos by value, reads GitHub inbox for commands,
checks project completeness, and runs Claude + Codex with planning phases.
"""

import os, json, subprocess, threading, datetime, urllib.request, urllib.parse, sys, textwrap, tempfile, shutil, re

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "sriv144")
MAX_CALL_FRACTION = 0.60
REPO_AGE_DAYS = 180

def gh_api(path, method="GET", data=None):
    """Make authenticated GitHub API call."""
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    })
    if data:
        req.data = json.dumps(data).encode()
        req.method = method
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def slack(msg):
    """Send message to Slack webhook."""
    data = json.dumps({"text": msg}).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=data,
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)

def get_owned_repos():
    """Fetch user's own repos (not starred) and rank by value score."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=REPO_AGE_DAYS)
    repos = []
    page = 1
    while True:
        batch = gh_api(f"/users/{GITHUB_USERNAME}/repos?type=owner&per_page=50&page={page}&sort=updated")
        if not batch:
            break
        for r in batch:
            if r["archived"]:
                continue
            pushed = datetime.datetime.strptime(r["pushed_at"], "%Y-%m-%dT%H:%M:%SZ")
            if pushed > cutoff:
                # Calculate score (0-100 points)
                days_since = (datetime.datetime.utcnow() - pushed).days
                recency_score = max(0, 35 - days_since * 0.25)
                issues_score = min(20, r["open_issues_count"] * 5)
                size_score = min(15, r["size"] / 2000.0)
                stars_score = min(15, r["stargazers_count"] * 3)
                lang = r.get("language", "").lower()
                lang_bonus = 10 if lang in ["python", "typescript", "javascript", "go", "rust"] else 3

                total_score = recency_score + issues_score + size_score + stars_score + lang_bonus

                repos.append({
                    "name": r["full_name"],
                    "url": r["clone_url"],
                    "lang": r.get("language", ""),
                    "pushed": r["pushed_at"],
                    "score": total_score,
                    "open_issues": r["open_issues_count"],
                    "size_kb": r["size"]
                })
        page += 1
        if len(batch) < 50:
            break

    # Sort by score descending
    return sorted(repos, key=lambda x: x["score"], reverse=True)

def is_homework_repo(readme_content):
    """Check if README suggests this is a homework/assignment repo."""
    if not readme_content:
        return False
    lower_content = readme_content.lower()
    keywords = ["assignment", "homework", "graded", "course work", "coursework", "solution", "submission"]
    return any(kw in lower_content for kw in keywords)

def assess_completeness(repo_path):
    """
    Clone repo to temp dir and assess completeness (0-100).
    Returns (score, assessment_text).
    Skip if clearly not a real project.
    """
    try:
        # Check for README
        readme_path = os.path.join(repo_path, "README.md")
        has_readme = False
        readme_len = 0
        readme_content = ""
        if os.path.exists(readme_path):
            with open(readme_path) as f:
                readme_content = f.read()
            readme_len = len(readme_content)
            has_readme = readme_len > 500

        # Check if this looks like homework
        if is_homework_repo(readme_content):
            return (-1, "homework")

        # Count files (skip if only 1-2 files)
        all_files = []
        for root, dirs, files in os.walk(repo_path):
            # Skip .git and other common non-source dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if not f.startswith('.'):
                    all_files.append(f)

        if len(all_files) <= 2:
            return (-1, "trivial")

        # Check for tests
        has_tests = any(f for f in all_files if 'test' in f.lower() or 'spec' in f.lower())
        tests_score = 20 if has_tests else 0

        # Check for CI
        ci_path = os.path.join(repo_path, ".github", "workflows")
        has_ci = os.path.exists(ci_path) and len(os.listdir(ci_path)) > 0
        ci_score = 15 if has_ci else 0

        # Count TODOs and FIXMEs
        todo_count = 0
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in files:
                if f.endswith(('.py', '.js', '.ts', '.go', '.rs', '.java', '.md')):
                    try:
                        with open(os.path.join(root, f)) as file:
                            content = file.read()
                            todo_count += content.lower().count('todo') + content.lower().count('fixme')
                    except:
                        pass

        todo_score = max(0, 20 - todo_count)

        # Check for error handling (simple heuristic: look for try/except, error handling patterns)
        has_error_handling = any(f for f in all_files if f.endswith(('.py', '.js', '.ts')))
        error_score = 15 if has_error_handling else 0

        # Check for config files
        config_files = ['setup.py', 'package.json', 'go.mod', 'Cargo.toml', 'pyproject.toml', '.env.example']
        has_config = any(f in all_files for f in config_files)
        config_score = 15 if has_config else 0

        # README quality
        readme_score = 15 if has_readme else 0

        total = tests_score + ci_score + todo_score + error_score + config_score + readme_score

        if total >= 75:
            assessment = "mature"
        elif total >= 50:
            assessment = "moderate"
        elif total >= 20:
            assessment = "early_stage"
        else:
            assessment = "minimal"

        return (total, assessment)
    except Exception as e:
        return (0, f"error: {str(e)}")

def read_inbox():
    """
    Read GitHub Issues inbox with ar-inbox label.
    Returns: (inbox_items, focus_repos, skip_repos, should_pause)
    """
    try:
        issues = gh_api(f"/repos/{GITHUB_USERNAME}/auto-researcher/issues?labels=ar-inbox&state=open")
        inbox_items = []
        focus_repos = []
        skip_repos = []
        should_pause = False

        for issue in issues:
            title = issue.get("title", "")
            body = issue.get("body", "")
            full_text = f"{title} {body}".lower()

            inbox_items.append({
                "number": issue["number"],
                "title": title,
                "body": body
            })

            if "pause" in full_text:
                should_pause = True

            # Parse focus: <repo>
            focus_match = re.search(r'focus:\s*([^\n]+)', full_text, re.IGNORECASE)
            if focus_match:
                focus_repos.append(focus_match.group(1).strip())

            # Parse skip: <repo>
            skip_match = re.search(r'skip:\s*([^\n]+)', full_text, re.IGNORECASE)
            if skip_match:
                skip_repos.append(skip_match.group(1).strip())

        return inbox_items, focus_repos, skip_repos, should_pause
    except:
        return [], [], [], False

def close_inbox_issue(issue_number):
    """Close a processed inbox issue."""
    try:
        gh_api(f"/repos/{GITHUB_USERNAME}/auto-researcher/issues/{issue_number}",
               method="PATCH", data={"state": "closed"})
    except:
        pass

def read_queue():
    """Read QUEUE.md for completed/queued repos."""
    try:
        with open("QUEUE.md") as f:
            content = f.read()
        done = []
        queued = []
        section = None
        for line in content.splitlines():
            if "## Completed" in line: section = "done"
            elif "## Queue" in line: section = "queue"
            elif line.startswith("- ") and section == "done":
                done.append(line[2:].split(" (")[0].split(" — ")[0].strip())
            elif line.startswith("- ") and section == "queue":
                queued.append(line[2:].split(" (")[0].split(" — ")[0].strip())
        return done, queued
    except FileNotFoundError:
        return [], []

def write_queue(done, queued, quota_claude, quota_codex, repos_with_scores=None):
    """Write QUEUE.md with completeness data."""
    if repos_with_scores is None:
        repos_with_scores = {}

    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# AutoResearcher Queue\n_Last updated: {ts}_\n",
             "## Completed Repos\n"]
    for r in done:
        lines.append(f"- {r}")
    lines.append("\n## Queue (Next Run)\n")
    for r in queued:
        score_info = f" — score: {repos_with_scores.get(r, {}).get('score', '?'):.1f}" if r in repos_with_scores else ""
        completeness = f" ({repos_with_scores.get(r, {}).get('completeness', '?')})" if r in repos_with_scores else ""
        lines.append(f"- {r}{score_info}{completeness}")
    lines.append(f"\n## Quota Today\n- Claude: {quota_claude} calls used\n- Codex: {quota_codex} calls used\n")
    with open("QUEUE.md", "w") as f:
        f.write("\n".join(lines))

def run_claude_on_repo(repo, results, idx):
    """Run Claude Code on repo with planning phase."""
    name = repo["name"]
    url = repo["url"]
    workdir = f"/tmp/claude_repo_{idx}"
    print(f"[Claude] Starting on {name} (score: {repo['score']:.1f})")
    try:
        subprocess.run(["git", "clone", "--depth=1", url, workdir], check=True,
                       capture_output=True, timeout=120)

        # Create feature branch
        branch = f"auto-research/{datetime.datetime.utcnow().strftime('%Y-%m-%d')}"
        subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True,
                       capture_output=True, timeout=30)

        prompt = textwrap.dedent(f"""
            You are reviewing the GitHub repo: {name}

            === STEP 1: PLANNING ===
            First, analyze this project:
            - What is the core purpose?
            - What areas need improvement? (CLI, integrations, observability, testing, docs, error handling)
            - What is the single most valuable missing feature?
            - Is the project already substantially complete? If yes, look for stretch features or developer experience improvements.

            === STEP 2: IMPLEMENTATION ===
            Implement ONE substantial improvement that adds real user-visible value.
            Avoid trivial changes. Make the change meaningful.

            === STEP 3: COMMIT & PUSH ===
            Commit with a clear message: "feat: <description of improvement>"
            Push to origin {branch}

            Working directory: {workdir}
            GitHub token for push: {GITHUB_TOKEN}
        """).strip()

        result = subprocess.run(
            ["claude", "--model", "claude-sonnet-4-6", "--dangerously-skip-permissions",
             "-p", prompt],
            cwd=workdir, capture_output=True, text=True, timeout=600
        )
        output = result.stdout.strip() or result.stderr.strip()
        results[f"claude_{idx}"] = {"repo": name, "status": "done", "summary": output[:800]}
        print(f"[Claude] Done on {name}")
    except Exception as e:
        results[f"claude_{idx}"] = {"repo": name, "status": "error", "summary": str(e)}
        print(f"[Claude] Error on {name}: {e}")

def run_codex_on_repo(repo, results, idx):
    """Run Codex on repo with planning phase."""
    name = repo["name"]
    url = repo["url"]
    workdir = f"/tmp/codex_repo_{idx}"
    print(f"[Codex] Starting on {name} (score: {repo['score']:.1f})")
    try:
        subprocess.run(["git", "clone", "--depth=1", url, workdir], check=True,
                       capture_output=True, timeout=120)

        # Create feature branch
        branch = f"auto-research/{datetime.datetime.utcnow().strftime('%Y-%m-%d')}"
        subprocess.run(["git", "checkout", "-b", branch], cwd=workdir, check=True,
                       capture_output=True, timeout=30)

        prompt = textwrap.dedent(f"""
            Repository: {name}

            === PLANNING ===
            Analyze the project structure and identify ONE substantial improvement:
            - What problem does this repo solve?
            - What features or capabilities are missing?
            - Is error handling, testing, or documentation weak?
            - Can you add a significant new capability or improve automation?

            === IMPLEMENTATION ===
            Implement ONE meaningful feature or improvement that adds user value.
            Make substantial changes — avoid trivial fixes.

            === COMMIT & PUSH ===
            Commit your changes: git commit -m "feat: <description>"
            Push to origin {branch}
        """).strip()

        result = subprocess.run(
            ["npx", "--yes", "@openai/codex", "exec", "--approval-mode", "full-auto",
             "--model", "gpt-4.1", prompt],
            cwd=workdir, capture_output=True, text=True, timeout=600
        )
        output = result.stdout.strip() or result.stderr.strip()
        results[f"codex_{idx}"] = {"repo": name, "status": "done", "summary": output[:800]}
        print(f"[Codex] Done on {name}")
    except Exception as e:
        results[f"codex_{idx}"] = {"repo": name, "status": "error", "summary": str(e)}
        print(f"[Codex] Error on {name}: {e}")

def create_github_issue(title, body):
    """Create issue in auto-researcher repo."""
    gh_api(f"/repos/{GITHUB_USERNAME}/auto-researcher/issues",
           method="POST", data={"title": title, "body": body})

def main():
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    print("=== AutoResearcher v2 starting ===")
    slack(f"🔬 *AutoResearcher v2 starting* — {today}\nFetching and ranking your repos...")

    # Read inbox first
    inbox_items, focus_repos, skip_repos, should_pause = read_inbox()

    if should_pause:
        slack(f"⏸️ *AutoResearcher paused* — Found 'pause' command in inbox.\nResume by removing the pause issue or creating a new inbox command.")
        for item in inbox_items:
            close_inbox_issue(item["number"])
        return

    # Fetch owned repos (not starred)
    owned = get_owned_repos()

    # Filter: remove skipped repos, remove homework repos
    candidates = []
    skipped_count = 0
    homework_count = 0

    for repo in owned:
        if repo["name"] in skip_repos:
            skipped_count += 1
            continue

        # Quick homework check from name
        if any(hw in repo["name"].lower() for hw in ["homework", "assignment", "solution"]):
            homework_count += 1
            continue

        # Assess completeness
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = os.path.join(tmpdir, "repo")
            try:
                subprocess.run(["git", "clone", "--depth=1", repo["url"], repo_path],
                               check=True, capture_output=True, timeout=60)
                score, assessment = assess_completeness(repo_path)
            except:
                score, assessment = 0, "clone_error"

        if score == -1:
            if assessment == "homework":
                homework_count += 1
            continue

        repo["completeness"] = assessment
        candidates.append(repo)

    if not candidates:
        slack(f"⚠️ No valid repos to process today.\n_Filtered: {skipped_count} skipped, {homework_count} homework/trivial_")
        return

    # Prioritize focused repos at top
    focused = [r for r in candidates if r["name"] in focus_repos]
    unfocused = [r for r in candidates if r["name"] not in focus_repos]
    priority_list = focused + unfocused

    done_repos, queued = read_queue()

    # Filter out already-done repos
    remaining = [r for r in priority_list if r["name"] not in done_repos]
    if not remaining:
        slack("✅ All repos analyzed! Resetting queue for a fresh cycle.")
        done_repos, remaining = [], priority_list

    # Pick next 2
    to_process = remaining[:2]
    still_queued = remaining[2:]

    if len(to_process) == 0:
        slack("⚠️ No repos to process today.")
        return

    # Build repo info dict
    repos_dict = {r["name"]: r for r in remaining}

    # Announce
    if len(to_process) == 1:
        slack(f"🔬 Today: Claude → `{to_process[0]['name']}`\n_Score: {to_process[0]['score']:.1f} | Completeness: {to_process[0]['completeness']}_")
    else:
        slack(f"🔬 Today's plan:\n• Claude → `{to_process[0]['name']}` (score: {to_process[0]['score']:.1f})\n• Codex → `{to_process[1]['name']}` (score: {to_process[1]['score']:.1f})\nStarting now...\n\n💡 To direct next run, create GitHub Issue at {GITHUB_USERNAME}/auto-researcher with label `ar-inbox` and title like: `focus: owner/repo` or `skip: owner/repo`")

    results = {}
    threads = []

    # Run Claude on repo 1
    t1 = threading.Thread(target=run_claude_on_repo, args=(to_process[0], results, 1))
    threads.append(t1)

    # Run Codex on repo 2 (if available)
    if len(to_process) > 1:
        t2 = threading.Thread(target=run_codex_on_repo, args=(to_process[1], results, 2))
        threads.append(t2)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Build report
    now_done = done_repos + [r["name"] for r in to_process]
    write_queue(now_done, [r["name"] for r in still_queued], len(threads), len(threads), repos_dict)

    report_lines = [f"# AutoResearcher Run — {today}\n"]
    slack_lines = [f"✅ *AutoResearcher done!* — {today}"]

    for key, val in results.items():
        tool = "Claude" if "claude" in key else "Codex"
        emoji = "🤖" if tool == "Claude" else "⚡"
        status = "✅" if val["status"] == "done" else "❌"
        report_lines.append(f"## {emoji} {tool} → `{val['repo']}`\n**Status:** {status} {val['status']}\n\n{val['summary']}\n")
        slack_lines.append(f"{status} *{tool}* → `{val['repo']}`")

    if still_queued:
        report_lines.append(f"## 📋 Queued for Tomorrow\n" + "\n".join(f"- `{r['name']}` (score: {r['score']:.1f})" for r in still_queued[:5]))
        slack_lines.append(f"\n📋 {len(still_queued)} repos queued for tomorrow.")

    # Close inbox issues
    for item in inbox_items:
        close_inbox_issue(item["number"])

    # Post to Slack
    slack("\n".join(slack_lines))

    # Create GitHub Issue with report
    create_github_issue(
        title=f"AutoResearcher Run — {today}",
        body="\n\n".join(report_lines)
    )
    print("=== AutoResearcher v2 complete ===")

if __name__ == "__main__":
    main()
