"""工作区快照工具。

这个模块负责在 agent 按需读文件之前，先给它一份便宜的"仓库第一印象"。
这份快照刻意保持小而稳定：主要包含 Git 事实和少量白名单项目文档。
"""

import subprocess
import textwrap
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
# 这些文件最可能直接影响 agent 的行动方式。
# 我们不会预加载整个仓库，只会先给模型一小份"导航包"。
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
IGNORED_PATH_NAMES = {".git", ".enigma", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}


def now():
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


PROJECT_MARKERS = {".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"}


def _find_project_root(cwd):
    """非 git 仓库时，尝试在子目录中找项目根。
    如果 cwd 没有项目标记，但恰好只有一个子目录有，就用那个。
    """
    if any((cwd / marker).exists() for marker in PROJECT_MARKERS):
        return cwd
    try:
        subdirs = [
            d for d in cwd.iterdir()
            if d.is_dir() and d.name not in IGNORED_PATH_NAMES and not d.name.startswith(".")
        ]
    except PermissionError:
        return cwd
    if len(subdirs) != 1:
        return cwd
    candidate = subdirs[0]
    if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
        return candidate
    return cwd


def _top_level_summary(repo_root):
    """生成顶层目录摘要，如 'enigma/ (12 .py), tests/ (8 .py), docs/ (3 .md)'"""
    root = Path(repo_root)
    try:
        entries = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name not in IGNORED_PATH_NAMES and not d.name.startswith(".")),
            key=lambda d: d.name.lower(),
        )
    except PermissionError:
        return "(permission denied)"
    if not entries:
        return "(empty)"
    parts = []
    shown = entries[:20]
    for d in shown:
        try:
            items = list(d.iterdir())
            py_count = sum(1 for f in items if f.is_file() and f.suffix == ".py")
            total = sum(1 for f in items if f.is_file())
            if py_count > 0:
                parts.append(f"{d.name}/ ({py_count} .py)")
            else:
                parts.append(f"{d.name}/ ({total} files)")
        except PermissionError:
            parts.append(f"{d.name}/ (?)")
    summary = ", ".join(parts)
    if len(entries) > 20:
        summary += f" (+{len(entries) - 20} more)"
    return summary


class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs, in_git_repo=False):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs
        self._in_git_repo = in_git_repo

    @classmethod
    def build(cls, cwd, repo_root_override=None):
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        git_toplevel = git(["rev-parse", "--show-toplevel"])
        in_git_repo = bool(git_toplevel)
        repo_root = (
            Path(repo_root_override).resolve()
            if repo_root_override is not None
            else Path(git_toplevel or str(cwd)).resolve()
        )

        # 非 git 仓库时，尝试在子目录中找项目根
        if not in_git_repo and not repo_root_override:
            repo_root = _find_project_root(cwd)
        docs = {}
        # 同时扫描 repo_root 和 cwd，这样在子目录启动时也能看到本地文档；
        # 但用相对路径做 key，避免同一份文档被重复收集。
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(
                lambda branch: branch[len("origin/") :] if branch.startswith("origin/") else branch
            )(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
            in_git_repo=in_git_repo,
        )

    def text(self):
        # 这段文本会被塞进 prompt prefix，作为相对稳定的基线上下文。
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        structure = _top_level_summary(self.repo_root)
        return textwrap.dedent(
            f"""\
            Workspace:
            - cwd: {self.cwd}
            - repo_root: {self.repo_root}
            - structure: {structure}
            - branch: {self.branch}
            - default_branch: {self.default_branch}
            - status:
            {self.status}
            - recent_commits:
            {commits}
            - project_docs:
            {docs}
            """
        ).strip()

    def fingerprint(self):
        # 这个指纹用来判断仓库状态是否发生了足够大的变化，
        # 从而决定是否需要重建缓存中的 prompt prefix。
        # 包含顶层目录名，这样新增/删除文件夹时能触发刷新。
        try:
            dir_names = sorted(
                d.name for d in Path(self.repo_root).iterdir()
                if d.is_dir() and d.name not in IGNORED_PATH_NAMES and not d.name.startswith(".")
            )
        except (PermissionError, OSError):
            dir_names = []
        payload = {
            "cwd": self.cwd,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "status": self.status,
            "recent_commits": list(self.recent_commits),
            "project_docs": dict(self.project_docs),
            "top_dirs": dir_names,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    # ── git diff 辅助方法 ──────────────────────────────────────────

    @staticmethod
    def _run_git(args, cwd=None, fallback=""):
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout.strip() or fallback
        except Exception:
            return fallback

    @property
    def is_git_repo(self):
        return self._in_git_repo

    def git_diff(self, base_branch=None):
        """获取 git diff。返回 (diff_text, source_description)。

        优先级：未提交变更 > 当前分支 vs 默认分支的 diff。
        """
        if base_branch is None:
            base_branch = self.default_branch or "main"
        cwd = self.repo_root
        # 未暂存 + 已暂存的变更
        unstaged = self._run_git(["diff"], cwd=cwd)
        staged = self._run_git(["diff", "--cached"], cwd=cwd)
        combined = (staged + "\n" + unstaged).strip()
        if combined:
            source = "uncommitted changes"
            if staged and unstaged:
                source = "staged + unstaged changes"
            elif staged:
                source = "staged changes"
            return combined, source
        # 无未提交变更，对比默认分支
        diff = self._run_git(["diff", f"{base_branch}...HEAD"], cwd=cwd)
        if diff:
            return diff, f"branch '{self.branch}' vs '{base_branch}'"
        return "", "no changes"

    def changed_files(self, base_branch=None):
        """返回变更文件路径列表。"""
        if base_branch is None:
            base_branch = self.default_branch or "main"
        cwd = self.repo_root
        # 未提交变更
        files = self._run_git(["diff", "--name-only"], cwd=cwd)
        staged = self._run_git(["diff", "--cached", "--name-only"], cwd=cwd)
        combined = (staged + "\n" + files).strip()
        if combined:
            return [f for f in combined.splitlines() if f]
        # 对比默认分支
        result = self._run_git(["diff", "--name-only", f"{base_branch}...HEAD"], cwd=cwd)
        return [f for f in result.splitlines() if f] if result else []

    def pr_diff(self, pr_number):
        """通过 gh CLI 拉取 PR 的 diff。返回 (diff_text, error_message)。"""
        try:
            result = subprocess.run(
                ["gh", "pr", "diff", str(pr_number)],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            diff = result.stdout.strip()
            if diff:
                return diff, ""
            return "", "PR diff is empty"
        except FileNotFoundError:
            return "", "gh CLI not found. Install from https://cli.github.com/"
        except subprocess.CalledProcessError as exc:
            return "", f"gh pr diff failed: {exc.stderr.strip()}"
