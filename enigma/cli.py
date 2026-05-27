"""命令行入口。

这个模块负责把"用户怎么启动 enigma"翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import re
import shutil
import sys
import textwrap
import threading

from . import repl
from .context_manager import DEFAULT_TOTAL_BUDGET
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient, get_context_window
from .runtime import Enigma, PlanResult, SessionStore
from .skills import build_skill_prompt, discover_skills, list_skills
from .workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "enigma"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /compact [focus]  Compact 工作记忆, 情景记忆, 语义记忆, and history.
    /memory  Show the agent's 工作记忆.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /plan <task>  Enter plan mode: explore read-only, produce a plan.
    /review [target]  Review changes: PR link, branch, files, or auto-detect.
    /skills  List available skills.
    /<skill-name> [args]  Invoke a skill (e.g. /test-writer auth.py).
    /exit    Exit the agent.
    """
).strip()


DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "ENIGMA_SECRET_ENV_NAMES"


def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = os.environ.get("OPENAI_MODEL")
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL")
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    return DEFAULT_OLLAMA_MODEL


def _first_env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    provider = getattr(args, "provider", "openai")
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL
        api_key = os.environ.get("OPENAI_API_KEY", "")
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("ANTHROPIC_API_BASE") or DEFAULT_ANTHROPIC_BASE_URL
        api_key = _first_env("ANTHROPIC_API_KEY", "RIGHT_CODES_API_KEY", "OPENAI_API_KEY")
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    status = "plan mode, exploring..." if agent.plan_mode else WELCOME_STATUS
    # 根目录显示：带父目录，方便识别同名嵌套
    cwd = agent.workspace.cwd
    repo_root = agent.workspace.repo_root
    parent = os.path.dirname(repo_root)
    if parent and parent != repo_root:
        root_display = f"{repo_root} (from {parent})"
    else:
        root_display = repo_root
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(status),
            divider("-"),
            row(""),
            row("ROOT  " + middle(root_display, inner - 6)),
            row("WORKSPACE  " + middle(cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Enigma 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把"启动参数"翻译成"agent 运行现场"。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Enigma`，或一个从旧 session 恢复出来的 `Enigma`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先整理 secret 名单，再采集工作区快照，随后决定是恢复旧 session
    # 还是创建一个新的 Enigma 实例。
    configured_secret_names = _configured_secret_names(args)
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(workspace.repo_root + "/.enigma/sessions")
    model = _build_model_client(args)
    user_home = os.path.expanduser("~")

    # 根据模型上下文窗口自动计算 prompt 预算（token）
    model_name = getattr(model, "model", "")
    context_window = get_context_window(model_name)
    total_budget = int(context_window * 0.75)

    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        agent = Enigma.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            total_budget=total_budget,
            plan_mode=getattr(args, "plan", False),
            secret_env_names=configured_secret_names,
            show_tool_activity=True,
            show_token_usage=True,
        )
    else:
        agent = Enigma(
            model_client=model,
            workspace=workspace,
            session_store=store,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            total_budget=total_budget,
            plan_mode=getattr(args, "plan", False),
            secret_env_names=configured_secret_names,
            show_tool_activity=True,
            show_token_usage=True,
        )
    agent.skills = discover_skills(workspace.repo_root, user_home)
    # 启动时确保 MEMORY.md 存在（不存在就写一份骨架）。
    # 放在 CLI 层而不是 Enigma 构造里，避免测试临时目录意外生成文件。
    memory_store = getattr(agent.memory, "durable_store", None)
    if memory_store is not None:
        try:
            memory_store.ensure_index()
        except Exception:
            pass
    return agent


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, or Anthropic-compatible models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic"), default="openai", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, OPENAI_MODEL for openai, and ANTHROPIC_MODEL for anthropic when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--plan", action="store_true", default=False, help="Plan mode: explore read-only first, produce plan, execute after approval.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    return parser


def handle_plan_approval(agent, plan, user_message):
    """内联审批循环：approve / revise / reject"""
    while True:
        print(f"\n{'='*60}")
        print("PLAN")
        print(f"{'='*60}")
        print(plan)
        print(f"{'='*60}")
        print("  [1] Approve  - execute this plan")
        print("  [2] Revise   - give feedback to refine")
        print("  [3] Reject   - stop here, do nothing")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nPlan rejected.")
            return None
        if choice == "1":
            agent.exit_plan_mode(plan)
            print("\nPlan approved. Executing...\n")
            return agent.ask(f"Execute the approved plan for: {user_message}")
        elif choice == "2":
            try:
                feedback = input("Your feedback: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nPlan rejected.")
                return None
            if not feedback:
                continue
            result = agent.ask(f"User feedback on your plan: {feedback}\nRevise the plan accordingly.")
            if isinstance(result, PlanResult):
                plan = result.plan
                continue
            else:
                return result
        elif choice == "3":
            print("\nPlan rejected.")
            return None
        else:
            print("Invalid choice. Enter 1, 2, or 3.")


def main(argv=None):
    # 解析命令行参数，并按参数组装本次运行使用的 agent。
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    # 从模型客户端中取出展示用的模型名和服务地址，用于启动欢迎信息。
    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                handled = handle_builtin_command(agent, prompt)
                if not handled:
                    result = agent.ask(prompt)
                    if isinstance(result, PlanResult):
                        handle_plan_approval(agent, result.plan, prompt)
                    else:
                        print(result)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    spinner = repl.Spinner()
    pending_message = ""

    while True:
        sys.stdout.write(repl.prompt_idle())
        sys.stdout.flush()
        try:
            user_input = input("").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        # 处理暂存消息：如果上次运行时用户打了字，这里自动发送
        if not user_input and pending_message:
            user_input = pending_message
            pending_message = ""

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if handle_builtin_command(agent, user_input):
            continue

        # 运行 agent，支持 spinner + 暂存消息 + 中断
        # 工作线程中 approve() 的 input() 会跟主线程的 msvcrt 争抢 stdin，
        # 所以临时切换为 auto 审批，完成后恢复。
        result_box = [None]
        error_box = [None]
        saved_approval = agent.approval_policy
        if saved_approval == "ask":
            agent.approval_policy = "auto"

        def run_agent():
            try:
                r = agent.ask(user_input)
                result_box[0] = r
            except Exception as exc:
                error_box[0] = exc

        worker = threading.Thread(target=run_agent, daemon=True)
        worker.start()
        spinner.start()
        pending_message = ""

        # 非阻塞输入循环：读键盘，管理暂存消息
        while worker.is_alive():
            try:
                ch = repl.read_key_nonblocking()
                if ch is not None:
                    if ch == "\x1b":  # Esc → 清除暂存消息
                        if pending_message:
                            pending_message = ""
                            repl.draw_pending_message("", sys.stderr)
                    elif ch == "\r" or ch == "\n":
                        pass  # 忽略回车，等 agent 完成后自动发送
                    elif ch == "\x03":  # Ctrl+C → 中断 agent
                        agent.request_cancel()
                    elif ch == "\x7f" or ch == "\b":  # Backspace
                        if pending_message:
                            pending_message = pending_message[:-1]
                            repl.draw_pending_message(pending_message, sys.stderr)
                    elif ch.isprintable():
                        pending_message += ch
                        repl.draw_pending_message(pending_message, sys.stderr)
                else:
                    import time as _time
                    _time.sleep(0.05)
            except KeyboardInterrupt:
                agent.request_cancel()
                break

        spinner.stop()
        worker.join()
        agent.approval_policy = saved_approval

        # 显示结果
        if error_box[0] is not None:
            print(str(error_box[0]), file=sys.stderr)
        elif result_box[0] is not None:
            r = result_box[0]
            if isinstance(r, PlanResult):
                handle_plan_approval(agent, r.plan, user_input)
            else:
                print(r)

        # 暂存消息在下一轮 input() 返回空时自动发送（line 377-379）


def _build_review_prompt(diff, source, files):
    file_list = "\n".join(f"- {f}" for f in files) if files else "(no files)"
    return textwrap.dedent(f"""\
        Review the following code changes ({source}).

        Changed files:
        {file_list}

        Diff:
        ```
        {diff}
        ```

        Analyze for:
        - Logic errors and potential bugs
        - Security vulnerabilities (injection, path traversal, secret leaks, etc.)
        - Performance issues (unnecessary loops, memory leak risks)
        - Code style and readability
        - Error handling adequacy

        Group findings by severity: critical > warning > suggestion.
        For each finding, reference the specific file and line.
        If the changes look good, say so briefly.
    """).strip()


def _handle_review(agent, target):
    ws = agent.workspace
    diff_text = ""
    source = ""
    files = []

    if not target:
        # 无参数：自动检测
        if not ws.is_git_repo:
            print("Not a git repository. Usage: /review <file1> [file2] ...")
            return
        diff_text, source = ws.git_diff()
        files = ws.changed_files()
    elif re.search(r"github\.com/[\w.-]+/[\w.-]+/pull/\d+", target):
        # PR 链接
        pr_num = re.search(r"/pull/(\d+)", target).group(1)
        diff_text, err = ws.pr_diff(pr_num)
        if err:
            print(err)
            return
        source = f"PR #{pr_num}"
    elif ws.is_git_repo and ws._run_git(["rev-parse", "--verify", target], cwd=ws.repo_root):
        # 分支名
        diff_text, source = ws.git_diff(base_branch=target)
        files = ws.changed_files(base_branch=target)
    else:
        # 文件路径
        from pathlib import Path
        file_paths = []
        for part in target.split():
            p = Path(ws.repo_root) / part
            if p.exists():
                file_paths.append(str(p))
            else:
                print(f"File not found: {part}")
                return
        contents = []
        for fp in file_paths:
            try:
                text = Path(fp).read_text(encoding="utf-8", errors="replace")
                rel = str(Path(fp).relative_to(ws.repo_root))
                contents.append(f"--- {rel}\n{text}")
                files.append(rel)
            except Exception as exc:
                print(f"Cannot read {fp}: {exc}")
                return
        diff_text = "\n\n".join(contents)
        source = f"{len(files)} file(s)"

    if not diff_text.strip():
        print("No pending changes to review.")
        return

    print(f"\nReviewing: {source}")
    if files:
        for f in files[:20]:
            print(f"  - {f}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
    print()

    prompt = _build_review_prompt(diff_text, source, files)
    try:
        result = agent.ask(prompt)
        print(result)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)


def handle_builtin_command(agent, user_input):
    user_input = str(user_input or "").strip()
    if user_input == "/help":
        print(HELP_DETAILS)
        return True
    if user_input == "/memory":
        print(agent.memory_text())
        return True
    if user_input == "/session":
        print(agent.session_path)
        return True
    if user_input == "/reset":
        agent.reset()
        print("session reset")
        return True
    if user_input == "/compact" or user_input.startswith("/compact "):
        focus = user_input[len("/compact"):].strip()
        print(agent.compact_context(focus=focus))
        return True
    if user_input.startswith("/plan "):
        task = user_input[len("/plan "):].strip()
        if not task:
            print("Usage: /plan <task description>")
            return True
        prev_plan_mode = agent.plan_mode
        agent.plan_mode = True
        agent.tools = agent.build_tools()
        agent.prefix_state = agent.build_prefix()
        agent.prefix = agent.prefix_state.text
        try:
            result = agent.ask(task)
            if isinstance(result, PlanResult):
                handle_plan_approval(agent, result.plan, task)
            else:
                print(result)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            agent.plan_mode = prev_plan_mode
            agent.tools = agent.build_tools()
            agent.prefix_state = agent.build_prefix()
            agent.prefix = agent.prefix_state.text
        return True
    if user_input == "/review" or user_input.startswith("/review "):
        _handle_review(agent, user_input[len("/review"):].strip())
        return True
    if user_input == "/skills":
        print(list_skills(getattr(agent, "skills", {})))
        return True
    if user_input.startswith("/") and not user_input.startswith("//"):
        name = user_input[1:].split()[0]
        skills = getattr(agent, "skills", {})
        if name in skills:
            user_message = user_input[len(name) + 2:].strip() or name
            prompt = build_skill_prompt(skills[name], user_message)
            try:
                result = agent.ask(prompt)
                print(result)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
            return True
    return False
