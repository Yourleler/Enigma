# Enigma Agent 组装机制

## 面试速查：核心技术亮点

> 面试官问"你的项目有什么亮点"时，从这里挑 2-3 个展开。

| 亮点 | 一句话概括 | 展开见 |
|------|---------|--------|
| **分层记忆系统** | 三层记忆（工作/情景/语义）+ SHA-256 freshness 绑定，文件改了旧摘要自动失效 | 第五章 |
| **跨会话记忆与 FTS5 检索** | SQLite + FTS5 全历史搜索、`MEMORY.md` 短启动记忆 + 按需主题加载、每会话滚动摘要——灵感来自 Claude Code 的 `CLAUDE.md` 与 Hermes 的会话摘要分离 | 第十章 |
| **自动反思与语义记忆沉淀** | Reflection system：每 10 步工具调用 + 任务结束 + compact 前自动触发，用子 agent 审查历史并提取稳定事实写入语义记忆，输入签名去重 | 5.6 节 |
| **上下文预算压缩** | Token 计数 + 按模型自动缩放（75% 上下文窗口），六段式 prompt（prefix/startup_memory/memory/relevant_memory/rolling_summary/history），四道收口（工具裁剪→记忆窗口→history 分类压缩→prompt 优先级让位），超限自动 compact | 第六章 |
| **模型上下文窗口匹配** | 85+ 模型前缀映射，最长前缀优先匹配，未知模型 fallback 32K，零依赖 | 4.8 节 |
| **Prompt Cache 复用** | 用 prefix hash 做 cache key，history 变化不影响缓存命中 | 4.4 节 |
| **Plan 模式** | 只读探索→结构化计划→内联审批（approve/revise/reject），模仿 Claude Code | 2.8 节 |
| **工具安全护栏** | 7 层流水线：存在性→校验→重复检测→审批→快照→执行→记忆更新，路径穿越防护 | 2.4、3.6 节 |
| **Skill 系统** | 三源发现（内置/用户/项目）、渐进加载、模板变量替换 | 2.10 节 |
| **代码审查命令** | `/review` 四种输入模式（自动检测/PR 链接/分支对比/指定文件），severity 分组，WorkspaceContext 封装 git diff | 1.5 节 |
| **终端 UX** | 双线程 REPL、spinner + 暂存消息队列、Ctrl+C 中断、文件 diff 红绿显示、流式 token 输出 | 2.9 节 |
| **BM25 记忆检索** | 零依赖，分路排序（情景+语义各保底 2 条），不用 embedding | 5.7 节 |
| **会话恢复** | Checkpoint 链表 + 文件 freshness + runtime_identity 五维状态评估 | 第七章 |

---

## 第零章 全局总览

Enigma 是一个本地 coding agent，整个启动到运行的链路可以用一句话概括：**终端命令 → 参数解析 → 对象组装 → 控制循环**。

程序并不复杂——总共约 14 个 Python 模块，核心入口在 `enigma/cli.py` 的 `main()` 函数。但"组装"这一步值得细看，因为它是把用户敲的一行字符串翻译成一整个可运行 agent 的关键转换层。

启动链路分为四层：

```
┌─────────────────────────────────────────────────┐
│  终端命令   enigma --provider ollama "fix bug"    │
├─────────────────────────────────────────────────┤
│  入口注册   pyproject.toml [project.scripts]     │
│             → 生成 enigma.exe，调用 cli.main()   │
├─────────────────────────────────────────────────┤
│  参数解析   argparse 把 sys.argv → Namespace     │
├─────────────────────────────────────────────────┤
│  对象组装   build_agent(args) → Enigma 实例      │
│             · WorkspaceContext（工作区快照）       │
│             · ModelClient（模型后端）             │
│             · SessionStore（会话持久化）           │
│             · LayeredMemory（分层记忆）           │
│             · ContextManager（prompt 组装器）     │
│             · ToolRegistry（工具白名单）          │
│             · Display（终端显示工具）             │
│             · REPL（交互终端组件）                │
├─────────────────────────────────────────────────┤
│  运行分发   one-shot: agent.ask(prompt)          │
│             REPL: spinner + 暂存消息 + 中断      │
└─────────────────────────────────────────────────┘
```

下面按层展开。

---

## 第一章 从命令行到可运行的 Agent

### 1.1 入口注册：`pyproject.toml` 如何让 `enigma` 成为命令

`pyproject.toml` 中声明了一个 console_scripts 入口点：

```toml
[project.scripts]
enigma = "enigma.cli:main"
```

执行 `pip install -e .` 或 `uv sync` 后，包管理器会在 Python 环境的 `Scripts/` 目录下自动生成一个 `enigma.exe`（Windows）或 `enigma`（Unix）。这个可执行文件的内容等价于：

```python
from enigma.cli import main
main()
```

开发模式（`-e`）下，这个脚本每次都实时 import 磁盘上的源码，所以改了代码不需要重新安装。同时 `enigma/__main__.py` 提供了 `python -m enigma` 的备用入口，它调用同一个 `main()`。

### 1.2 参数解析：`argparse` 把字符串变成结构化对象

`cli.py:234` 的 `build_arg_parser()` 定义了所有 CLI 参数：

| 参数 | 类型 | 默认值 | 作用 |
|------|------|--------|------|
| `prompt` | 位置参数（`nargs="*"`） | 无 | 一次性任务文字，如 `enigma "fix bug"` |
| `--provider` | 枚举 | `openai` | 模型后端：ollama / openai / anthropic |
| `--model` | 字符串 | 按 provider 回退 | 模型名覆盖 |
| `--host` | 字符串 | `http://127.0.0.1:11434` | Ollama 服务地址 |
| `--base-url` | 字符串 | 按 provider 回退 | API 基础 URL |
| `--approval` | 枚举 | `ask` | 工具审批策略：ask / auto / never |
| `--max-steps` | 整数 | 6 | 每次请求最大工具调用轮数 |
| `--max-new-tokens` | 整数 | 512 | 模型单步最大输出 token 数 |
| `--temperature` | 浮点数 | 0.2 | 采样温度 |
| `--resume` | 字符串 | 无 | 恢复指定 session 或 `latest` |
| `--plan` | 开关 | `False` | Plan 模式：只读探索 → 产出计划 → 审批后执行 |

`argparse.parse_args(sys.argv)` 自动完成三件事：
1. 按定义把原始字符串列表拆解为命名字段
2. 类型转换（字符串 → int/float）和枚举校验
3. 填充默认值

结果是一个 `Namespace` 对象，如 `args.provider == "ollama"`, `args.max_steps == 6`。

### 1.3 对象组装：`build_agent(args)` 的内部结构

`cli.py:186` 的 `build_agent(args)` 是整个组装链路的核心。它把扁平的参数翻译成一整棵对象图。下面按执行顺序拆解。

#### 第一步：整理 Secret 名单

```python
configured_secret_names = _configured_secret_names(args)
```

合并三个来源：
- 内置默认值：`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_PAT` 等
- 用户通过 `--secret-env-name` 传入的额外名称
- 环境变量 `ENIGMA_SECRET_ENV_NAMES`（逗号分隔）

这份名单后续用于 trace/report 中的密钥脱敏。

#### 第二步：采集工作区快照

```python
workspace = WorkspaceContext.build(args.cwd)
```

`WorkspaceContext.build()` 位于 `workspace.py:54`，是 agent 对仓库的"第一印象"采集器。它通过调用 `git` 子进程获取以下信息：

| 字段 | 来源 | 说明 |
|------|------|------|
| `cwd` | `Path(cwd).resolve()` | 用户启动时的工作目录（绝对路径） |
| `repo_root` | `git rev-parse --show-toplevel` | Git 仓库根目录 |
| `branch` | `git branch --show-current` | 当前分支名 |
| `default_branch` | `git symbolic-ref --short refs/remotes/origin/HEAD` | 默认分支（通常 `main` 或 `master`） |
| `status` | `git status --short` | 工作区变更状态（截断到 1500 字符） |
| `recent_commits` | `git log --oneline -5` | 最近 5 条提交 |
| `project_docs` | 扫描白名单文件 | 项目文档内容（见下文） |

**项目文档扫描**：同时检查 `repo_root` 和 `cwd` 两个目录下是否存在 `AGENTS.md`、`README.md`、`pyproject.toml`、`package.json`。找到的文件内容被截断到 1200 字符后存入 `project_docs` 字典，key 是相对于 `repo_root` 的路径。

所有 git 命令都有 5 秒超时，失败时返回空字符串或 `"-"` 等 fallback 值，不会阻塞启动。

**非 Git 仓库的根目录探测**：当 `git rev-parse --show-toplevel` 失败时（非 git 仓库），`repo_root` 默认等于 `cwd`。此时 `_find_project_root()` 会检查 cwd 下是否恰好只有一个子目录包含项目标记文件（`.git`、`pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`），如果是则自动使用该子目录作为 `repo_root`。这解决了从外层目录（如 `E:\Enigma\`）启动时，自动发现内层项目根（如 `E:\Enigma\Enigma\`）的问题。

**顶层目录摘要**：`text()` 方法输出中包含 `structure` 字段，列出 `repo_root` 下第一层目录及其文件数（如 `enigma/ (12 .py), tests/ (8 .py), docs/ (3 .md)`）。最多显示 20 个目录，跳过 `.git`、`.enigma` 等忽略项。目录名列表也纳入 `fingerprint()` 的哈希计算，新增/删除文件夹时自动触发 prefix 刷新。

**欢迎信息**：`build_welcome()` 显示 ROOT 路径时附带父目录（如 `ROOT E:\Enigma\Enigma (from E:\Enigma)`），方便识别同名嵌套目录。

#### 第三步：创建 Session 存储

```python
store = SessionStore(workspace.repo_root + "/.enigma/sessions")
```

`SessionStore`（`runtime.py:66`）是一个极简的 JSON 文件存储。每个 session 对应一个 `{session_id}.json` 文件，包含：
- `id`：时间戳 + 随机 hex，如 `20260503-143022-a1b2c3`
- `history`：完整的对话历史
- `memory`：分层记忆状态
- `checkpoints`：任务检查点

`SessionStore.latest()` 按文件修改时间排序，返回最新的 session ID。

#### 第四步：构建模型客户端

```python
model = _build_model_client(args)
```

`_build_model_client()`（`cli.py:103`）根据 `--provider` 参数选择对应的客户端类，并注入配置：

**Ollama 后端** → `OllamaModelClient`：
- `model`：默认 `qwen3.5:4b`
- `host`：默认 `http://127.0.0.1:11434`
- 使用 Ollama 的 `/api/generate` 接口，纯 HTTP POST，无 prompt cache 支持

**OpenAI 兼容后端** → `OpenAICompatibleModelClient`：
- `model`：默认 `gpt-5.4`（可通过 `OPENAI_MODEL` 环境变量覆盖）
- `base_url`：默认 `https://www.right.codes/codex/v1`
- `api_key`：从 `OPENAI_API_KEY` 环境变量读取

**Anthropic 兼容后端** → `AnthropicCompatibleModelClient`：
- `model`：默认 `claude-sonnet-4-6`（可通过 `ANTHROPIC_MODEL` 环境变量覆盖）
- `base_url`：默认 `https://www.right.codes/claude/v1`
- `api_key`：依次尝试 `ANTHROPIC_API_KEY` → `RIGHT_CODES_API_KEY` → `OPENAI_API_KEY`

三个客户端类都暴露统一接口 `complete(prompt, max_new_tokens, **kwargs)`，runtime 不关心底层 HTTP 协议差异。

#### 第五步：组装 Enigma 实例

根据是否传了 `--resume`，走两条路径：

**新建 session**（默认路径）：
```python
Enigma(
    model_client=model,
    workspace=workspace,
    session_store=store,
    approval_policy=args.approval,
    max_steps=args.max_steps,
    max_new_tokens=args.max_new_tokens,
    plan_mode=getattr(args, "plan", False),
    secret_env_names=configured_secret_names,
)
```

**恢复旧 session**：
```python
Enigma.from_session(
    model_client=model,
    workspace=workspace,
    session_store=store,
    session_id=session_id,  # 具体 ID 或 "latest"
    ...同上参数...
)
```

`from_session()` 从磁盘加载 session JSON，然后调用同一个 `__init__`，只是把 `session` 参数从 `None` 替换为加载的数据。

### 1.4 Enigma 构造函数内部发生了什么

`Enigma.__init__()`（`runtime.py:88`）是组装的终点，也是运行的起点。它依次完成以下初始化：

```
__init__(model_client, workspace, session_store, ...)
  │
  ├─ 基础属性赋值
  │   model_client, workspace, root, approval_policy, plan_mode, max_steps ...
  │
  ├─ Session 初始化
  │   新建或恢复 session dict
  │   _ensure_session_shape() → 补全缺失字段（history, memory, checkpoints ...）
  │
  ├─ 记忆系统
  │   LayeredMemory(session["memory"], workspace_root)
  │   ├── working memory：当前任务摘要 + 最近接触的文件
  │   ├── episodic notes：会话内短期笔记
  │   ├── file summaries：文件内容摘要缓存
  │   └── durable memory：跨会话持久记忆（存储在 .enigma/memory/topics/*.md）
  │
  ├─ 工具注册
  │   build_tools() → toolkit.build_tool_registry(self)
  │   正常模式：7 个工具（list_files, read_file, search, run_shell,
  │   write_file, patch_file, delegate）
  │   Plan 模式：过滤掉 risky 工具，只保留只读工具
  │
  ├─ Prompt 前缀构建
  │   build_prefix() → 生成 agent 的"工作手册"
  │   包含：身份声明 + 工具列表 + 调用格式示例 + 工作区快照文本
  │   返回 PromptPrefix（text, hash, workspace_fingerprint, tool_signature）
  │
  ├─ 上下文管理器
  │   ContextManager(self)
  │   负责按预算组装完整 prompt（prefix + memory + history + request）
  │
  ├─ 恢复状态评估
  │   evaluate_resume_state() → 检查 checkpoint 是否过期
  │   对比：key_files 的 freshness、runtime_identity 是否变化
  │   返回 status：full-valid / partial-stale / workspace-mismatch / ...
  │
  └─ Session 持久化
      session_store.save(session) → 写入 .enigma/sessions/{id}.json
```

### 1.5 运行分发：One-shot vs REPL

组装完成后，`main()` 根据 `args.prompt` 是否为空决定运行模式：

**One-shot 模式**（`args.prompt` 非空）：
```python
prompt = " ".join(args.prompt).strip()
agent.ask(prompt)  # 跑一次，输出结果，退出
```

**交互式 REPL**（`args.prompt` 为空）：

REPL 使用双线程架构（`enigma/repl.py`）：主线程处理键盘输入和 spinner 显示，工作线程执行 `agent.ask()`。

```
┌─ 主线程（键盘 + spinner）──────────────┐
│  空闲时：input("enigma> ") 阻塞等待    │
│  运行时：非阻塞逐键读取                 │
│    · 正常字符 → 追加到暂存消息          │
│    · Esc → 清除暂存消息                 │
│    · Ctrl+C → 中断 agent（不退出）      │
│  spinner 线程：每 100ms 更新动画 + 耗时 │
├─ 工作线程（agent.ask()）───────────────┤
│  执行模型调用 + 工具执行                │
│  检查 _cancel_requested flag            │
└────────────────────────────────────────┘
```

**斜杠命令**（本地处理，不发送给 agent）：
- `/help` — 显示帮助
- `/memory` — 显示工作记忆
- `/session` — 显示 session 文件路径
- `/reset` — 清除会话
- `/compact [focus]` — 压缩上下文
- `/plan <task>` — 进入 Plan 模式
- `/review [target]` — 代码审查（支持 PR 链接、分支对比、指定文件、自动检测）
- `/skills` — 列出可用 Skill
- `/<skill-name> [args]` — 调用 Skill
- `/exit` — 退出

**暂存消息队列**：agent 运行期间用户键入的文本会显示在输入框上方作为暂存消息，`[Esc] cancel` 提示可清除。agent 完成后暂存消息自动发送。效果：

```
 enigma> fix the bug
◐ Working... (3s)
  > add error handling too        ← 暂存消息
    [Esc] cancel
  1/6  ✓ read_file auth.py  (89ms)
  > add error handling too        ← agent 完成后自动发送
```

**Prompt 状态指示**：
- 空闲：`enigma>` （绿色）
- 运行中：`enigma⟳` （灰色，配合 spinner）

REPL 模式下，同一个 `Enigma` 实例贯穿整个会话，所以 session history 和 working memory 会跨轮延续。

### 1.6 数据流总览

```
enigma --provider ollama "fix the bug"
  │
  ▼
[入口] enigma.exe → cli.main()
  │
  ▼
[参数解析] argparse.parse_args()
  args.prompt = ["fix", "the", "bug"]
  args.provider = "ollama"
  args.model = None → "qwen3.5:4b"
  args.approval = "ask"
  args.max_steps = 6
  ...
  │
  ▼
[组装] build_agent(args)
  ├─ WorkspaceContext.build(".")
  │   cwd = "E:/projects/myrepo"
  │   repo_root = "E:/projects/myrepo"
  │   branch = "feature/auth"
  │   default_branch = "main"
  │   status = "M src/auth.py"
  │   recent_commits = ["a1b2c3 Add login", ...]
  │   project_docs = {"README.md": "...", "pyproject.toml": "..."}
  │
  ├─ _build_model_client(args)
  │   → OllamaModelClient(model="qwen3.5:4b", host="http://127.0.0.1:11434")
  │
  ├─ SessionStore(".enigma/sessions")
  │
  └─ Enigma(model_client, workspace, session_store, ...)
      ├─ LayeredMemory（分层记忆）
      ├─ ToolRegistry（7 个工具，Plan 模式下过滤为只读）
      ├─ PromptPrefix（工作手册 + 工作区快照）
      ├─ ContextManager（prompt 预算组装器）
      └─ evaluate_resume_state()（检查点状态评估）
  │
  ▼
[运行] agent.ask("fix the bug")
  → 主循环：感知 → 决策 → 行动 → 记录
```

这就是从一行终端命令到一个完整 agent 实例的全部组装过程。

---

## 第二章 运行时主循环与执行编排设计

### 2.1 主循环架构：`ask()` 方法总览

`ask()` 是 Enigma 的心脏（`runtime.py:776`）。它把用户的一句话展开成一条可持续推进的控制循环，直到模型给出最终答案或系统主动停下。

整个方法可以抽象为四阶段循环：

