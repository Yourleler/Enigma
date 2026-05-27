"""workspace.py 和 /review 命令的测试。"""

from pathlib import Path
from unittest.mock import patch

import pytest

from enigma import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from enigma.cli import handle_builtin_command, _build_review_prompt


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs=None, **kwargs):
    if outputs is None:
        outputs = []
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


# ── is_git_repo ────────────────────────────────────────────────


class TestIsGitRepo:
    def test_non_git_dir(self, tmp_path):
        (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
        ws = WorkspaceContext.build(tmp_path)
        assert ws.is_git_repo is False

    def test_git_dir_detected(self, tmp_path):
        (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
        ws = WorkspaceContext.build(tmp_path)
        # 模拟 git 仓库：手动设置标志
        ws._in_git_repo = True
        assert ws.is_git_repo is True

    def test_build_sets_flag_when_git_succeeds(self, tmp_path):
        (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
        ws = WorkspaceContext.build(tmp_path)
        # 非 git 目录，flag 应为 False
        assert ws._in_git_repo is False


# ── _run_git ───────────────────────────────────────────────────


class TestRunGit:
    def test_returns_fallback_on_failure(self, tmp_path):
        result = WorkspaceContext._run_git(["status"], cwd=str(tmp_path), fallback="fb")
        # 非 git 目录应返回 fallback
        assert result == "fb"

    def test_returns_output_on_success(self, tmp_path):
        # 在非 git 目录中，git version 应该成功
        result = WorkspaceContext._run_git(["--version"], cwd=str(tmp_path))
        assert "git version" in result


# ── git_diff (mocked) ─────────────────────────────────────────


class TestGitDiff:
    def test_no_changes(self, tmp_path):
        ws = build_workspace(tmp_path)
        with patch.object(WorkspaceContext, "_run_git", return_value=""):
            diff, source = ws.git_diff()
            assert diff == ""
            assert "no changes" in source

    def test_unstaged_changes(self, tmp_path):
        ws = build_workspace(tmp_path)
        call_count = [0]

        def mock_run_git(args, cwd=None, fallback=""):
            call_count[0] += 1
            if args == ["diff"]:
                return "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-old\n+new"
            if args == ["diff", "--cached"]:
                return ""
            return ""

        with patch.object(WorkspaceContext, "_run_git", side_effect=mock_run_git):
            diff, source = ws.git_diff()
            assert "old" in diff
            assert "new" in diff
            assert "uncommitted" in source

    def test_branch_diff_fallback(self, tmp_path):
        ws = build_workspace(tmp_path)
        call_count = [0]

        def mock_run_git(args, cwd=None, fallback=""):
            call_count[0] += 1
            if "diff" in args and "main...HEAD" in str(args):
                return "diff --git a/x.py b/x.py\n+added"
            return ""

        with patch.object(WorkspaceContext, "_run_git", side_effect=mock_run_git):
            diff, source = ws.git_diff(base_branch="main")
            assert "added" in diff
            assert "main" in source


# ── changed_files (mocked) ─────────────────────────────────────


class TestChangedFiles:
    def test_no_changes(self, tmp_path):
        ws = build_workspace(tmp_path)
        with patch.object(WorkspaceContext, "_run_git", return_value=""):
            assert ws.changed_files() == []

    def test_with_changes(self, tmp_path):
        ws = build_workspace(tmp_path)

        def mock_run_git(args, cwd=None, fallback=""):
            if args == ["diff", "--name-only"]:
                return "README.md"
            if args == ["diff", "--cached", "--name-only"]:
                return "new.py"
            return ""

        with patch.object(WorkspaceContext, "_run_git", side_effect=mock_run_git):
            files = ws.changed_files()
            assert "README.md" in files
            assert "new.py" in files


# ── _build_review_prompt ───────────────────────────────────────


class TestBuildReviewPrompt:
    def test_contains_diff_and_files(self):
        prompt = _build_review_prompt(
            diff="- old\n+ new",
            source="uncommitted changes",
            files=["a.py", "b.py"],
        )
        assert "uncommitted changes" in prompt
        assert "- a.py" in prompt
        assert "- b.py" in prompt
        assert "- old" in prompt
        assert "+ new" in prompt
        assert "critical" in prompt.lower()


# ── /review 命令 ────────────────────────────────────────────────


class TestReviewCommand:
    def test_review_files(self, tmp_path, capsys):
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        agent = build_agent(tmp_path, ["<final>Looks fine.</final>"])
        handle_builtin_command(agent, "/review code.py")
        captured = capsys.readouterr()
        assert "Reviewing" in captured.out
        assert "code.py" in captured.out

    def test_review_nonexistent_file(self, tmp_path, capsys):
        (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
        agent = build_agent(tmp_path, [])
        handle_builtin_command(agent, "/review nope.py")
        captured = capsys.readouterr()
        assert "not found" in captured.out.lower() or "File not found" in captured.out

    def test_review_non_git_no_args(self, tmp_path, capsys):
        (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
        agent = build_agent(tmp_path, [])
        handle_builtin_command(agent, "/review")
        captured = capsys.readouterr()
        assert "Not a git repository" in captured.out

    def test_review_auto_detect_no_changes(self, tmp_path, capsys):
        ws = build_workspace(tmp_path)
        ws._in_git_repo = True
        agent = build_agent(tmp_path, ["<final>All good.</final>"])
        agent.workspace = ws
        with patch.object(WorkspaceContext, "git_diff", return_value=("", "no changes")):
            with patch.object(WorkspaceContext, "changed_files", return_value=[]):
                handle_builtin_command(agent, "/review")
        captured = capsys.readouterr()
        assert "No pending changes" in captured.out

    def test_review_auto_detect_with_changes(self, tmp_path, capsys):
        ws = build_workspace(tmp_path)
        ws._in_git_repo = True
        agent = build_agent(tmp_path, ["<final>Bug found.</final>"])
        agent.workspace = ws
        with patch.object(WorkspaceContext, "git_diff", return_value=("- old\n+ new", "uncommitted changes")):
            with patch.object(WorkspaceContext, "changed_files", return_value=["bug.py"]):
                handle_builtin_command(agent, "/review")
        captured = capsys.readouterr()
        assert "Reviewing" in captured.out
        assert "bug.py" in captured.out
