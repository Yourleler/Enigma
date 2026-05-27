"""Agent 运行时核心逻辑。

Enigma 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import sys
import textwrap
import uuid
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import memory as memorylib
from . import display as displaymod
from .context_manager import ContextManager, estimate_tokens
from .models import should_compact
from .run_store import RunStore
from .skills import build_skill_metadata_block
from .storage import SessionDB
from .task_state import TaskState
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
DEFAULT_SHELL_ENV_ALLOWLIST = (
    "COMSPEC",
    "ComSpec",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PWD",
    "SHELL",
    "SYSTEMROOT",
    "SystemRoot",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "USER",
    "WINDIR",
    "windir",
)
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
    "reflection": True,
}
CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"
DURABLE_MEMORY_INTENT_PATTERN = re.compile(r"(?i)\b(capture|remember|save|store|persist|note|semantic memory)\b")
DURABLE_MEMORY_INTENT_ZH_PATTERN = re.compile(r"(记住|保存|记录|沉淀|长期记忆|持久记忆|语义记忆)")
DURABLE_MEMORY_LINE_PATTERNS = (
    ("project-conventions", re.compile(r"(?i)^Project convention:\s*(.+)$")),
    ("key-decisions", re.compile(r"(?i)^Decision:\s*(.+)$")),
    ("dependency-facts", re.compile(r"(?i)^Dependency:\s*(.+)$")),
    ("user-preferences", re.compile(r"(?i)^Preference:\s*(.+)$")),
    ("project-conventions", re.compile(r"^项目约定：\s*(.+)$")),
    ("key-decisions", re.compile(r"^决策：\s*(.+)$")),
    ("dependency-facts", re.compile(r"^依赖：\s*(.+)$")),
    ("user-preferences", re.compile(r"^偏好：\s*(.+)$")),
)
SECRET_SHAPED_TEXT_PATTERN = re.compile(r"(?i)(\b(api[_ -]?key|token|secret|password)\b|sk-[A-Za-z0-9_-]{6,})")
COMPACT_HEAD_HISTORY_LIMIT = 2
COMPACT_TAIL_HISTORY_LIMIT = 6
COMPACT_MAX_NEW_TOKENS = 1200
REFLECTION_MAX_NEW_TOKENS = 800
PLAN_FILE_NAME = "plan.md"

REFLECTION_SYSTEM_PROMPT = """\
You are a memory curator for a coding agent. Your job is to review the agent's work history and extract stable facts worth keeping across sessions.

Rules:
- Only extract facts that remain true independent of this specific session.
- If nothing is worth extracting, return all empty lists. Empty output is normal, not a failure.
- Do NOT summarize what the agent did. Do NOT restate session events as facts.
- Each item must be self-contained (understandable without context), under 200 characters.
- No secrets, tokens, API keys, stdout/stderr, tracebacks, or transient state.
- When in doubt, leave it out. Missing one fact is better than adding one noise.
- You output the COMPLETE desired state for each topic you include. It will REPLACE all existing notes in that topic.
- Each topic: at most 25 items. If existing + new exceeds 25, keep only the most important.

What IS worth extracting:
- Project conventions (tech choices, naming rules, testing patterns, build commands)
- Key decisions (architecture choices, trade-offs the user confirmed)
- Dependency facts (pinned versions, known incompatibilities, required env vars)
- User preferences (style preferences, workflow habits, explicit requests)

What is NOT worth extracting:
- Single tool call details, one-time errors, transient blockers
- "Agent completed X" / "Agent fixed Y" — these are events, not facts
- Anything that would become stale after the next session