```
用户请求
  │
  ▼
┌──────────────────────────────────────────────┐
│  初始化：记录用户消息 → 创建 TaskState        │
│           → 创建 run 目录 → 写第一条 trace    │
└──────────────────────────────────────────────┘
  │
  ▼
┌──── 主循环 ──────────────────────────────────┐
│                                              │
│  ① 感知  组装 prompt（prefix + memory +       │
│          history + request）                  │
│          ↓                                   │
│  ② 决策  调用模型 → 解析输出                  │
│          → (tool | final | retry)            │
│          ↓                                   │
│  ③ 行动  如果是 tool：校验 → 审批 → 执行      │
│          ↓                                   │
│  ④ 记录  写 history / trace / checkpoint /   │
│          更新 memory                         │
│          ↓                                   │
│  → 回到 ①，直到停机条件                       │
│                                              │
└──────────────────────────────────────────────┘
  │
  ▼
停机：返回最终答案 + 写 report
```

循环的两个计数器控制停机：

- **`tool_steps`**：实际执行工具的次数，上限 `max_steps`（默认 6）
- **`attempts`**：模型返回任意内容的总次数（包括格式错误的重试），上限 `min(max_steps * 3, max_steps + 4)`

这个设计允许模型偶尔输出格式错误，但不允许它用重试无限消耗 API 调用。

### 2.2 感知阶段：Prompt 组装

每轮循环开始时，`_build_prompt_and_metadata()` 被调用（`runtime.py:547`），它做三件事：

```
_build_prompt_and_metadata(user_message)
  │
  ├─ 1. refresh_prefix()
  │     检查工作区是否变化 → 如果变了，重建 prefix
  │
  ├─ 2. evaluate_resume_state()
  │     检查 checkpoint 是否过期
  │
  └─ 3. context_manager.build(user_message)
        按预算拼装完整 prompt
```

#### 2.2.1 Prefix 刷新

`refresh_prefix()`（`runtime.py:385`）重新采集工作区快照 `WorkspaceContext.build()`，和上一轮的 `fingerprint` 对比。只有工作区事实真的变了（分支切换、新的 commit、status 变化），才重建完整的 prefix 文本。这是一个廉价的缓存策略——大多数轮次只需要一次 `fingerprint` 比较。

#### 2.2.2 ContextManager：基于预算的 Prompt 拼装

`ContextManager.build()`（`context_manager.py`）是 prompt 组装的核心。它把 prompt 分成 6 个 section（外加 current_request），每个有独立预算。预算按总预算的比例分配，而非硬编码：

| Section | 占比 | 内容 | 压缩优先级 |
|---------|------|------|-----------|
| `prefix` | 28% | 身份声明 + 工具列表 + 工作区快照 | 最后压缩 |
| `startup_memory` | 6% | `MEMORY.md` 启动记忆（跨会话稳定事实） | 倒数第二 |
| `memory` | 12% | 工作记忆（任务摘要、最近文件、笔记） | 第三 |
| `relevant_memory` | 9% | BM25 召回的情景 + 语义记忆片段 | 最先压缩 |
| `rolling_summary` | 5% | `/compact` 产生的滚动摘要 | 第四 |
| `history` | 40% | 完整对话历史（带分类压缩） | 第二 |
| `current_request` | 不限 | 用户当前请求 | **永不裁剪** |

总预算由模型上下文窗口自动决定：`total_budget = context_window × 0.75`。例如 `claude-sonnet-4-6`（1M 窗口）自动分配 750K token 预算，`qwen3`（32K 窗口）分配 24K token。下限为各 section 预算的 25%。当 prompt 超出总预算时，按固定顺序逐段压缩：

```
relevant_memory → history → memory → rolling_summary → startup_memory → prefix
（先牺牲召回记忆，最后才动 prefix）
```

压缩是迭代的：每次只削一个 section 的预算，重新渲染后再检查是否还超。直到不超预算或所有 section 都到达下限。

**历史压缩策略**（`context_manager.py:297`）不是简单截断：
- 最近 6 条历史记录保留较高细节（每条 900 字符）
- 更早的 `read_file` 调用：如果同一文件被读过多次，只保留第一次；如果有 file summary 可复用，用一行摘要替代完整内容
- 更早的 `run_shell` 调用：压缩为 `command → stdout 前 3 行`
- 其他旧记录：压缩到 60 字符

#### 2.2.3 Prompt 最终拼装顺序

`_assemble_prompt()`（`context_manager.py:444`）把 5 个 section 按固定顺序拼接：

```
prefix          ← 稳定的身份和规则
memory          ← 工作记忆
relevant_memory ← 与当前请求相关的笔记
history         ← 对话历史
current_request ← 用户本轮输入（放在最后，离模型最近）
```

这个顺序是刻意的：越稳定的内容越靠前（方便 prompt cache），越动态的越靠后，用户的当前请求永远在最后——因为它是本轮最重要的信号。

### 2.3 决策阶段：模型调用与输出解析

#### 2.3.1 模型调用

prompt 组装完成后，调用 `model_client.complete(prompt, max_new_tokens)`（`runtime.py:919`）。如果后端支持 prompt cache（如 Anthropic），会把 prefix 的 hash 作为 cache key 传给后端，避免每轮重复处理相同的 prefix。

模型返回一段原始文本 `raw`。

#### 2.3.2 输出解析：`Enigma.parse()`

`parse()`（`runtime.py:1212`）是模型输出进入平台控制流的第一道结构化关口。它把自然语言文本解析为三种动作之一：

```
raw 模型输出
  │
  ├─ 包含 <tool>...</tool>？
  │   ├─ 内容是 JSON → ("tool", {"name": "...", "args": {...}})
  │   └─ JSON 格式错误 → ("retry", "请重新输出合法格式")
  │
  ├─ 包含 <tool name="..." ...>...</tool>？（XML 风格）
  │   └─ 解析属性和子标签 → ("tool", {"name": "...", "args": {...}})
  │
  ├─ 包含 <plan>...</plan>？
  │   └─ ("plan", "计划文本")  ← Plan 模式专用
  │
  ├─ 包含 <final>...</final>？
  │   └─ ("final", "最终答案文本")
  │
  └─ 都不包含？
      ├─ 非空文本 → ("final", raw)  ← 兜底：当最终答案处理
      └─ 空文本 → ("retry", "请返回非空内容")
```

`<plan>` 标签优先级高于 `<final>` 但低于 `<tool>`。Plan 模式下如果模型输出 `<final>` 而非 `<plan>`，内容仍被当作计划返回 `PlanResult`（兜底处理）。

**XML 风格工具调用**（`parse_xml_tool()`，`runtime.py:1276`）是为 `write_file`/`patch_file` 设计的替代语法。因为这两个工具的参数（文件内容）可能包含 JSON 需要转义的字符，用 XML 标签包裹更自然：

```xml
<tool name="write_file" path="app.py"><content>def hello():
    pass
</content></tool>
```

**Retry 机制**：当模型输出格式不合法时，`parse()` 返回 `("retry", notice)`，主循环会把这条 notice 写入 history，然后继续下一轮。模型在下一轮能读到这条"格式纠正提示"，有机会自我修正。但 retry 计入 `attempts` 上限，防止无限重试。

### 2.4 行动阶段：工具执行流水线

当 `parse()` 返回 `("tool", payload)` 时，进入 `run_tool()`（`runtime.py:1000`）。这不是简单的"调函数"，而是一条带多层护栏的流水线：

```
模型输出: <tool>{"name":"write_file","args":{...}}</tool>
  │
  ▼
┌─ ① 工具存在性检查 ─────────────────────────┐
│  self.tools.get(name)                       │
│  不存在 → "error: unknown tool"             │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ② 参数校验 ───────────────────────────────┐
│  validate_tool(name, args)                  │
│  · 路径穿越检查（所有路径锚定在 repo_root）  │
│  · 必填参数检查                              │
│  · delegate 深度检查                         │
│  不合法 → "error: invalid arguments"        │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ③ 重复调用检测 ───────────────────────────┐
│  比较最近 2 条 tool history                  │
│  如果 name + args 完全相同 → 拒绝           │
│  "error: repeated identical tool call"      │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ④ 审批策略 ───────────────────────────────┐
│  只有 risky=True 的工具才需要审批            │
│  · "auto"  → 自动通过                       │
│  · "never" → 自动拒绝                       │
│  · "ask"   → 交互式 y/N 确认                │
│  read_only 模式下所有 risky 工具被拒绝       │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ⑤ 工作区快照（仅 risky 工具）─────────────┐
│  执行前 capture_workspace_snapshot()         │
│  执行后再次 capture，对比 diff               │
│  记录：哪些文件变了、workspace fingerprint   │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ⑥ 真正执行 ──────────────────────────────┐
│  result = tool["run"](args)                 │
│  result = clip(result)  ← 截断到 4000 字符  │
└─────────────────────────────────────────────┘
  │
  ▼
┌─ ⑦ 结果后处理 ─────────────────────────────┐
│  · update_memory_after_tool()               │
│    读文件 → 生成摘要进 memory               │
│    写文件 → 失效旧摘要                      │
│  · record_process_note_for_tool()           │
│    失败/部分成功 → 写入过程笔记              │
│  · 构建 _last_tool_result_metadata          │
│    供 trace 写入                            │
└─────────────────────────────────────────────┘
  │
  ▼
返回 result（字符串）给主循环
```

**工具白名单**（`tools.py`）共 8 个工具（7 个基础 + 1 个条件注册）：

| 工具 | risky | 说明 |
|------|-------|------|
| `list_files` | No | 列出目录文件 |
| `read_file` | No | 读取文件内容（按行范围） |
| `search` | No | rg 搜索（降级到 grep） |
| `web_search` | No | 联网搜索，支持域名过滤 |
| `run_shell` | **Yes** | 执行 shell 命令 |
| `write_file` | **Yes** | 写入文件 |
| `patch_file` | **Yes** | 精确文本替换 |
| `delegate` | No | 委派只读子 agent（条件注册） |

**路径穿越防护**：所有文件类工具调用 `Enigma.path()`（`runtime.py:1338`），它把相对路径锚定在 `repo_root` 下，然后用 `os.path.commonpath()` 检查解析后的路径是否还在仓库内。符号链接解析后跳出仓库也会被拦截。

### 2.5 记录阶段：状态更新

每轮循环结束后（无论模型返回的是工具调用、最终答案还是重试），都要写入多个状态层。记录的目的是：即使程序崩溃，已执行的步骤不会丢失；下次恢复时能清楚知道"做到了哪一步"。

#### 2.5.1 Session History

```python
self.append_session_history({"role": "tool", "name": ..., "args": ..., "content": ..., "created_at": ...})
```

`append_session_history()`（`runtime.py:463`）把一条记录追加到 `session["history"]` 列表，然后立即 `session_store.save()` 落盘。History 记录三种角色：

- `"user"`：用户请求
- `"assistant"`：模型最终回答或重试提示
- `"tool"`：工具执行结果（附带 `name` 和 `args`）

#### 2.5.2 Trace 与 Report

每次 `ask()` 运行期间，系统会产出两类持久化产物：

- **Trace**（`trace.jsonl`）：逐事件时间线，由 `emit_trace()` 在关键节点写入。覆盖的事件包括 run 生命周期（started/finished）、prompt 构建、模型调用、工具执行、checkpoint 创建等，每条事件附带当时的上下文数据和耗时。
- **Report**（`report.json`）：运行结束时写入一次，包含最终状态、工具步数、prompt 元数据、durable memory 提升记录等摘要信息。

两者都经过 `redact_artifact()` 递归脱敏，确保环境变量中的密钥不会出现在落盘文件中。具体产物结构在"会话状态、运行工件与恢复机制"章节展开。

#### 2.5.3 记忆更新（概述）

工具执行后，`update_memory_after_tool()`（`runtime.py:642`）会更新 working memory，供下一轮 prompt 的 memory section 使用。具体机制在记忆系统章节展开，这里只需知道：

- `read_file` 后：文件路径记入 `recent_files`，文件摘要记入 `file_summaries`
- `write_file` / `patch_file` 后：该文件的旧摘要被失效（因为内容已变）
- 工具失败时：一条过程笔记（kind=`"process"`）被追加到 episodic notes

#### 2.5.4 Checkpoint 创建

每次工具执行后或收到最终答案后，都调用 `create_checkpoint()`（`runtime.py:601`）。Checkpoint 存储在 session JSON 中，是恢复机制的数据基础。每条 checkpoint 记录了：当前目标、已完成步骤、关键文件列表（附 SHA-256 freshness）、运行环境快照（`runtime_identity`），以及通过 `parent_checkpoint_id` 指向上一条 checkpoint 的链式指针。

主循环中有 6 个触发点创建 checkpoint：工具执行后、运行正常结束、运行因限制停止、恢复时文件 freshness 不一致、恢复时 runtime 身份变更、prompt 超预算被压缩。

Checkpoint 的内部结构和恢复机制在"会话状态、运行工件与恢复机制"章节展开。

### 2.6 停机条件与异常处理

主循环有三种退出路径：

#### 路径 1：正常结束（模型返回 `<final>`）

```python
final = (payload or raw).strip()
task_state.finish_success(final)
return final
```

这是最理想的路径：模型完成了任务，返回最终答案。

#### 路径 2：工具步数耗尽

```python
# tool_steps >= max_steps
final = "Stopped after reaching the step limit without a final answer."
task_state.stop_step_limit(final)
```

模型一直在调用工具但始终没有给出最终答案。`max_steps` 默认 6，意味着最多执行 6 次工具调用。

#### 路径 3：重试次数耗尽

```python
# attempts >= max_attempts
final = "Stopped after too many malformed model responses without a valid tool call or final answer."
task_state.stop_retry_limit(final)
```

模型反复输出格式错误的内容。`max_attempts = min(max_steps * 3, max_steps + 4)`，默认配置下是 `min(18, 10) = 10`。

三种路径都会执行相同的收尾：写 history → promote durable memory → 创建 checkpoint → 写 trace → 写 report → 返回 final 字符串。

#### 路径 4：用户中断（Ctrl+C）

```python
if self.check_cancel():
    final = "Stopped by user."
    task_state.stop("user_cancel", final_answer=final)
    return final
```

REPL 中用户按 Ctrl+C 时，主线程设置 `_cancel_requested = True`。主循环在每个迭代开头调用 `check_cancel()`，发现 flag 后立即返回。当前正在执行的 HTTP 请求不会被打断（等 timeout 或自然完成），但下一个检查点会立即停止。

#### 路径 5：Plan 模式产出计划

```python
if kind == "plan":
    task_state.stop(STOP_REASON_PLAN_READY, status=STATUS_COMPLETED, final_answer=plan_content)
    return PlanResult(plan=plan_content, session_id=self.session["id"])
```

Plan 模式下模型输出 `<plan>` 标签时，`ask()` 不返回字符串而返回 `PlanResult` 对象，由 REPL 的审批循环处理。

### 2.7 检查点与恢复机制

Checkpoint 通过 `parent_checkpoint_id` 形成链式结构，`SessionStore` 保留所有历史检查点并用 `current_id` 指向最新一条。

每次 `ask()` 开始时，`evaluate_resume_state()`（`runtime.py:211`）会检查上次 checkpoint 是否还"新鲜"——依次比对 schema 版本、key_files 的 SHA-256 freshness、以及 runtime_identity（cwd、model、approval_policy 等 11 个维度）。根据比对结果返回五种状态之一：`no-checkpoint`、`full-valid`、`partial-stale`、`workspace-mismatch`、`schema-mismatch`。

主循环根据状态做出响应：`partial-stale` 时立即创建新 checkpoint 避免过期推理；`workspace_mismatch` 时记录差异到 trace 并创建新 checkpoint；上下文被压缩时也会创建 checkpoint 记录这一事实。

Checkpoint 的详细数据结构和恢复流程在"会话状态、运行工件与恢复机制"章节展开。

### 2.8 Plan 模式

Plan 模式让 agent 先以只读方式探索代码库，产出结构化计划，用户审批后再执行。

#### 2.8.1 进入 Plan 模式

两种方式：
- CLI：`enigma --plan "任务描述"`
- REPL：`/plan 任务描述`

进入后 `plan_mode=True`，触发三个变化：
1. **工具过滤**：`build_tools()` 移除所有 `risky=True` 的工具（write_file、patch_file、run_shell），只保留只读工具
2. **Prefix 替换**：`build_prefix()` 使用 Plan Mode 专用提示词，强调只读探索和 `<plan>` 输出格式
3. **输出解析**：`parse()` 识别 `<plan>` 标签，返回 `("plan", content)`

#### 2.8.2 Plan Mode 的 Prefix

Plan Mode 的 prefix 模仿 Claude Code 的 plan mode 提示词：

```
You are enigma in PLAN MODE. Your job is to explore the codebase and design an implementation plan.

Rules:
- You are in READ-ONLY phase. Do NOT write files, run shell commands, or make any changes.
- Use read-only tools to understand the codebase before planning.
- Return exactly one <tool>...</tool> or one <plan>...</plan>.
- When ready, output your plan as:
  <plan>
  ## Goal
  ## Files to modify
  ## Steps
  ## Risks / open questions
  </plan>
- Be thorough in exploration before producing the plan.
```

#### 2.8.3 审批流程

`ask()` 返回 `PlanResult` 后，REPL 进入内联审批循环：

```
============================================================
PLAN
============================================================
## Goal
Add error handling to the API
## Steps
1. ...
============================================================
  [1] Approve  - execute this plan
  [2] Revise   - give feedback to refine
  [3] Reject   - stop here, do nothing
> 2
Your feedback: 先不改依赖，只做接口层抽象
[agent 修订计划，再次弹出菜单]
> 1
Plan approved. Executing...
```

`exit_plan_mode()` 方法负责模式切换：设 `plan_mode=False`，重建工具表（恢复 risky 工具），重建 prefix（恢复正常模式提示词），将批准的计划写入 `.enigma/plan.md` 并在 history 中注入文件路径引用。

**Plan 文件持久化**：approve 后计划写入 `.enigma/plan.md`，而非直接塞进 history。原因是 history 会被 context manager 压缩截断，而 plan 是执行阶段的核心参考。正常模式的 prefix 会注入提示让模型在需要时读取该文件。执行完毕或 plan mode 退出时自动清理。

REPL 线程安全：工作线程运行期间，审批策略临时切换为 `auto`（避免 worker 线程的 `input()` 与主线程的 `msvcrt` 争抢 stdin），完成后恢复原值。

#### 2.8.4 PlanResult 数据类

```python
@dataclass
class PlanResult:
    plan: str        # 计划文本
    session_id: str  # 当前 session ID
```

`ask()` 的返回类型从 `str` 变为 `str | PlanResult`。调用方通过 `isinstance(result, PlanResult)` 判断。

### 2.9 终端显示与视觉增强

#### 2.9.1 工具结果显示

`display_tool_result()` 在工具执行后显示颜色标注的结果标题和预览：

```
  1/6  ✓ read_file README.md  (12ms)    ← 绿色，成功
  2/6  ✗ run_shell "pytest"  (3.2s)     ← 红色，失败
  3/6  ~ patch_file app.py  (89ms)      ← 黄色，部分成功
```

