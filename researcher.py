#!/usr/bin/env python3
"""
AutoResearcher - Autonomous code improvement agent
Runs Claude Code and Codex in parallel on your top starred GitHub repos.
"""

import os, json, subprocess, threading, datetime, urllib.request, urllib.parse, sys, textwrap

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "sriv144")
MAX_CALL_FRACTION = 0.60  # 60% quota cap
REPO_AGE_DAYS = 180       # Only repos updated in last 6 months

def gh_api(path, method="GET", data=None):
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
    data = json.dumps({"text": msg}).encode()
    req = urllib.request.Request(SLACK_WEBHOOK, data=data,
                                  headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)

def get_starred_repos():
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=REPO_AGE_DAYS)
    repos, page = [], 1
    while True:
        batch = gh_api(f"/users/{GITHUB_USERNAME}/starred?per_page=50&page={page}&sort=updated")
        if not batch:
            break
        for r in batch:
            pushed = datetime.datetime.strptime(r["pushed_at"], "%Y-%m-%dT%H:%M:%SZ")
            if pushed > cutoff and not r["archived"]:
                repos.append({"name": r["full_name"], "url": r["clone_url"],
                               "lang": r.get("language",""), "pushed": r["pushed_at"]})
        page += 1
        if len(batch) < 50:
            break
    return sorted(repos, key=lambda x: x["pushed"], reverse=True)

def read_queue():
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
                done.append(line[2:].split(" (")[0].strip())
            elif line.startswith("- ") and section == "queue":
                queued.append(line[2:].split(" (")[0].strip())
        return done, queued
    except FileNotFoundError:
        return [], []

def write_queue(done, queued, quota_claude, quota_codex):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# AutoResearcher Queue\n_Last updated: {ts}_\n",
             "## Completed Repos\n"]
    for r in done:
        lines.append(f"- {r}")
    lines.append("\n## Queue (Next Run)\n")
    for r in queued:
        lines.append(f"- {r}")
    lines.append(f"\n## Quota Today\n- Claude: {quota_claude} calls used\n- Codex: {quota_codex} calls used\n")
    with open("QUEUE.md", "w") as f:
        f.write("\n".join(lines))

def run_claude_on_repo(repo, results, idx):
    name = repo["name"]
    url = repo["url"]
    workdir = f"/tmp/claude_repo_{idx}"
    print(f"[Claude] Starting on {name}")
    try:
        subprocess.run(["git", "clone", "--depth=1", url, workdir], check=True,
                       capture_output=True, timeout=120)
        prompt = textwrap.dedent(f"""
            You are reviewing the GitHub repo: {name}
            Your task: identify and implement the SINGLE most valuable improvement missing from this project.
            Focus on: CLI gaps, integration gaps, observability (logging/tests), or developer experience.
            Avoid adding complex features that are not needed. If the project is already complete, say so clearly.
            Make the change, commit it with a meaningful message, and push to origin main.
            Working directory: {workdir}
            GitHub token for push: {GITHUB_TOKEN}
        """).strip()
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "Bash,Read,Write,Edit",
             "--output-format", "text", "--max-turns", "10"],
            cwd=workdir, capture_output=True, text=True, timeout=600
        )
        output = result.stdout.strip() or result.stderr.strip()
        results[f"claude_{idx}"] = {"repo": name, "status": "done", "summary": output[:800]}
        print(f"[Claude] Done on {name}")
    except Exception as e:
        results[f"claude_{idx}"] = {"repo": name, "status": "error", "summary": str(e)}
        print(f"[Claude] Error on {name}: {e}")

def run_codex_on_repo(repo, results, idx):
    name = repo["name"]
    url = repo["url"]
    workdir = f"/tmp/codex_repo_{idx}"
    print(f"[Codex] Starting on {name}")
    try:
        subprocess.run(["git", "clone", "--depth=1", url, workdir], check=True,
                       capture_output=True, timeout=120)
        prompt = textwrap.dedent(f"""
            Review this repo ({name}) and implement the single most valuable missing feature.
            Focus on: gaps in automation, missing integrations, missing tests or docs.
            Keep it practical — skip anything too complex or unnecessary.
            Commit and push your changes to origin main.
        """).strip()
        result = subprocess.run(
            ["codex", "--approval-mode", "full-auto", "-q", prompt],
            cwd=workdir, capture_output=True, text=True, timeout=600,
            env={**os.environ, "CODEX_QUIET": "1"}
        )
        output = result.stdout.strip() or result.stderr.strip()
        results[f"codex_{idx}"] = {"repo": name, "status": "done", "summary": output[:800]}
        print(f"[Codex] Done on {name}")
    except Exception as e:
        results[f"codex_{idx}"] = {"repo": name, "status": "error", "summary": str(e)}
        print(f"[Codex] Error on {name}: {e}")

def create_github_issue(title, body):
    gh_api(f"/repos/{GITHUB_USERNAME}/auto-researcher/issues",
           method="POST", data={"title": title, "body": body})

def main():
    today = datetime.datetime.utcnow().strftime("%B %d, %Y")
    print("=== AutoResearcher starting ===")
    slack(f"🔬 *AutoResearcher starting* — {today}\nFetching your starred repos...")

    starred = get_starred_repos()
    done_repos, queued = read_queue()

    # Filter out already-done repos
    remaining = [r for r in starred if r["name"] not in done_repos]
    if not remaining:
        slack("✅ All starred repos have been analyzed! Resetting queue for a fresh cycle.")
        done_repos, remaining = [], starred

    # Pick next 2
    to_process = remaining[:2]
    still_queued = [r["name"] for r in remaining[2:]]

    if len(to_process) == 0:
        slack("⚠️ No repos to process today.")
        return

    if len(to_process) == 1:
        slack(f"🔬 Today: Claude → `{to_process[0]['name']}`\nOnly 1 repo available.")
    else:
        slack(f"🔬 Today's plan:\n• Claude → `{to_process[0]['name']}`\n• Codex → `{to_process[1]['name']}`\nStarting now...")

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
    write_queue(now_done, still_queued, len(threads), len(threads))

    report_lines = [f"# AutoResearcher Run — {today}\n"]
    slack_lines = [f"✅ *AutoResearcher done!* — {today}"]

    for key, val in results.items():
        tool = "Claude" if "claude" in key else "Codex"
        emoji = "🤖" if tool == "Claude" else "⚡"
        status = "✅" if val["status"] == "done" else "❌"
        report_lines.append(f"## {emoji} {tool} → `{val['repo']}`\n**Status:** {status} {val['status']}\n\n{val['summary']}\n")
        slack_lines.append(f"{status} *{tool}* → `{val['repo']}`")

    if still_queued:
        report_lines.append(f"## 📋 Queued for Tomorrow\n" + "\n".join(f"- `{r}`" for r in still_queued[:5]))
        slack_lines.append(f"\n📋 {len(still_queued)} repos queued for tomorrow.")

    # Post to Slack
    slack("\n".join(slack_lines))

    # Create GitHub Issue
    create_github_issue(
        title=f"AutoResearcher Run — {today}",
        body="\n\n".join(report_lines)
    )
    print("=== AutoResearcher complete ===")

if __name__ == "__main__":
    main()