Output JSON with exactly these keys (use empty lists for no updates):
{
  "project-conventions": [],
  "key-decisions": [],
  "dependency-facts": [],
  "user-preferences": []
}
"""


@dataclass
class PromptPrefix:
    # prefix 除了文本本身，还带一小份元数据，
    # 这样 runtime 才能明确判断 prefix 是否可以复用。
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str


@dataclass
class PlanResult:
    plan: str
    session_id: str


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class Enigma:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        run_store=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        total_budget=None,
        depth=0,
        max_depth=1,
        read_only=False,
        plan_mode=False,
        shell_env_allowlist=None,
        secret_env_names=None,
        feature_flags=None,
        show_tool_activity=False,
        show_token_usage=False,
        tool_activity_stream=None,
    ):
        # 保存外部已经装配好的核心依赖：模型客户端、工作区快照和 session 存储。
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store

        # 记录本次运行的策略开关和执行上限，后续 ask/tool 循环都会读取这些配置。
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.plan_mode = plan_mode
        self.show_tool_activity = bool(show_tool_activity)
        self.show_token_usage = bool(show_token_usage)
        self.tool_activity_stream = tool_activity_stream or sys.stdout

        # shell 环境只暴露白名单变量；secret(就是不希望记录的隐私信息) 名称统一转大写，便于后续脱敏判断。
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}

        # 默认功能开关先铺底，再用调用方传入的 feature_flags 覆盖。
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key, value in feature_flags.items()})

        # run_store 负责保存单次 ask 运行产物；没有传入时放到仓库的 .enigma/runs 下。
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".enigma" / "runs")

        # 如果是恢复旧会话，直接使用传入的 session；否则创建一个新的会话骨架。
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()

        # LayeredMemory 包装 session 中的 memory 字典，提供读写和持久化前的标准形状。
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root=self.root,
        )
        self.session["memory"] = self.memory.to_dict()

        # 初始化工具表、系统 prompt prefix 和上下文管理器，它们共同决定每轮模型输入。
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        cm_kwargs = {}
        if total_budget is not None:
            cm_kwargs["total_budget"] = total_budget
        self.context_manager = ContextManager(self, **cm_kwargs)

        # SessionDB：跨会话的长期档案（SQLite + FTS5）。
        # 注意：它不是当前轮 prompt 的来源，只用来搜索旧会话和保存滚动摘要。
        # 任何失败都不能阻塞 agent 主流程，所以失败时退化成 None。
        self.session_db = self._open_session_db()
        self._register_session_in_db()

        # 计算恢复状态并立即保存 session，确保新建或恢复后的会话路径可用。
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)

        # 以下字段记录当前运行中的临时状态和最近一次 ask 的元信息。
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self._last_reflection_signature = ""
        self._last_tool_result_metadata = {}
        self._last_step_results = []
        self._last_preview_lines = 0
        self._last_result_compact = ""
        self._last_result_header = ""
        self._last_preview_header_printed = False
        self._last_diff_info = None
        self._cancel_requested = False
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        self.session.setdefault("history", [])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", {})
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items", {})
        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}
        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}
        compact_summary = self.session.setdefault("compact_summary", {})
        if not isinstance(compact_summary, dict):
            self.session["compact_summary"] = {}

    def current_runtime_identity(self):
        return {
            "session_id": self.session.get("id", ""),
            "cwd": str(self.root),
            "model": str(getattr(self.model_client, "model", "")),
            "model_client": self.model_client.__class__.__name__,
            "approval_policy": self.approval_policy,
            "read_only": bool(self.read_only),
            "plan_mode": bool(self.plan_mode),
            "max_steps": int(self.max_steps),
            "max_new_tokens": int(self.max_new_tokens),
            "feature_flags": dict(self.feature_flags),
            "shell_env_allowlist": list(self.shell_env_allowlist),
            "workspace_fingerprint": getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", self.workspace.fingerprint()),
            "tool_signature": self.tool_signature(),
        }

    def checkpoint_state(self):
        self._ensure_session_shape()
        return self.session["checkpoints"]

    def current_checkpoint(self):
        state = self.checkpoint_state()
        checkpoint_id = str(state.get("current_id", "")).strip()
        if not checkpoint_id:
            return None
        return state.get("items", {}).get(checkpoint_id)

    def invalidate_stale_memory(self):
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        previous_resume_state = dict(self.session.get("resume_state", {}) or {})
        invalidated = self.invalidate_stale_memory()
        checkpoint = self.current_checkpoint()
        status = CHECKPOINT_NONE_STATUS
        stale_paths = list(invalidated)
        mismatch_fields = []
        if checkpoint:
            if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
            else:
                for item in checkpoint.get("key_files", []):
                    path = str(item.get("path", "")).strip()
                    if not path:
                        continue
                    expected = item.get("freshness")
                    current = memorylib.file_freshness(path, self.root)
                    if expected != current and path not in stale_paths:
                        stale_paths.append(path)
                saved_identity = dict(checkpoint.get("runtime_identity", {}) or self.session.get("runtime_identity", {}) or {})
                current_identity = self.current_runtime_identity()
                identity_keys = (
                    "cwd",
                    "model",
                    "model_client",
                    "approval_policy",
                    "read_only",
                    "plan_mode",
                    "max_steps",
                    "max_new_tokens",
                    "feature_flags",
                    "shell_env_allowlist",
                    "workspace_fingerprint",
                    "tool_signature",
                )
                for key in identity_keys:
                    if key not in saved_identity:
                        continue
                    if saved_identity.get(key) != current_identity.get(key):
                        mismatch_fields.append(key)
                mismatch_fields.sort()
                if stale_paths:
                    status = CHECKPOINT_PARTIAL_STALE_STATUS
                elif mismatch_fields:
                    status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
                else:
                    status = CHECKPOINT_FULL_VALID_STATUS

        resume_state = {
            "status": status,
            "stale_paths": stale_paths,
            "runtime_identity_mismatch_fields": mismatch_fields,
            "stale_summary_invalidations": max(
                len(invalidated),
                int(previous_resume_state.get("stale_summary_invalidations", 0))
                if status == CHECKPOINT_PARTIAL_STALE_STATUS
                else 0,
            ),
        }
        self.session["resume_state"] = resume_state
        self.session["runtime_identity"] = self.current_runtime_identity()
        return resume_state

    def render_checkpoint_text(self):
        checkpoint = self.current_checkpoint()
        if not checkpoint:
            return ""
        lines = [
            "Task checkpoint:",
            f"- Resume status: {self.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
            f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
            f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
            f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
        ]
        current_plan = self.checkpoint_list(checkpoint.get("current_plan", []))
        open_questions = self.checkpoint_list(checkpoint.get("open_questions", []))
        confirmed_findings = self.checkpoint_list(checkpoint.get("confirmed_findings", []))
        blocked_on = str(checkpoint.get("blocked_on", "")).strip()
        next_action = str(checkpoint.get("next_action", "")).strip()
        if current_plan:
            lines.append("- Current plan: " + " | ".join(current_plan))
        if confirmed_findings:
            lines.append("- Confirmed findings: " + " | ".join(confirmed_findings))
        if open_questions:
            lines.append("- Open questions: " + " | ".join(open_questions))
        if blocked_on:
            lines.append(f"- Blocked on: {blocked_on}")
        if next_action:
            lines.append(f"- Next action: {next_action}")
        key_files = [str(item.get("path", "")).strip() for item in checkpoint.get("key_files", []) if str(item.get("path", "")).strip()]
        lines.append(f"- Key files: {', '.join(key_files) or '-'}")
        if checkpoint.get("completed"):
            lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
        if checkpoint.get("excluded"):
            lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))
        if self.resume_state.get("stale_paths"):
            lines.append("- Stale paths: " + ", ".join(self.resume_state["stale_paths"]))
        summary = str(checkpoint.get("summary", "")).strip()
        if summary:
            lines.append(f"- Summary: {summary}")
        return "\n".join(lines)

    @staticmethod
    def checkpoint_list(value):
        if isinstance(value, (list, tuple, set)):
            items = value
        elif value in (None, ""):
            items = []
        else:
            items = [value]
        return [str(item).strip() for item in items if str(item).strip()]

    @staticmethod
    def merge_checkpoint_lists(*groups, limit=8):
        merged = []
        seen = set()
        for group in groups:
            for item in Enigma.checkpoint_list(group):
                key = item.lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def _get_plan_path(self):
        return Path(self.root) / ".enigma" / PLAN_FILE_NAME

    def _cleanup_plan_file(self):
        plan_path = self._get_plan_path()
        if plan_path.exists():
            try:
                plan_path.unlink()
            except OSError:
                pass

    def build_tools(self):
        tools = toolkit.build_tool_registry(self)
        if self.plan_mode:
            tools = {name: spec for name, spec in tools.items() if not spec.get("risky", False)}
        return tools

    def tool_signature(self):
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": tool["schema"],
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def build_prefix(self):
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
            risk = "approval required" if tool["risky"] else "safe"
            tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        if self.plan_mode:
            examples = "\n".join(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
                    '<tool>{"name":"search","args":{"pattern":"def main","path":"."}}</tool>',
                    '<tool>{"name":"web_search","args":{"query":"Python asyncio best practices","max_results":5}}</tool>',
                    '<plan>\n## Goal\nAdd a --version flag to the CLI\n\n## Files to modify\n- cli.py: add --version argument\n- __init__.py: export __version__\n\n## Steps\n1. ...\n</plan>',
                ]
            )
            text = textwrap.dedent(
                f"""\
                You are enigma in PLAN MODE. Your job is to explore the codebase and design an implementation plan.

                Rules:
                - You are in READ-ONLY phase. Only use the tools listed below (all safe/read-only). Do not attempt any file writes or shell commands.
                - Return exactly one <tool>...</tool> or one <plan>...</plan>.
                - Tool calls must look like:
                  <tool>{{"name":"tool_name","args":{{...}}}}</tool>
                - Focus on understanding the user's request and the code associated with their request.
                - Actively search for existing functions, utilities, and patterns that can be reused.
                - Explore enough to answer: what files need to change, what existing code to reuse, what risks exist. Stop exploring once these are clear.
                - When ready, output your plan as:
                  <plan>
                  ## Goal
                  <what needs to be done and why>

                  ## Files to modify
                  <list of files and what changes are needed in each>

                  ## Steps
                  <numbered implementation steps, concrete enough to execute>

                  ## Risks / open questions
                  <anything uncertain, trade-offs, things to verify>
                  </plan>
                - Keep the plan concrete and actionable. Each step should be verifiable.
                - If the user provided feedback on a previous plan, address it in the revised plan.

                Tools:
                {tool_text}

                Valid response examples:
                {examples}

                {self.workspace.text()}
                """
            ).strip()
        else:
            examples = "\n".join(
                [
                    '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                    '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
                    '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
                    '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
                    '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
                    '<tool>{"name":"web_search","args":{"query":"Python asyncio best practices","max_results":5}}</tool>',
                    '<tool>{"message":"Delegating investigation to a sub-agent.","name":"delegate","args":{"task":"inspect how auth is wired in this repo","max_steps":3}}</tool>',
                    "<final>Done.</final>",
                ]
            )
            # prefix 可以理解成 agent 的"工作手册"：
            # 它是谁、工具怎么调用、当前仓库是什么状态，都写在这里。
            plan_path = self._get_plan_path()
            plan_hint = ""
            if plan_path.exists():
                plan_hint = (
                    f"\n- An approved plan is stored at {plan_path}. "
                    "Read it at the start and before each major step to stay on track."
                )
            skill_block = build_skill_metadata_block(getattr(self, "skills", {}))
            if skill_block:
                skill_block = "\n" + skill_block
            text = textwrap.dedent(
                f"""\
                You are enigma, a small local coding agent working inside a local repository.

                Rules:
                - Use tools instead of guessing about the workspace.
                - Return exactly one <tool>...</tool> or one <final>...</final>.
                - Tool calls must look like:
                  <tool>{{"name":"tool_name","args":{{...}}}}</tool>
                - Tool calls may include a brief user-facing message:
                  <tool>{{"message":"I will inspect the file first.","name":"read_file","args":{{"path":"README.md"}}}}</tool>
                - For write_file and patch_file with multi-line text, prefer XML style:
                  <tool name="write_file" path="file.py"><content>...</content></tool>
                - Final answers must look like:
                  <final>your answer</final>
                - Never invent tool results.
                - Keep answers concise and concrete.
                - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
                - Use web_search for current information, news, prices, latest versions, or external documentation changes.
                - When answering from web_search results, include source URLs and mention conflicts between sources if they matter.
                - Before writing tests for existing code, read the implementation first.
                - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
                - New files should be complete and runnable, including obvious imports.
                - Use delegate to investigate complex questions in isolation (e.g. "how is auth wired?", "what does this module do?"). The child agent is read-only and returns a summary. Prefer delegate over long manual exploration when the question is broad.
                - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
                - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, web_search, or delegate with args={{}}.
                - When "启动记忆 (MEMORY.md)" appears, treat it as stable cross-session facts; do not restate them to the user unless asked.
                - When "会话滚动摘要" appears, it is the compressed state of earlier turns; continue the task from that state instead of restarting.
                - Skills listed below are user-invocable via /<name>. When the user types /<name>, follow the skill's instructions.{plan_hint}{skill_block}

                Tools:
                {tool_text}

                Valid response examples:
                {examples}

                {self.workspace.text()}
                """
            ).strip()
        return PromptPrefix(
            text=text,
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint=self.workspace.fingerprint(),
            tool_signature=self.tool_signature(),
            built_at=now(),
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text

    def refresh_prefix(self, force=False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；
        # 只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)

        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed,
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()

    def rolling_summary_text(self):
        """当前会话的滚动摘要。先查 SessionDB，没有就 fallback 到 session.compact_summary。"""
        state = None
        if self.session_db is not None:
            try:
                state = self.session_db.get_session_state(self.session["id"])
            except Exception:
                state = None
        if state and str(state.get("rolling_summary") or "").strip():
            return str(state["rolling_summary"]).strip()
        fallback = (self.session.get("compact_summary") or {}).get("text", "")
        return str(fallback or "").strip()

    def _open_session_db(self):
        db_path = Path(self.root) / ".enigma" / "sessions" / "state.db"
        try:
            return SessionDB(db_path)
        except Exception:
            return None

    def _register_session_in_db(self):
        if self.session_db is None:
            return
        try:
            self.session_db.start_session(
                self.session["id"],
                parent_id=self.session.get("parent_id"),
                title=clip(str(self.session.get("title", "")), 200) or None,
                model=str(getattr(self.model_client, "model", "")) or None,
                cwd=str(self.root),
            )
        except Exception:
            pass

    def _log_db_message(self, role, kind, content, **extra):
        if self.session_db is None:
            return
        try:
            self.session_db.append_message(
                self.session["id"],
                role,
                kind,
                self.redact_text(str(content or "")),
                token_estimate=estimate_tokens(str(content or "")),
                **extra,
            )
        except Exception:
            pass

    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name), False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def append_session_history(self, item):
        # 追加一条 user/assistant/tool 记录到当前 session history，并立即保存会话文件。
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)
        self._mirror_history_item_to_db(item)

    def _mirror_history_item_to_db(self, item):
        """把 history 条目镜像写入 SessionDB，用于跨会话全文检索。"""
        if self.session_db is None or not isinstance(item, dict):
            return
        role = str(item.get("role", ""))
        if role == "tool":
            args = item.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            self._log_db_message(
                role="tool",
                kind="tool_result",
                content=item.get("content", ""),
                tool_name=str(item.get("name") or "") or None,
                file_path=str(args.get("path") or "") or None,
                command=str(args.get("command") or "") or None,
            )
            return
        kind = "message"
        content = str(item.get("content", ""))
        if content.startswith("<plan>"):
            kind = "plan"
        elif role == "system" and item.get("kind") == "compact_summary":
            kind = "compact_summary"
        self._log_db_message(role=role or "assistant", kind=kind, content=content)

    def compact_context(self, focus=""):
        """显式压缩工作记忆、情景记忆、语义记忆和 history。"""
        focus = str(focus or "").strip()
        before_history = list(self.session.get("history", []))
        before_memory = json.loads(json.dumps(self.memory.to_dict()))
        self.reflect_and_update_semantic_memory()  # 压缩前反思，趁情景笔记还没被磨碎
        prompt = self.build_compact_prompt(focus=focus)
        raw = self.model_client.complete(prompt, max(COMPACT_MAX_NEW_TOKENS, self.max_new_tokens))
        payload = self.parse_compact_response(raw)
        self.apply_compact_payload(payload, focus=focus, original_history=before_history, original_memory=before_memory)
        return self.compact_result_message(before_history_count=len(before_history))

    def build_compact_prompt(self, focus=""):
        history = list(self.session.get("history", []))
        head = history[:COMPACT_HEAD_HISTORY_LIMIT]
        tail = history[-COMPACT_TAIL_HISTORY_LIMIT:] if len(history) > COMPACT_TAIL_HISTORY_LIMIT else history
        middle_end = max(COMPACT_HEAD_HISTORY_LIMIT, len(history) - COMPACT_TAIL_HISTORY_LIMIT)
        middle = history[COMPACT_HEAD_HISTORY_LIMIT:middle_end]
        semantic_notes = {}
        if self.memory.durable_store is not None:
            for topic in self.memory.durable_store.load_index():
                slug = topic["topic"]
                semantic_notes[slug] = [
                    note["text"] for note in self.memory.durable_store.load_topic_notes(slug)
                ]
        payload = {
            "focus": focus,
            "previous_compact_summary": self.session.get("compact_summary", {}),
            "working_memory": self.memory.to_dict().get("working", {}),
            "file_summaries": self.memory.to_dict().get("file_summaries", {}),
            "episodic_notes": self.memory.to_dict().get("episodic_notes", []),
            "semantic_memory": semantic_notes,
            "history_head": head,
            "history_middle": middle,
            "history_tail": tail,
        }
        return textwrap.dedent(
            f"""\
            You are compacting Enigma's local agent context. Return JSON only.

            Goal:
            - Update the rolling compact summary instead of rewriting from scratch.
            - Preserve durable facts, user preferences, decisions, relevant files, blockers, and next steps.
            - Compress early episodic notes and old history more aggressively than recent items.
            - Keep working memory useful but compact.
            - Do not include secrets, API keys, tokens, stdout/stderr dumps, tracebacks, or transient checkpoint fields.
            - Do not invent file paths or freshness hashes.

            Return this JSON shape:
            {{
              "compact_summary": "rolling summary",
              "working_memory": {{
                "task_summary": "short task summary",
                "file_summaries": {{"path/from/input": "compressed summary"}}
              }},
              "episodic_notes": [
                {{"text": "compressed note", "tags": ["tag"], "source": "source", "kind": "episodic"}}
              ]
            }}

            Note: Do NOT include semantic_memory in your output. Semantic memory is managed separately.

            Context JSON:
            {json.dumps(self.redact_artifact(payload), ensure_ascii=False, indent=2)}
            """
        ).strip()

    def parse_compact_response(self, raw):
        text = str(raw or "").strip()
        if "<final>" in text:
            text = self.extract(text, "final").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"compact failed: model returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("compact failed: model response must be a JSON object")
        self.validate_compact_payload(payload)
        return payload

    def validate_compact_payload(self, payload):
        if not str(payload.get("compact_summary", "")).strip():
            raise RuntimeError("compact failed: compact_summary is required")
        working_memory = payload.get("working_memory")
        if not isinstance(working_memory, dict):
            raise RuntimeError("compact failed: working_memory must be an object")
        file_summaries = working_memory.get("file_summaries", {})
        if not isinstance(file_summaries, dict):
            raise RuntimeError("compact failed: working_memory.file_summaries must be an object")
        episodic_notes = payload.get("episodic_notes")
        if not isinstance(episodic_notes, list):
            raise RuntimeError("compact failed: episodic_notes must be a list")
        for index, item in enumerate(episodic_notes):
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                raise RuntimeError(f"compact failed: episodic_notes[{index}] must be a string or object")
            if not str(item.get("text", "")).strip():
                raise RuntimeError(f"compact failed: episodic_notes[{index}].text is required")
            tags = item.get("tags", [])
            if tags is not None and not isinstance(tags, (list, tuple, set, str)):
                raise RuntimeError(f"compact failed: episodic_notes[{index}].tags must be a list or string")

    def apply_compact_payload(self, payload, focus, original_history, original_memory):
        memory_state = memorylib.normalize_memory_state(original_memory, self.root)
        working = memory_state["working"]
        compact_working = payload.get("working_memory", {})
        if isinstance(compact_working, dict):
            task_summary = str(compact_working.get("task_summary", "")).strip()
            if task_summary:
                working["task_summary"] = clip(task_summary, 300)
                memory_state["task"] = working["task_summary"]
            compact_summaries = compact_working.get("file_summaries", {})
            if isinstance(compact_summaries, dict):
                for raw_path, raw_summary in compact_summaries.items():
                    path = memorylib.canonicalize_path(raw_path, self.root)
                    if path not in memory_state.get("file_summaries", {}):
                        continue
                    summary = clip(str(raw_summary).strip(), 500)
                    if summary:
                        memory_state["file_summaries"][path]["summary"] = summary

        compact_notes = payload.get("episodic_notes", [])
        if isinstance(compact_notes, list):
            memory_state["episodic_notes"] = []
            memory_state["notes"] = []
            memory_state["next_note_index"] = 0
            for item in compact_notes:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    tags = item.get("tags", ())
                    source = item.get("source", "")
                    kind = item.get("kind", "episodic")
                else:
                    text = item
                    tags = ()
                    source = ""
                    kind = "episodic"
                if self.reject_durable_reason(text):
                    continue
                memory_state = memorylib.append_note(
                    memory_state,
                    text,
                    tags=tags,
                    source=source,
                    created_at=now(),
                    workspace_root=self.root,
                    kind=kind,
                )

        self.memory = memorylib.LayeredMemory(memory_state, workspace_root=self.root)
        # 稳定记忆（语义记忆/MEMORY.md）始终从磁盘读，不让 compact 的模型输出覆盖。
        # 这样 /compact 之后，下一轮 prompt 依然会自动重新注入启动记忆和主题召回。
        # semantic_memory 字段即便存在也只作为提示保留，不写回磁盘。

        self.session["memory"] = self.memory.to_dict()
        summary_text = clip(str(payload.get("compact_summary", "")).strip(), 2000)
        self.session["compact_summary"] = {
            "text": summary_text,
            "focus": focus,
            "updated_at": now(),
        }
        self.session["history"] = self.compacted_history(original_history, summary_text, focus)
        self.session_path = self.session_store.save(self.session)
        if self.session_db is not None and summary_text:
            try:
                self.session_db.update_session_state(
                    self.session["id"],
                    rolling_summary=summary_text,
                    recent_files=list(self.memory.to_dict().get("working", {}).get("recent_files", [])),
                )
                self.session_db.append_message(
                    self.session["id"],
                    role="system",
                    kind="compact_summary",
                    content=self.redact_text(summary_text),
                    metadata={"focus": focus},
                    token_estimate=estimate_tokens(summary_text),
                )
            except Exception:
                pass

    def compacted_history(self, history, summary_text, focus):
        history = list(history)
        head = history[:COMPACT_HEAD_HISTORY_LIMIT]
        tail = history[-COMPACT_TAIL_HISTORY_LIMIT:] if len(history) > COMPACT_TAIL_HISTORY_LIMIT else history
        if len(history) <= COMPACT_HEAD_HISTORY_LIMIT + COMPACT_TAIL_HISTORY_LIMIT:
            head = []
            tail = history
        compact_event = {
            "role": "system",
            "content": "compact summary"
            + (f" (focus: {focus})" if focus else "")
            + ":\n"
            + summary_text,
            "created_at": now(),
            "kind": "compact_summary",
        }
        return [*head, compact_event, *tail]

    def compact_result_message(self, before_history_count):
        after_history_count = len(self.session.get("history", []))
        note_count = len(self.memory.to_dict().get("episodic_notes", []))
        return (
            "compact complete: "
            f"history {before_history_count}->{after_history_count}, "
            f"情景记忆 {note_count}, "
            f"summary {len(self.session.get('compact_summary', {}).get('text', ''))} chars"
        )

    @staticmethod
    def looks_sensitive_env_name(name):
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def configured_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if str(name).upper() in self.secret_env_names and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def detected_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if self.is_secret_env_name(name) and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.configured_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def detected_secret_env_summary(self):
        names = [name for name, _ in self.detected_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def redact_text(self, text):
        text = str(text)
        for _, value in sorted(self.detected_secret_env_items(), key=lambda item: len(item[1]), reverse=True):
            text = text.replace(value, REDACTED_VALUE)
        return text

    def redact_artifact(self, value, key=None):
        if key and self.is_secret_env_name(key):
            return REDACTED_VALUE
        if isinstance(value, dict):
            return {
                str(item_key): self.redact_artifact(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, str):
            redacted = self.redact_text(value)
            return redacted
        return value

    def shell_env(self):
        env = {
            name: os.environ[name]
            for name in self.shell_env_allowlist
            if name in os.environ
        }
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        if os.name == "nt":
            system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
            comspec = os.environ.get("ComSpec") or os.environ.get("COMSPEC") or str(Path(system_root) / "System32" / "cmd.exe")
            env.setdefault("SystemRoot", system_root)
            env.setdefault("SYSTEMROOT", system_root)
            env.setdefault("windir", os.environ.get("windir", system_root))
            env.setdefault("WINDIR", os.environ.get("WINDIR", system_root))
            env.setdefault("ComSpec", comspec)
            env.setdefault("COMSPEC", comspec)
        return env

    def shell_executable(self):
        if os.name != "nt":
            return None
        env = self.shell_env()
        for candidate in (env.get("ComSpec"), env.get("COMSPEC")):
            if candidate and Path(candidate).exists():
                return candidate
        fallback = Path(env.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        return str(fallback) if fallback.exists() else None

    def prompt_metadata(self, user_message, prompt):
        _, metadata = self._build_prompt_and_metadata(user_message)
        return metadata

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        # 这里把"这轮 prompt 是怎么拼出来的"连同缓存相关状态一起记下来，
        # 后面 trace/report 才能解释清楚：为什么这一轮 prefix 变了、缓存有没有命中。
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": self.prefix_state.hash,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def emit_trace(self, task_state, event, payload=None):
        payload = self.redact_artifact(payload or {})
        payload["event"] = event
        payload["created_at"] = now()
        # trace 是运行中的逐事件时间线，适合回答"这一轮 agent 到底做了什么"。
        self.run_store.append_trace(task_state, payload)
        return payload

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created:{path}")
            elif path not in after:
                summaries.append(f"deleted:{path}")
            else:
                summaries.append(f"modified:{path}")
        return changed_paths, summaries

    def create_checkpoint(self, task_state, user_message, trigger):
        state = self.checkpoint_state()
        current = self.current_checkpoint()
        checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
        key_files = []
        freshness = {}
        for path in self.memory.to_dict()["working"]["recent_files"]:
            file_freshness = memorylib.file_freshness(path, self.root)
            freshness[path] = file_freshness
            key_files.append({"path": path, "freshness": file_freshness})
        if task_state.status == "completed":
            current_plan = self.infer_current_plan(task_state, trigger)
            open_questions = []
            confirmed_findings = self.infer_confirmed_findings(task_state)
        else:
            current_plan = self.merge_checkpoint_lists(
                current.get("current_plan", []) if current else [],
                self.infer_current_plan(task_state, trigger),
            )
            open_questions = self.merge_checkpoint_lists(
                current.get("open_questions", []) if current else [],
                self.infer_open_questions(task_state, trigger),
            )
            confirmed_findings = self.merge_checkpoint_lists(
                current.get("confirmed_findings", []) if current else [],
                self.infer_confirmed_findings(task_state),
            )
        completed = self.merge_checkpoint_lists(
            current.get("completed", []) if current else [],
            [task_state.final_answer] if task_state.final_answer else [],
        )
        excluded = self.checkpoint_list(current.get("excluded", []) if current else [])
        inferred_blocker = self.infer_blocked_on(task_state)
        current_blocker = inferred_blocker or (str(current.get("current_blocker", "")).strip() if current else "")
        blocked_on = inferred_blocker or (str(current.get("blocked_on", "")).strip() if current else "")
        if task_state.status == "completed":
            current_blocker = ""
            blocked_on = ""
            next_action = self.infer_next_step(task_state)
        else:
            next_action = str(current.get("next_action", "")).strip() if current else ""
            next_action = next_action or self.infer_next_step(task_state)
        current_goal = str(user_message)
        if current and trigger in {"freshness_mismatch", "workspace_mismatch"}:
            current_goal = str(current.get("current_goal", "")).strip() or current_goal
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "created_at": now(),
            "current_goal": current_goal,
            "current_plan": current_plan,
            "open_questions": open_questions,
            "confirmed_findings": confirmed_findings,
            "completed": completed,
            "excluded": excluded,
            "current_blocker": current_blocker,
            "blocked_on": blocked_on,
            "next_action": next_action,
            "next_step": next_action,
            "key_files": key_files,
            "freshness": freshness,
            "summary": f"{trigger}: {clip(str(user_message), 120)}",
            "runtime_identity": self.current_runtime_identity(),
        }
        state["items"][checkpoint_id] = checkpoint
        state["current_id"] = checkpoint_id
        task_state.checkpoint_id = checkpoint_id
        self.session["runtime_identity"] = checkpoint["runtime_identity"]
        self.session_path = self.session_store.save(self.session)
        return checkpoint

    def infer_current_plan(self, task_state, trigger):
        if task_state.status == "completed":
            return ["Task completed; preserve the final answer for follow-up context."]
        if task_state.stop_reason == "step_limit_reached":
            return ["Resume from the latest checkpoint.", "Inspect the last tool result before continuing."]
        if task_state.last_tool:
            return [f"Use the result of {task_state.last_tool}.", self.infer_next_step(task_state)]
        if trigger in {"freshness_mismatch", "workspace_mismatch"}:
            return ["Re-anchor stale context.", "Refresh the next prompt with current workspace state."]
        return ["Clarify the request context.", "Choose the next workspace action."]

    def infer_open_questions(self, task_state, trigger):
        questions = []
        if trigger == "freshness_mismatch":
            questions.append("Which stale file facts still need to be re-read?")
        if trigger == "workspace_mismatch":
            questions.append("Which runtime or workspace assumptions changed since the checkpoint?")
        if task_state.stop_reason == "retry_limit_reached":
            questions.append("What output format correction is needed before continuing?")
        return questions

    def infer_confirmed_findings(self, task_state):
        findings = []
        if task_state.last_tool:
            findings.append(f"Last tool executed: {task_state.last_tool}.")
        if task_state.final_answer:
            findings.append(f"Final answer: {clip(task_state.final_answer, 180)}")
        return findings

    def infer_blocked_on(self, task_state):
        stop_reason = str(task_state.stop_reason or "").strip()
        if not stop_reason or stop_reason == "final_answer_returned":
            return ""
        return stop_reason

    def infer_next_step(self, task_state):
        if task_state.status == "completed":
            return "No next step recorded."
        if task_state.stop_reason == "step_limit_reached":
            return "Resume from the latest checkpoint and continue the task."
        if task_state.last_tool:
            return f"Decide the next action after {task_state.last_tool}."
        return "Continue the task from the latest checkpoint."

    def update_memory_after_tool(self, name, args, result):
        """把少量高价值工具结果沉淀到工作记忆/情景记忆。

        为什么存在：
        并不是每个工具结果都值得长期带进下一轮 prompt。完整结果已经进了
        `history`，这里只挑少量"下一轮大概率还会用到"的事实做提纯，
        例如最近读写过哪些文件、某个文件读出来的短摘要。

        输入 / 输出：
        - 输入：工具名 `name`、参数 `args`、执行结果 `result`
        - 输出：无显式返回值，副作用是更新 `self.memory`

        在 agent 链路里的位置：
        它发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前。
        也就是说：工具结果先进入完整历史，再由这个函数择优沉淀成轻量记忆。
        """
        if not self.feature_enabled("memory"):
            return
        if name == "run_shell":
            summary = memorylib.summarize_shell_result(result, command=args.get("command", ""))
            if summary and not SECRET_SHAPED_TEXT_PATTERN.search(summary):
                tags = ["shell", "run_shell"]
                lowered = (str(args.get("command", "")) + "\n" + str(result)).lower()
                if "pytest" in lowered or "test" in lowered:
                    tags.append("test")
                exit_match = re.search(r"exit_code:\s*(-?\d+)", str(result))
                if exit_match:
                    tags.append(f"exit_code_{exit_match.group(1)}")
                self.memory.append_note(summary, tags=tuple(tags), source="run_shell", kind="process")
                self.session["memory"] = self.memory.to_dict()
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        # 不是所有工具结果都进入工作记忆。
        # 读文件会生成摘要；写文件/patch 会让旧摘要失效，因为它们可能过期了。
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args, result)

    def record_process_note_for_tool(self, name, metadata):
        status = str(metadata.get("tool_status", "")).strip()
        if status not in {"partial_success", "error", "rejected"}:
            return
        affected_paths = [str(path).strip() for path in metadata.get("affected_paths", []) if str(path).strip()]
        if name == "run_shell" and status == "error" and not affected_paths:
            return
        path_text = ", ".join(affected_paths) or "workspace"
        if status == "partial_success":
            text = f"{name} partial_success on {path_text}; inspect diff before retry"
        elif status == "error":
            text = f"{name} error on {path_text}; check the failure before retry"
        else:
            text = f"{name} rejected; choose a different action before retry"
        tags = ["process", status, *affected_paths]
        self.memory.append_note(text, tags=tuple(tags), source=name, kind="process")
        self.session["memory"] = self.memory.to_dict()

    def reject_durable_reason(self, note_text):
        text = str(note_text or "").strip()
        lowered = text.lower()
        if not text:
            return "empty"
        if REDACTED_VALUE in text or SECRET_SHAPED_TEXT_PATTERN.search(text):
            return "secret_shaped"
        checkpoint_like_prefixes = (
            "current goal",
            "current blocker",
            "next step",
            "current phase",
            "key files",
            "freshness",
            "当前目标",
            "当前卡点",
            "下一步",
            "当前阶段",
            "关键文件",
            "已完成",
            "已排除",
        )
        if any(lowered.startswith(prefix) for prefix in checkpoint_like_prefixes):
            return "transient_task_state"
        if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", text) or len(text) > 220:
            return "noisy_output"
        return ""

    def _should_reflect(self, start_count, user_message=""):
        """判断是否应该触发反思：有新 process 笔记或用户消息含意图关键词。"""
        notes = self.memory.to_dict().get("episodic_notes", [])
        has_new_process = (
            len(notes) > start_count
            and any(note.get("kind") == "process" for note in notes[start_count:])
        )
        has_intent = bool(
            DURABLE_MEMORY_INTENT_PATTERN.search(str(user_message))
            or DURABLE_MEMORY_INTENT_ZH_PATTERN.search(str(user_message))
        )
        return has_new_process or has_intent

    def reflect_and_update_semantic_memory(self):
        """用子 agent 审查全部上下文，自动沉淀语义记忆。

        触发点：compact 完成后、ask 结束前。
        用输入状态去重：如果 history 长度和 episodic notes 数量自上次反思后没变，跳过。
        """
        if not self.feature_enabled("memory") or not self.feature_enabled("reflection"):
            return
        if self.memory.durable_store is None:
            return

        # 用输入状态做去重：history 长度 + episodic notes 数量
        history = self.session.get("history", [])
        episodic_notes = self.memory.to_dict().get("episodic_notes", [])
        input_signature = f"{len(history)}:{len(episodic_notes)}"
        if input_signature == self._last_reflection_signature:
            return

        # 构造反思 prompt
        existing_semantic = {}
        for topic in self.memory.durable_store.load_index():
            slug = topic["topic"]
            existing_semantic[slug] = [
                note["text"] for note in self.memory.durable_store.load_topic_notes(slug)
            ]

        history_summary = []
        for entry in history[-20:]:
            role = entry.get("role", "")
            content = str(entry.get("content", ""))[:300]
            kind = entry.get("kind", "")
            if kind == "compact_summary":
                history_summary.append({"role": "system", "kind": "compact_summary", "content": content})
            elif role in ("user", "assistant"):
                history_summary.append({"role": role, "content": content})
            elif role == "tool":
                history_summary.append({"role": "tool", "name": entry.get("name", ""), "content": content[:200]})

        working_memory = self.memory.to_dict().get("working", {})
        compact_summary = self.session.get("compact_summary", {})

        redact = self.redact_artifact
        prompt = textwrap.dedent(f"""\
            {REFLECTION_SYSTEM_PROMPT}

            ---

            Existing semantic memory (your output REPLACES these):
            {json.dumps(redact(existing_semantic), ensure_ascii=False, indent=2)}

            Recent session history:
            {json.dumps(redact(history_summary), ensure_ascii=False, indent=2)}

            Episodic notes (includes tool failures and retries):
            {json.dumps(redact(episodic_notes), ensure_ascii=False, indent=2)}

            Working memory:
            {json.dumps(redact(working_memory), ensure_ascii=False, indent=2)}

            Compact summary (if any):
            {json.dumps(redact(compact_summary), ensure_ascii=False, indent=2)}

            Return JSON only.
        """)

        try:
            raw = self.model_client.complete(prompt, REFLECTION_MAX_NEW_TOKENS)
            payload = self._parse_reflection_response(raw)
        except Exception:
            return

        if not payload:
            return

        # 写入：只替换 JSON 中出现且非空的 topic
        topic_notes = {
            topic: notes for topic, notes in payload.items()
            if notes and topic in memorylib.DURABLE_TOPIC_DEFAULTS
        }
        if topic_notes:
            self.memory.replace_durable_topics(topic_notes)
            self.session["memory"] = self.memory.to_dict()

        # 更新签名，防止同一次 ask 中 compact + ask-end 触发两次反思
        self._last_reflection_signature = input_signature

    def _parse_reflection_response(self, raw):
        text = str(raw or "").strip()
        if "<final>" in text:
            text = self.extract(text, "final").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        validated = {}
        for topic, notes in payload.items():
            if topic not in memorylib.DURABLE_TOPIC_DEFAULTS:
                continue
            if not isinstance(notes, list):
                continue
            cleaned = []
            for note in notes:
                note_text = str(note).strip()
                if not note_text:
                    continue
                reason = self.reject_durable_reason(note_text)
                if reason:
                    continue
                cleaned.append(note_text)
            if cleaned:
                validated[topic] = cleaned
        return validated

    def extract_durable_promotions(self, user_message, final_answer):
        user_text = str(user_message or "")
        if not (DURABLE_MEMORY_INTENT_PATTERN.search(user_text) or DURABLE_MEMORY_INTENT_ZH_PATTERN.search(user_text)):
            return [], []
        promotions = []
        rejections = []
        for line in str(final_answer or "").splitlines():
            text = line.strip()
            if not text or REDACTED_VALUE in text:
                continue
            for topic, pattern in DURABLE_MEMORY_LINE_PATTERNS:
                match = pattern.match(text)
                if not match:
                    continue
                note_text = match.group(1).strip()
                if note_text:
                    reason = self.reject_durable_reason(note_text)
                    if reason:
                        rejections.append(f"{topic}:{reason}")
                        break
                    promotions.append((topic, note_text))
                break
        return promotions, rejections

    def promote_durable_memory(self, user_message, final_answer):
        promotions, rejections = self.extract_durable_promotions(user_message, final_answer)
        promoted, superseded = self.memory.promote_durable(promotions)
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        return promoted, rejections, superseded

    def ask(self, user_message):
        """执行一次完整的 agent 回合，直到产出最终答案或命中停止条件。

        为什么存在：
        `ask()` 是整个 runtime 的总调度器。它把"用户提一个请求"扩展成一条
        可持续推进的控制循环：记录会话、组 prompt、调用模型、执行工具、
        写 trace/report、更新状态，直到模型给出最终答案或系统主动停下。

        输入 / 输出：
        - 输入：`user_message`，即用户这一次的任务描述
        - 输出：字符串形式的最终回答；如果中途达到步数上限或重试上限，
          返回的是一条停止原因说明

        在 agent 链路里的位置：
        它是 CLI 和底层工具/模型之间的核心桥梁。CLI 收到用户输入后基本只做
        一件事：调用 `agent.ask()`。而 `ask()` 内部再去驱动 `ContextManager`
        组 prompt、`model_client.complete()` 调模型、`run_tool()` 执行动作。
        如果新人想理解 enigma 是怎么"从一句话跑成一个 agent 流程"的，
        这里就是最关键的入口。
        """
        # 记录本轮 ask 的起始时间，用于最后生成 run_duration_ms。
        run_started_at = time.monotonic()
        self._last_reflection_signature = ""
        ep_notes_at_ask_start = len(self.memory.to_dict().get("episodic_notes", []))

        # 先把用户请求写进短期任务摘要和会话历史：
        # - set_task_summary 只是更新当前任务摘要，方便 prompt 里说明"现在要做什么"
        # - append_session_history 写的是 history，属于本会话流水账，不是跨会话语义记忆
        self.memory.set_task_summary(user_message)
        self.append_session_history({"role": "user", "content": user_message, "created_at": now()})

        # TaskState 是这一轮 ask 的运行账本：记录 task_id、尝试次数、工具次数、
        # stop_reason、trace/report 所需的状态等。
        task_state = TaskState.create(run_id=self.new_run_id(), task_id=self.new_task_id(), user_request=user_message)
        #这轮是从头开始 还是完全恢复 还是部分恢复
        task_state.resume_status = self.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        self.current_task_state = task_state

        # 为本轮运行创建落盘目录，并写第一条 trace，方便之后排查每一步发生了什么。
        self.current_run_dir = self.run_store.start_run(task_state)
        self.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        # tool_steps 限制"真正执行工具"的次数；
        # attempts 限制"模型返回无效内容导致重试"的总次数。
        tool_steps = 0
        attempts = 0
        # 小 max_steps 时允许几次格式修正；大 max_steps 时只额外给 4 次重试，
        # 避免 malformed response 把模型调用次数放大到工具预算的 3 倍。
        max_attempts = min(self.max_steps * 3, self.max_steps + 4)

        self._last_step_results = []
        self._last_preview_lines = 0
        self._last_result_compact = ""
        self._last_preview_header_printed = False

        # 这是 agent 的主循环，可以按"感知 -> 决策 -> 行动 -> 记录"来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while tool_steps < self.max_steps and attempts < max_attempts:
            if self.check_cancel():
                final = "Stopped by user."
                task_state.stop("user_cancel", final_answer=final)
                self.append_session_history({"role": "assistant", "content": final, "created_at": now()})
                self.run_store.write_task_state(task_state)
                self._cleanup_plan_file()
                return final
            attempts += 1
            task_state.record_attempt()
            self.run_store.write_task_state(task_state)

            # 每一轮都重新构建 prompt，因为上一轮可能新增了工具结果、memory 或 checkpoint。
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = self._build_prompt_and_metadata(user_message)
            self.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )

            # 如果恢复的 checkpoint 和当前工作区状态不一致，就先创建新 checkpoint，
            # 避免模型在旧上下文上继续做有风险的推理。
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
                self.resume_state = self.evaluate_resume_state()
                continue

            # 如果 runtime 身份不同，比如 cwd/model/配置变了，也记录 mismatch 并打 checkpoint。
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                self.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
                self.resume_state = self.evaluate_resume_state()
                continue

            # prompt 太长时 ContextManager 可能会裁剪上下文；
            # 这里记录一次 checkpoint，说明这轮 prompt 使用了压缩后的上下文。
            if prompt_metadata.get("budget_reductions"):
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="context_reduction")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            self.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                },
            )
            prompt_cache_key = None
            prompt_cache_retention = None
            if getattr(self.model_client, "supports_prompt_cache", False):
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"

            # 调模型：模型会返回一段文本，后面 parse() 会判断它是工具调用、重试提示还是最终答案。
            model_started_at = time.monotonic()
            on_token = self._make_stream_callback() if self.show_tool_activity else None
            raw = self.model_client.complete(
                prompt,
                self.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
                on_token=on_token,
            )
            if on_token:
                print("", file=self.tool_activity_stream)
            completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            self.last_completion_metadata = completion_metadata
            self.last_prompt_metadata = prompt_metadata
            self.display_token_usage(completion_metadata)

            # 安全网：prompt token 接近模型上下文窗口时，提前触发压缩。
            # 只在模型真正报 context 窗口紧张时触发，FakeModelClient / 未知模型 fallback 不触发。
            prompt_tokens = prompt_metadata.get("prompt_tokens", 0)
            model_name = str(getattr(self.model_client, "model", ""))
            if (
                prompt_tokens
                and model_name
                and model_name.lower() != "fakemodelclient"
                and should_compact(model_name, prompt_tokens)
            ):
                try:
                    self.compact_context()
                except Exception:
                    pass

            kind, payload = self.parse(raw)
            self.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                # 模型要求调用工具：取出工具名和参数，执行后把工具结果写回 history，
                # 下一轮模型就能基于工具结果继续推理。
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                tool_message = self.tool_activity_message(name, args, payload.get("message", ""))
                task_state.record_tool(name)
                self.display_tool_activity(name, args, tool_message)
                tool_started_at = time.monotonic()
                result = self.run_tool(name, args)
                tool_elapsed_ms = int((time.monotonic() - tool_started_at) * 1000)
                self.display_tool_result(
                    name, args, result, tool_elapsed_ms,
                    tool_steps, self.max_steps, self._last_tool_result_metadata,
                )
                self.display_file_diff()
                self.append_session_history(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "message": tool_message,
                        "content": result,
                        "created_at": now(),
                    }
                )
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "message": tool_message,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(self._last_tool_result_metadata or {}),
                    },
                )
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="tool_executed")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                if tool_steps % 10 == 0 and self._should_reflect(ep_notes_at_ask_start, user_message):
                    self.reflect_and_update_semantic_memory()
                continue

            if kind == "plan":
                plan_content = payload
                self.append_session_history(
                    {
                        "role": "assistant",
                        "content": f"<plan>\n{plan_content}\n</plan>",
                        "created_at": now(),
                    }
                )
                from .task_state import STOP_REASON_PLAN_READY, STATUS_COMPLETED
                task_state.stop(STOP_REASON_PLAN_READY, status=STATUS_COMPLETED, final_answer=plan_content)
                checkpoint = self.create_checkpoint(task_state, user_message, trigger="plan_ready")
                self.run_store.write_task_state(task_state)
                self.emit_trace(
                    task_state,
                    "plan_ready",
                    {
                        "plan": clip(plan_content, 500),
                    },
                )
                self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
                return PlanResult(plan=plan_content, session_id=self.session["id"])

            if kind == "retry":
                # 模型输出格式不合法但还能继续：把提示写入历史，再进入下一轮重新请求模型。
                self.append_session_history({"role": "assistant", "content": payload, "created_at": now()})
                self.run_store.write_task_state(task_state)
                continue

            # 不是工具也不是重试，就当作最终回答；成功收尾并生成 checkpoint/report。
            # plan mode 兜底：模型没用 <plan> 标签但处于 plan mode，将内容作为 plan 返回。
            if self.plan_mode:
                plan_content = (payload or raw).strip()
                self.append_session_history(
                    {"role": "assistant", "content": plan_content, "created_at": now()}
                )
                from .task_state import STOP_REASON_PLAN_READY, STATUS_COMPLETED
                task_state.stop(STOP_REASON_PLAN_READY, status=STATUS_COMPLETED, final_answer=plan_content)
                self.run_store.write_task_state(task_state)
                return PlanResult(plan=plan_content, session_id=self.session["id"])
            final = (payload or raw).strip()
            self.append_session_history({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            if self._should_reflect(ep_notes_at_ask_start, user_message):
                self.reflect_and_update_semantic_memory()
            self.promote_durable_memory(user_message, final)
            checkpoint = self.create_checkpoint(task_state, user_message, trigger="run_finished")
            self.run_store.write_task_state(task_state)
            self.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            self.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
            self._cleanup_plan_file()
            return final

        # 循环退出但没有最终答案：说明要么模型连续给不出有效格式，
        # 要么工具步数达到上限。这里统一生成停止说明并落盘。
        if attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        self.append_session_history({"role": "assistant", "content": final, "created_at": now()})
        if self._should_reflect(ep_notes_at_ask_start, user_message):
            self.reflect_and_update_semantic_memory()
        self.promote_durable_memory(user_message, final)
        self.run_store.write_task_state(task_state)
        checkpoint = self.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        self.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        self.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        self.run_store.write_report(task_state, self.redact_artifact(self.build_report(task_state)))
        self._cleanup_plan_file()
        return final

    def run_tool(self, name, args):
        """执行一次工具调用，并在执行前后套上完整护栏。

        为什么存在：
        在 agent 系统里，真正危险的不是"模型会不会想调用工具"，而是
        "平台有没有在执行前把边界守住"。这个函数就是工具层的总闸口：
        所有工具调用都必须先经过它，不能让模型直接碰到底层函数。

        输入 / 输出：
        - 输入：工具名 `name`，参数字典 `args`
        - 输出：字符串结果。无论是成功结果还是错误信息，都会统一返回文本，
          这样模型下一轮都能继续消费这份反馈。

        在 agent 链路里的位置：
        它位于 `ask()` 的"模型决定要调用工具"之后，是控制循环里真正把模型
        意图落到外部世界的一步。因此这里串起了几乎所有安全与可控设计：
        工具是否存在、参数是否合法、是否重复、是否需要审批、执行结果是否裁剪、
        是否需要回写记忆。
        """
        # 工具执行不是"直接调函数"，而是一条带护栏的流水线：
        # 工具是否存在 -> 参数是否合法 -> 是否重复调用 -> 是否通过审批
        # -> 真正执行 -> 更新记忆。
        self._last_diff_info = None
        tool = self.tools.get(name)
        if tool is None:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
                "risk_level": "high",
                "read_only": False,
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: unknown tool '{name}'"
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return message
        if self.repeated_tool_call(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        if tool["risky"] and not self.approve(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
                "risk_level": "high",
                "read_only": False,
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: approval denied for {name}"
        before_snapshot = self.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        # 为 write_file/patch_file 捕获旧内容，用于 diff 显示
        old_content_for_diff = None
        diff_path = ""
        if name == "write_file":
            diff_path = str(args.get("path", ""))
            try:
                target = self.path(diff_path)
                if target.is_file():
                    old_content_for_diff = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif name == "patch_file":
            diff_path = str(args.get("path", ""))
            try:
                target = self.path(diff_path)
                if target.is_file():
                    old_content_for_diff = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        try:
            result = clip(tool["run"](args))
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            # 计算 diff 信息供 display 层使用
            if old_content_for_diff is not None and name == "write_file":
                try:
                    new_content = self.path(diff_path).read_text(encoding="utf-8", errors="replace")
                    self._last_diff_info = {"old": old_content_for_diff, "new": new_content, "path": diff_path}
                except Exception:
                    self._last_diff_info = None
            elif old_content_for_diff is not None and name == "patch_file":
                try:
                    new_content = self.path(diff_path).read_text(encoding="utf-8", errors="replace")
                    self._last_diff_info = {"old": old_content_for_diff, "new": new_content, "path": diff_path}
                except Exception:
                    self._last_diff_info = None
            else:
                self._last_diff_info = None
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", result)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            self.update_memory_after_tool(name, args, result)
            self._last_tool_result_metadata = {
                "tool_status": tool_status,
                "tool_error_code": tool_error_code,
                "security_event_type": "",
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
            }
            self.record_process_note_for_tool(name, self._last_tool_result_metadata)
            return result
        except Exception as exc:
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "partial_success" if workspace_changed else "error",
                "tool_error_code": "tool_partial_success" if workspace_changed else "tool_failed",
                "security_event_type": security_event_type,
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
            }
            self.record_process_note_for_tool(name, self._last_tool_result_metadata)
            return f"error: tool {name} failed: {exc}"

    def repeated_tool_call(self, name, args):
        # agent 很常见的一种坏循环，是在没有新信息的情况下反复发起同一调用。
        # 这里提前挡掉最简单的这种循环。
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    def tool_activity_message(self, name, args, message=""):
        message = str(message or "").strip()
        if message:
            return clip(" ".join(message.split()), 160)
        if name == "web_search":
            query = str((args or {}).get("query", "")).strip()
            return clip(f"Searching the web for {query}.", 160) if query else "Searching the web."
        if name == "read_file":
            path = str((args or {}).get("path", "")).strip()
            return f"Reading {path}." if path else "Reading a file."
        if name == "search":
            pattern = str((args or {}).get("pattern", "")).strip()
            return clip(f"Searching the workspace for {pattern}.", 160) if pattern else "Searching the workspace."
        if name == "list_files":
            path = str((args or {}).get("path", ".")).strip() or "."
            return f"Listing files in {path}."
        if name == "run_shell":
            return "Running a shell command."
        if name in {"write_file", "patch_file"}:
            path = str((args or {}).get("path", "")).strip()
            return f"Updating {path}." if path else "Updating a file."
        if name == "delegate":
            return "Asking a read-only delegate to investigate."
        return f"Running {name}."

    def display_tool_activity(self, name, args, message):
        if not self.show_tool_activity:
            return
        stream = self.tool_activity_stream
        summary = self.tool_activity_summary(name, args)
        if self.stream_supports_color(stream):
            badge = "\033[48;2;91;78;255m\033[38;2;255;255;255m enigma \033[0m"
            tool = f"\033[38;2;118;214;255m{name}\033[0m"
            dim = "\033[2m"
            reset = "\033[0m"
            line = f"{badge} {tool} {dim}{summary}{reset}"
            body = f"\033[38;2;188;195;207m| {message}\033[0m"
        else:
            line = f"[enigma] {name} {summary}"
            body = f"| {message}"
        print(line, file=stream)
        print(body, file=stream)
        try:
            stream.flush()
        except Exception:
            pass

    def display_tool_result(self, name, args, result, elapsed_ms, tool_steps, max_steps, metadata):
        if not self.show_tool_activity:
            return
        stream = self.tool_activity_stream
        use_color = self.stream_supports_color(stream)
        summary = self.tool_activity_summary(name, args)
        status = str((metadata or {}).get("tool_status", "")).strip()
        success = status != "error" and status != "rejected"
        if status == "partial_success":
            success = None
        # 折叠上一条结果的预览：用 ANSI cursor up 把旧预览替换为单行摘要
        prev_lines = self._last_preview_lines
        if prev_lines > 0 and self._last_preview_header_printed:
            move_up = prev_lines
            for i in range(move_up):
                print(f"\033[A\033[2K", end="", file=stream)
            if self._last_result_compact:
                compact_text = self._last_result_compact
                if use_color:
                    print(f"{displaymod.DIM}  {compact_text}{displaymod.RESET}", file=stream)
                else:
                    print(f"  {compact_text}", file=stream)
            else:
                print("", file=stream)
        header = displaymod.format_step_header(
            tool_steps, max_steps, name, summary, elapsed_ms, success, use_color=use_color
        )
        print(header, file=stream)
        preview_lines = 0
        compact = ""
        if result:
            preview_text, preview_lines = displaymod.format_result_preview(result, max_lines=15, use_color=use_color)
            if preview_text:
                print(preview_text, file=stream)
            compact = displaymod.format_result_compact(result)
        try:
            stream.flush()
        except Exception:
            pass
        self._last_preview_lines = preview_lines
        self._last_result_compact = compact
        self._last_preview_header_printed = True
        self._last_step_results.append({
            "name": name,
            "summary": summary,
            "elapsed_ms": elapsed_ms,
            "success": success,
            "result_lines": len(str(result).splitlines()),
        })

    def display_file_diff(self):
        if not self.show_tool_activity:
            return
        diff_info = getattr(self, "_last_diff_info", None)
        if not diff_info:
            return
        stream = self.tool_activity_stream
        use_color = self.stream_supports_color(stream)
        text, line_count = displaymod.format_diff(
            diff_info["old"], diff_info["new"],
            path=diff_info.get("path", ""),
            max_lines=40, use_color=use_color,
        )
        if text:
            print(text, file=stream)
            try:
                stream.flush()
            except Exception:
                pass

    def _make_stream_callback(self):
        stream = self.tool_activity_stream
        use_color = self.stream_supports_color(stream)
        if use_color:
            prefix = "\033[38;2;118;214;255m"
            reset = "\033[0m"
        else:
            prefix = ""
            reset = ""

        def on_token(token):
            print(f"{prefix}{token}{reset}", end="", file=stream, flush=True)

        return on_token

    def display_token_usage(self, metadata):
        if not self.show_token_usage or not metadata:
            return
        fields = []
        for label, key in (
            ("in", "input_tokens"),
            ("out", "output_tokens"),
            ("total", "total_tokens"),
            ("cached", "cached_tokens"),
        ):
            value = metadata.get(key)
            if value is not None:
                fields.append(f"{label}={value}")
        if metadata.get("cache_hit") is not None:
            fields.append(f"cache_hit={str(bool(metadata.get('cache_hit'))).lower()}")
        if not fields:
            return

        stream = self.tool_activity_stream
        if self.stream_supports_color(stream):
            badge = "\033[48;2;37;99;235m\033[38;2;255;255;255m usage \033[0m"
            body = "\033[38;2;188;195;207m" + "  ".join(fields) + "\033[0m"
            print(f"{badge} {body}", file=stream)
        else:
            print(f"[usage] {'  '.join(fields)}", file=stream)
        try:
            stream.flush()
        except Exception:
            pass

    @staticmethod
    def stream_supports_color(stream):
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("TERM", "").lower() == "dumb":
            return False
        return bool(getattr(stream, "isatty", lambda: False)())

    @staticmethod
    def tool_activity_summary(name, args):
        args = args or {}
        if name == "web_search":
            return Enigma._format_summary_arg("query", args.get("query", ""))
        if name in {"read_file", "write_file", "patch_file"}:
            return Enigma._format_summary_arg("path", args.get("path", ""))
        if name == "search":
            return Enigma._format_summary_arg("pattern", args.get("pattern", ""))
        if name == "list_files":
            return Enigma._format_summary_arg("path", args.get("path", "."))
        if name == "run_shell":
            return Enigma._format_summary_arg("command", args.get("command", ""))
        if name == "delegate":
            return Enigma._format_summary_arg("task", args.get("task", ""))
        return ""

    @staticmethod
    def _format_summary_arg(label, value):
        value = clip(" ".join(str(value or "").split()), 80)
        return f"{label}={json.dumps(value, ensure_ascii=True)}" if value else ""

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        # report 是一次运行的最终摘要；
        # 和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": task_state.run_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "redacted_env": self.detected_secret_env_summary(),
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """把通用工具校验和 runtime 级额外约束串起来。"""
        toolkit.validate_tool(self, name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_search(self, args):
        return toolkit.tool_search(self, args)

    def tool_web_search(self, args):
        return toolkit.tool_web_search(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    def request_cancel(self):
        self._cancel_requested = True

    def check_cancel(self):
        if self._cancel_requested:
            self._cancel_requested = False
            return True
        return False

    def exit_plan_mode(self, plan_content):
        if not self.plan_mode:
            return
        self.plan_mode = False

        plan_path = self._get_plan_path()
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(plan_content, encoding="utf-8")

        self.append_session_history(
            {
                "role": "system",
                "content": (
                    "The user has approved a plan. "
                    f"It is stored at {plan_path}. "
                    "Read it before executing each step. "
                    "Proceed to execute it using all available tools."
                ),
                "created_at": now(),
            }
        )
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.session_path = self.session_store.save(self.session)

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        "这是工具调用"还是"这是最终答案"。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`plan`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw)
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            body = Enigma.extract(raw, "tool")
            try:
                payload = json.loads(body)
            except Exception:
                return "retry", Enigma.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", Enigma.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", Enigma.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", Enigma.retry_notice()
            if "message" in payload:
                payload["message"] = clip(" ".join(str(payload.get("message", "")).split()), 160)
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = Enigma.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Enigma.retry_notice()
        if "<plan>" in raw:
            plan_content = Enigma.extract(raw, "plan").strip()
            if plan_content:
                return "plan", plan_content
            return "retry", Enigma.retry_notice("model returned an empty <plan>")
        if "<final>" in raw:
            final = Enigma.extract(raw, "final").strip()
            if final:
                return "final", final
            return "retry", Enigma.retry_notice("model returned an empty <final> answer")
        raw = raw.strip()
        if raw:
            return "final", raw
        return "retry", Enigma.retry_notice("model returned an empty response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = Enigma.parse_attrs(match.group("attrs"))
        message = str(attrs.pop("message", "")).strip()
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = Enigma.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        payload = {"name": name, "args": args}
        if message:
            payload["message"] = clip(" ".join(message.split()), 160)
        return payload

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["compact_summary"] = {}
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root=self.root)
        self.session_path = self.session_store.save(self.session)

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        # 所有文件类工具都被锚定在 workspace root 之下。
        # 这样既能防住 "../" 逃逸，也能防住符号链接解析后跳出仓库。
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved


MiniAgent = Enigma