最近一条结果展开最多 15 行预览。当新结果出现时，旧结果的预览被 ANSI cursor up 操作替换为单行摘要（`format_result_compact`）。

显示逻辑在 `enigma/display.py` 中实现为纯函数，`show_tool_activity=False` 时（测试默认）不执行。

#### 2.9.2 文件 Diff 显示

`write_file` 和 `patch_file` 执行后，`display_file_diff()` 自动显示 unified diff：

```
  4/6  ✓ patch_file app.py  (5ms)
 --- a/app.py
 +++ b/app.py
 @@ -10,3 +10,3 @@
  def greet(name):
 -    return "hello"
 +    return f"hello {name}"
```

实现方式：`run_tool()` 在执行前读取旧内容存入 `_last_diff_info`，执行后读取新内容，用 `difflib.unified_diff` 生成差异，`+` 行绿色、`-` 行红色、`@@` 行青色。

#### 2.9.3 模型 Token 流式输出

三个 model client 的 `complete()` 方法新增 `on_token` 回调参数：

```python
def complete(self, prompt, max_new_tokens, ..., on_token=None):
```

当 `on_token` 不为 None 时，`stream=True`，逐 SSE/NDJSON event 调用 `on_token(delta)` 打印到终端。`FakeModelClient` 通过 `**kwargs` 自动忽略，测试不受影响。

`ask()` 中通过 `_make_stream_callback()` 创建回调闭包，模型输出结束后打印换行。

### 2.10 Skill 系统

Skill 系统让 agent 具备可复用的任务模板能力——用户可以通过 `/skill-name args` 快速调用预定义的工作流。

#### 2.10.1 设计理念

与 Claude Code 的 skill 机制类似，但适配了 enigma 的零依赖约束：

- **三级渐进加载**：metadata 始终可见（名称 + 描述），SKILL.md 按需读取，references/ 目录下的参考文件在模型需要时才加载
- **三源发现**：内置 skills（`enigma/skills/`）、用户级（`~/.enigma/skills/`）、项目级（`.enigma/skills/`），同名覆盖（项目 > 用户 > 内置）
- **目录式 + 单文件兼容**：一个 skill 可以是单个 `.md` 文件，也可以是包含 `SKILL.md` + `references/` 的目录

#### 2.10.2 Skill 文件格式

每个 skill 是一个带 frontmatter 的 Markdown 文件：

```markdown
---
name: test-writer
description: 为指定文件编写全面的测试用例
arguments:
  - name: file
    description: 要测试的文件路径
    required: true
---

你是一个测试编写专家。请为 {{file}} 编写全面的测试用例。

要求：
- 覆盖正常路径和边界情况
- 使用项目现有的测试框架
- 测试文件命名为 test_{{file}}
```

`{{file}}` 是模板变量，在调用时被用户参数替换。

#### 2.10.3 三源发现机制

```python
def discover_skills(cwd):
    # 1. 内置 skills（enigma/skills/）
    # 2. 用户级（~/.enigma/skills/）
    # 3. 项目级（.enigma/skills/）
    # 同名 skill 后者覆盖前者
```

`build_agent()` 启动时调用 `discover_skills()`，将结果挂载到 `agent.skills` 字典。Skill 的 metadata 始终注入到 prefix 中，模型能看到所有可用 skill 的名称和描述。当用户调用 `/skill-name` 时，SKILL.md 内容被读取并组装成 prompt 发给模型。

#### 2.10.4 Skill 在 Prompt 中的位置

```
prefix
  │
  ├─ 身份声明 + 行为规则
  ├─ 工具清单
  ├─ Skill 列表（名称 + 描述，始终可见）
  │   - test-writer: 为指定文件编写全面的测试用例
  │   - reviewer: 代码审查
  │   - refactor: 重构建议
  ├─ 工作区快照
  │
  ▼
用户输入 /skill-name args
  │
  ▼
SKILL.md 内容被读取，组装为 prompt 发给模型
```

内置 5 个 skill：test-writer、reviewer、refactor、explain、doc-writer。

---

## 第三章 工具的接入与调用设计

### 3.1 工具注册架构：白名单而非发现

Enigma 的工具不是动态发现的，而是在 `tools.py` 中显式声明、在 `build_tool_registry()` 中显式注册。这样做有一个核心理由：**模型看到的是一个有边界、可审计的动作集合**，而不是"凡是 import 能到的函数都可以调"。

注册发生在 `Enigma.__init__()` 中：

```python
# agent 初始化时一次性注册所有可用工具，后续不再动态增减
self.tools = build_tool_registry(self)
```

`build_tool_registry()`（`tools.py:71`）做两件事：

1. 遍历 `BASE_TOOL_SPECS` 字典，为每个工具绑定一个 `run` 函数（通过 `functools.partial` 把 `agent` 实例注入进去）
2. 如果当前 agent 的深度还没到上限（`depth < max_depth`），额外注册 `delegate` 工具

```python
def build_tool_registry(agent):
    # 把每个工具的 schema/risky/description 和对应的执行函数打包在一起
    # partial(tool_func, agent) 把 agent 绑定为第一个参数，后面调 run(args) 就行
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # delegate 只在深度未耗尽时才暴露，防止子 agent 无限嵌套
    if agent.depth < agent.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
    return tools
```

结果是一个扁平字典，key 是工具名，value 包含 `schema`、`risky`、`description`、`run` 四个字段。

### 3.2 工具清单

当前共 8 个工具（7 个基础 + 1 个条件注册）：

| 工具 | risky | 参数 | 说明 |
|------|-------|------|------|
| `list_files` | No | `path='.'` | 列出目录下的文件和子目录，跳过 `.git`、`__pycache__` 等忽略项，上限 200 条 |
| `read_file` | No | `path, start=1, end=200` | 按行范围读取 UTF-8 文件，输出带行号 |
| `search` | No | `pattern, path='.'` | 优先用 `rg`（ripgrep）搜索，找不到则回退到 Python 纯文本搜索 |
| `web_search` | No | `query, max_results=5, allowed_domains='', blocked_domains=''` | 联网搜索，返回带来源的结果片段 |
| `run_shell` | **Yes** | `command, timeout=20` | 在仓库根目录执行 shell 命令，使用过滤后的环境变量 |
| `write_file` | **Yes** | `path, content` | 写入文本文件，自动创建父目录 |
| `patch_file` | **Yes** | `path, old_text, new_text` | 精确文本替换，`old_text` 必须在文件中恰好出现一次 |
| `delegate` | No | `task, max_steps=3` | 委派只读子 agent 调查任务（条件注册） |

`risky=True` 的工具需要经过审批流程（见 3.5 节），并且在执行前后会采集工作区快照对比。

**Plan 模式下**，`build_tools()` 过滤掉所有 `risky=True` 的工具，只保留 `list_files`、`read_file`、`search`、`web_search` 和 `delegate`。delegate 子 agent 不继承父 agent 的 `plan_mode` 状态。

### 3.3 参数校验机制

`validate_tool()`（`tools.py:89`）在 `run_tool()` 流水线的第二阶段被调用。它为每个工具定义了独立的校验规则：

- **路径类工具**（`list_files`、`read_file`、`write_file`、`patch_file`）：通过 `agent.path()` 解析路径并做穿越检查（见 3.6 节）
- **`read_file`**：额外校验 `start >= 1` 且 `end >= start`
- **`search`**：`pattern` 不能为空
- **`web_search`**：`query` 不能为空且不超过 300 字符；`max_results` 必须在 `[1, 10]` 范围内
- **`run_shell`**：`command` 不能为空；`timeout` 必须在 `[1, 120]` 秒范围内
- **`patch_file`**：除了路径检查外，还会读取文件内容验证 `old_text` 是否恰好出现一次——这是刻意的严格设计，确保修改行为是确定性的：

```python
if name == "patch_file":
    path = agent.path(args["path"])         # 路径穿越检查
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    # 必须恰好出现一次：0 次说明目标不存在，多次说明意图不明确
    # 这比正则替换更安全——模型知道确切的修改位置
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
```

- **`delegate`**：`task` 不能为空

校验失败时，`run_tool()` 会把错误信息返回给模型，同时附上该工具的调用示例（`tool_example()`），让模型有机会自我修正。

### 3.4 工具执行详解

各工具的核心设计要点：

| 工具 | 关键设计 |
|------|---------|
| `list_files` | 先文件后目录排序，跳过 `.git`/`__pycache__` 等噪音，上限 200 条，路径相对仓库根 |
| `read_file` | `errors="replace"` 防二进制崩溃，行号右对齐输出，头部标注文件路径 |
| `search` | 优先用 `rg`（搜索延迟直接影响 agent 效率），无 rg 时回退 Python 纯文本搜索，上限 200 匹配 |
| `web_search` | 零依赖 DuckDuckGo HTML 搜索，支持域名白/黑名单过滤，失败返回错误文本而非抛异常 |
| `run_shell` | `cwd` 锚定仓库根，环境变量只放行白名单（减少密钥泄露），`partial_success` 区分非零退出码 |
| `write_file` | `parents=True` 自动创建中间目录，覆盖写入 |
| `patch_file` | `old_text` 必须恰好出现一次（确定性修改），单次替换 |
| `delegate` | 子 agent `read_only=True`，默认 3 步，深度递增防嵌套 |

`run_shell` 的环境变量隔离是安全设计的重点——`shell_env()` 只保留白名单变量（`PATH`、`HOME`、`PWD` 等），不继承父进程完整环境，减少 API_KEY 等密钥被意外带入命令执行的风险。

#### 3.4.5 `run_shell`

通过 `subprocess.run()` 在仓库根目录执行命令：

```python
subprocess.run(
    command,                # 模型生成的原始命令字符串
    cwd=agent.root,        # 工作目录锚定在仓库根，不会跑到系统其他位置
    shell=True,            # 通过 shell 执行，支持管道、重定向等语法
    capture_output=True,   # 捕获 stdout 和 stderr，不让它直接打到终端
    text=True,             # 输出按字符串（而非 bytes）返回
    timeout=timeout,       # 超时上限（默认 20 秒，最大 120 秒）
    env=agent.shell_env(), # 不继承父进程环境，只放行白名单变量
)
```

关键设计：**环境变量不是直接继承父进程的**。`shell_env()` 只保留白名单中的变量：

```python
def shell_env(self):
    # 只从系统环境中取出 allowlist 里声明过的变量
    # 比如 allowlist=["HOME", "NODE_ENV"]，就只透传这两个
    env = {
        name: os.environ[name]
        for name in self.shell_env_allowlist
        if name in os.environ
    }
    # PWD 强制设为仓库根目录，确保命令在预期位置执行
    env["PWD"] = str(self.root)
    # PATH 通常需要保留，否则大部分命令会找不到可执行文件
    if "PATH" not in env and os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    return env
```

这减少了敏感信息（如 API_KEY、数据库密码）被意外带入命令执行环境的风险。

返回格式统一为 `exit_code` + `stdout` + `stderr`。在 `run_tool()` 的后处理阶段，如果 `exit_code != 0` 且工作区发生了变化，状态标记为 `partial_success` 而非 `error`——因为有些命令（如 `npm install`）会修改文件但返回非零退出码。

#### 3.4.6 `write_file`

```python
# parents=True: 递归创建中间目录（如 src/utils/）
# exist_ok=True: 目录已存在时不报错
path.parent.mkdir(parents=True, exist_ok=True)
# 覆盖写入整个文件内容，编码强制 UTF-8
path.write_text(content, encoding="utf-8")
```

写入后返回 `wrote <相对路径> (<字符数> chars)`。

#### 3.4.7 `patch_file`

```python
text = path.read_text(encoding="utf-8")
count = text.count(old_text)
# 校验阶段已经检查过 count == 1，这里再验一次确保安全
if count != 1:
    raise ValueError(f"old_text must occur exactly once, found {count}")
# 第三个参数 1 表示只替换第一次出现，即使 old_text 在其他地方也有
# 也不会误伤（虽然校验阶段已经保证了只有一次）
path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
```

这种"精确匹配 + 单次替换"的设计是有意为之：让每次修改都是确定性的，不会因为正则转义或多处匹配导致意外结果。如果 `old_text` 出现 0 次或多次，直接报错并告知出现次数，模型可以据此调整。

#### 3.4.8 `delegate`

`delegate` 是一个特殊的工具——它不直接操作文件或执行命令，而是创建一个受限的子 agent 来完成调查任务。

```python
child = Enigma(
    model_client=agent.model_client,  # 共享同一个模型后端，不重复创建连接
    workspace=agent.workspace,        # 共享工作区快照，子 agent 能看到同样的文件
    approval_policy="never",          # 子 agent 跳过审批，避免交互式确认阻塞
    max_steps=int(args.get("max_steps", 3)),  # 默认 3 步，比父 agent（默认 6）更少
    depth=agent.depth + 1,            # 深度 +1，到达 max_depth 后 delegate 不再可用
    read_only=True,                   # 只读模式：risky 工具一律拒绝
    ...
)
# 把父 agent 当前的任务描述和历史摘要塞进子 agent 的记忆
# 这样子 agent 不用从零理解上下文
child.session["memory"]["task"] = task
child.session["memory"]["notes"] = [clip(agent.history_text(), 300)]
# 子 agent 执行完整的 ask() 循环（感知→决策→行动→记录），但受上述约束限制
return "delegate_result:\n" + child.ask(task)
```

子 agent 的约束：
- **只读**：`read_only=True`，所有 `risky` 工具的审批自动拒绝
- **步数更少**：默认最多 3 步，避免子 agent 跑得太远
- **深度限制**：`depth` 递增，当 `depth >= max_depth` 时 `delegate` 工具本身不再注册——防止无限嵌套
- **共享上下文**：继承父 agent 的 `model_client`、`workspace`、`session_store`、`feature_flags`，但有独立的 session 和 memory
- **继承 feature_flags**：子 agent 复制父 agent 的功能开关（包括 `reflection`），避免子 agent 意外触发反思消耗额外模型调用

父 agent 拿到的是子 agent 的最终答案文本，以 `delegate_result:` 前缀标识。

### 3.5 审批策略

`approve()`（`runtime.py:1343`）在 `run_tool()` 流水线的第四阶段被调用，仅对 `risky=True` 的工具生效：

```python
def approve(self, name, args):
    if self.read_only:
        return False                    # 只读模式一律拒绝
    if self.approval_policy == "auto":
        return True                     # 自动放行
    if self.approval_policy == "never":
        return False                    # 自动拒绝
    # "ask" 模式：交互式确认
    answer = input(f"approve {name} {json.dumps(args)}? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}
```

三种策略的适用场景：
- `auto`：CI/CD 环境或信任度高的场景
- `ask`（默认）：交互式开发，每次 risky 操作前人工确认
- `never`：纯分析模式，只读不写

审批被拒绝后，`run_tool()` 记录 `tool_error_code: "approval_denied"` 到 metadata，返回 `error: approval denied for <tool>` 给模型。

### 3.6 路径穿越防护

`Enigma.path()`（`runtime.py:1489`）是所有文件类工具的路径入口：

```python
def path(self, raw_path):
    path = Path(raw_path)
    # 相对路径自动锚定到仓库根目录下，绝对路径保持原样
    path = path if path.is_absolute() else self.root / path
    # resolve() 把 .. 和符号链接都解析成最终的绝对路径
    resolved = path.resolve()
    # commonpath 比较：解析后的路径的共同前缀必须是仓库根目录
    # 如果不是，说明路径逃出了仓库（比如 ../../../etc/passwd）
    if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved
```

防护机制：
1. 相对路径自动锚定在 `self.root`（仓库根目录）下
2. `resolve()` 解析符号链接和 `..` 为绝对路径
3. `os.path.commonpath()` 检查解析后的路径是否还在仓库内

这意味着 `../../etc/passwd` 和指向仓库外的符号链接都会被拦截。校验失败时，`run_tool()` 把 `security_event_type` 标记为 `"path_escape"` 写入 metadata。

### 3.7 重复调用检测

`repeated_tool_call()`（`runtime.py:1194`）检查最近 2 条 tool history 的 `name` 和 `args` 是否完全相同：

```python
def repeated_tool_call(self, name, args):
    # 从历史中筛出所有 tool 角色的记录
    tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
    if len(tool_events) < 2:
        return False  # 不够 2 条，没法判断重复
    # 只看最近 2 条：如果名字和参数都一样，就是重复调用
    recent = tool_events[-2:]
    return all(item["name"] == name and item["args"] == args for item in recent)
```

这是为了防止模型陷入一种常见的坏循环：在没有新信息的情况下反复发起同一调用（比如反复读同一个文件期望得到不同结果）。检测粒度是"最近 2 条"而非"全部历史"，因为偶尔的重复（中间隔了其他操作）是合理的。

### 3.8 工具活动展示

工具执行的终端展示分三个阶段：

**阶段 1：工具调用提示**（执行前）

`display_tool_activity()` 显示工具名和参数摘要：

```
 enigma read_file  path="src/auth.py"
| Reading lines 1-200.
```

**阶段 2：结果标题 + 预览**（执行后）

`display_tool_result()` 显示步骤进度、颜色状态和结果预览：

```
  1/6  ✓ read_file README.md  (12ms)
       # README.md
       1: # My Project
       2: A demo project
```

成功 `✓` 绿色，失败 `✗` 红色，部分成功 `~` 黄色。最近一条展开最多 15 行，旧结果自动折叠为单行摘要。

**阶段 3：文件 Diff**（write_file/patch_file 后）

`display_file_diff()` 显示 unified diff：

```
 --- a/app.py
 +++ b/app.py
 @@ -10,3 +10,3 @@
 -    return "hello"
 +    return f"hello {name}"
```

所有展示逻辑在 `enigma/display.py` 中实现为纯函数，`show_tool_activity=False` 时不执行。终端不支持颜色时回退到纯文本格式。

---

## 第四章 提示词形态与缓存复用设计

### 4.1 Prompt 的六段式结构

每轮发给模型的 prompt 由 `ContextManager._assemble_prompt()`（`context_manager.py`）按固定顺序拼接：

```
┌─────────────────────────────────────────────┐
│  prefix           稳定的身份声明和规则        │  ← 最稳定，适合缓存
├─────────────────────────────────────────────┤
│  startup_memory   MEMORY.md 启动记忆         │  ← 跨会话稳定事实
├─────────────────────────────────────────────┤
│  memory           工作记忆                   │
├─────────────────────────────────────────────┤
│  relevant_memory  与当前请求相关的笔记        │
├─────────────────────────────────────────────┤
│  rolling_summary  滚动摘要（/compact 产物）   │  ← 长任务接力
├─────────────────────────────────────────────┤
│  history          对话历史                   │
├─────────────────────────────────────────────┤
│  current_request  用户本轮输入               │  ← 最动态，永不裁剪
└─────────────────────────────────────────────┘
```

各 section 的预算占比（`context_manager.py:28`）：

