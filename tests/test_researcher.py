"""
Tests for AutoResearcher v2 core logic.
Run with: python -m pytest tests/ -v
"""
import datetime
import json
import os
import tempfile
import pytest
from unittest.mock import MagicMock, patch

# Set required env vars before import
os.environ.setdefault("GITHUB_TOKEN", "test_token")
os.environ.setdefault("SLACK_WEBHOOK_URL", "")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from researcher import (
    score_repo,
    get_recently_done_repos,
    read_queue,
    write_queue,
    QUEUE_COOLDOWN_DAYS,
)


# ── Score repo ─────────────────────────────────────────────────────────────────

class TestScoreRepo:
    def _make_repo(self, days_ago=0, open_issues=0, size=0, stars=0, language="Python"):
        pushed = (datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return {
            "pushed_at": pushed,
            "open_issues_count": open_issues,
            "size": size,
            "stargazers_count": stars,
            "language": language,
        }

    def test_max_score_fresh_python_repo(self):
        now = datetime.datetime.utcnow()
        repo = self._make_repo(days_ago=0, open_issues=4, size=30000, stars=5, language="Python")
        score = score_repo(repo, now)
        # recency=35, issues=20, complexity=15, stars=15, language=10 → 95
        assert score == pytest.approx(95.0, abs=1.0)

    def test_recency_decay(self):
        now = datetime.datetime.utcnow()
        fresh = self._make_repo(days_ago=0)
        old = self._make_repo(days_ago=100)
        assert score_repo(fresh, now) > score_repo(old, now)

    def test_non_scored_language_penalty(self):
        now = datetime.datetime.utcnow()
        py_repo = self._make_repo(language="Python")
        r_repo = self._make_repo(language="R")
        assert score_repo(py_repo, now) > score_repo(r_repo, now)

    def test_zero_score_for_missing_pushed_at(self):
        now = datetime.datetime.utcnow()
        repo = {"pushed_at": "", "open_issues_count": 0, "size": 0, "stargazers_count": 0}
        assert score_repo(repo, now) == 0.0

    def test_issues_capped_at_20(self):
        now = datetime.datetime.utcnow()
        repo = self._make_repo(open_issues=100)
        score = score_repo(repo, now)
        # issues component should be capped at 20
        repo_no_issues = self._make_repo(open_issues=0)
        score_no = score_repo(repo_no_issues, now)
        assert (score - score_no) == pytest.approx(20.0, abs=0.1)


# ── Queue management ───────────────────────────────────────────────────────────

class TestQueueManagement:
    def test_write_and_read_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = os.path.join(tmpdir, "QUEUE.md")
            # Write
            completed = [
                {"name": "my-repo", "date": "2026-04-15"},
                {"name": "other-repo", "date": "2026-04-10"},
            ]
            ideas = ["add dark mode", "support webhooks"]

            # Patch open to write to tmpdir
            orig_dir = os.getcwd()
            os.chdir(tmpdir)
            try:
                write_queue(completed, ideas)
                result = read_queue()
            finally:
                os.chdir(orig_dir)

            assert len(result["completed"]) == 2
            assert result["completed"][0]["name"] == "my-repo"
            assert result["completed"][1]["date"] == "2026-04-10"
            assert "add dark mode" in result["ideas"]

    def test_recently_done_repos_cooldown(self):
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=8)).strftime("%Y-%m-%d")

        queue_data = {
            "completed": [
                {"name": "fresh-repo", "date": today},  # within cooldown
                {"name": "old-repo", "date": week_ago},  # outside cooldown
            ]
        }
        recent = get_recently_done_repos(queue_data)
        assert "fresh-repo" in recent
        assert "old-repo" not in recent

    def test_empty_queue_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_dir = os.getcwd()
            os.chdir(tmpdir)
            try:
                result = read_queue()
            finally:
                os.chdir(orig_dir)
            assert result["completed"] == []
            assert result["ideas"] == []

    def test_7day_boundary(self):
        """Repos done exactly QUEUE_COOLDOWN_DAYS ago should still be skipped."""
        exactly_cutoff = (
            datetime.datetime.utcnow() - datetime.timedelta(days=QUEUE_COOLDOWN_DAYS)
        ).strftime("%Y-%m-%d")
        queue_data = {"completed": [{"name": "boundary-repo", "date": exactly_cutoff}]}
        recent = get_recently_done_repos(queue_data)
        assert "boundary-repo" in recent


# ── Inbox parsing (unit tests via mocked gh_api) ───────────────────────────────

class TestInboxParsing:
    def test_parse_focus_command(self):
        from researcher import read_inbox
        mock_issues = [
            {
                "number": 42,
                "title": "Focus on my-app",
                "body": "focus: my-app\nskip: old-repo",
            }
        ]
        with patch("researcher.gh_api", return_value=mock_issues):
            result = read_inbox()
        assert "my-app" in result["focus"]
        assert "old-repo" in result["skip"]
        assert result["pause"] is False
        assert 42 in result["issue_numbers"]

    def test_parse_pause_command(self):
        from researcher import read_inbox
        mock_issues = [{"number": 10, "title": "Pause", "body": "pause"}]
        with patch("researcher.gh_api", return_value=mock_issues):
            result = read_inbox()
        assert result["pause"] is True

    def test_parse_idea_command(self):
        from researcher import read_inbox
        mock_issues = [{"number": 5, "title": "Idea", "body": "idea: add streaming support"}]
        with patch("researcher.gh_api", return_value=mock_issues):
            result = read_inbox()
        assert "add streaming support" in result["ideas"]

    def test_empty_inbox(self):
        from researcher import read_inbox
        with patch("researcher.gh_api", return_value=[]):
            result = read_inbox()
        assert result["pause"] is False
        assert result["focus"] == []
        assert result["skip"] == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