| Section | 占比 | 说明 |
|---------|------|------|
| `prefix` | 28% | 身份声明 + 工具列表 + 工作区快照 |
| `startup_memory` | 6% | `MEMORY.md` 头部（200 行 / 25KB 上限） |
| `memory` | 12% | 工作记忆（任务摘要、最近文件、笔记） |
| `relevant_memory` | 9% | BM25 召回的情景 + 语义记忆片段 |
| `rolling_summary` | 5% | `/compact` 产生的滚动摘要，从 SessionDB 读取 |
| `history` | 40% | 对话历史（带分类压缩） |
| `current_request` | 不限 | 用户当前请求，**永不裁剪** |

```python
def _assemble_prompt(self, rendered):
    # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
    # 越靠前的内容越稳定，越适合被 prompt cache 缓存；
    # 越靠后的内容越动态，每轮都在变化。
    return "\n\n".join(
        [
            rendered["prefix"].rendered,           # 身份声明 + 工具列表 + 工作区快照
            rendered["startup_memory"].rendered,    # MEMORY.md 启动记忆
            rendered["memory"].rendered,            # 任务摘要 + 最近文件 + 笔记
            rendered["relevant_memory"].rendered,   # 与当前请求相关的记忆片段
            rendered["rolling_summary"].rendered,   # 滚动摘要
            rendered["history"].rendered,           # 对话历史（带压缩）
            rendered[CURRENT_REQUEST_SECTION].rendered,  # 用户本轮输入
        ]
    ).strip()
```

`current_request` 永远不被裁剪——它是本轮最重要的信号。上下文预算的压缩只会作用于前四个 section。预算压缩的详细算法在"上下文瘦身与输出管理设计"章节展开。

### 4.2 Prefix：Agent 的"工作手册"

Prefix 是 prompt 中最稳定、最长的段落，可以理解为 agent 的"工作手册"。它在 `build_prefix()`（`runtime.py:349`）中生成，包含四个部分：

```
┌─────────────────────────────────────────────┐
│  1. 身份声明                                │
│     "You are enigma, a small local coding   │
│      agent working inside a local           │
│      repository."                           │
├─────────────────────────────────────────────┤
│  2. 行为规则（16 条）                        │
│     · 用工具而非猜测                         │
│     · 每次只返回一个 <tool> 或 <final>       │
│     · 不要编造工具结果                       │
│     · 写测试前先读实现                       │
│     · 用 web_search 获取外部信息             │
│     · 不要重复相同的工具调用                  │
│     · ...                                   │
├─────────────────────────────────────────────┤
│  3. 工具清单 + 调用示例                      │
│     - read_file(path, start, end) [safe]    │
│     - run_shell(command, timeout) [approval │
│       required]                             │
│     <tool>{"name":"list_files",...}</tool>  │
│     <final>Done.</final>                    │
├─────────────────────────────────────────────┤
│  4. 工作区快照                               │
│     WORKSPACE /repo/path                    │
│     BRANCH  main                            │
│     STATUS  M src/auth.py                   │
│     COMMITS a1b2c3 Add login ...            │
│     DOCS    README.md: "# My Project" ...   │
└─────────────────────────────────────────────┘
```

Prefix 的总长度通常在 2000-3500 字符，具体取决于工作区状态和注册的工具数量。

**Plan 模式下**，prefix 的四个部分全部替换：

| 部分 | 正常模式 | Plan 模式 |
|------|---------|----------|
| 身份声明 | "You are enigma, a small local coding agent" | "You are enigma in PLAN MODE" |
| 行为规则 | 16 条（含写操作指南） | 只读规则 + `<plan>` 输出格式 |
| 工具清单 | 全部工具 + 写操作示例 | 只读工具 + plan 示例 |
| 工作区快照 | 相同 | 相同 |

Plan 模式的 prefix 不提及 `write_file`、`patch_file`、`run_shell`，避免模型尝试调用不存在的工具。

#### 4.2.1 PromptPrefix 数据结构

`build_prefix()` 返回一个 `PromptPrefix` dataclass（`runtime.py:57`），除了文本本身还携带元数据：

```python
@dataclass
class PromptPrefix:
    text: str                    # prefix 的完整文本
    hash: str                    # SHA-256(text)，用于判断文本是否变化
    workspace_fingerprint: str   # 工作区快照的指纹
    tool_signature: str          # 工具注册表的 SHA-256 签名
    built_at: str                # 构建时间戳
```

这些元数据让 runtime 能够精确判断 prefix 是否需要重建，而不需要每次都重新拼接整个文本。

### 4.3 Prefix 刷新机制

`refresh_prefix()`（`runtime.py:415`）在每轮 `ask()` 开始时被调用。它采用一种廉价的"指纹比对"策略，避免不必要的重建：

```python
def refresh_prefix(self, force=False):
    # 第一步：重新采集工作区快照
    refreshed_workspace = WorkspaceContext.build(self.root)
    refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()

    # 第二步：对比指纹——只有工作区事实真的变了才标记 changed
    workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
    if workspace_changed:
        self.workspace = refreshed_workspace  # 更新工作区快照

    # 第三步：只有工作区变了（或强制刷新），才重建完整的 prefix 文本
    prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
    prefix_changed = force or previous_hash != prefix_state.hash

    # 第四步：如果 prefix 文本变了，更新运行中的 prefix
    if prefix_changed:
        self._apply_prefix_state(prefix_state)
```

这个设计的关键点：**大多数轮次只需要一次 `fingerprint` 比较**。`WorkspaceContext.build()` 虽然每轮都调，但它内部的 git 命令都是轻量级的（`git branch --show-current`、`git log --oneline -5` 等），加上 5 秒超时保护，不会拖慢循环。

只有当分支切换、新的 commit、工作区 status 变化等事实性变更发生时，fingerprint 才会不同，prefix 才会被重建。

### 4.4 Prompt Cache 复用原理

#### 4.4.1 问题：为什么需要缓存？

Agent 的控制循环每轮都要调一次模型。假设一次 `ask()` 平均 4 轮工具调用，每轮都要把完整的 prompt（约 8000-12000 字符）发给后端。其中 prefix（约 3000 字符）和 memory（约 1000 字符）在连续几轮之间变化很小，但后端每次都要重新处理。

Prompt cache 的核心思想：**让后端记住上一轮处理过的 prefix，下一轮如果 prefix 没变，就直接复用已处理的结果**，省掉重复的 tokenization 和注意力计算。

#### 4.4.2 为什么用 prefix hash 而不是整段 prompt hash？

这是一个关键设计决策。如果用整段 prompt 的 hash 作为 cache key，那么每轮的 history 变化都会导致 cache miss——因为 history 几乎每轮都在增长。

Enigma 的做法是：**只用 prefix 的 SHA-256 hash 作为 cache key**：

```python
# runtime.py:575 — _build_prompt_and_metadata() 中
metadata["prompt_cache_key"] = self.prefix_state.hash
```

这意味着：
- prefix 没变 → cache key 不变 → 后端可以复用缓存
- prefix 变了（工作区切换、工具列表变化）→ cache key 变化 → 后端重新处理

history 和 memory 的变化不会影响 cache key，因为缓存的是"prefix 这一段"，不是整段 prompt。后端在处理新一轮请求时，prefix 部分可以直接从缓存中取出，只需要重新处理后面动态变化的部分。

#### 4.4.3 Cache 参数的流转路径

从 runtime 到后端 API，cache 参数经过三层传递：

```
Enigma.ask()
  │
  ├─ _build_prompt_and_metadata()
  │   计算 prompt_cache_key = prefix_state.hash
  │   prompt_cache_retention = "in_memory"
  │
  ├─ 判断后端是否支持缓存
  │   if self.model_client.supports_prompt_cache:
  │       prompt_cache_key = metadata["prompt_cache_key"]
  │       prompt_cache_retention = "in_memory"
  │
  └─ model_client.complete(prompt, max_new_tokens,
                           prompt_cache_key=...,
                           prompt_cache_retention=...)
      │
      └─ OpenAICompatibleModelClient.complete()
          if self.supports_prompt_cache and prompt_cache_key:
              payload["prompt_cache_key"] = prompt_cache_key
          if self.supports_prompt_cache and prompt_cache_retention:
              payload["prompt_cache_retention"] = prompt_cache_retention
```

```python
# runtime.py:921-934 — ask() 中调模型前的 cache 参数准备
prompt_cache_key = None
prompt_cache_retention = None
if getattr(self.model_client, "supports_prompt_cache", False):
    # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去
    prompt_cache_key = prompt_metadata.get("prompt_cache_key")
    prompt_cache_retention = "in_memory"

# 调模型，cache 参数作为可选参数传入
raw = self.model_client.complete(
    prompt,
    self.max_new_tokens,
    prompt_cache_key=prompt_cache_key,
    prompt_cache_retention=prompt_cache_retention,
)
```

`prompt_cache_retention = "in_memory"` 告诉后端：这个缓存只需要在内存中保持，不需要持久化到磁盘。这是性能和成本之间的平衡——内存缓存快但生命周期短。

### 4.5 各后端的 Cache 支持

| 后端 | `supports_prompt_cache` | 说明 |
|------|------------------------|------|
| Ollama | `False` | Ollama 当前不支持 prompt cache 语义，runtime 传下来的缓存参数会被忽略 |
| OpenAI 兼容 | 仅 `openai.com` 和 `right.codes` | 通过 base URL 判断是否为已知支持缓存的后端 |
| Anthropic 兼容 | `False` | 当前 Anthropic-compatible 路径没有接缓存复用 |

```python
# models.py:233 — OpenAI 兼容客户端的 cache 支持判断
self.supports_prompt_cache = any(
    host in self.base_url
    for host in ("openai.com", "right.codes")
)
```

这个判断是保守的：只有在明确知道后端支持 `prompt_cache_key` 参数时才启用，避免对不支持的后端传一个"看起来有意义、其实被忽略"的伪参数。

不支持缓存的后端（如 Ollama）仍然能正常工作——只是每轮都要重新处理完整的 prompt，没有前缀复用的加速。

### 4.6 Cache 命中率的观测

每次模型调用返回后，后端会在响应中附带 usage 统计。`_extract_usage_cache_details()`（`models.py:207`）从中提取 cache 相关指标：

```python
def _extract_usage_cache_details(data):
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    # 从 input_tokens_details 中取出 cached_tokens
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,  # 有 cached tokens 就算命中
    }
```

这些指标会：
1. 写入 `last_completion_metadata`，供 trace/report 使用
2. 通过 `display_token_usage()`（`runtime.py:1253`）实时显示在终端

终端输出示例：

```
 usage  in=3200  out=150  total=3350  cached=2800  cache_hit=true
```

`cached_tokens=2800` 意味着 2800 个 token 从缓存中复用，不需要重新计算。在 prefix 稳定的连续轮次中，这个数字通常接近 prefix 的 token 数。

### 4.7 完整数据流：一轮 Prompt 的生命周期

以一个典型的工具调用轮次为例，展示 prompt 从构建到缓存命中的完整路径：

```
用户输入: "fix the login bug"
  │
  ▼
refresh_prefix()
  ├─ WorkspaceContext.build() → fingerprint 未变
  └─ prefix 不需要重建，复用上一轮的 prefix_state
  │
  ▼
evaluate_resume_state()
  └─ checkpoint 状态检查（此处略过）
  │
  ▼
ContextManager.build("fix the login bug")
  ├─ prefix          = 上一轮的 prefix（约 3000 字符）
  ├─ startup_memory  = "MEMORY.md 头部（项目约定、测试命令等）"
  ├─ memory          = "Task: fix login bug\nRecent: auth.py"
  ├─ relevant_mem    = "auth.py uses deprecated session API"
  ├─ rolling_summary = "之前 compact 过的滚动摘要"
  ├─ history         = "[user] fix the login bug"
  │                    "[tool:read_file] auth.py content..."
  ├─ current_req     = "Current user request:\nfix the login bug"
  │
  ├─ 总字符数检查 → 超出 12000？
  │   └─ 如果超出，按 relevant_memory → history → memory → rolling_summary → startup_memory → prefix 顺序压缩
  │
  └─ 拼接为最终 prompt 字符串
  │
  ▼
_build_prompt_and_metadata()
  ├─ prompt_cache_key = SHA-256(prefix 文本)  ← 没变，和上轮一样
  ├─ prompt_cache_retention = "in_memory"
  └─ metadata 记录各 section 长度、压缩日志
  │
  ▼
model_client.complete(prompt, max_new_tokens,
                      prompt_cache_key="a1b2c3...",
                      prompt_cache_retention="in_memory")
  │
  ├─ 构建 payload:
  │   {
  │     "model": "gpt-5.4",
  │     "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
  │     "max_output_tokens": 512,
  │     "temperature": 0.2,
  │     "prompt_cache_key": "a1b2c3...",      ← 告诉后端：prefix 没变
  │     "prompt_cache_retention": "in_memory"  ← 内存缓存即可
  │   }
  │
  ▼
后端处理
  ├─ 看到 prompt_cache_key == 上一轮的 key
  ├─ prefix 部分从缓存中取出，不重新计算
  ├─ 只处理 history 和 current_request 的增量部分
  └─ 返回结果 + usage: { cached_tokens: 2800, cache_hit: true }
  │
  ▼
display_token_usage() → 终端显示: cached=2800 cache_hit=true
```

这就是从用户输入到 cache 命中的完整链路。Prefix 的稳定性是缓存复用的前提——工作区没变、工具没变、规则没变，prefix 就不变，cache 就能命中。

### 4.8 模型上下文窗口自动匹配

不同模型的上下文窗口差异巨大（8K 到 10M），硬编码一个固定预算显然不合理。`models.py` 维护了一个 85+ 模型的前缀映射表，按最长前缀优先匹配：

```python
MODEL_CONTEXT_WINDOWS = {
    "gpt-5.5-pro": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5": 400_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4": 200_000,
    "qwen3-235b": 131_072,
    "qwen3": 32_768,
    # ... 覆盖 OpenAI/Claude/Gemini/Grok/DeepSeek/Mistral/Qwen/Kimi/MiMo/GLM/Llama
}
DEFAULT_CONTEXT_WINDOW = 32_768  # 未知模型保守 fallback
```

**最长前缀优先**是关键设计——`sorted(key=len, reverse=True)` 确保 `gpt-5.5` 不被 `gpt-5`（400K）错误匹配，而是命中 `gpt-5.5`（1.05M）。同理 `glm-4.6` 不会被 `glm`（32K）吃掉。

`build_agent()` 在启动时自动计算预算：

```python
context_window = get_context_window(model_name)
total_budget = int(context_window * 0.75)  # 75% 给 prompt，25% 留给输出+系统
```

这个表是纯静态字典，零依赖，新模型出来加一行就行。未知模型 fallback 到 32K（宁可早点压缩，不要等请求超限）。

---

## 第五章 结构化的会话记忆

### 5.1 这一层要解决什么问题

Agent 的控制循环是多轮的：用户说一句话，模型可能调 4-6 次工具才给出答案。每轮都要重新组装 prompt，而 prompt 中的 history 会随着轮次增长、被压缩、最终可能丢失细节。

记忆层要回答的核心问题是：**在多轮代码任务里，哪些事实值得跨轮留下来，留下来以后怎么保证它们还能继续可信。**

如果记忆层只是"把所有历史塞进一个大摘要"，那它和 history 没有本质区别。Enigma 的做法是：**只沉淀少量结构化事实，并把每条事实和文件的 SHA-256 freshness 绑定**——文件内容变了，旧摘要自动失效。

### 5.2 记忆的三层结构

**为什么分三层而不是一个大摘要？** 一个大摘要的问题是：所有信息混在一起，无法区分"当前任务需要什么"（工作记忆）、"之前发生过什么"（情景记忆）、"这个项目的长期事实是什么"（语义记忆）。分层后，每层有独立的生命周期和容量控制，prompt 组装时可以按需取用。

`LayeredMemory`（`memory.py:736`）管理三层记忆：

```
┌─────────────────────────────────────────────────────┐
│  工作记忆（Working Memory）                          │
│  生命周期：当前 ask() 轮次内                         │
│  内容：task_summary + recent_files + file_summaries  │
│  用途：下一轮 prompt 的 工作记忆 section             │
├─────────────────────────────────────────────────────┤
│  情景记忆（Episodic Notes）                          │
│  生命周期：当前会话内（最多 24 条）                   │
│  内容：工具执行摘要、失败过程笔记                    │
│  用途：召回记忆，给模型"之前发生过什么"              │
├─────────────────────────────────────────────────────┤
│  语义记忆（Durable Memory）                          │
│  生命周期：跨会话（存储在 .enigma/memory/topics/）    │
│  内容：项目约定、关键决策、依赖事实、用户偏好        │
│  用途：长期知识沉淀，跨会话复用                      │
└─────────────────────────────────────────────────────┘
```

每层都有明确的容量上限（`WORKING_FILE_LIMIT=8`、`EPISODIC_NOTE_LIMIT=24`、`FILE_SUMMARY_LIMIT=6`），防止记忆本身成为上下文膨胀的来源。

### 5.3 工具执行如何驱动记忆更新

记忆不是定时刷新的，而是**由工具执行事件即时驱动**。`update_memory_after_tool()` 在每次工具执行后被调用，根据工具类型做不同处理：

| 工具 | recent_files | file_summaries | episodic_notes |
|------|-------------|----------------|----------------|
| `read_file` | 加入路径 | 生成摘要（绑定 freshness） | 追加摘要笔记 |
| `write_file` / `patch_file` | 加入路径 | **失效旧摘要** | 不追加（避免噪音） |
| `run_shell` | 不动 | 不动 | 仅失败/测试时追加 |

设计意图：**只有读操作才产生值得记住的事实，写操作只会让旧事实过期。**

文件摘要生成：`summarize_read_result()` 优先提取结构性行（函数签名、类定义、import），没有结构性行时回退到前 3 行，上限 180 字符。

失败过程笔记：工具失败或部分成功时追加描述性笔记（`{tool} error on {path}; check the failure before retry`），让模型在下一轮知道"上一步哪里出了问题"。成功时不记，避免噪音。

### 5.4 摘要失效机制：freshness 绑定

这是记忆层可信度的核心保障。每条 `file_summary` 都绑定了一个 `freshness`——文件内容的 SHA-256 哈希：

```python
def set_file_summary(state, path, summary, workspace_root=None):
    state["file_summaries"][path] = {
        "summary": summary,
        "created_at": now(),
        "freshness": file_freshness(path, workspace_root),  # SHA-256(文件内容)
    }
```

当文件被 `write_file` 或 `patch_file` 修改后，`invalidate_file_summary()` 直接删除该文件的摘要条目：

```python
def invalidate_file_summary(state, path, workspace_root=None):
    path = canonicalize_path(path, workspace_root).strip()
    state["file_summaries"].pop(path, None)  # 直接删除，不保留旧值
```

此外，在每次 `ask()` 开始时，`invalidate_stale_file_summaries()` 会扫描所有已存摘要，对比当前 freshness。不一致的摘要直接删除——解决**文件被外部修改**（用户手动编辑、git pull）导致记忆过期的问题。失效后的摘要不会自动重建，等下次 `read_file` 时重新生成。这是"宁可没有摘要，也不要错误摘要"的保守策略。

### 5.5 记忆如何减少重复读取

记忆层的核心价值：**模型不需要反复读同一个文件来回忆"那个文件里有什么"。** 第 1 轮读了 `auth.py` 生成摘要后，第 4 轮需要参考时 memory section 已经有摘要，省下 1 个工具步数。`render_memory_text()` 渲染时只展示 freshness 仍然一致的摘要，过期的不显示。

### 5.6 语义记忆的提升机制

情景记忆是会话内的，会话结束就丢失。但有些信息值得跨会话保留——比如项目约定、关键决策、依赖事实。这些通过两条独立路径提升到语义记忆。

#### 5.6.1 路径一：关键词匹配（用户显式触发）

当用户输入**包含记忆意图关键词**（"记住"、"保存"、"record"等），且模型回答中包含**结构化格式的结论**（`Project convention: ...`、`Decision: ...`、`Dependency: ...`、`Preference: ...`，中英文均支持）时，`promote_durable_memory()`（`runtime.py:905`）直接提取并写入。

这是一条**快速通道**——不调额外模型，纯字符串前缀匹配，零成本。

#### 5.6.2 路径二：自动反思子 agent（系统触发）

关键词匹配的问题是：用户不说"记住"，教训就永远不会沉淀。工具失败、返工、反复踩坑的模式，如果用户没意识到要"记住"，就只能等着被 compact 磨碎。

自动反思通过 `reflect_and_update_semantic_memory()`（`runtime.py:1365`）解决这个问题。它用一个独立的子 agent 调用，审查全部上下文（会话历史 + 情景笔记 + 工作记忆 + 现有语义记忆），返回结构化 JSON，由主 agent 写入 topic markdown。

**触发条件**（四路，任一满足即触发）：

| 触发点 | 条件 |
|--------|------|
| compact 前 | 无条件（用输入签名去重，避免同一次 ask 重复触发） |
| 工具循环每 10 次 | 这 10 次内有新 `kind=process` 笔记或用户消息含意图关键词 |
| ask 正常结束 | 本轮有未被反射过的 process 笔记或意图关键词 |
| ask 非正常结束 | 同上（步数上限 / 重试上限） |

**反思 prompt 的设计原则**（`REFLECTION_SYSTEM_PROMPT` 常量）：
- **角色**：记忆审查员，不是总结机器
- **宁缺毋滥**：没有值得提取的内容就返回全空 JSON，空列表是正常输出
- **不瞎总结**：只提取**独立于这次会话也成立的事实**，不把"agent 做了 X"当语义记忆
- **替换语义**：输出的是每个 topic 的**完整期望状态**，替换而非追加——天然控制增长
- **每 topic 上限 25 条**：prompt 明确告知，代码层面 `replace_topics()` 做 `[:TOPIC_NOTE_LIMIT]` 兜底

**输出格式**：

```json
{
  "project-conventions": ["条目1", "条目2"],
  "key-decisions": ["条目1"],
  "dependency-facts": [],
  "user-preferences": []
}
```

空列表 = 该 topic 本次无变更。key 必须是这 4 个之一，不接受新 topic。

**安全措施**：
- 所有注入 prompt 的数据经过 `redact_artifact()` 脱敏
- 每条输出经过 `reject_durable_reason()` 过滤（密钥、traceback、过长文本）
- JSON 解析失败或模型异常 → 静默跳过，不影响主流程
- 输入签名（history 长度 + episodic notes 数量）去重，防止同一次 ask 内 compact + ask-end 触发两次

#### 5.6.3 质量过滤与去重

`reject_durable_reason()` 过滤掉空文本、含密钥的文本、像 checkpoint 状态的瞬态内容。

`DurableMemoryStore.promote()` 写入时做主题去重：用正则提取句子主语（如 "ruff"、"pytest"），如果新旧笔记主语相同，旧的被替换。避免"ruff should be used" 和 "ruff is replaced by ruff-lsp" 同时存在。

#### 5.6.4 语义记忆增长控制

每个 topic 最多 25 条（`TOPIC_NOTE_LIMIT = 25`），4 个 topic × 25 = 100 条上限，控制在启动记忆的 200 行读取窗口内。

增长控制分两道防线：
1. **prompt 层面**：反思子 agent 被明确告知"每 topic 最多 25 条"，它输出的就是最终状态
2. **代码层面**：`promote()` append 前检查 `len(existing) >= TOPIC_NOTE_LIMIT`，超限拒绝；`replace_topics()` 写入前 `[:TOPIC_NOTE_LIMIT]` 截断

`_subject_key` 匹配的替换不受限制（替换不增加总数）。

语义记忆存储在 `.enigma/memory/topics/*.md` 文件中，每个主题一个文件，跨会话持久化。

### 5.7 记忆检索：分路排序，BM25 而非 Embedding

**为什么不用 embedding？** Enigma 的核心约束是零运行时依赖。引入 embedding 模型意味着额外的网络调用或本地模型加载，违背了"纯 Python、零依赖"的设计原则。BM25 是纯算法实现，不需要任何模型调用，在短文本（笔记摘要通常 < 200 字符）上的相关性表现足够好。

两路独立召回再合并：

- **第一路**：情景记忆排序
- **第二路**：语义记忆排序
- **合并**：每路保底 2 条（`reserve_each=2`），防止一路被挤掉

排序优先级：Tag 精确匹配 > BM25 得分 > 时间新旧 > 插入顺序。默认 `limit=4`，即 2 条情景 + 2 条语义保底。

### 5.8 记忆在 Prompt 中的位置

记忆在 prompt 中占据两个 section：

```
prefix
  │
工作记忆 section ← render_memory_text() 输出
  │  工作记忆:
  │  - task: fix the login bug
  │  - recent_files: auth.py, session.py
  │  - file_summaries:
  │    - auth.py: def login(request) | class AuthMiddleware
  │  - 情景记忆: 3
  │  - 语义记忆主题: project-conventions, key-decisions
  │
召回记忆 section ← retrieval_candidates() 输出（分路排序，最多 4 条）
  │  召回记忆:
  │  - auth.py uses deprecated session API (from 情景记忆)
  │  - Use ruff for linting (from 语义记忆/project-conventions)
  │
history
  │
current_request
```

工作记忆 section 是完整的"仪表盘"——任务是什么、最近碰过哪些文件、文件里有什么。召回记忆 section 是按需召回的"相关片段"——情景记忆和语义记忆分路排序，默认 2+2 保底，最多 4 条。

这种"全景 + 焦点"的设计让模型既能了解整体上下文，又不会被不相关的笔记分散注意力。语义记忆不会被大量情景记忆挤掉。

---

---

## 第六章 上下文瘦身与输出管理设计

### 6.1 这一层要解决什么问题

Agent 的控制循环是累积性的：每读一个文件，history 就多一条；每跑一个命令，结果就追加进来。如果不加管理，几轮下来 prompt 就会撑到几万字符，模型要么被截断，要么被无关信息淹没。

这一章要回答的核心问题是：**当 agent 在一轮任务里不断读文件、跑命令、积累历史时，runtime 怎么把最有用的上下文继续送给模型，又不让 prompt 被撑爆。**

Enigma 的策略不是"到最后一刀切"，而是**在每一层都提前收口**——工具结果刚出来就裁剪、历史积累时就压缩、最终 prompt 超预算时按优先级逐段让位。

### 6.2 第一道收口：工具结果裁剪

工具执行结果是上下文膨胀的最直接来源。一个 `run_shell` 的 stdout 可能有几千行，一个 `read_file` 可能读了整个大文件。这些结果在进入 history 之前就被裁剪。

```python
# run_tool() 中，工具执行后的第一件事就是裁剪
result = clip(tool["run"](args))
```

`clip()`（`workspace.py:26`）是统一的裁剪函数：

```python
MAX_TOOL_OUTPUT = 4000  # 工具结果的硬上限

def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    # 超出部分用截断标记替代，让模型知道内容被裁剪了
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
```

4000 字符的上限是经验值——足够模型理解工具结果的上下文，又不会因为一个大文件的完整输出把整个 prompt 撑爆。

截断标记 `...[truncated N chars]` 不是简单的省略号，它告诉模型"这里还有 N 个字符没显示"，让模型知道这不是完整结果，需要时可以请求更小的范围重新读取。

### 6.3 第二道收口：记忆层的容量控制

记忆层本身也有严格的容量上限，防止记忆成为新的膨胀来源：

| 数据结构 | 容量上限 | 超出时的行为 |
|---------|---------|------------|
| `recent_files` | 8 个路径 | 只保留最近 8 个，旧的被丢弃 |
| `episodic_notes` | 24 条 | 只保留最近 24 条，最旧的被淘汰 |
| `file_summaries` | 6 个文件 | 只展示 recent_files 中 freshness 匹配的前 6 个 |
| 单条 note | 500 字符 | `clip(note, 500)` |
| 单条 file summary | 500 字符 | `clip(summary, 500)` |
| task_summary | 300 字符 | `clip(summary, 300)` |

```python
# memory.py — 每层都有明确的容量守卫
WORKING_FILE_LIMIT = 8
EPISODIC_NOTE_LIMIT = 24
FILE_SUMMARY_LIMIT = 6

# 加入新 note 时，超出上限的旧 note 被丢弃
state["episodic_notes"] = notes[-EPISODIC_NOTE_LIMIT:]
# 加入新文件时，超出上限的旧路径被丢弃
state["working"]["recent_files"] = files[-WORKING_FILE_LIMIT:]
```

这种"固定窗口"策略比"按权重淘汰"更简单可预测——模型看到的记忆 section 大小始终在可控范围内。

### 6.4 第三道收口：History 压缩

History 是 prompt 中增长最快的 section。`ContextManager` 对 history 采用分层压缩策略：**最近的保留细节，更早的逐步压缩。**

#### 6.4.1 最近 6 条：高保真保留

```python
recent_window = 6
recent_start = max(0, len(history) - recent_window)
# 最近 6 条历史每条最多 900 字符
for index, item in enumerate(history):
    recent = index >= recent_start
    if recent:
        line_limit = 900
        entries.append({"recent": True, "lines": self._render_history_item(item, line_limit)})
```

最近 6 条保留较高细节（每条 900 字符），因为下一步决策通常最依赖刚刚发生的工具结果。

#### 6.4.2 更早的记录：分类压缩

对于 `recent_window` 之前的旧记录，根据类型做不同处理：

```python
# read_file：同一文件只保留第一次读取，有摘要时用一行替代
if item["role"] == "tool" and item["name"] == "read_file":
    path = item["args"].get("path")
    if path in seen_older_reads:
        continue  # 重复读取，直接跳过
    seen_older_reads.add(path)
    summary = self._reusable_file_summary(path)
    if summary:
        entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
        continue  # 用一行摘要替代完整内容

# run_shell：压缩为 "command → stdout 前 3 行"
if item["role"] == "tool":
    summary_line = self._summarize_old_tool_item(item)
    entries.append({"recent": False, "lines": [summary_line]})
    continue

# 其他记录：压缩到 60 字符
entries.append({"recent": False, "lines": self._render_history_item(item, 60)})
```

压缩策略对比：

| 原始记录类型 | 压缩方式 | 压缩后大小 |
|------------|---------|----------|
| 最近 6 条（任何类型） | 保留细节 | 每条 900 字符 |
| 旧的 `read_file`（有摘要） | 一行摘要替代 | ~100 字符 |
| 旧的 `read_file`（重复读取） | 直接跳过 | 0 |
| 旧的 `run_shell` | `command → stdout 前 3 行` | ~200 字符 |
| 旧的 user/assistant | 截断到 60 字符 | 60 字符 |

#### 6.4.3 整体 History 裁剪

即使经过上述压缩，history 文本仍然可能很长。`history_text()` 有一个最终的硬上限：

```python
MAX_HISTORY = 12000  # history 文本的总上限

def history_text(self):
    ...
    return clip("\n".join(lines), MAX_HISTORY)
```

### 6.5 第四道收口：Prompt 预算压缩

当所有 section 组装完成后，如果 prompt token 数超过 `total_budget`（按模型上下文窗口 × 0.75 自动计算），`ContextManager.build()` 启动迭代压缩：

```python
# 压缩顺序：先牺牲召回记忆，最后才动 prefix
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
```

```python
while estimate_tokens(prompt) > self.total_budget:
    overflow = estimate_tokens(prompt) - self.total_budget
    reduced = False
    for section in self.reduction_order:
        floor = int(self.section_floors.get(section, 0))
        current_budget = int(budgets.get(section, 0))
        if current_budget <= floor:
            continue  # 已经到下限了，跳过这个 section
        new_budget = max(floor, current_budget - overflow)
        if new_budget >= current_budget:
            continue
        budgets[section] = new_budget
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduced = True
        break  # 每轮只压缩一个 section
    if not reduced:
        break  # 所有 section 都到下限了，停止
```

预算按比例分配（`SECTION_RATIO`），下限为各 section 预算的 25%。例如 200K 模型的 150K token 预算中，history 分得 ~70K token，prefix 分得 ~45K token。

Token 估算使用 `estimate_tokens(text) = len(text) / 3.5`（中英混合经验值），零依赖，误差 ±20%。

#### 6.5.1 为什么这个顺序？

```
relevant_memory → history → memory → prefix
（先牺牲召回记忆，最后才动 prefix）
```

- **relevant_memory 最先牺牲**：它是"锦上添花"的辅助信息，没有它模型仍然能从 history 和 memory 中获取上下文
- **history 第二**：history 是完整的事件流，但最近 6 条已经保留了高细节，压缩更早的记录影响有限
- **memory 第三**：工作记忆的 task_summary 和 recent_files 是模型理解当前任务的关键
- **prefix 最后**：prefix 包含身份声明、行为规则和工具列表，是模型正确行动的基础——没有 prefix，模型不知道自己是谁、能调什么工具

#### 6.5.2 `current_request` 为什么永不裁剪？

用户当前的请求是本轮最重要的信号。如果把用户的请求裁剪了，模型就不知道这一轮到底要做什么——其他所有 section 都是为了服务这个请求。

#### 6.5.3 压缩是迭代的

压缩不是一次性把某个 section 砍到下限，而是**每轮只削一个 section 的一部分**，然后重新检查是否还超预算。这样可以更精细地分配预算——如果削一点 relevant_memory 就够了，就不会动 history。

#### 6.5.4 自动升级：预算裁剪压到底还超限时触发 `/compact`

预算裁剪有下限保护——每个 section 最多压缩到 25%。如果所有 section 都到了下限，prompt 仍然超预算，系统会自动调用 `compact_context()` 做深度压缩（模型驱动的智能压缩），然后重新组装 prompt。防重入标志 `_auto_compacting` 防止循环触发。

此外，`runtime.py` 主循环中还有一层安全网：每次模型返回后，用 `should_compact(model_name, used_tokens)` 检查 token 用量是否接近模型上下文窗口（80% 阈值，预留 16K output）。如果是，提前触发 compact，避免下一次 API 调用因超限而失败。

```python
def should_compact(model_name, used_tokens, reserve_output_tokens=16_000, threshold_ratio=0.80):
    window = get_context_window(model_name)
    usable = max(0, window - reserve_output_tokens)
    return used_tokens >= int(usable * threshold_ratio)
```

### 6.6 显式压缩：`/compact` 命令

除了 prompt 组装时的自动预算压缩，用户还可以通过 `/compact [focus]` 命令**主动触发一次全面的上下文压缩**。这在长会话中特别有用——当 history 和记忆积累到一定程度，用户可以主动瘦身。

#### 6.6.1 `/compact` 做了什么

`compact_context()`（`runtime.py:545`）用模型来压缩四层上下文：

```python
def compact_context(self, focus=""):
    # 1. 保存压缩前的状态
    before_history = list(self.session.get("history", []))
    before_memory = json.loads(json.dumps(self.memory.to_dict()))
    # 2. 压缩前：反思子 agent 提取语义记忆（趁情景笔记还没被磨碎）
    self.reflect_and_update_semantic_memory()
    # 3. 构建 compact prompt，把所有上下文交给模型
    prompt = self.build_compact_prompt(focus=focus)
    # 4. 调用模型，让它生成压缩后的 JSON
    raw = self.model_client.complete(prompt, max(COMPACT_MAX_NEW_TOKENS, self.max_new_tokens))
    # 5. 解析并校验模型返回的 JSON
    payload = self.parse_compact_response(raw)
    # 6. 应用压缩结果
    self.apply_compact_payload(payload, focus=focus, ...)
    return self.compact_result_message(before_history_count=len(before_history))
```

#### 6.6.2 四层上下文的压缩方式

| 层 | 压缩方式 | 保留什么 |
|----|---------|---------|
| 工作记忆 | 压缩 file_summaries 的文本 | 路径、recent_files、freshness 不变 |
| 情景记忆 | 压缩成更少的 notes | 每条 note 的 tags/source/kind 保留 |
| 语义记忆 | **compact 前由反思子 agent 提取**，compact 本身不改写 | `MEMORY.md` 及 topics/ 下主题文件下一轮重新注入 |
| history | head + compact_summary + tail | 保留前 2 条和后 6 条，中间插入摘要 |

**为什么 compact 不写回语义记忆？** 早期版本让模型重写 `semantic_memory` 字段并落盘，但长会话里会出现"模型想不起早期的 convention 就把它删掉"的问题。新设计把语义记忆的更新拆到独立的反思系统（§5.6.2）——compact 前由子 agent 提取，compact 本身只输出"滚动摘要 + 情景记忆"。下一轮 `ContextManager` 会再从 `MEMORY.md` 和主题文件重新注入稳定记忆。

压缩产生的 **rolling_summary 会写入 SessionDB**（第 10.4 节），下一轮 prompt 的"会话滚动摘要"section 从 DB 读，断电或切会话都不丢。

#### 6.6.3 History 的 head + summary + tail 结构

```
compact 前的 history:
  [user] fix the bug
  [tool:read_file] auth.py content...
  [tool:run_shell] pytest output...
  [tool:patch_file] patched auth.py
  [tool:read_file] session.py content...
  [tool:run_shell] pytest output... (passed)
  [assistant] The bug is fixed.
  [user] now add tests
  [tool:read_file] test_auth.py ...
  [tool:write_file] wrote test_auth.py
  [assistant] Tests added.

compact 后的 history:
  [user] fix the bug                                          ← head (前 2 条)
  [tool:read_file] auth.py content...
  [system] compact summary:                                   ← 模型生成的摘要
    Fixed login bug in auth.py by correcting session
    validation. Added tests in test_auth.py. All tests pass.
  [user] now add tests                                        ← tail (后 6 条)
  [tool:read_file] test_auth.py ...
  [tool:write_file] wrote test_auth.py
  [assistant] Tests added.
```

```python
COMPACT_HEAD_HISTORY_LIMIT = 2   # 保留前 2 条
COMPACT_TAIL_HISTORY_LIMIT = 6   # 保留后 6 条
COMPACT_MAX_NEW_TOKENS = 1200    # compact 模型的最大输出 token
```

#### 6.6.4 滚动摘要与迭代更新

`/compact` 不是从零重写摘要，而是**更新已有的 compact_summary**：

```python
payload = {
    "previous_compact_summary": self.session.get("compact_summary", {}),
    "working_memory": ...,
    "episodic_notes": ...,
    "semantic_memory": ...,
    "history_head": head,
    "history_middle": middle,
    "history_tail": tail,
}
```

`build_compact_prompt()` 会把上一次的 `compact_summary` 传给模型，让它在已有摘要的基础上增量更新，而不是每次从头总结。这样摘要能保留更早期的上下文，不会因为多轮 compact 而丢失历史信息。

#### 6.6.5 校验与安全

compact 返回的 JSON 必须通过严格校验，否则整个操作失败且不写回：

```python
def validate_compact_payload(self, payload):
    # compact_summary 必须存在且非空
    if not str(payload.get("compact_summary", "")).strip():
        raise RuntimeError("compact failed: compact_summary is required")
    # working_memory 必须是对象
    if not isinstance(payload.get("working_memory"), dict):
        raise RuntimeError("compact failed: working_memory must be an object")
    # episodic_notes 必须是列表
    if not isinstance(payload.get("episodic_notes"), list):
        raise RuntimeError("compact failed: episodic_notes must be a list")
    # semantic_memory 不再出现在 compact 输出中，由反思系统独立管理
```

此外，每条 note 都经过 `reject_durable_reason()` 过滤，确保不会把密钥、checkpoint 状态等敏感/瞬态内容写入压缩后的记忆。

#### 6.6.6 `/compact focus...`

用户可以传入 focus 参数，指定压缩的重点方向：

```
enigma> /compact focus on the auth module changes
```

focus 会被传入 compact prompt，让模型在压缩时特别关注指定领域的信息保留。

### 6.7 为什么比粗暴截断更稳

整套瘦身策略的核心设计原则是：**按信息价值分层处理，而不是到最后一刀切。**

粗暴截断（比如"prompt 超过 12000 字符就从头砍"）的问题：
- 不区分信息类型——一条关键的行为规则和一条 3 轮前的 shell 输出被同等对待
- 不区分新旧——最近一轮的工具结果和 5 轮前的结果被同等对待
- 不保留结构——砍完之后 prompt 可能变成半句话，模型无法理解

Enigma 的分层策略避免了这些问题：

```
工具结果 → 4000 字符硬截断（第一道）
  ↓
记忆层 → 固定窗口淘汰（第二道）
  ↓
History → 分类压缩：最近高保真、旧的按类型压缩（第三道）
  ↓
Prompt → 按优先级迭代让位：召回记忆 → history → 工作记忆 → prefix（第四道）
  ↓
/compact → 模型驱动的智能压缩（用户主动触发）
```

每一层都在自己的边界内做最优决策，而不是把所有压力留给最后一层。结果是：模型看到的 prompt 始终是结构完整、信息密度最高的版本。

---

## 第七章 会话状态、运行工件与恢复机制设计

### 7.1 这一章要解决什么问题

Agent 不是只跑一次的脚本——用户可能中途关掉终端，第二天想从昨天的进度继续；也可能跑完一轮之后想复盘：到底调了哪些工具、模型在哪一步卡住了、最终答案是什么。

这一章回答三个问题：

- **运行过程中，哪些状态在不断变化？**（TaskState 状态机）
- **运行结束后，留下了什么可供复盘的工件？**（RunStore 三件套）
- **下次打开终端，agent 怎么知道上次跑到哪了？**（SessionStore + Checkpoint）

### 7.2 两层持久化的分工

Enigma 把持久化拆成两层，各有各的职责：

```
┌─────────────────────────────────────────────────────────┐
│  SessionStore（.enigma/sessions/{id}.json）              │
│  "可恢复的会话状态"                                       │
│  · history（完整对话流水账）                               │
│  · memory（三层记忆的当前快照）                            │
│  · checkpoints（检查点链表 + 恢复元数据）                   │
│  · runtime_identity（运行时指纹）                         │
│  · compact_summary（/compact 产生的滚动摘要）              │
│  生命周期：跨 ask() 存活，用户关终端后仍可恢复              │
├─────────────────────────────────────────────────────────┤
│  RunStore（.enigma/runs/{run_id}/）                      │
│  "单次运行的审计工件"                                     │
│  · task_state.json（状态机快照，每轮循环更新）              │
│  · trace.jsonl（逐事件时间线）                            │
│  · report.json（运行结束时的最终摘要）                     │
│  生命周期：一次 ask() 一组，写完不再修改                   │
└─────────────────────────────────────────────────────────┘
```

为什么分开？因为"恢复现场"和"复盘证据"是两种完全不同的读取模式。恢复需要最新的完整状态；复盘需要某一次运行的不可变快照。混在一起会让两种需求互相干扰。

### 7.3 TaskState：一次 ask() 的状态机

每次调用 `ask()` 都会创建一个 `TaskState` 对象，它是这次运行的"账本"：

```python
@dataclass
class TaskState:
    run_id: str        # "run_20260505-143022-a3f1c2" — 每次 ask() 唯一
    task_id: str       # "task_20260505-143022-b7e4d1" — 任务维度唯一
    user_request: str  # 用户原始输入
    status: str        # running → completed / stopped / failed
    tool_steps: int    # 已执行工具次数（不含重试）
    attempts: int      # 模型被调用总轮次（含重试）
    last_tool: str     # 最后一个执行的工具名
    stop_reason: str   # 停机原因（见下文）
    final_answer: str  # 最终回答文本
    checkpoint_id: str # 关联的检查点 ID
    resume_status: str # 恢复状态（full-valid / partial-stale / ...）
```

状态流转路径：

```
创建 (running)
  ├─ 模型给出 final → completed（stop_reason = final_answer_returned）
  ├─ 工具步数达到 max_steps → stopped（stop_reason = step_limit_reached）
  ├─ 模型连续给不出有效格式 → stopped（stop_reason = retry_limit_reached）
  ├─ 模型调用出错 → failed（stop_reason = model_error）
  ├─ 用户 Ctrl+C 中断 → stopped（stop_reason = user_cancel）
  └─ Plan 模式产出计划 → completed（stop_reason = plan_ready）
```

`stop_reason` 和 `status` 是分开存的——`status` 回答"停下时是什么状态"，`stop_reason` 回答"怎么停的"。比如 `step_limit_reached` 的 status 是 `stopped`，而 `model_error` 的 status 是 `failed`。

TaskState 在 `ask()` 循环中不断更新，每轮都会通过 `RunStore.write_task_state()` 落盘。这样即使进程异常退出，也能从磁盘上看到最后一步的状态。

### 7.4 RunStore：单次运行的三件套

每次 `ask()` 启动时，`RunStore.start_run()` 会创建一个独立目录：

```
.enigma/runs/run_20260505-143022-a3f1c2/
  ├── task_state.json   # 状态机快照（每轮更新）
  ├── trace.jsonl       # 事件流（逐条追加）
  └── report.json       # 运行结束时写入
```

**task_state.json** 是 TaskState 的 JSON 序列化，每轮循环都会重新写入。它的作用是"运行中观察"——外部工具可以随时读取它来了解当前进度。

**trace.jsonl** 是逐行追加的事件流，记录 agent 从启动到结束的每一步：

```
{"event": "run_started", "task_id": "...", "user_request": "fix the bug", ...}
{"event": "prompt_built", "prompt_chars": 3200, "prefix_chars": 1800, ...}
{"event": "model_called", "max_new_tokens": 512, ...}
{"event": "tool_executed", "tool": "read_file", "tool_status": "ok", ...}
{"event": "checkpoint_created", "checkpoint_id": "ckpt_a3f1c2b7", "trigger": "tool_executed", ...}
{"event": "run_finished", "status": "completed", "stop_reason": "final_answer_returned", "run_duration_ms": 8432, ...}
```

trace 用 jsonl 而不是 JSON 数组，原因是 agent 运行过程是流式事件序列，逐条落盘比"最后一次性写整份 trace"更稳——中途崩溃也不会丢掉已有的事件。

**report.json** 在运行结束时一次性写入，是这次运行的最终摘要。它和 trace 的区别在于：trace 关注过程（每一步发生了什么），report 关注结果与关键指标。

```python
def build_report(self, task_state):
    return {
        "run_id": task_state.run_id,
        "status": task_state.status,
        "stop_reason": task_state.stop_reason,
        "final_answer": task_state.final_answer,
        "tool_steps": task_state.tool_steps,
        "attempts": task_state.attempts,
        "checkpoint_id": task_state.checkpoint_id,
        "resume_status": task_state.resume_status,
        "prompt_metadata": self.last_prompt_metadata,
        "durable_promotions": list(self.last_durable_promotions),
        "durable_rejections": list(self.last_durable_rejections),
        "redacted_env": self.detected_secret_env_summary(),
    }
```

注意 `redact_artifact()` 会递归遍历整个 report，把匹配 `SECRET_SHAPED_TEXT_PATTERN`（如 `sk-xxxx`、含 `api_key`/`token` 的文本）的内容替换为 `[REDACTED]`。这样即使 report 被分享或提交到版本控制，也不会泄露密钥。

### 7.5 SessionStore：跨 ask() 的会话持久化

SessionStore 比 RunStore 简单得多——它只负责把整个 session 字典序列化为 JSON 文件：

```python
class SessionStore:
    def __init__(self, root):
        self.root = Path(root)

    def save(self, session):
        path = self.root / f"{session['id']}.json"
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        # 按修改时间排序，返回最新的 session 文件名
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime)
        return files[-1].stem if files else None
```

session 字典里装的是"可恢复的全部上下文"：

```python
session = {
    "id": "20260505-143022-a3f1c2",
    "created_at": "2026-05-05T14:30:22Z",
    "workspace_root": "/path/to/repo",
    "history": [...],           # 完整对话历史
    "memory": {...},            # 三层记忆快照
    "checkpoints": {...},       # 检查点链表
    "runtime_identity": {...},  # 运行时指纹
    "compact_summary": {...},   # /compact 滚动摘要
}
```

恢复会话时，`Enigma.from_session()` 加载 session 文件，把它传给 `Enigma.__init__()`。构造函数会用 `_ensure_session_shape()` 补全缺失字段（向后兼容旧格式 session），然后从 session 中恢复 memory、checkpoints 等状态。

### 7.6 Checkpoint：恢复的锚点

Checkpoint 是 session 中最核心的恢复机制。每次工具执行完成、运行结束、或检测到上下文变化时，`create_checkpoint()` 都会生成一个检查点。

一个 checkpoint 包含：

```python
checkpoint = {
    "checkpoint_id": "ckpt_a3f1c2b7",
    "parent_checkpoint_id": "ckpt_prev1234",  # 链接到上一个检查点
    "schema_version": 2,
    "created_at": "2026-05-05T14:30:45Z",
    "current_goal": "修复 auth 模块的 token 过期 bug",
    "current_plan": ["Use the result of read_file.", "Patch the token validation logic."],
    "open_questions": ["Which stale file facts still need to be re-read?"],
    "confirmed_findings": ["Last tool executed: read_file."],
    "completed": ["已读取 auth.py 的当前实现"],
    "excluded": [],
    "current_blocker": "",
    "blocked_on": "",
    "next_action": "Decide the next action after read_file.",
    "next_step": "Decide the next action after read_file.",
    "key_files": [
        {"path": "auth.py", "freshness": "sha256:a3f1c2..."},
        {"path": "config.yaml", "freshness": "sha256:b7e4d1..."}
    ],
    "freshness": {"auth.py": "sha256:a3f1c2...", "config.yaml": "sha256:b7e4d1..."},
    "runtime_identity": {...},  # 当前运行时指纹快照
}
```

Checkpoint 是链表结构——每个 checkpoint 都通过 `parent_checkpoint_id` 指向上一个。这样 `checkpoints["items"]` 里存的是完整的检查点历史，而 `checkpoints["current_id"]` 指向最新那个。

checkpoint 有五个触发时机：

```
tool_executed      — 工具执行成功后
run_finished       — 模型给出最终回答后
run_stopped        — 达到步数/重试上限后
freshness_mismatch — 恢复时发现 key_files 内容变了
workspace_mismatch — 恢复时发现运行时环境变了
```

### 7.7 恢复状态评估：你还能信上次的 checkpoint 吗？

恢复会话时最大的风险是：checkpoint 里记录的事实已经过期了。比如上次 checkpoint 说 `auth.py` 的 hash 是 `a3f1c2`，但用户在两次会话之间修改了这个文件——如果 agent 还按旧摘要理解 `auth.py`，就会做出错误决策。

`evaluate_resume_state()` 在每次构建 prompt 前都会运行，做两层检查：

**第一层：文件新鲜度检查**

遍历 checkpoint 中 `key_files` 里记录的每个文件，重新计算 SHA-256，和 checkpoint 中保存的 `freshness` 对比。如果任何一个文件的 hash 变了，就加入 `stale_paths` 列表。

同时，`invalidate_stale_file_summaries()` 会在 memory 层做同样的检查——任何 hash 过期的文件摘要都会被清除（详见第五章 5.4 节）。

**第二层：运行时身份检查**

对比 checkpoint 中保存的 `runtime_identity` 和当前环境，逐字段比较：

```python
identity_keys = (
    "cwd", "model", "model_client", "approval_policy",
    "read_only", "plan_mode", "max_steps", "max_new_tokens",
    "feature_flags", "shell_env_allowlist",
    "workspace_fingerprint", "tool_signature",
)
```

如果用户换了模型、改了审批策略、或者工作区指纹变了（比如切换了分支），这些都会被记录为 `runtime_identity_mismatch_fields`。

两层检查的结果合并为一个状态码：

```
full-valid          — 一切没变，可以直接继续
partial-stale       — 有文件过期了，需要先 re-read
workspace-mismatch  — 运行时环境变了，需要刷新上下文
schema-mismatch     — checkpoint 格式太旧，无法使用
no-checkpoint       — 没有 checkpoint（新会话）
```

这个状态码会被注入到 prompt 中（通过 `render_checkpoint_text()`），让模型知道当前恢复的可信度：

```
Task checkpoint:
- Resume status: partial-stale
- Current goal: 修复 auth 模块的 token 过期 bug
- Current blocker: -
- Next step: Decide the next action after read_file.
- Stale paths: auth.py
```

模型看到 `partial-stale` 和 `Stale paths: auth.py`，就知道下一步应该先重新读取 `auth.py`，而不是依赖过期的摘要。

### 7.8 trace 与 report：运行后复盘

trace 和 report 共同构成了"一次运行的完整证据链"。

trace 回答"每一步发生了什么"——它是按时间顺序的事件流，可以用来重建 agent 的决策过程。比如：

```
run_started → prompt_built → model_called → tool_executed →
prompt_built → model_called → tool_executed → prompt_built →
model_called → run_finished
```

report 回答"这次运行的最终状态是什么"——它是扁平的键值对，适合程序化消费（比如评测框架读取 `status` 和 `final_answer` 来判断任务是否完成）。

所有落盘内容都经过 `redact_artifact()` 递归脱敏：

```python
def redact_artifact(self, value, key=None):
    if key and self.is_secret_env_name(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: self.redact_artifact(v, key=k) for k, v in value.items()}
    if isinstance(value, str):
        return self.redact_text(value)  # 匹配 SECRET_SHAPES_TEXT_PATTERN
    # ... list/tuple 递归处理
```

这样即使用户把 `.enigma/runs/` 目录提交到版本控制，也不会意外泄露 `sk-xxxx` 类型的密钥或环境变量中的 token。

### 7.9 为什么需要 checkpoint 而不只是保存 history

一个自然的问题是：既然 history 已经保存了完整对话，为什么还需要 checkpoint？

原因是 history 记录的是"说了什么"，而不是"进行到哪了"。恢复时如果只看 history，agent 知道之前读过 `auth.py`，但不知道：

- 当前目标是什么（可能用户中途换过方向）
- 哪些步骤已经完成、哪些还没做
- 哪些文件在上次运行后被修改了
- 运行时环境是否发生了变化

Checkpoint 把这些"进行中的状态"显式记录下来，让恢复后第一轮 prompt 就能包含准确的上下文，而不是让模型从 history 里自己推断。这在长任务（比如跨多轮的重构）中尤其重要——没有 checkpoint，恢复后的 agent 可能会重复已经完成的工作，或者忽略已经过期的假设。

---

## 第八章 评测框架与实验方法设计

### 8.1 这一章要解决什么问题

改了一行 prompt 模板，怎么知道 agent 变好了还是变差了？加了一层记忆，到底省了多少重复读取？换了模型后端，通过率有没有掉？

没有评测框架，这些问题只能靠"感觉"回答。Enigma 的评测体系分两层：**固定基准测试**（回答"通过率多少"）和**实验套件**（回答"某个机制的单独贡献有多大"）。

### 8.2 固定基准测试：BenchmarkEvaluator

`BenchmarkEvaluator` 是评测体系的主入口。它的工作流程：

```
加载 coding_tasks.json
  → 逐任务复制 fixture 仓库（隔离环境）
    → 用 FakeModelClient 运行 agent
      → 检查产物 + 运行 verifier 脚本
        → 汇总为 JSON artifact
```

每个 benchmark 任务定义了八个字段：

```json
{
  "id": "sample_beta_locked",
  "prompt": "In sample.txt, replace beta with beta-locked.",
  "fixture_repo": "tests/fixtures/bench_repo_patch",
  "allowed_tools": ["read_file", "patch_file"],
  "step_budget": 4,
  "expected_artifact": "sample.txt contains beta-locked",
  "verifier": "python3 -c \"...assert 'beta-locked' in text...\"",
  "category": "text-edit"
}
```

- `prompt`：给 agent 的任务描述
- `fixture_repo`：一个干净的测试仓库副本，每次运行都会重新复制
- `allowed_tools`：这个任务允许使用的工具白名单
- `step_budget`：工具步数上限
- `verifier`：一个 shell 命令，退出码 0 表示通过
- `category`：任务分类（documentation / text-edit / tool-boundary 等）

任务通过需要同时满足四个条件：产物文件存在、在步数预算内、verifier 退出码为 0、stop_reason 是 `final_answer_returned`。不通过时会被归类为 `missing_artifact` / `budget_exceeded` / `verifier_failed` / `failure_stop_reason`。

最终产物是一份 JSON artifact，包含运行时 git commit、fixture 快照 hash、解码参数等可复现信息，以及每个任务的详细行数据。

### 8.3 FakeModelClient：确定性测试

基准测试用 `FakeModelClient` 代替真实模型——它按预定义的输出序列逐条返回，确保每次运行的行为完全一致：

```python
SCRIPTED_MODEL_OUTPUTS = {
    "sample_beta_locked": [
        # 第一步：调用 patch_file 工具
        '<tool name="patch_file" path="sample.txt">'
        '<old_text>beta</old_text><new_text>beta-locked</new_text></tool>',
        # 第二步：给出最终回答
        "<final>Done.</final>",
    ],
    "invalid_patch_recovery": [
        # 故意给一个缺少参数的调用，触发格式修正
        '<tool>{"name":"patch_file","args":{"path":"README.md","old_text":"..."}}</tool>',
        # 恢复后用正确的替代语法重试
        '<tool name="patch_file" path="README.md">...</tool>',
        "<final>Done.</final>",
    ],
    # ...
}
```

这样做的好处是：benchmark 测的不是"模型够不够聪明"，而是"agent 的控制循环、工具护栏、上下文管理是否按预期工作"。模型换了，同样的脚本输出应该得到同样的通过率。

### 8.4 评测 artifact 的结构

每次运行 `BenchmarkEvaluator.run()` 都会生成一份 artifact JSON：

```python
artifact = {
    "schema_version": 1,
    "captured_at": "2026-05-05T14:30:00+0800",
    "runtime": {
        "commit_sha": "a3f1c2b7...",
        "branch": "main",
    },
    "reproducibility": {
        "fixture_snapshot_id": "sha256:...",  # fixture 文件的哈希
        "model_name": "FakeModelClient",
        "model_version": "scripted-deterministic",
        "decoding": {"temperature": 0.0, "top_p": 1.0, "max_new_tokens": 64},
        "timezone": "Asia/Shanghai",
    },
    "summary": {
        "total_tasks": 10,
        "passed": 9,
        "failed": 1,
        "pass_rate": 0.9,
        "within_budget": 10,
        "verifier_passes": 9,
        "failure_category_counts": {"verifier_failed": 1},
    },
    "rows": [...]  # 每个任务的详细数据
}
```

`fixture_snapshot_id` 是所有 fixture 目录的联合 SHA-256——如果 fixture 文件被修改了，这个 hash 就会变，提醒你基准线已经漂移。

### 8.5 实验套件：单独测量每个机制

基准测试回答"整体通过率"，但有时候需要回答更细的问题：记忆层到底有没有用？上下文压缩省了多少空间？安全护栏拦住了什么？

Enigma 提供四类实验：

**记忆依赖实验**（`run_memory_dependency_experiment`）

对比三种配置下 agent 能否正确回忆之前读过的事实：

```
memory_on       — 完整记忆层
memory_off      — 关闭记忆和召回
memory_irrelevant — 记忆里只有无关内容
```

核心指标：`correct_rate`（回答正确率）和 `repeated_reads`（需要重新读文件的次数）。如果 memory_on 的 repeated_reads 为 0 且 correct_rate 接近 1，说明记忆层确实减少了重复读取。

**上下文压力矩阵**（`run_context_stress_matrix`）

交叉测试 history 长度（4/12/24 条）× 记忆量（2/10 条笔记）× 请求长度（短/长），测量 prompt 压缩比和当前请求是否被保留：

```python
# 三个维度的组合
history_levels = [("short", 4), ("medium", 12), ("long", 24)]
note_levels = [("low", 2), ("high", 10)]
request_levels = [("short", "recall"), ("long", "recall the relevant...")]
```

核心指标：`avg_prompt_compression_ratio`（压缩比）和 `current_request_preserved_rate`（当前请求是否被保留）。理想情况下，即使 history 和 memory 都很长，压缩后 prompt 仍在预算内，且用户当前请求不被截断。

**安全场景实验**（`run_security_experiment_suite`）

覆盖 10 个安全场景：

| 场景 | 验证什么 |
|------|---------|
| path_escape_read | `../outside.txt` 被路径校验拦截 |
| symlink_escape | 符号链接指向工作区外被拦截 |
| search_escape | search 的 path 参数不能逃逸 |
| approval_denied_shell | 审批策略为 never 时工具被拒绝 |
| read_only_write | 只读模式下写操作被阻止 |
| repeated_identical_call | 连续相同调用被检测为重复 |
| patch_nonunique | old_text 匹配多处时报错 |
| patch_missing_new_text | 缺少必要参数时报错 |
| timeout_out_of_range | 超时值超出允许范围时报错 |
| empty_delegate_task | 空任务描述被拒绝 |

核心指标：每个场景的 `security_event_type` 和 `tool_error_code` 是否符合预期。

**特征消融实验**（`measure_feature_ablation_metrics`）

对比关闭不同 feature flag 后 prompt 的变化：

```python
variants = {
    "full": {},                                    # 全部功能
    "no_context_reduction": {"context_reduction": False},  # 关闭上下文压缩
    "no_memory": {"memory": False, "relevant_memory": False},  # 关闭记忆
    "no_reflection": {"reflection": False},         # 关闭自动反思
}
```

核心指标：各变体的 `prompt_chars`、`memory_chars`、`history_chars`、`budget_reduction_count`。这组数据能直接回答"每层压缩各贡献了多少空间"。

### 8.6 运行工件聚合：`aggregate_run_artifacts`

除了基准测试，日常开发中也可以聚合某次实际使用的所有运行工件：

```python
def aggregate_run_artifacts(runs_root):
    # 遍历 .enigma/runs/ 下所有 run 目录
    # 从 report.json 读取：tool_steps、attempts、prompt_chars、stop_reason
    # 从 trace.jsonl 读取：tool 调用分布、安全事件、运行耗时
    return {
        "run_count": ...,
        "avg_tool_steps": ...,
        "cache_hit_rate": ...,
        "prefix_reuse_rate": ...,
        "tool_name_counts": {"read_file": 12, "patch_file": 5, ...},
        "security_event_counts": {"path_escape": 1, ...},
        "avg_run_duration_ms": ...,
    }
```

这组指标适合回答"这次实际使用中 agent 的表现如何"——和基准测试的"人工构造场景"互补。

### 8.7 小结

评测体系的设计原则是**分离关注点**：

```
基准测试（BenchmarkEvaluator）→ "通过率是否稳定"
实验套件（metrics.py）         → "某个机制的单独贡献"
工件聚合（aggregate_run_artifacts）→ "实际使用中的表现"
```

三者共用同一套产物格式（report.json + trace.jsonl），但回答的问题不同。基准测试用 FakeModelClient 保证确定性；实验套件用 feature flag 做消融对比；工件聚合从真实运行中提取统计。改了代码之后，跑一遍基准测试看通过率有没有退化，再跑相关实验套件看具体影响——这就是 Enigma 的迭代方式。

---

## 第九章 辅助模块速览

这一章记录前面章节没展开讲、但对理解整个项目结构有帮助的辅助模块——终端显示、REPL 交互、流式输出。

### 9.1 终端显示层：`enigma/display.py`

纯函数模块，不依赖 runtime 状态，提供：

- `supports_color(stream)`：ANSI 颜色检测
- `format_step_header(...)`：步骤结果标题，`✓` 绿 / `✗` 红 / `~` 黄
- `format_result_preview / format_result_compact`：工具结果预览与单行摘要
- `format_diff(...)`：unified diff 红绿高亮（`+`/`-`/`@@`）

把显示逻辑抽成纯函数，好处是可以单独测试颜色输出、不耦合 runtime；runtime 改动也不会影响终端 UX。

### 9.2 REPL 交互层：`enigma/repl.py`

| 组件 | 作用 |
|------|------|
| `Spinner` | 后台线程，每 100ms 刷新 `◐◓◑◒` 动画 + 已耗时 |
| `read_key_nonblocking()` | 非阻塞读键（Windows 用 `msvcrt`，Unix 用 `select`） |
| `draw_pending_message / clear_pending_display` | Spinner 运行中可以暂存用户输入，显示在输入行上方 |

**为什么单开线程做 spinner**：主线程等模型 HTTP 响应时是 I/O 阻塞的，如果不开后台线程刷新，用户看不到任何"agent 还在动"的迹象。Ctrl+C 信号从主线程捕获后转成 `request_cancel()`，ask() 循环每个迭代开头检查 `_cancel_requested`，保证中断粒度在工具边界（不会中断一半的文件写入）。

### 9.3 流式输出：`on_token` 回调

三个 `ModelClient` 的 `complete()` 都新增了 `on_token=None` 参数：

- `OllamaModelClient` — `stream=True` 走 NDJSON，逐行解析 `response` 字段回调
- `OpenAICompatibleModelClient` — `stream=True` 走 SSE，逐 delta 回调
- `AnthropicCompatibleModelClient` — `stream=True` 走 SSE，逐 `content_block_delta` 回调
- `FakeModelClient` — 通过 `**kwargs` 自动忽略 `on_token`，测试不受影响

runtime 在 `ask()` 的模型调用处用 `_make_stream_callback()` 把 delta 打印到 `tool_activity_stream`，用户看到 token 一个一个吐出来——在模型慢的时候显著改善 UX。

---

## 第十章 跨会话记忆与会话持久化

> 第五章讲的是**当前会话内**的分层记忆。这一章讲的是**跨会话**的长期持久化，以及长会话内部如何通过滚动摘要接力任务状态。

### 10.1 这一章要解决什么问题

第五章的 `LayeredMemory` 只活在当前 `session` 里：会话一结束，episodic_notes 随 session 文件归档，再也没人检索。但真实使用中经常出现三类需求：

1. **"我上周那个 CLI 改动里，readme 的 title 我最后定成什么了？"** — 跨会话搜索旧消息和工具结果。
2. **"项目的测试命令是什么来着？"** — 跨会话留存稳定事实，每次启动都加载少量关键信息。
3. **"这个长任务我 compact 过两次，还能记得整体目标吗？"** — 长会话内部有滚动摘要接力，`/compact` 之后稳定记忆不丢。

这三件事由三个独立部件解决：**SessionDB（SQLite + FTS5）**、**`MEMORY.md` 启动记忆 + 按需主题**、**rolling_summary（每会话滚动摘要）**。

### 10.2 设计灵感来源

| 借鉴对象 | 借来了什么 | Enigma 的简化 |
|---------|-----------|---------------|
| **Claude Code 的 `CLAUDE.md`** | 项目根放一份短的"启动记忆"，每次启动先加载；详细内容放在外部文件按需读取 | Enigma 用 `.enigma/memory/MEMORY.md`（200 行 / 25KB 上限），主题文件在 BM25 召回时才加载 |
| **Claude Code 的 `/compact` 语义** | compact 只压缩对话历史，不改写稳定项目规则 | 明确拆分"稳定记忆（只从磁盘读）vs 易变历史（被 compact 重写）" |
| **Hermes 的滚动摘要** | 每会话一份 rolling summary，记录目标 / 状态 / 已触文件 / 下一步 | Enigma 用 SQLite 的 `session_state` 表持久化，断电不丢；`ContextManager` 每轮注入到 prompt |
| **Hermes 的分会话隔离** | 不同 session_key 的滚动摘要互不污染 | Enigma 直接复用 `session["id"]` 作为 key，CLI 模式下每个 `.enigma/sessions/*.json` 独立；DB 的 `session_state` 主键就是 session_id |
| **Hermes 的自动反思（nudge）** | 定期触发反思，从历史中提取教训写入记忆 | Enigma 改为"变化驱动"——有新 process 笔记或意图关键词时才触发，不用定时器；用输入签名去重防重复；反思结果替换而非追加，天然控制增长 |

**关键刻意的简化**（面试时可以展开说）：
- **不做 embedding**：FTS5 的 BM25 + tokenize='unicode61' 已经能处理中英混合关键字，零依赖；多语言/语义检索是后续迭代空间。
- **不做 JSONL 双写**：SQLite 已经可检索可回放，再加 JSONL 只增加运维复杂度，没实际价值。
- **不引入 `index.json`**：`MEMORY.md` 本身就是人类可读的索引，主题文件放 `topics/`，不另搞一份 JSON 索引。
- **不自动拆会话**：compact 只产滚动摘要不新开 session；但 `sessions.parent_id` 字段已预留，未来要做长任务拆分不用改 schema。
- **不做定时反思**：Hermes 每 15 个 tool call 触发一次 nudge，不管有没有值得反思的内容。Enigma 改为"变化驱动"——有新 process 笔记或意图关键词才触发，避免浪费 token 在"没什么好反思"的轮次上。
- **不做自动 skill 蒸馏**：Hermes 的 skill 自动生成被用户诟病为"junk drawer"（200+ 条低质量 skill）。Enigma 的 skill 全部手写，反思系统只产出语义记忆（4 个固定 topic），不生成可执行 skill。

### 10.3 SessionDB：跨会话的长期档案

**位置**：`.enigma/sessions/state.db`（SQLite）
**代码**：`enigma/storage/session_db.py`

#### 10.3.1 Schema 三张表

```sql
-- 会话元信息
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    parent_id TEXT,                -- 未来支持 compact 拆会话预留
    title TEXT, model TEXT, cwd TEXT,
    started_at TEXT, ended_at TEXT,
    metadata_json TEXT
);

-- 消息流：user / assistant / tool_call / tool_result / compact_summary
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, role TEXT, kind TEXT, content TEXT,
    tool_name TEXT, file_path TEXT, command TEXT,
    token_estimate INTEGER, created_at TEXT, metadata_json TEXT
);

-- 每会话的滚动摘要 + 最近文件 + 待办
CREATE TABLE session_state (
    session_id TEXT PRIMARY KEY,
    rolling_summary TEXT,
    recent_files_json TEXT,
    open_tasks_json TEXT,
    updated_at TEXT, metadata_json TEXT
);

-- FTS5 虚表 + 三个触发器（insert/delete/update 自动同步）
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content, file_path, command, tool_name,
    content='messages', content_rowid='id',
    tokenize='unicode61'
);
```

#### 10.3.2 为什么用触发器同步 FTS

FTS5 的 content-table 模式要求 `messages_fts` 和 `messages` 内容一致，但手动维护两张表容易漏。SQLite 触发器在 `messages` 的 INSERT/DELETE/UPDATE 时自动更新 FTS，一行 Python 都不用写同步逻辑——这是 SQLite 内置能力，面试时可以强调"选型理由：零依赖 + 触发器自动一致性"。

#### 10.3.3 搜索 API

```python
db.search_messages("foo")                        # 全局 FTS 搜索
db.search_messages("foo", session_id="abc")      # 限定在某个会话内
db.recent_messages(session_id, limit=20)         # 按 id DESC 取最近 N 条
```

BM25 排序天然按相关度，不需要额外排序逻辑。对 Agent 来说，这等价于"我曾经在别的会话里讨论过 X，去把那段对话找回来"。

#### 10.3.4 与 `SessionStore` 的职责分工

| | `SessionStore`（`.enigma/sessions/*.json`） | `SessionDB`（`.enigma/sessions/state.db`） |
|---|---|---|
| 角色 | 当前轮 prompt 的数据源 | 跨会话可检索的长期档案 |
| 结构 | 单个 session 的完整状态快照（history + memory + checkpoints） | 消息流 + 滚动摘要 + 会话元信息 |
| 读取时机 | 每次 `ask()` 构建 prompt 时 | 只在用户显式搜索 / compact 写入时 |
| 失败策略 | 不能失败（否则 agent 不可用） | 失败降级（runtime 用 try/except 包住，不阻塞主流程） |

这是**刻意的冗余**：即使 SQLite 文件损坏，当前会话仍然跑得起来，只是丢了"跨会话检索"能力。面试时强调**"主流程只依赖 JSON，DB 是增强而非关键路径"**。

#### 10.3.5 Windows 文件锁的坑

早期版本 `SessionDB` 持有一个长连接，在 Windows 下临时目录清理时会被 `PermissionError: [WinError 32]` 卡住（SQLite 进程还握着文件句柄）。现在改成**每个方法内开关连接**（`_session()` 上下文管理器），代价是每次调用多一次 `sqlite3.connect`，但测试清理、跨进程调试都不再被锁住。
### 10.4 MEMORY.md：启动记忆与按需主题

**位置**：`.enigma/memory/MEMORY.md`（启动入口）、`.enigma/memory/topics/*.md`（详细主题）
**代码**：`enigma/memory.py::DurableMemoryStore`

#### 10.4.1 两类记忆在磁盘上的分工

```
.enigma/memory/
  MEMORY.md                   # 启动记忆入口：前 200 行 / 25KB 每轮注入
  topics/
    project-conventions.md    # 详细主题：只有 BM25 召回时才读
    key-decisions.md
    dependency-facts.md
    user-preferences.md
```

| 文件 | 进入 prompt 的时机 | 上限 |
|------|---------------------|------|
| `MEMORY.md` | **每轮 prompt 都注入** | 200 行或 25KB，取较小 |
| `topics/*.md` | 只有查询命中 BM25 时注入 | 单条 note 500 字符 |

这模仿 Claude Code 的 `CLAUDE.md` 哲学：**高频刚需的事实（"项目用 uv 不是 pip"、"测试用 pytest"）必须每轮都在 prompt 里**，让模型不会突然忘掉；低频详细信息（"2026-03 的一次重构决定"）按需召回，不浪费 token。

#### 10.4.2 启动记忆的读取

```python
def load_startup_memory(self, line_limit=200, byte_limit=25*1024):
    if not self.index_path.exists():
        return ""                         # 没文件就返回空，不是 raise
    raw = self.index_path.read_text(encoding="utf-8")
    encoded = raw.encode("utf-8")
    if len(encoded) > byte_limit:
        raw = encoded[:byte_limit].decode("utf-8", errors="ignore")
    lines = raw.splitlines()
    if len(lines) > line_limit:
        lines = lines[:line_limit]
    return "\n".join(lines).rstrip()
```

**为什么双重上限？** 只限行数，一行被塞进整个 README 就炸预算；只限字节，换行符多的文件读进半个段落不完整。双重取较小，对中英混合文本都友好。

#### 10.4.3 默认文件的懒初始化

`DurableMemoryStore.ensure_index()` 在 `MEMORY.md` 不存在时写一份默认骨架。**但调用点在 CLI 层的 `build_agent()`，不在 `Enigma.__init__` 或 `ContextManager.build()`**——这是为了避免测试临时目录意外生成 `.enigma/memory/MEMORY.md`，导致 evaluator fixture 的预算计算偏移。

#### 10.4.4 按需主题召回

主题文件走第五章的 BM25 通路（`DurableMemoryStore.retrieval_candidates`），和情景记忆一起参与"召回记忆"section 的分路排序：情景记忆保底 2 条，语义主题保底 2 条，再按 BM25 分数补齐到 `RELEVANT_MEMORY_LIMIT=4`。

### 10.5 Rolling Summary：长任务的内部接力

**位置**：SQLite `session_state` 表（以 `session_id` 为主键）
**代码**：`enigma/runtime.py::Enigma.rolling_summary_text()`、`apply_compact_payload()`

#### 10.5.1 更新时机

滚动摘要只在 **`/compact` 时**更新一次。这里刻意没有做"每 N 轮自动更新"——自动更新需要再调一次模型，成本高且噪音大；让用户在长任务卡点时显式触发 compact 更可控。

```python
# runtime.py::apply_compact_payload
self.session_db.update_session_state(
    self.session["id"],
    rolling_summary=summary_text,
    recent_files=list(self.memory.to_dict()["working"]["recent_files"]),
)
self.session_db.append_message(
    self.session["id"], role="system", kind="compact_summary",
    content=summary_text, metadata={"focus": focus},
)
```

#### 10.5.2 `ContextManager` 的注入

```
prompt 顺序:
  prefix                 ← 工具 manual + workspace 快照
  startup_memory         ← MEMORY.md 头部（跨会话稳定）
  memory                 ← 工作记忆（当前会话）
  relevant_memory        ← BM25 召回（情景 + 主题）
  rolling_summary        ← SessionDB 读出的滚动摘要
  history                ← 最近 history 条目
  current_request        ← 本轮用户输入（永不裁剪）
```

读取顺序很重要：**rolling_summary 紧靠 history 之前**，让模型先看压缩后的宏观状态，再看最近几轮的原文细节——这模仿 Hermes 的设计，在"长程视野"和"近端精度"之间做平衡。

#### 10.5.3 读取回退链

`Enigma.rolling_summary_text()` 读取顺序：

1. SessionDB `session_state.rolling_summary`
2. Fallback 到 `session["compact_summary"]["text"]`（JSON store）

两层都是为 SQLite 不可用时留的兜底。

### 10.6 Compact 后稳定记忆不丢：设计原理

**旧问题**：早期 compact 把语义记忆也交给模型重写，经常出现模型"偷懒删掉没涉及的主题"的现象，长会话连续 compact 几次后 durable memory 严重损耗。

**新规则**：compact 产物只允许写三类内容：

```
allowed:  compact_summary（rolling_summary）
          working_memory.task_summary / file_summaries（当前任务相关）
          episodic_notes（被压缩的情景记忆）
forbidden: semantic_memory（稳定主题）—— compact prompt 不再要求模型输出此字段
           MEMORY.md              —— compact 从不碰磁盘上的 MEMORY.md
```

实现上：
- `build_compact_prompt()` 的 JSON 输出格式中**删除了 `semantic_memory` 字段**，并明确告知模型"语义记忆由其他机制管理"
- `validate_compact_payload()` 删除了 `semantic_memory` 相关校验
- `compact_context()` 在 compact **之前**调用 `reflect_and_update_semantic_memory()`，趁情景笔记还没被磨碎，先用子 agent 提取值得保留的语义信息

语义记忆的更新由独立的反思系统（§5.6.2）全权负责——compact 只管压缩历史和情景记忆，不动稳定记忆。下一轮 `ContextManager.build()` 会重新从磁盘读 `MEMORY.md` 和 `topics/`，**稳定记忆的权威源头固化在磁盘，而不是会话状态里**。

### 10.7 会话隔离 + 未来扩展

#### 10.7.1 当前隔离边界

CLI 模式下每次 `enigma` 启动产生一个新 `session["id"]`（格式 `YYYYMMDD-HHMMSS-<6hex>`），`session_state` 表以此为主键。两个不同 session 的 rolling_summary 互不覆盖。

跨会话搜索时用 `session_id` 过滤；也可以省略过滤，做全库搜索。

#### 10.7.2 预留的扩展字段

- `sessions.parent_id` — 未来 compact 拆会话时，新会话会记录父会话。当前版本不拆会话，但 schema 已就位。
- `sessions.title` — 未来支持会话标题（"refactor-auth"、"bugfix-login"）。
- `session_state.open_tasks_json` — 预留的"待办列表"字段，目前 compact 不写入；未来可接入计划模式。

#### 10.7.3 未来迭代方向（面试时可以主动提）

| 方向 | 现在的限制 | 下一步做什么 |
|------|-----------|------------|
| 语义检索 | 只有 BM25，无法处理同义词/跨语言 | 接入 embedding（可选 local embedder，例如 bge-m3），向量索引和 FTS5 做双路召回 |
| 多 agent 共享记忆 | 每个 Enigma 进程独立 DB | 改 DB 路径到 `~/.enigma/global.db`，加 workspace_fingerprint 隔离 |
| 记忆衰减 | MEMORY.md 无过期机制 | 给 topics 加 `last_used_at`，长期未召回的自动沉到 archive/ |
| Rolling summary 自动更新 | 只在 /compact 时更新 | 加步数/token 触发阈值，但要做去噪避免摘要漂移 |

### 10.8 关键代码位置速查

| 功能 | 模块 | 入口 |
|------|------|------|
| SessionDB + FTS5 | `enigma/storage/session_db.py` | `SessionDB.search_messages()` |
| 启动记忆 | `enigma/memory.py` | `DurableMemoryStore.load_startup_memory()` |
| Prompt 注入 | `enigma/context_manager.py` | `_collect_startup_memory()`、`_collect_rolling_summary()` |
| Compact 稳定记忆保护 | `enigma/runtime.py` | `apply_compact_payload()`（删除 `replace_durable_topics` 分支） |
| 滚动摘要读取 | `enigma/runtime.py` | `Enigma.rolling_summary_text()` |
| CLI 默认 MEMORY.md | `enigma/cli.py` | `build_agent()` 末尾 `ensure_index()` |

### 10.9 测试与可验证的行为

`tests/test_session_db.py`（12 个用例）覆盖：

- SessionDB schema 创建 + 消息回写 + 查询
- FTS5 按关键字搜索 / 按 session 过滤 / 空查询兜底
- `session_state` 表的滚动摘要持久化和部分字段更新
- `MEMORY.md` 缺失时的默认骨架创建
- 启动记忆 200 行 / 25KB 上限
- 启动记忆在 `ContextManager` 出 prompt 中可见
- **Compact 不重写 `MEMORY.md`，滚动摘要写入 DB，下一轮 prompt 还能读到**
- History 镜像到 SessionDB
- 两个 session 的滚动摘要互不污染

`tests/test_enigma.py::test_compact_context_does_not_rewrite_stable_semantic_topics` 验证 compact 不再擦除 durable topic。

## 第十一章 面试追问速答

> 这一章给面试现场用：面试官按着亮点表追问时，能 30 秒内抓出重点回答。
> 所有答案都可以落回前面章节的具体代码/数据结构。

### 11.1 基础问答

**Q：这个项目是做什么的？为什么做？**
A：本地 coding agent。终端输入一句任务，agent 自己读仓库、调工具（读文件/写文件/搜索/跑 shell/Web 搜索/委派子 agent）推进任务，直到返回最终答案。做它的动机是理解 "Claude Code 这类工具内部到底是怎么把一个自然语言任务拆成工具调用链"——从组 prompt 到工具安全护栏到记忆持久化，自己动手把每一层写一遍。(§0)

**Q：代码量和结构？**
A：约 14 个 Python 模块，纯标准库零运行时依赖；核心是 `runtime.py::Enigma.ask()` 的控制循环（感知 → 决策 → 行动 → 记录）。(§2.1)

**Q：为什么选 Python 不选 Go/Rust？**
A：agent 是 I/O 密集（模型 HTTP、工具 subprocess、文件 IO），性能瓶颈在外部，不在 Python 解释器。Python 的 dict/dataclass 建模上下文状态最快；标准库自带 `sqlite3`/`subprocess`/`argparse` 够用，省掉依赖地狱。

### 11.2 核心循环（§2）

**Q：ask() 一轮循环里会发生什么？**
A：
1. **感知**：`ContextManager.build()` 按预算组 prompt（prefix + 稳定记忆 + 工作记忆 + 召回记忆 + 滚动摘要 + history + 当前请求）
2. **决策**：调 `model_client.complete(prompt)`，拿到文本
3. **解析**：`parse()` 认出 `<tool>...</tool>` / `<plan>...</plan>` / `<final>...</final>` 三种 XML 标签
4. **行动**：工具走 7 层安全护栏，结果写回 history；plan 进审批环；final 落盘 + 返回
5. **记录**：trace.jsonl 每一步 emit 事件，report.json 收尾

**Q：为什么限制工具步数？**
A：防跑飞。模型可能因 hallucination 无限调同一个工具；`max_steps` 是硬上限，`attempts` 是"模型输出不合法"的重试上限。小 `max_steps` 时允许少量格式修正，大 `max_steps` 时只给 +4 次格式重试，防止 malformed 把模型调用数放大 3 倍。(§2.1)

### 11.3 Prompt 组装 & Cache（§4）

**Q：Prompt cache 怎么做的？**
A：`prefix`（工具 manual + workspace 快照 + 规则）是最稳定的那段，取它的 SHA-256 当 cache key，扔给支持 cache 的后端（Anthropic）。Prefix 只有工作区 fingerprint 变化时才重建，history 变化不影响 cache key。(§4.3-4.4)

**Q：怎么决定不同模型要用多大预算？**
A：`MODEL_CONTEXT_WINDOWS` 硬编码 85+ 个模型的窗口大小（从 OpenAI/Claude/Gemini/Qwen/DeepSeek 到本地小模型），用**最长前缀优先匹配**。未知模型 fallback 32K 的保守值。总预算 = 窗口 × 75%，剩下 25% 留给模型输出 + 系统 overhead。(§4.8)

### 11.4 记忆系统（§5 + §10）

**Q：为什么分三层记忆而不是一个大摘要？**
A：一个摘要所有信息混在一起，prompt 不好按优先级裁剪。分层后：
- **工作记忆**（当前任务摘要 + 最近文件 + 文件摘要）每轮都注入
- **情景记忆**（压缩过的上下文笔记）BM25 召回时注入
- **语义记忆**（跨会话稳定事实）存在 `MEMORY.md` 和 `topics/`，头部每轮注入，详细主题按召回
每层独立容量 + 独立生命周期，压缩时优先牺牲"影响最小"的那一层。(§5.2, §10)

**Q：跨会话记忆怎么实现？灵感来自哪？**
A：参考 Claude Code 的 `CLAUDE.md`（项目根一份短启动记忆）和 Hermes 的会话摘要分离思路。实现上三件东西：
1. **`MEMORY.md` + `topics/`**：磁盘上的稳定记忆，启动入口 200 行 / 25KB，按需召回主题
2. **SessionDB（SQLite + FTS5）**：跨会话全历史可搜，触发器自动同步 FTS
3. **rolling_summary（`session_state` 表）**：每会话一份滚动摘要，`/compact` 时更新
关键刻意简化：不做 embedding（BM25 + unicode61 已足够），不做 JSONL 双写，不自动拆会话。(§10.2)

**Q：文件改了，旧摘要怎么处理？**
A：每个 file_summary 和文件的 SHA-256 绑定（叫 freshness）。下一轮进 prompt 前比对当前 hash；对不上就从工作记忆中失效，不让模型基于过时的摘要决策。(§5.4)

**Q：语义记忆是自动沉淀的还是用户手动触发的？**
A：两条路径并存：
1. **手动**：用户说"记住"，模型 final 里出现 `项目约定：` 等前缀，纯字符串匹配，零成本
2. **自动**：反思子 agent 在三个时机触发——compact 前（趁情景笔记还没被磨碎）、工具循环每 10 次（有新 process 笔记或意图关键词时）、ask 结束前

反思子 agent 审查全部上下文后返回结构化 JSON，直接替换 topic 文件。设计上"宁缺毋滥"——没有值得提取的就返回空，不会瞎总结。(§5.6)

**Q：反思系统怎么避免"觉得自己干得漂亮"的问题？**
A：Hermes 被用户骂最多的点就是反思环节总输出正面总结。我们的对策：
1. prompt 里明确要求"只提取独立于这次会话也成立的事实"，不接受"agent 完成了 X"这种流水账
2. 输出必须过 `reject_durable_reason()` 过滤——密钥、traceback、过长文本全丢
3. 全空 JSON 是合法输出，不强制产出内容
4. 替换语义——输出的是 topic 的完整期望状态，不是追加，天然控制增长(§5.6.2)

**Q：语义记忆会不会无限增长？**
A：不会。两道防线：(1) 反思 prompt 明确告知"每 topic 最多 25 条"；(2) 代码层面 `promote()` 超限拒绝 append，`replace_topics()` 截断到 25。4 个 topic × 25 = 100 条上限，在启动记忆的 200 行读取窗口内。`_subject_key` 匹配的替换不受限（替换不增加总数）。(§5.6.4)

**Q：为什么用 BM25 不用 embedding？**
A：三个理由：(1) embedding 要引入 sentence-transformers + 向量库（faiss/chroma），项目目标是零依赖；(2) BM25 + `unicode61` tokenizer 对中英混合、关键字召回足够；(3) embedding 的优势是"同义词 / 跨语言"，coding agent 的召回多是精确关键字（文件名、API 名、类名），BM25 反而更准。后续迭代可以加 embedding 做双路召回。(§10.2, §10.7.3)

### 11.5 Compact & 稳定性（§6 + §10.6）

**Q：`/compact` 干了什么？有什么坑？**
A：把四层上下文（工作记忆 / 情景记忆 / 语义记忆 / history）交给模型压缩，产 rolling_summary + 压缩后的情景记忆。history 保留 head 2 + tail 6 + 中间摘要。

**旧版本的坑**：让模型重写语义记忆，长会话连续 compact 几次后 durable memory 严重损耗（模型"想不起早期的 convention 就删掉"）。**新版本的修正**：稳定记忆只从磁盘读，compact 产物**不允许写回 `MEMORY.md` / `topics/`**；下一轮 prompt 会从磁盘重新注入。这样稳定记忆的权威源头固化在磁盘而非会话状态。(§10.6)

**Q：自动 compact 怎么触发？**
A：两层触发：
1. **`ContextManager.build()` 内部**：prompt 被预算裁剪到地板了还超预算、且 overflow > 25% 时，调 `compact_context()`。加 25% 门槛是避免小幅超支时白调一次模型。
2. **Runtime 主循环安全网**：调模型后看 `should_compact(model, prompt_tokens)`，prompt 占到模型窗口 80% 且剩余不够 16K 输出时，下一轮之前触发 compact。

重入保护 `_auto_compacting` 标志防止死循环。(§6.5.4, §6.6)

### 11.6 工具与安全（§3）

**Q：工具执行有哪些安全护栏？**
A：7 层流水线（`run_tool()`）：
1. **白名单**：工具必须注册过，`delegate` 在子 agent 里会被递归禁用
2. **Schema 校验**：参数类型检查，不允许空参数
3. **路径穿越防护**：所有路径必须落在 `repo_root` 下，`Path.resolve()` 后再比较
4. **重复调用检测**：连续同名 + 同参调用直接拒绝，防 hallucination 死循环
5. **审批**：risky 工具（write_file / patch_file / run_shell / delegate）按 `ask/auto/never` 策略
6. **快照 + 执行**：执行前拍 workspace snapshot，执行后 diff 出改了哪些文件
7. **记忆更新**：成功执行的 read_file 更新 file_summary；工具结果汇总进 history

(§3.4, §3.6, §3.7)

**Q：Plan 模式怎么实现的？**
A：进入 plan 模式时，剔除所有 risky 工具（只留只读），换一套 prompt prefix 要求模型输出 `<plan>...</plan>`。用户看到计划后三选一：approve（切回执行模式，注入计划到 history）/ revise（带反馈再来一轮 plan）/ reject。这个 UX 直接模仿 Claude Code 的 `/plan`。(§2.8)

### 11.7 会话与恢复（§7）

**Q：两层持久化的关系？**
A：
- **`SessionStore`**（`sessions/*.json`）：当前会话的完整状态快照，每次 `ask()` 构建 prompt 时读。这是 agent 主流程的**关键路径**。
- **`SessionDB`**（`sessions/state.db`）：跨会话可检索的长期档案 + 每会话滚动摘要。是**增强路径**，失败时降级，不影响 agent 运行。

刻意冗余：SQLite 坏了，agent 仍能跑；只是不能搜历史对话、长会话接续能力退化到单纯的 compact_summary。(§10.3.4)

**Q：会话恢复怎么评估能不能信任旧 checkpoint？**
A：五维状态评估——
- 文件 freshness（SHA-256）：有摘要的文件还在不在、内容变没变
- runtime_identity：cwd / model / approval_policy / feature_flags 有没有变
- workspace_fingerprint：工作区布局变化
- tool_signature：工具注册签名
- schema_version：checkpoint 格式版本号

全部匹配 → `full-valid`；有文件变了 → `partial-stale`（创建新 checkpoint 打断旧推理）；runtime 变了 → `workspace-mismatch`。(§7.7)

### 11.8 评测与工程化（§8）

**Q：怎么保证迭代不回退？**
A：三条线：
1. **单元测试**：200 个用例，覆盖安全不变式（路径穿越 / 审批拒绝 / secret 脱敏）和核心循环
2. **固定 benchmark**：`benchmarks/coding_tasks.json` 12 个任务，用 `FakeModelClient` 确定性回放，跑通过率
3. **消融实验套件**：按 feature flag 关掉某一层（memory / relevant_memory / context_reduction / prompt_cache）对比通过率差

改完代码先跑单元测试 → benchmark → 相关消融 → 观察报表。(§8)

### 11.9 关于"把它当作面试项目"的自我评价

**优点**：
- 单仓库纯 Python 零依赖，面试官可以跑起来；
- 设计要点都有对照（Claude Code / Hermes），能讲"借鉴了什么，简化了什么"，而不是空谈原创；
- 关键路径（安全护栏、记忆、恢复）有单元测试护住。

**已知局限**（主动说出来能加分）：
- BM25 召回对同义词无能为力，需要加 embedding 双路召回
- token 估算用的是 `len/3.5`，中英混合场景差 ±15%，要更准需要接 `tiktoken`
- 跨会话搜索目前只有 FTS5 关键字，没有向量相似
- rolling_summary 只在 `/compact` 时更新，长任务中间过程丢失精度
- 自动 compact 触发阈值是硬编码比例（25% overflow / 80% 窗口），没接自适应
- 反思子 agent 的质量依赖底模型推理能力——弱模型可能产出低质量的语义条目，但有过滤兜底

**还没做但 schema 已就位**：
- `sessions.parent_id` 为 compact 拆会话预留
- `session_state.open_tasks_json` 为待办列表预留

---
