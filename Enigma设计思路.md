# Enigma 设计思路与面试说明

本文档用于说明 Enigma 的项目定位、核心设计、关键实现和面试表达。重构后的文档分成两大部分：

- 第一部分是项目说明与设计思路，适合给面试官或 reviewer 展示项目为什么这样设计。
- 第二部分是面试表达与答辩材料，适合面试前快速复习，尤其用于回应“这个项目解决什么真实问题”“为什么不是只靠 prompt”“multi-agent 是否太轻”等追问。

本文保留原文档的核心信息，但不再按源码细节流水账展开，而是按 agent harness 的工程问题重新组织。

## 变动标注

- **本轮补强**：这次面试复盘后新增或重点修改的内容。
- **已有能力**：项目原本已有，但在本轮说明中重新梳理叙事。
- **可验证证据**：可以用代码、测试、trace、report 或实机调用证明的内容。

***

# 第一部分：项目说明与设计思路

## 1. 项目定位

Enigma 是一个面向本地代码仓库的轻量级 coding agent harness。它运行在终端中，能够读取当前工作区、调用受约束的工具、执行一次性工程任务或进入交互式 REPL，并把会话、记忆和运行产物保存在本地。

它不是一个单纯的聊天壳，也不是训练大模型的项目。它关注的是 agent 应用层的工程底座：

- 如何把自然语言任务转成可追踪的工具调用链。
- 如何限制本地文件和 shell 工具的边界。
- 如何在长任务中管理上下文、记忆和恢复点。
- 如何让计划、执行、审查、修复形成闭环。
- 如何用测试和运行工件证明 agent 没有“黑箱跑完”。

一句话概括：

```text
Enigma = CLI + Agent Runtime + Tool Harness + Memory + Context Manager + Traceable Orchestration
```

## 2. 要解决的真实问题

面试复盘后，Enigma 的叙事不再是“我实现了很多功能”，而是聚焦 coding agent 在真实使用中的几个痛点。

| 真实问题 | Enigma 的设计回应 | 证据位置 |
| --- | --- | --- |
| 长任务中模型会重复读文件、忘记刚探索过的上下文 | 工作记忆、文件摘要、情景记忆、相关记忆召回 | 第 7 章、第 8 章 |
| 本地 agent 能读写文件和跑 shell，不能只靠 prompt 限制 | 工具白名单、路径锚定、risky tool 审批、HarnessPolicy | 第 5 章 |
| coding agent 需要稳定的定位、读取、修改、测试闭环 | `glob`、`search`、轻量 `symbol_search`、`read_file`、`write_file` / `patch_file`、`run_shell`，并在 prompt 里写明 loop | 第 5 章、第 6 章 |
| 用户跨会话反复教项目约定，成本高 | durable memory、`MEMORY.md`、topics、`applies_when` | 第 7 章、第 12 章 |
| 计划、执行、审查混在一个模型循环里，失败原因难追踪 | `/plan`、`/review`、`/orchestrate`、trace/report | 第 6 章、第 10 章 |
| prompt 容易越来越大，工具结果和历史会淹没当前任务 | 分段 prompt 预算、历史压缩、自动 compact、rolling summary | 第 8 章、第 9 章 |
| 面试或复盘时很难证明 agent 为什么这么做 | `task_state.json`、`trace.jsonl`、`report.json`、FakeModel 测试 | 第 10 章、第 13 章 |

## 3. 总体架构

Enigma 从终端命令到一次 agent 运行，可以分为四层：

```text
终端命令
  |
  v
CLI 入口
  - argparse 参数解析
  - provider / model 选择
  - workspace snapshot
  - session bootstrap
  |
  v
Agent Runtime
  - Enigma.ask() 主循环
  - ContextManager 组装 prompt
  - ModelClient 调模型
  - parse() 解析 tool / plan / final
  |
  v
Harness 能力
  - Tool registry
  - HarnessPolicy / ToolPolicy
  - Memory
  - RunStore / Trace / Report
  - Plan / Review / Orchestration
```

核心模块如下：

| 模块 | 职责 |
| --- | --- |
| `enigma/cli.py` | CLI 参数解析、模型后端选择、session 启动、REPL 循环、斜杠命令 |
| `enigma/runtime.py` | `Enigma.ask()` 主循环、工具调度、审批策略、trace/report、plan/review 接入 |
| `enigma/models.py` | Ollama / OpenAI-compatible / Anthropic-compatible 客户端，统一 `complete()` 接口 |
| `enigma/tools.py` | 工具声明、schema 校验、文件/shell/search/web/delegate 工具实现 |
| `enigma/policy.py` | **本轮补强**：`HarnessPolicy` / `ToolPolicy`，承载确定性规则 |
| `enigma/orchestration.py` | **本轮补强**：Plan / Execute / Review / Repair 状态机 |
| `enigma/context_manager.py` | prompt 预算、上下文组装、历史压缩、相关记忆召回 |
| `enigma/memory.py` | 工作记忆、情景记忆、语义记忆、durable memory、文件摘要 freshness |
| `enigma/workspace.py` | Git 状态、目录结构、最近提交、项目文档、diff、changed files |
| `enigma/task_state.py` | 一次任务运行的状态数据结构 |
| `enigma/run_store.py` | `.enigma/runs/<run_id>/` 运行工件持久化 |
| `enigma/storage/session_db.py` | SQLite + FTS5 跨会话检索、rolling summary |
| `enigma/evaluator.py` | 固定 benchmark 执行框架 |
| `enigma/metrics.py` | 消融实验、安全性、provider 对比、上下文压力测试 |
| `enigma/skills.py` | skill 发现、frontmatter 解析、模板组装 |
| `enigma/repl.py` | spinner、非阻塞输入、暂存消息、Ctrl+C 中断 |
| `enigma/display.py` | 终端颜色、工具进度、diff 展示 |

## 4. 启动与对象组装

Enigma 的启动链路可以概括为：

```text
enigma --provider ollama "fix bug"
  |
  v
pyproject.toml console script
  |
  v
cli.main()
  |
  v
argparse -> Namespace
  |
  v
build_agent(args)
  |
  +-- WorkspaceContext.build(args.cwd)
  +-- _build_model_client(args)
  +-- SessionStore(repo_root/.enigma/sessions)
  +-- Enigma(...)
```

### 4.1 CLI 参数

常用参数包括：

| 参数 | 作用 |
| --- | --- |
| `prompt` | 一次性任务文字，例如 `enigma "fix bug"` |
| `--provider` | 模型后端：`ollama` / `openai` / `anthropic` |
| `--model` | 模型名覆盖 |
| `--host` | Ollama 服务地址 |
| `--base-url` | OpenAI / Anthropic compatible API 地址 |
| `--approval` | 工具审批策略：`ask` / `auto` / `never` |
| `--max-steps` | 单次请求最大工具调用轮数 |
| `--max-new-tokens` | 模型单步最大输出 token 数 |
| `--temperature` | 采样温度 |
| `--resume` | 恢复指定 session 或 `latest` |
| `--plan` | 进入只读 plan mode |

### 4.2 WorkspaceContext

`WorkspaceContext.build()` 是 agent 对仓库的第一印象，负责采集：

- `cwd`：用户启动目录。
- `repo_root`：Git 仓库根目录。
- `branch`：当前分支。
- `default_branch`：默认分支。
- `status`：`git status --short`，截断后放入 prompt。
- `recent_commits`：最近提交。
- `project_docs`：`AGENTS.md`、`README.md`、`pyproject.toml`、`package.json` 等项目文档。
- `structure`：顶层目录摘要，用于让模型快速了解仓库形状。

所有 Git 命令都有超时和 fallback，启动不会因为某个 Git 命令失败而卡死。

### 4.3 ModelClient

`models.py` 把不同 provider 抹平成统一接口：

```python
complete(prompt, max_new_tokens, on_token=None, **kwargs)
```

支持：

- Ollama：本地 `/api/generate`。
- OpenAI-compatible：Responses / compatible API。
- Anthropic-compatible：Messages / compatible API。
- FakeModelClient：测试中确定性回放模型输出。

### 4.4 Enigma 初始化

`Enigma.__init__()` 完成以下装配：

```text
Enigma(...)
  |
  +-- session 初始化或恢复
  +-- LayeredMemory(session["memory"], workspace_root)
  +-- build_tools() 注册工具
  +-- build_prefix() 构建 agent 工作手册
  +-- ContextManager(self)
  +-- evaluate_resume_state()
  +-- session_store.save(session)
```

初始化后的 `Enigma` 是一个完整 runtime：既知道当前仓库事实，也知道模型后端、工具边界、会话历史、记忆状态和恢复信息。

## 5. 工具与 Harness Policy

### 5.1 工具设计原则

Enigma 的工具不是动态发现的，而是在 `tools.py` 中显式声明和注册。这样做的原因是：模型看到的是一个有边界、可审计的动作集合，而不是任意 Python 函数。

工具表：

| 工具 | risky | 说明 |
| --- | --- | --- |
| `list_files` | No | 列目录，跳过 `.git`、`__pycache__` 等噪音 |
| `read_file` | No | 按行范围读取 UTF-8 文件，输出带行号 |
| `search` | No | 优先使用 `rg`，找不到则回退 Python 纯文本搜索 |
| `glob` | No | 按 glob pattern 定位工作区文件，补齐 Claude Code 常见的文件发现能力 |
| `symbol_search` | No | 用轻量源码模式查定义、类型和引用，不引入完整 LSP 依赖 |
| `web_search` | No | 零依赖 web search，支持域名过滤 |
| `run_shell` | Yes | 在仓库根目录执行 shell 命令，环境变量白名单 |
| `write_file` | Yes | 写入文本文件，自动创建父目录 |
| `patch_file` | Yes | 精确文本替换，`old_text` 必须出现且只出现一次 |
| `delegate` | Yes | 委派只读子 agent 调查任务，仍需审批 |

`delegate` 本身不写文件，但它会扩大模型调用和任务范围，因此本轮补强后被视为 risky tool。

这套工具面刻意对齐常见 coding agent 闭环：先用 `glob` / `list_files` 找候选文件，用 `search` / `symbol_search` 缩小到关键定义、引用或类型，再用 `read_file` 读取相关范围，随后用 `write_file` / `patch_file` 修改，用 `run_shell` 跑测试、lint 或构建，最后根据失败输出和 diff 继续修复。正常模式和 Plan 模式的系统 prompt 都显式写入了这套流程；Plan 模式只执行只读版探索，不暴露写文件和 shell 工具。

### 5.2 工具执行流水线

一次工具调用不是直接 `tool(args)`，而是经过 7 层控制：

```text
模型输出 <tool>...</tool>
  |
  v
1. 工具存在性检查
  |
  v
2. 参数校验
  - 必填字段
  - 类型范围
  - 路径穿越
  - patch_file 精确匹配
  |
  v
3. 重复调用检测
  - 最近两次完全相同 tool call 会被拒绝
  |
  v
4. 审批策略
  - ask / auto / never
  - read_only 下拒绝 risky tool
  |
  v
5. 工作区快照
  - risky tool 执行前后记录 changed files
  |
  v
6. 真正执行
  |
  v
7. 结果后处理
  - 结果裁剪
  - 更新 memory
  - 写 trace metadata
```

### 5.3 HarnessPolicy / ToolPolicy

**本轮补强**：新增 `enigma/policy.py`，把确定性规则从 prompt 下沉到 harness 层。

核心思想：

> prompt 是软约束，harness policy 是硬边界。

适合放到 policy / tool 层的规则：

- 文件读写默认 UTF-8。
- 读取失败时可用 `errors="replace"` 返回可读内容。
- 所有路径必须锚定 workspace root。
- `write_file`、`patch_file`、`run_shell`、`delegate` 走审批。
- plan mode 只暴露只读工具。
- reviewer 角色只拥有只读工具面。
- 能映射为工具默认参数的用户偏好，不只写进 prompt。

这直接回应面试官的质疑：确定性规则不能依赖模型“记得遵守”。

### 5.4 路径安全

所有文件类工具最终都走 `Enigma.path()`：

```text
raw path
  -> 相对路径锚定 repo_root
  -> Path.resolve()
  -> os.path.commonpath([repo_root, resolved])
  -> 不在仓库内则拒绝
```

这样可以拦截：

- `../../etc/passwd`
- 指向仓库外的符号链接
- 模型误写绝对路径造成的越界访问

### 5.5 审批策略

审批策略有三种：

| 策略 | 行为 |
| --- | --- |
| `ask` | 默认，risky tool 前交互确认 |
| `auto` | 自动放行 risky tool，适合受控测试或信任场景 |
| `never` | 自动拒绝 risky tool，适合纯分析 |

只读模式下，即使 approval 是 `auto`，risky tool 也会被拒绝。

### 5.6 重复调用检测

Enigma 会检查最近两条工具历史。如果模型连续三次尝试同名同参数工具调用，第三次会被拒绝，并返回：

```text
error: repeated identical tool call for read_file; choose a different tool or return a final answer
```

真实 Ollama 测试中，`qwen3.6:35b-a3b` 曾重复读取同一个 README，重复调用防护成功促使模型收敛到 final。

## 6. Plan、Review 与 Orchestration

### 6.1 Plan 模式

`/plan <task>` 或 `--plan` 让 agent 先只读探索，产出结构化计划，由用户审批后再执行。

进入 plan mode 后：

- `plan_mode=True`。
- `build_tools()` 过滤所有 risky tool，只保留只读工具。
- prefix 替换为 Plan Mode 提示词。
- 模型应输出 `<plan>...</plan>`。

审批流程：

```text
Plan ready
  |
  +-- Approve -> 切回执行模式
  +-- Revise  -> 带反馈重新规划
  +-- Reject  -> 停止，不执行
```

计划被写入 `.enigma/plan.md`，避免 history 被压缩后计划丢失。

### 6.2 Review 命令

`/review` 用于代码审查，支持：

- 自动检测当前工作区变更。
- 指定文件。
- 分支对比。
- PR 链接。

它不是只把 diff 丢给模型。当前实现会先对 Python 文件运行一组零依赖 AST 静态规则，产出结构化 finding：

- `rule_id`
- `severity`
- `category`
- `file`
- `line`
- `message`
- `evidence`

首批规则刻意保持小而高信号：可变默认参数、裸 `except`、`subprocess.*(..., shell=True)`、`eval` / `exec`、`open()` 缺少 `encoding`、疑似硬编码密钥，以及语法错误 / 读取失败的 warning finding。

这些规则的判断点和原因是：

| 规则 | 典型代码 | 为什么需要提示 |
| --- | --- | --- |
| 可变默认参数 | `def f(items=[])` | 默认值只创建一次，多次调用会共享同一个列表 / 字典，容易让上一次调用的数据污染下一次调用。 |
| 裸 `except` | `except:` | 会捕获过宽，可能吞掉中断、系统退出或真正严重的错误，让失败被隐藏。 |
| `shell=True` | `subprocess.run(cmd, shell=True)` | 如果 `cmd` 拼了用户输入，可能变成命令注入；更安全的写法通常是传参数列表。 |
| `eval` / `exec` | `eval(user_input)` | 会把字符串当代码执行，遇到不可信输入时等于开放任意代码执行。 |
| `open()` 缺少 `encoding` | `open("data.txt")` | 不同系统默认编码可能不同，本地正常的文件读写到别的机器可能乱码或报错。 |
| 疑似硬编码密钥 | `API_KEY = "sk-..."` | 密钥写进源码容易被提交到 git、进入日志或被他人复用，应该放环境变量或本地配置。 |
| 语法错误 / 读取失败 | AST parse/read 失败 | 说明结构化扫描没有完整跑完，需要在 review 结果中显式暴露这个事实。 |

这些不是为了覆盖所有 bug，而是先抓最容易出事故、最容易解释、最适合本地 review demo 的 Python 常见坑。

随后 `/review` 把 workspace diff、文件内容和 static findings 一起交给模型，要求模型对 finding 做 triage：判断是真实问题、误报，还是需要回归测试验证。Reviewer 的定位仍然是“发现问题”，不是直接修改。

这一步的关键不是把 Enigma 包装成完整静态分析器，而是把 review 从纯 LLM 主观判断推进到一个可追踪 pipeline：

```text
Python AST rules
  -> structured static findings
  -> LLM triage
  -> trace.jsonl / report.json
```

独立 `/review` 会在 `trace.jsonl` 中写入 `review_static_findings` 事件，并把结构化结果写入 `report.json` 的 `review.static_findings`。`/orchestrate` 的 Reviewer 也会读取 static findings；如果出现 critical 静态 finding，会阻断 `DONE` 并进入 RepairPlanner。

面试时可以这样讲：

> 我没有把 `/review` 做成一个纯 prompt 功能。它前面先接了一层确定性的 Python AST 静态扫描，把一些高信号缺陷模式变成结构化 finding，比如 `rule_id`、严重程度、文件、行号和证据。然后模型不是凭空审查，而是对这些 finding 和 diff 一起做 triage：判断是真问题、误报，还是需要回归测试验证。最后这些 finding 会进入 `trace.jsonl` 和 `report.json`，所以审查过程可复盘、可统计，也能在 `/orchestrate` 里作为阻断条件。

如果面试官问“这和 Codex / Claude Code 的 review 有什么区别”，回答重点是：

- Codex / Claude Code 的优势是大模型能力、上下文理解、真实工程交互和成熟 UX，Enigma 不试图复刻这些。
- Enigma 这里展示的是 harness 层的工程思路：把一部分 review 从主观自然语言判断，前移到可复现的静态规则；把结果结构化落盘；再让 LLM 做解释、降噪和修复建议。
- 这不是完整静态分析器，还没有 CFG / dataflow / taint analysis，也没有跨语言规则库。当前目标是最小闭环：规则发现 -> 模型复核 -> 审计产物 -> critical 阻断。

如果面试官继续问“下一步怎么增强”，不要先说单测自愈。更稳的回答是：

> 下一步我会给 high / critical finding 增加回归测试生成：先让 agent 尝试写一个能复现问题的 failing test，修复后再跑测试验证。这样 review finding 就不只是建议，而是尽量变成可验证缺陷。

### 6.3 `/orchestrate` 轻量多智能体编排

**本轮补强**：新增 `/orchestrate <task>`，把原来的 Plan / Review 能力组合成一个有状态闭环。

它不是开放式 swarm，也不是线性流水线，而是 bounded multi-agent orchestration。

这里的“多智能体”不是同时启动一群互相聊天的独立进程，而是在同一个 `Enigma` runtime 上切换不同角色。每个角色都有自己的 prompt、工具面和状态责任，运行结果通过结构化状态对象传给下一个角色。这样做的好处是：行为可追踪、权限可收缩、失败可以回到明确的修复节点，而不是让多个 agent 自由讨论到不可审计。

角色：

| 角色 | 职责 | 工具边界 |
| --- | --- | --- |
| `Planner` | 只读探索，生成计划、风险和验收标准 | plan mode，只读工具 |
| `Executor` | 只执行当前 active step | 正常工具面，仍受 approval policy 控制 |
| `Reviewer` | 审查执行结果、diff 和验收标准 | 只读工具面，不能 patch |
| `RepairPlanner` | 基于 failure / finding 生成最小修复计划 | plan mode，只读工具 |

规划和通信方式：

```text
User task
  |
  v
Planner
  - 输入：原始任务 + 只读探索能力
  - 输出：<plan>，解析成 PlanStep[]
  |
  v
Plan Review
  - 输入：PlanStep[]
  - 输出：可执行计划或 abort reason
  |
  v
Executor
  - 输入：单个 active Step JSON + 原始任务
  - 输出：final summary + changed_files + status/error
  |
  v
Reviewer
  - 输入：原始任务 + plan + step_results + changed_files + git diff
  - 输出：ReviewFinding[]
  |
  +-- 无 blocking finding -> DONE
  |
  +-- 有 blocking finding / 执行失败
        |
        v
      RepairPlanner
        - 输入：失败 step、最近 step_results、review_findings、retry 计数
        - 输出：最小 repair plan，只取第一个 repair step
        |
        v
      Executor
```

通信载体不是自然语言随意转发，而是几个明确的数据结构：

| 上游 | 下游 | 传递内容 |
| --- | --- | --- |
| Planner | Executor | `PlanStep`：`id`、`goal`、`expected_change`、`acceptance_check` |
| Executor | Reviewer | `StepResult`：`status`、`changed_files`、`summary`、`error` |
| Workspace | Reviewer | `git diff`、`changed_files` |
| Reviewer | RepairPlanner | `ReviewFinding`：`severity`、`file`、`line`、`message`、`recommended_action` |
| RepairPlanner | Executor | 新的 repair `PlanStep` |

权限编排：

- `Planner` 和 `RepairPlanner` 临时开启 `plan_mode=True`，工具表过滤掉 risky tool，只允许只读探索。
- `Executor` 使用正常工具面，可以 `write_file`、`patch_file`、`run_shell`，但仍受 `approval_policy` 和 `HarnessPolicy` 限制。
- `Reviewer` 临时设置 `read_only=True`，并且只保留非 risky 工具；它可以读 diff、读文件、搜索，但不能修改。
- 每个阶段结束后都会恢复原来的 `plan_mode`、`read_only`、`tools` 和 `prefix`，避免角色权限泄漏到下一阶段。

状态转移：

```text
正常路径：
Plan -> Execute -> Review -> Done

执行失败：
Execute -> RepairPlan -> Execute

审查不通过：
Review -> RepairPlan -> Execute -> Review

超过重试：
Abort with reason
```

关键约束：

- `retry_limit` 默认 2。
- Reviewer 只能输出 finding。
- RepairPlanner 必须基于具体 failure / finding。
- Executor 每次围绕 active step 工作。
- Planner 输出会被 `parse_plan_steps()` 转成最多 8 个顶层步骤；repair plan 只取第一个最小修复步骤。
- critical / high / error / blocker 会被视为 blocking finding，触发 repair。
- 每个主要阶段都写入 trace/report。
- 不引入 LangGraph、AutoGen 或复杂图框架。

### 6.4 核心类型

本轮新增或调整的 orchestration 类型：

| 类型 | 最小字段 |
| --- | --- |
| `OrchestrationRun` | `task`、`state`、`plan_steps`、`step_results`、`review_findings`、`retries`、`final_answer` |
| `PlanStep` | `id`、`goal`、`allowed_tools`、`expected_change`、`acceptance_check` |
| `StepResult` | `step_id`、`status`、`changed_files`、`summary`、`error` |
| `ReviewFinding` | `severity`、`file`、`line`、`message`、`recommended_action` |

### 6.5 实机发现与修补

真实 `/orchestrate` 测试中，`qwen3.6:35b-a3b` 会把缩进 bullet 和 `Risks` 里的条目误当作执行步骤，导致一个只读检查任务被拆成 8 步。

本轮修补：

- `parse_plan_steps()` 只解析 `## Steps` 段。
- 只把顶层编号或顶层 bullet 当成 PlanStep。
- 缩进 bullet 和 `Risks / open questions` 不再当成 active step。

修补后的真实结果：

```text
Orchestration complete.
- state: DONE
- retries: 0/2
- steps: 3
- review_findings: 0
```

## 7. 记忆系统

### 7.1 三层记忆

Enigma 的记忆不是一个大摘要，而是三层结构：

| 层级 | 生命周期 | 解决的问题 |
| --- | --- | --- |
| 工作记忆 | 当前任务内高频更新 | 减少重复读文件、保留当前任务状态 |
| 情景记忆 | 会话内压缩笔记 | 记录失败、过程节点、长任务恢复线索 |
| 语义记忆 | 跨会话持久化 | 记录项目约定、用户偏好、依赖事实 |

这种分层让 prompt 可以按优先级裁剪，而不是把所有东西混成一个容易漂移的大摘要。

### 7.2 工作记忆

工作记忆包含：

- 当前任务摘要。
- recent files。
- 文件摘要。
- 最近工具结果。
- 情景笔记数量。
- 语义记忆主题概览。

文件摘要会和文件 SHA-256 绑定。如果文件改了，旧摘要不会进入 prompt，避免模型基于过时上下文决策。

### 7.3 情景记忆

情景记忆用于记录当前会话中重要但不一定长期成立的信息：

- 工具失败原因。
- repair 或 review 中发现的问题。
- 用户临时约束。
- 长任务过程节点。

它主要服务于长任务恢复和本轮推理，不一定跨会话保留。

### 7.4 语义记忆

语义记忆用于存储跨会话稳定事实，例如：

- Project convention。
- Key decision。
- Dependency fact。
- User preference。

存储位置：

```text
.enigma/memory/MEMORY.md
.enigma/memory/topics/*.md
```

### 7.5 MemoryNote 使用条件

**本轮补强**：长期记忆不只保存文本，还保存使用场景。

最小字段：

| 字段 | 作用 |
| --- | --- |
| `text` | 要记住的事实或约定 |
| `topic` | 所属主题 |
| `problem` | 这条记忆解决什么问题 |
| `applies_when` | 未来什么时候应该召回 |

示例：

```text
text: Use ruff for linting.
topic: project-conventions
problem: Avoid repeatedly asking which lint tool this repo uses.
applies_when: Running lint, fixing style failures, or adding tests.
```

这样可以回答“agent 没有读心术，怎么知道什么时候用记忆”的问题：记忆本身带触发场景。

### 7.6 记忆提升

语义记忆有两条提升路径：

| 路径 | 触发方式 | 成本 |
| --- | --- | --- |
| 用户显式触发 | 用户说“记住”“保存”等，模型 final 里出现结构化结论 | 纯字符串处理 |
| 自动反思 | compact 前、工具循环若干步后、ask 结束前 | 额外模型调用 |

自动反思子 agent 的原则：

- 只提取独立于本次会话也成立的事实。
- 没有值得记的内容可以返回空 JSON。
- 输出经过密钥、traceback、过长文本过滤。
- 每个 topic 有数量上限。
- 输入签名去重，避免同一次 ask 内重复反思。

### 7.7 BM25 召回

Enigma 保持零运行时依赖，因此没有引入 embedding 和向量库，而是用 BM25 / 关键词检索。

召回分两路：

- 情景记忆一路。
- 语义记忆一路。

合并时每路保底，避免大量情景笔记把语义记忆挤掉。

排序考虑：

- tag 精确匹配。
- BM25 分数。
- `applies_when` 匹配。
- 新旧程度。
- 插入顺序。

### 7.8 召回 query 怎么生成

**本轮补强**：相关记忆召回不再简单等于“拿本轮用户输入整句去搜”。

旧版问题：

```text
user_message -> tokenize -> BM25
```

这种写法能跑，但在面试时很容易被追问：如果用户说“那刚才那个怎么办”“继续”“this one 呢”，本轮输入本身没有足够关键词，直接搜会漏召回；如果盲目把全部 history 都拼进去，又会把无关历史噪音带进检索，导致误召回。

新版做法是在 `ContextManager.build()` 里先构造 `retrieval_query`，再把它传给 `memory.retrieval_candidates()`：

```text
当前请求
  |
  v
判断是否是上下文依赖型请求
  - 短追问
  - this / that / continue / previous
  - 刚才 / 这个 / 那个 / 继续 / 上次
  |
  +-- 否 -> 只用当前请求召回
  |
  +-- 是 -> 构造扩展 retrieval_query
          - current_request
          - 最近 6 条 history
          - working.task_summary
          - recent_files
          - 最近 process / error notes
```

这里有一个刻意的取舍：**不是所有请求都无脑拼 history**。如果用户的当前请求已经很明确，例如“Finish the README update after prompt compression creates a checkpoint”，就只用当前请求做召回，避免历史里大量重复词把无关记忆拉进来。如果用户明显在追问上下文，例如“what about that?” 或“继续”，才启用扩展 query。

工程上可以解释成“两级策略”：

| 场景 | 策略 | 原因 |
| --- | --- | --- |
| 当前请求语义明确 | `strategy=current_request` | 减少历史噪音，保持 BM25 精准 |
| 短追问 / 代词 / 继续类请求 | `strategy=expanded` | 用最近对话和任务状态补全省略信息 |

扩展 query 仍然是确定性构造，不额外调用模型，所以不会增加延迟和成本。后续如果要加 LLM query rewriter，可以把它作为第三路补充，而不是替代这个确定性兜底。

面试时可以这样讲：

> 记忆召回的 query 不是大模型临场“想一个关键词”，也不是永远拿用户原话去搜。Enigma 现在先判断本轮请求是不是上下文依赖型。如果是明确任务，就用当前请求，避免历史噪音；如果是“刚才那个”“continue”这类追问，就把最近 history、task summary、recent files 和错误过程笔记拼成扩展 query。这样既能处理追问，又不会把所有历史都变成检索噪音。

如果面试官问“这是不是一个专门的 memory agent”，回答：

> 当前实现不是单开一个 LLM 子 agent，而是一个轻量 retrieval planner。它在 ContextManager 里确定性地产生 retrieval_query。这样更便宜、更可测，也避免 query rewrite 本身被模型写错。未来可以在这个 planner 旁边加一个可选小模型 query rewrite，形成 raw query + deterministic context query + LLM rewrite 的多路召回。

可验证证据：

- `tests/test_context_manager.py` 覆盖 `what about that?` 通过最近 history 召回。
- `tests/test_context_manager.py` 覆盖 `continue` 通过 task summary / recent files 召回。
- `prompt_metadata["retrieval_query"]` 会记录本轮实际 query、策略、query 长度、history 条数、recent files 和 process note 数量。

### 7.9 记忆进入 prompt 的方式

prompt 中记忆分成两类：

```text
memory
  - 当前任务
  - recent files
  - file summaries
  - 记忆主题概览

relevant_memory
  - 本轮请求召回到的情景记忆
  - 本轮请求召回到的语义记忆
  - applies_when / problem 提示
```

这是一种“全景 + 焦点”的结构：工作记忆提供当前状态，相关记忆提供本轮需要的具体提示。

## 8. Prompt 与上下文预算

### 8.1 Prompt 结构

Enigma 的 prompt 不是简单拼接，而是分段预算：

| Section | 内容 | 压缩优先级 |
| --- | --- | --- |
| `prefix` | 身份、规则、工具列表、工作区快照 | 最后压缩 |
| `startup_memory` | `MEMORY.md` 启动记忆 | 靠后压缩 |
| `memory` | 工作记忆概览 | 中等 |
| `relevant_memory` | BM25 召回片段 | 最先压缩 |
| `rolling_summary` | compact 产生的会话摘要 | 中等 |
| `history` | 对话历史和工具结果 | 第二压缩 |
| `current_request` | 用户当前请求 | 永不裁剪 |

拼接顺序：

```text
prefix
startup_memory
memory
relevant_memory
rolling_summary
history
current_request
```

当前请求放最后，因为它是本轮最重要的信号。

### 8.2 预算计算

模型上下文窗口由 `models.py` 中的前缀映射决定：

- OpenAI。
- Claude。
- Gemini。
- Qwen。
- DeepSeek。
- Kimi。
- Llama。
- GLM。
- 其他未知模型 fallback 32K。

匹配方式是最长前缀优先。总 prompt 预算约为上下文窗口的 75%，预留输出和系统开销。

### 8.3 压缩策略

压缩不是粗暴截断，而是分层收口：

1. 工具结果先裁剪。
2. 记忆层有容量上限。
3. history 分类压缩。
4. prompt section 按优先级让位。
5. 超出阈值时触发 compact。

history 压缩细节：

- 最近几条保留较多细节。
- 多次读取同一文件时，旧的 `read_file` 可用摘要替代。
- `run_shell` 压缩为命令和 stdout 前几行。
- 其他旧记录压缩成短摘要。

### 8.4 召回与预算的关系

`relevant_memory` 是按需召回出来的片段，不是固定塞满 prompt 的历史。它有独立预算，而且在超预算时最先让位：

```text
relevant_memory -> history -> memory -> rolling_summary -> startup_memory -> prefix
```

这样设计的原因是：

- 当前请求永远最重要，不能被记忆挤掉。
- 召回记忆是“候选提示”，不是硬事实全集。
- 如果 prompt 太长，宁愿少带几条相关记忆，也不能破坏工具协议、路径安全和当前任务。

本轮新增的 `retrieval_query` 也不会直接进入 prompt 正文，它只用于决定哪些记忆进入 `relevant_memory`。真正放进 prompt 的仍然是召回后的短 note。调试和面试展示时，可以看 `prompt_metadata["retrieval_query"]`：

```json
{
  "strategy": "expanded",
  "history_entries": 2,
  "recent_files": ["enigma/runtime.py"],
  "has_task_summary": true,
  "process_notes": 0,
  "query_chars": 180
}
```

这可以回答“你怎么证明不是黑箱召回”：每一轮实际用什么 query、有没有走扩展、带了几条 history，都会落到 metadata 里。

### 8.5 Prompt Cache

`prefix` 是最稳定的一段，包括：

- agent 身份。
- 工具手册。
- 工具调用格式。
- 工作区快照。

Enigma 用 prefix hash 作为 cache key。history 变化不会影响 prefix cache，只有 workspace fingerprint 或工具签名变化时才重建 prefix。

## 9. Compact 与滚动摘要

`/compact [focus]` 用于把长会话压缩成更短的上下文。

压缩产物包括：

- rolling summary。
- 压缩后的情景记忆。
- 保留的 head / tail history。

一个重要修正是：compact 不再重写 durable memory。

旧问题：

```text
让模型在 compact 时重写语义记忆，会导致早期稳定约定被模型遗忘后删除。
```

新设计：

- durable memory 的权威源是磁盘上的 `MEMORY.md` / `topics/`。
- compact 只更新会话 rolling summary 和情景层。
- 下一轮 prompt 重新从磁盘注入稳定记忆。

这样能避免长会话多次 compact 后语义记忆越来越少。

## 10. 会话、运行工件与恢复

### 10.1 两层持久化

Enigma 有两层持久化：

| 层 | 位置 | 用途 |
| --- | --- | --- |
| SessionStore | `.enigma/sessions/*.json` | 当前会话关键路径，保存 history、memory、checkpoints |
| SessionDB | `.enigma/sessions/state.db` | 跨会话搜索、rolling summary、FTS5 |

SessionStore 是主流程必要依赖。SessionDB 是增强路径，失败时可以降级，不影响 agent 运行。

### 10.2 TaskState

每次 `ask()` 都会创建或更新 `TaskState`：

- `task_id`
- `run_id`
- `user_request`
- `status`
- `step_count`
- `stop_reason`
- `last_tool`
- `created_checkpoints`
- `final_answer`

它让一次任务的状态可以在结束后复盘。

### 10.3 RunStore

每次运行写入：

```text
.enigma/runs/<run_id>/
  task_state.json
  trace.jsonl
  report.json
```

用途：

- `task_state.json`：任务最终状态。
- `trace.jsonl`：每次状态转移、工具调用、审批、错误、review、orchestration 事件。
- `report.json`：最终总结和可读报告。

### 10.4 Checkpoint

checkpoint 是恢复锚点，记录：

- key files。
- 文件 freshness。
- runtime identity。
- workspace fingerprint。
- tool signature。
- parent checkpoint。

恢复时评估：

| 状态 | 含义 |
| --- | --- |
| `no-checkpoint` | 没有可用 checkpoint |
| `full-valid` | 工作区和 runtime 仍匹配 |
| `partial-stale` | 部分文件 freshness 失效 |
| `workspace-mismatch` | cwd、model、tools、policy 等发生变化 |
| `schema-mismatch` | checkpoint schema 版本不匹配 |

这样可以避免 agent 把旧上下文当成可信事实继续执行。

## 11. REPL、UX 与 Skills

### 11.1 REPL

交互式 REPL 使用双线程：

- 主线程处理键盘输入、spinner、暂存消息、Ctrl+C。
- 工作线程执行 `agent.ask()`。

agent 运行中用户输入不会丢失，而是进入暂存消息队列，当前任务结束后自动发送。

斜杠命令：

| 命令 | 作用 |
| --- | --- |
| `/help` | 查看帮助 |
| `/memory` | 查看当前工作记忆 |
| `/session` | 查看 session 文件 |
| `/reset` | 清空当前 session |
| `/compact [focus]` | 压缩上下文 |
| `/plan <task>` | 只读规划 |
| `/orchestrate <task>` | Plan / Execute / Review / Repair 编排 |
| `/review [target]` | 审查变更 |
| `/skills` | 列出 skills |
| `/<skill-name> [args]` | 调用 skill |
| `/exit` | 退出 |

### 11.2 终端显示

Enigma 会显示：

- 工具调用活动。
- 步骤进度。
- 成功 / 失败 / 部分成功状态。
- `write_file` / `patch_file` 的 unified diff。
- streaming token 输出。

### 11.3 Skill 系统

Skill 系统提供可复用任务模板：

- 内置 skills：`enigma/skills/`。
- 用户级 skills：`~/.enigma/skills/`。
- 项目级 skills：`.enigma/skills/`。

加载方式是渐进的：

- metadata 始终可见。
- `SKILL.md` 按需读取。
- references 目录在需要时读取。

这避免把所有 skill 内容一次性塞进 prompt。

## 12. 跨会话记忆与 SessionDB

### 12.1 设计灵感

跨会话记忆借鉴两个方向：

- Claude Code 的 `CLAUDE.md` / auto memory：Markdown-first、可人工审计。
- Hermes 的 persistent memory：稳定事实、用户偏好和会话检索分层。

Enigma 的实现更轻量：

- `MEMORY.md` 和 topics 存稳定事实。
- SessionDB 用 SQLite + FTS5 搜历史。
- rolling summary 存每个 session 的压缩摘要。
- 不引入 embedding 和向量数据库。

### 12.2 SessionDB

SessionDB 主要保存：

- sessions。
- messages。
- message FTS。
- session_state。

支持：

- 按关键词搜索历史消息。
- 按 session 过滤。
- 保存 rolling summary。
- 镜像当前 session history。

### 12.3 MEMORY.md

`MEMORY.md` 是短启动记忆，限制为 200 行 / 25KB，避免启动 prompt 被长期记忆撑爆。

topics 文件用于按需召回更细的主题内容：

```text
.enigma/memory/topics/project-conventions.md
.enigma/memory/topics/key-decisions.md
.enigma/memory/topics/dependency-facts.md
.enigma/memory/topics/user-preferences.md
```

## 13. 测试、评测与可验证证据

### 13.1 单元测试

测试使用 `FakeModelClient`，不依赖真实模型调用。

当前 `tests/` 下有 13 个 `test*.py` 文件，按 `def test_` 统计约 231 个测试用例。它们不是只测函数输入输出，而是围绕 coding agent harness 的关键不变式组织：模型输出可控、工具边界可控、运行产物可复盘。

覆盖方向：

- runtime 主循环。
- tool parse。
- path safety。
- approval denial。
- secret redaction。
- plan mode。
- review。
- memory。
- compact。
- SessionDB。
- orchestration。

文件级覆盖可以这样理解：

| 测试文件 | 重点内容 | 面试时回答什么问题 |
| --- | --- | --- |
| `tests/test_enigma.py` | 最大测试集，覆盖 `Enigma.ask()` 主循环、`<tool>/<final>` 解析、工具调用、session 保存恢复、delegate、web_search、模型 client、checkpoint、compact、durable memory、trace/report 脱敏 | “这个 agent 不是只写了 prompt，主循环和状态恢复都有回归测试” |
| `tests/test_safety_invariants.py` | 路径逃逸、symlink 穿越、risky tool 审批、只读 delegate、shell 环境变量白名单、密钥脱敏 | “文件和 shell 权限是 harness 硬边界，不靠模型自觉” |
| `tests/test_context_manager.py` | prompt section 顺序、预算压缩、相关记忆 BM25 召回、历史裁剪、重复文件读取摘要 | “长上下文不是简单截断，而是分区预算和降级策略” |
| `tests/test_memory.py` | 工作记忆、情景记忆、文件摘要 freshness、shell 失败摘要、durable memory topic 读写、去重和上限 | “记忆分层，而且有 freshness 和 topic contract” |
| `tests/test_plan_mode.py` | `<plan>` 解析、Plan 模式工具过滤、只读 prefix、审批后执行、delegate 不继承 plan mode、plan 文件持久化 | “规划阶段和执行阶段权限不同，计划不会悄悄写文件” |
| `tests/test_review.py` | git repo 检测、git diff、changed files、review prompt、review command | “review 不是随口问模型，会带 diff 和结构化上下文” |
| `tests/test_orchestration.py` | plan step 解析、blocking finding、Plan -> Execute -> Review -> Repair 状态机、retry limit、Reviewer 只读工具面 | “multi-agent 是 bounded 状态机，不是不可控群聊” |
| `tests/test_skills.py` | skill frontmatter、三源发现、目录式 skill、metadata block、skill prompt 构建 | “技能系统是按需加载，不把所有模板塞进 prompt” |
| `tests/test_session_db.py` | SQLite schema、消息落库、FTS 搜索、rolling summary、startup memory 注入、session 隔离 | “跨会话检索和摘要是可落盘、可搜索的” |
| `tests/test_evaluator.py` | benchmark schema 校验、fixture copy、run directory、结果 metadata、failure category 汇总 | “固定评测不是人工点一点，而是可重复 harness” |
| `tests/test_metrics.py` | context / memory / recovery ablation 产物、核心报告指标 | “项目能做消融实验，不只报最终成功/失败” |
| `tests/test_run_store.py` | run 目录、`task_state.json`、`trace.jsonl`、`report.json` | “每次运行都有审计工件” |
| `tests/test_task_state.py` | TaskState 初始状态、成功状态、step/retry limit、snapshot | “任务生命周期是显式状态机” |

如果面试官问“为什么用 FakeModelClient”，回答重点是：这里测的是 harness 稳定性，不是评测某个大模型能力。真实模型有随机性和网络变量，适合做端到端 sanity check；但路径安全、审批、恢复、trace 这些工程不变式必须用确定性模型输出来压住。

### 13.2 BenchmarkEvaluator

固定 benchmark 从 `benchmarks/coding_tasks.json` 读取任务，把 fixture repo 复制到临时 workspace，然后用 FakeModelClient 确定性执行。

当前固定 benchmark 有 12 个任务，使用两个极小 fixture：

- `tests/fixtures/bench_repo_readme/README.md`：一个 README 占位文档，包含 placeholder opening sentence 和两条 placeholder notes。
- `tests/fixtures/bench_repo_patch/sample.txt`：四行文本 `alpha / beta / gamma / placeholder`，用于测试精确文本替换和恢复路径。

测试集构建原则：

- 每个任务必须有独立 fixture repo，运行前复制到临时目录，避免 benchmark 之间互相污染。
- 每个任务必须声明 `prompt`、`fixture_repo`、`allowed_tools`、`step_budget`、`expected_artifact`、`verifier` 和 `category`。
- verifier 用本地命令验证最终状态，不只看模型 final 文本。
- FakeModelClient 负责回放确定性模型输出，让 benchmark 测 runtime 和 harness，不被真实模型随机性干扰。
- setup 字段用于构造特殊运行前状态，例如塞入历史、制造 stale freshness、模拟 workspace mismatch 或压缩压力。

任务 schema 可以概括为：

```json
{
  "id": "sample_beta_locked",
  "prompt": "In sample.txt, replace beta with beta-locked.",
  "fixture_repo": "tests/fixtures/bench_repo_patch",
  "allowed_tools": ["read_file", "patch_file"],
  "step_budget": 4,
  "expected_artifact": "sample.txt contains beta-locked",
  "verifier": "python3 -c \"...assert...\"",
  "category": "text-edit"
}
```

当前固定任务覆盖 5 类能力：

| category | 测什么 | 代表任务 |
| --- | --- | --- |
| `documentation` | README 级文本修改，验证基本读文件 + patch 闭环 | `readme_intro_locked`、`readme_schema_note` |
| `text-edit` | 精确文本替换，验证 `patch_file` 的最小修改能力 | `sample_beta_locked`、`sample_gamma_locked` |
| `tool-boundary` | 工具参数错误、路径逃逸、重复调用后能否恢复 | `invalid_patch_recovery`、`path_escape_recovery`、`repeated_read_recovery` |
| `recovery` | prompt 压缩 checkpoint、freshness mismatch、workspace mismatch | `context_reduction_checkpoint`、`freshness_reanchor_resume`、`workspace_mismatch_resume` |
| `durable-contract` | 稳定记忆提升与拒绝规则是否正确落盘和记录 | `durable_promotion_accept`、`durable_promotion_reject` |

逐项展开如下：

| 任务 id | 做什么 | 主要验证点 |
| --- | --- | --- |
| `readme_intro_locked` | 把 README 开头占位句替换为固定 benchmark 文案 | 基本 read + patch + verifier 闭环 |
| `readme_schema_note` | 把 README 第一条 bullet 改成 schema / baseline 固定说明 | 文档局部修改，验证最小补丁 |
| `sample_beta_locked` | 把 `sample.txt` 中的 `beta` 改成 `beta-locked` | 精确文本替换，确保旧 `beta` 不残留 |
| `sample_gamma_locked` | 把 `gamma` 改成 `gamma-locked` | 同类文本编辑回归 |
| `invalid_patch_recovery` | 先制造无效 patch 参数，再恢复完成 README patch | 工具参数错误后的恢复能力 |
| `path_escape_recovery` | 模拟路径逃逸被拒绝后，仍完成 `sample.txt` patch | path safety 不是终止任务，而是可恢复错误 |
| `repeated_read_recovery` | 模拟重复 identical read 被拒绝后，仍完成 patch | 重复调用防护能促使模型换策略 |
| `context_reduction_checkpoint` | 构造低预算和大量历史，让 prompt compression 触发 checkpoint | context reduction 和 checkpoint 事件落 trace/report |
| `freshness_reanchor_resume` | 构造 stale 文件摘要，恢复时重新锚定真实文件 | resume 状态应为 `partial-stale`，不能信旧摘要 |
| `workspace_mismatch_resume` | 构造 runtime/workspace identity 漂移 | resume 状态应为 `workspace-mismatch`，并记录 mismatch 事件 |
| `durable_promotion_accept` | 把稳定 benchmark facts 提升为长期记忆 | durable promotions 写入 topics 文件和 report |
| `durable_promotion_reject` | 只接受稳定事实，拒绝 transient 或 secret-shaped 内容 | durable rejections 记录原因，防止污染长期记忆 |

一次 benchmark 的执行流程：

```text
读取 coding_tasks.json
  |
  v
复制 fixture_repo -> 临时 workspace
  |
  v
按 setup 注入历史 / 记忆 / checkpoint / stale 文件等前置状态
  |
  v
用 FakeModelClient 创建 Enigma
  |
  v
执行 agent.ask(prompt)，受 step_budget 限制
  |
  v
运行 verifier 命令检查文件、trace、report、memory 等真实产物
  |
  v
记录 pass rate、verifier pass rate、tool steps、stop reason、trace/report metadata
```

这里刻意把“模型能力评测”和“harness 回归评测”拆开。固定 benchmark 主要回答：在模型输出可控的情况下，Enigma 的工具边界、恢复机制、记忆持久化和运行工件是否稳定。真实模型端到端能力另用 provider 对比和实机 sanity check 验证。

记录：

- pass rate。
- verifier pass rate。
- step budget。
- task state。
- trace。
- reproducibility metadata。

### 13.3 Metrics

`metrics.py` 提供：

- 消融实验。
- 安全性测试。
- provider 对比。
- 上下文压力测试。
- 运行工件聚合。

更具体地说，metrics 层不是单个“分数”，而是几组可解释实验：

| 实验方向 | 目的 |
| --- | --- |
| feature ablation | 关闭 memory、relevant_memory、context_reduction、prompt_cache 等 feature flag，观察 prompt 尺寸、召回效果和任务结果变化 |
| memory recall | 给定事实先写入记忆，再加入噪音历史，验证后续问题是否能少读文件或正确召回 |
| security scenarios | 覆盖路径穿越、空命令、timeout 越界、read_only 写入、delegate 空任务、重复工具调用等安全不变式 |
| provider comparison | 在真实 provider 上跑同一类任务，比较工具调用成功率、步数、失败类型和输出稳定性 |
| context stress | 构造长 history、大量 notes 和低预算，验证 prompt reduction、compact、checkpoint 是否按预期触发 |
| recovery benchmark | 构造 checkpoint、freshness、runtime identity 差异，验证 resume 状态是否被正确分类 |

评测输出不只看 final answer，而是同时检查：

- 文件内容是否真的变成目标状态。
- `.enigma/runs/<run_id>/trace.jsonl` 是否包含期望事件。
- `.enigma/runs/<run_id>/report.json` 是否记录 stop reason、checkpoint、durable promotions / rejections、prompt metadata。
- `.enigma/memory/topics/*.md` 是否按 durable memory 规则更新。
- 工具调用次数是否在 `step_budget` 内。
- 出错后是否进入预期恢复路径，而不是直接 hallucinate 成功。

### 13.4 本轮验证

本轮验证结果：

```bash
uv run pytest -q
# 242 passed

uv run ruff check .
# All checks passed!
```

真实 Ollama 工具链：

```bash
uv run enigma --provider ollama --model qwen3.6:35b-a3b --approval never --max-steps 4 --max-new-tokens 512 \
  "请先调用 read_file 工具读取 README.md 的第 1 到 20 行，然后根据工具结果用中文简要总结这个项目是什么。必须先调用工具，不要直接回答。"
```

验证结果：

```text
model -> read_file tool -> model -> final
```

真实 `/orchestrate`：

```bash
uv run enigma --provider ollama --model qwen3.6:35b-a3b --approval never --max-steps 6 --max-new-tokens 700 \
  "/orchestrate 只读检查 README.md 的项目定位，并给出是否符合 coding agent harness 叙事的简短结论。不要修改任何文件。"
```

验证结果：

```text
Orchestration complete.
- state: DONE
- retries: 0/2
- steps: 3
- review_findings: 0
```

## 14. 设计取舍与局限

### 14.1 为什么保持轻量

Enigma 没有引入 LangGraph、AutoGen、Chroma、FAISS、embedding 模型等重依赖。原因是它的目标不是做企业级 agent 平台，而是展示一个本地 coding agent harness 的关键边界：

- 工具安全。
- prompt 预算。
- 记忆触发。
- 状态恢复。
- 可追踪闭环。

轻量实现更容易在面试中讲清楚，也更容易测试。

### 14.2 已知局限

- BM25 对同义词和跨语言语义召回有限。
- token 估算不是 tokenizer 级别精确。
- 跨会话搜索目前主要是 FTS5 关键字。
- rolling summary 主要在 compact 时更新。
- 自动反思质量依赖底模型。
- orchestration 是 bounded 状态机，不适合复杂开放任务图。

### 14.3 可扩展方向

- 加 embedding 作为 BM25 的双路补充。
- 给 memory note 增加 `last_used_at` 和衰减机制。
- 将 SessionDB 扩展为多 workspace 共享。
- 给 orchestration 增加更细的 step verifier。
- 给 reviewer finding 增加结构化 JSON 输出。
- 增加真实模型 provider 的集成测试基准。

***

# 第二部分：面试表达与答辩材料

## 15. 项目总述

可以这样开场：

> Enigma 是我做的一个本地 coding agent harness。最开始我更关注功能复刻，比如工具调用、记忆、plan mode。后来我意识到 agent 项目不能只讲“我做了三层记忆”，而要讲它解决什么真实问题。所以我把它收敛成三个重点：确定性规则放到工具和 policy 层，长期记忆记录使用场景，多智能体部分用 Plan / Execute / Review / Repair 的轻量状态机形成闭环。

更短版本：

> Enigma 是一个本地 coding agent harness，重点是让 agent 能安全、稳定、可追踪地完成本地仓库任务。

## 16. 核心亮点速查

| 亮点 | 一句话概括 | 章节 |
| --- | --- | --- |
| **本轮补强：Harness Policy** | 把 UTF-8、路径安全、审批、plan/reviewer 工具面放到硬约束层 | 第 5 章 |
| **本轮补强：Orchestration** | `/orchestrate` 用 Planner / Executor / Reviewer / RepairPlanner 形成有回退的闭环 | 第 6 章 |
| **本轮补强：Memory applies_when** | 长期记忆不只存文本，还存未来什么时候召回 | 第 7 章 |
| **本轮补强：Retrieval query** | 追问类请求会结合 history / task / files 构造召回 query，明确任务避免历史噪音 | 第 7 章、第 8 章 |
| 工具安全护栏 | 白名单、schema、路径穿越、审批、重复调用、diff、trace | 第 5 章 |
| 上下文预算 | 六段式 prompt、模型窗口自动匹配、分类压缩 | 第 8 章 |
| 分层记忆 | 工作 / 情景 / 语义三层生命周期 | 第 7 章 |
| 跨会话记忆 | `MEMORY.md`、topics、SessionDB + FTS5、rolling summary | 第 12 章 |
| 会话恢复 | checkpoint + freshness + runtime_identity | 第 10 章 |
| 评测证据 | FakeModelClient、benchmark、unit tests、trace/report | 第 13 章 |

## 17. 面试官问题回应

### 17.1 “你的项目像练手复刻，真实问题是什么？”

回答：

> 我承认最早版本更像练手复刻。后来我把它重新收敛成 coding agent harness：解决长任务重复探索、本地工具边界、跨会话重复教学、计划执行审查混在一起难追踪这些真实问题。现在 README 和设计文档都按 Problem -> Design -> Evidence 组织。

可以展开：

- 重复探索 -> 工作记忆和文件摘要。
- 工具边界 -> HarnessPolicy 和审批策略。
- 跨会话教学 -> durable memory 和 applies_when。
- 长任务闭环 -> `/orchestrate` 状态机。

### 17.2 “为什么不是只用 prompt？”

回答：

> prompt 是软约束。像 UTF-8、路径安全、危险工具审批这种明确规则，不应该靠模型记住，而应该由 harness 层硬编码。prompt 适合表达任务目标和需要语义判断的规则，policy 适合表达确定性边界。

对应代码：

- `enigma/policy.py`
- `enigma/tools.py`
- `runtime.approve()`
- `Enigma.path()`

### 17.3 “你的记忆系统解决什么问题？”

回答：

> 工作记忆解决重复探索，情景记忆解决长任务恢复和失败追踪，语义记忆解决跨会话重复教学。新版里每条长期记忆还会记录 `problem` 和 `applies_when`，也就是它解决什么问题、未来什么时候应该被召回。

不要只说“三层记忆”，要说每一层的触发条件和使用场景。

### 17.4 “记忆召回的关键词是谁生成的？会不会写错？”

短回答：

> Enigma 不依赖模型手写一个“召回关键词”。召回 query 由 runtime 在 `ContextManager` 里确定性构造：明确任务用当前请求；短追问、代词或“继续”这类上下文依赖请求，会额外拼最近 history、task summary、recent files 和 process notes。模型可以影响记忆正文怎么写，但召回 query 本身不是模型临时写的，所以不会因为模型忘写关键词就完全失效。

展开回答：

```text
用户输入
  |
  +-- 明确任务 -> current_request query
  |
  +-- 追问/省略 -> expanded query
        - current_request
        - recent history
        - working.task_summary
        - recent_files
        - process/error notes
```

如果面试官继续追问“那为什么不总是把所有 history 拼进去”，回答：

> 因为检索 query 不是越长越好。history 里会有 benchmark、日志、重复工具输出这些噪音。无脑拼进去会让 BM25 命中无关记忆，甚至把 prompt 撑大。所以 Enigma 只在当前请求明显依赖上下文时扩展 query；普通明确任务保持 current_request 策略。

如果面试官问“这是不是应该单开一个子 agent”，回答：

> 成熟产品可能会有 memory agent 或 query rewriter，但我这里先做成 deterministic retrieval planner。原因是它便宜、可测、可解释，而且不会把召回成败押在另一个 LLM 输出上。未来可以加一个可选 LLM query rewrite，但它应该是多路召回的一路，而不是唯一入口。

一句话收束：

> 召回 query 是 harness 生成的，不是模型自由发挥；模型负责理解和使用召回结果，harness 负责让召回有可解释兜底。

### 17.5 “和 Claude Code / Hermes 比，你这个记忆系统有什么自己的点？”

短回答：

> 我没有把目标定成复刻成熟产品。Claude Code 的强项是 Markdown-first 的项目记忆和 auto memory，Hermes 的强项是长期个人 agent 的 persistent memory、用户偏好和会话检索。Enigma 取的是中间路线：长期事实仍然落在 Markdown，方便审计和人工修改；运行时再分 working / episodic / semantic 三层；召回不用黑箱向量库，而是 BM25 / FTS5；最关键的是每条长期记忆带 `problem` 和 `applies_when`，同时 runtime 会根据追问类型构造 retrieval query，所以它不是“记住一句话”，而是知道这条记忆未来解决什么问题、什么时候更应该被召回。

展开对比：

| 维度 | Claude Code | Hermes | Enigma 怎么取舍 |
| --- | --- | --- | --- |
| 稳定记忆载体 | `CLAUDE.md`、auto memory 的 `MEMORY.md` 和 topic files，Markdown 可读可改 | `MEMORY.md` / `USER.md` 加会话检索，偏长期个人 agent | 学 Claude Code 的 Markdown-first，把稳定事实存在 `.enigma/memory/`，便于审计和删除 |
| 自动沉淀 | Auto memory 会自动写入/召回项目经验 | 会学习环境、偏好、过往任务，甚至沉淀技能 | 有自动反思，但触发保守：process note、compact 前、用户明确“记住”时才提升 |
| 用户画像 | 更偏项目指令和工作流，不把所有东西都画像化 | `USER.md` 明确承载用户偏好、沟通风格、长期习惯 | 不做标准化 KV profile，只保留 `user-preferences` topic，避免把临时要求误写成永久人格标签 |
| 检索方式 | 启动加载索引，topic 按需读取 | SQLite / FTS5、episodic recall、插件或向量检索更完整 | BM25 + FTS5，零运行时依赖；明确任务用当前请求，追问类请求扩展 history / task / files |
| 记忆可信度 | Markdown 易审计，但自动记忆需要人工清理 | 召回和画像更强，但系统复杂度更高 | `problem` / `applies_when` 让每条 durable note 自带触发条件，可解释为什么进入 prompt |

为什么不做标准化 KV 用户画像：

> coding agent 的关键不是猜用户人格，而是稳定地记住项目约定、架构决策、依赖事实和失败经验。KV profile 容易过拟合一次对话，比如用户这次说“简短点”，不等于永久 `user.prefers_short_answers=true`。Markdown + 轻结构更适合本地仓库任务：用户可以直接 diff、删除、改写；同时 `MemoryNote` 仍然有 `topic`、`problem`、`applies_when`，不是完全散文。

可以收束成一句：

> Claude Code 给我的启发是 repo-native、Markdown-first、可审计；Hermes 给我的启发是长期 agent 需要把稳定事实、会话经验和检索分层。Enigma 的原创点是把这两者收缩成一个小而可测的 coding harness：Markdown 是权威源，BM25 / FTS5 是可解释召回，`applies_when` 是记忆触发条件，`retrieval_query` 是运行时可观测的召回决策。

### 17.6 “这算 multi-agent 吗？是不是太轻？”

回答：

> 它不是开放式 swarm，而是 bounded multi-agent orchestration。Planner、Executor、Reviewer、RepairPlanner 有明确职责、工具边界和状态转移。它和普通 pipeline 的区别是：执行失败会回到 RepairPlanner，review 不通过也会回到 RepairPlanner，超过 retry limit 会 abort。

强调：

- 有角色边界。
- 有共享 task state。
- 有非线性回退。
- 有 retry limit。
- 有 trace/report。

### 17.7 “为什么不用 LangGraph / AutoGen？”

回答：

> 这个项目的目标是展示本地 coding agent harness 的关键工程边界，不是搭一个复杂 agent graph 平台。引入 LangGraph 会让面试重点变成框架使用，而不是我自己对工具边界、状态机、记忆和 trace 的理解。所以我刻意做了一个小而可测的状态机。

### 17.8 “为什么适配 Apple LLM/Agent Intern？”

回答：

> Apple 岗位强调 LLM/Agent practical applications、multi-agent systems、backend integration、performance and efficiency。Enigma 对应的是应用层 agent harness：模型后端集成、工具安全、上下文预算、记忆召回、轻量多智能体编排和真实调用验证。

## 18. 本轮补强清单

| 文件 | 本轮改动 | 回应的问题 |
| --- | --- | --- |
| `enigma/policy.py` | 新增 `HarnessPolicy` / `ToolPolicy` | 不只靠 prompt |
| `enigma/orchestration.py` | 新增 Plan / Execute / Review / Repair 状态机 | multi-agent 太轻 |
| `enigma/cli.py` | 新增 `/orchestrate <task>` | 可实际调用 |
| `enigma/memory.py` | durable memory 支持 `problem` / `applies_when` | 记忆触发条件不清 |
| `enigma/context_manager.py` | 召回记忆时展示适用场景；新增 `retrieval_query` 构造，追问类请求会结合 history / task / files | 记忆如何进入 prompt、召回 query 谁生成 |
| `enigma/tools.py` | delegate 视为 risky，plan mode 工具面更严格 | 工具边界 |
| `tests/test_orchestration.py` | 新增 orchestration 状态机测试 | 闭环可验证 |
| `tests/test_memory.py` | 补强 memory applies_when 测试 | 记忆可验证 |
| `tests/test_context_manager.py` | 补强 follow-up retrieval、task summary / recent files 召回测试 | 召回 query 可验证 |
| `tests/test_plan_mode.py` | 补强只读工具面测试 | plan mode 安全 |
| `tests/test_safety_invariants.py` | 补强 policy / safety 测试 | harness 硬约束 |

## 19. 常见追问速答

**Q：`ask()` 一轮发生什么？**

A：感知 -> 决策 -> 行动 -> 记录。先由 ContextManager 组 prompt，再调用模型，parse 解析 `<tool>` / `<plan>` / `<final>`，工具走安全流水线，最后写 history、memory、trace、report。

**Q：为什么限制工具步数？**

A：防止模型跑飞。`max_steps` 限制工具次数，`attempts` 限制格式错误重试次数。

**Q：Prompt cache 怎么做？**

A：prefix 是最稳定的段，取 prefix hash 作为 cache key。history 变化不影响 cache，workspace fingerprint 或工具签名变化才重建。

**Q：文件改了，旧摘要怎么办？**

A：file summary 和 SHA-256 freshness 绑定。下一轮 prompt 前如果 hash 不匹配，摘要失效。

**Q：语义记忆会无限增长吗？**

A：不会。每个 topic 有上限，写入时过滤密钥、traceback、过长文本，并按 subject 去重或替换。

**Q：为什么 BM25 不用 embedding？**

A：项目目标是零依赖，本地 coding agent 的召回很多是文件名、API、类名、工具名，BM25 对关键字足够有效。embedding 可以作为后续双路召回扩展。

**Q：BM25 的 query 是用户整句话吗？**

A：不是简单整句硬搜。明确任务使用当前请求；短追问、代词和“继续/刚才/that/this”这类请求会构造扩展 query，把最近 history、task summary、recent files 和 process notes 加进去。这样既能处理上下文追问，又避免所有任务都被历史噪音污染。

**Q：如果模型没写关键词，记忆是不是就召不回来？**

A：召回 query 不是模型写的，是 `ContextManager` 确定性生成的。模型可能影响记忆正文质量，但不会因为它忘了写一个关键词就没有 query。后续可以加 LLM query rewrite，但只作为补充一路。

**Q：`/compact` 有什么坑？**

A：不能让模型重写 durable memory，否则稳定记忆会被摘要漂移磨掉。新版 compact 只更新 rolling summary 和情景层，稳定记忆从磁盘重新注入。

**Q：工具安全怎么保证？**

A：白名单、schema、路径锚定、重复调用检测、审批策略、执行前后快照、secret redaction、trace metadata。

**Q：会话恢复怎么判断旧 checkpoint 可信？**

A：看 freshness、runtime identity、workspace fingerprint、tool signature、schema version。匹配才 full-valid，不匹配就记录 stale 或 mismatch。

**Q：怎么保证迭代不回退？**

A：单元测试覆盖核心不变式，FakeModelClient 做确定性模型输出，benchmark 和 metrics 做可重复实验，真实模型调用做端到端 sanity check。

## 20. 自我评价

### 优点

- 单仓库 Python 实现，面试官可以直接读代码和跑测试。
- 核心路径不依赖外部框架，能讲清楚 harness 内部怎么工作。
- 安全、记忆、上下文、恢复、编排都有可验证测试。
- 本轮补强后，项目叙事从“功能复刻”转为“问题驱动的 agent harness”。

### 局限

- 没有做大规模真实任务 benchmark。
- memory 仍是轻量文本检索，没有 embedding。
- orchestration 是固定状态机，不适合复杂开放式协作。
- 真实模型仍可能重复输出或不完全遵守格式，需要 runtime guard 兜底。

### 下一步

- 给 orchestration 增加结构化 JSON 输出协议。
- 加 step verifier，让每个 PlanStep 有独立验收。
- 给 memory 增加 last_used_at 和 archive。
- 加 embedding + BM25 双路召回。
- 增加 provider 兼容性和真实模型稳定性 benchmark。

***

# 附录：源码级核心细节

这一部分保留旧文档中最有价值的实现细节。正文负责讲清楚主线，附录负责在面试官追问“具体怎么做”时提供硬证据。

## A1. `ask()` 主循环细节

`runtime.py::Enigma.ask()` 是核心控制循环。一次请求可以拆成四阶段：

```text
用户请求
  |
  v
初始化
  - append user history
  - create TaskState
  - start RunStore
  - emit trace
  |
  v
循环
  1. 感知：ContextManager.build()
  2. 决策：model_client.complete()
  3. 解析：parse() -> tool / plan / final / retry
  4. 行动：run_tool() 或返回 PlanResult / final
  5. 记录：history / memory / trace / checkpoint
  |
  v
停机
  - task_state.finish_success() 或 stop()
  - write report.json
```

两个计数器控制跑飞：

| 计数器 | 含义 | 用途 |
| --- | --- | --- |
| `tool_steps` | 真正执行工具的次数 | 受 `max_steps` 限制 |
| `attempts` | 模型返回任意内容的次数，包括格式错误 retry | 防止 malformed output 无限重试 |

旧设计里的关键公式保留：

```text
max_attempts = min(max_steps * 3, max_steps + 4)
```

这样允许模型偶尔修正格式，但不允许靠 retry 无限消耗模型调用。

## A2. 模型输出解析优先级

`Enigma.parse()` 把模型原始文本解析为四类控制动作：

```text
raw output
  |
  +-- <tool>...</tool>          -> tool
  +-- <tool name="...">...</tool> -> XML-style tool
  +-- <plan>...</plan>          -> plan
  +-- <final>...</final>        -> final
  +-- 非空普通文本               -> final fallback
  +-- 空文本 / 非法 JSON          -> retry
```

优先级：

```text
tool > plan > final > fallback
```

`write_file` / `patch_file` 支持 XML-style tool，是因为文件内容里可能包含需要 JSON 转义的大段文本：

```xml
<tool name="write_file" path="app.py">
  <content>...</content>
</tool>
```

当 JSON 不合法时，parse 返回 retry notice，主循环把格式纠正提示写回 history，让模型下一轮自我修正。

## A3. Prompt section 预算

旧文档中最重要的细节之一是 prompt 不是简单拼接，而是按 section 分预算。当前设计保留这个原则：

| Section | 占比 | 内容 | 压缩顺序 |
| --- | --- | --- | --- |
| `prefix` | 28% | 身份、工具说明、工作区快照 | 最后压缩 |
| `startup_memory` | 6% | `MEMORY.md` 启动记忆 | 倒数第二 |
| `memory` | 12% | 工作记忆、文件摘要、recent files | 第三 |
| `relevant_memory` | 9% | BM25 召回的情景 / 语义记忆 | 最先压缩 |
| `rolling_summary` | 5% | compact 后的会话摘要 | 第四 |
| `history` | 40% | 对话历史和工具结果 | 第二 |
| `current_request` | 不限 | 用户当前请求 | 永不裁剪 |

压缩顺序：

```text
relevant_memory -> history -> memory -> rolling_summary -> startup_memory -> prefix
```

设计原因：

- `current_request` 永远最重要，不能裁剪。
- `prefix` 是工具协议和边界，尽量不裁剪。
- `relevant_memory` 是召回片段，最容易让位。
- history 可以分类压缩，而不是直接截断。

## A4. History 分类压缩

history 压缩不是简单保留最后 N 条，而是按记录类型处理：

| 类型 | 压缩策略 |
| --- | --- |
| 最近记录 | 保留更多细节 |
| 旧 `read_file` | 同一文件多次读取时只保留关键摘要 |
| 旧 `run_shell` | 压缩为 command + stdout 前几行 |
| 普通消息 | 压缩成短摘要 |
| 工具大结果 | 先经过 result clip，再进入 history |

这个策略回应一个真实 coding agent 问题：长任务中读取文件和 shell 输出很容易把 prompt 撑爆，但完全丢掉历史又会让模型忘记走过的路径。

## A5. 工具参数校验细节

工具校验不只检查字段存在，还检查行为是否确定。

| 工具 | 关键校验 |
| --- | --- |
| `list_files` | path 必须在 workspace 内 |
| `read_file` | `start >= 1` 且 `end >= start` |
| `search` | `pattern` 不能为空 |
| `web_search` | query 非空且长度受限，`max_results` 有范围 |
| `run_shell` | command 非空，timeout 有上下限 |
| `write_file` | path 不能逃出 workspace，内容按 UTF-8 写入 |
| `patch_file` | `old_text` 非空，且在文件中恰好出现一次 |
| `delegate` | task 非空，深度不能超过限制 |

`patch_file` 的“恰好出现一次”是重要安全设计：如果 `old_text` 出现 0 次，说明目标不存在；出现多次，说明修改位置不明确。此时拒绝比猜测更安全。

## A6. `delegate` 子 agent 约束

`delegate` 是条件注册工具：只有 `depth < max_depth` 时才暴露，避免无限嵌套。

子 agent 约束：

- `read_only=True`。
- 默认步数更少。
- 深度 +1。
- 共享父 agent 的 model client、workspace、session store。
- 有独立 session 和 memory。
- 返回值以 `delegate_result:` 标识。

本轮补强后，`delegate` 被视为 risky tool。原因不是它会直接写文件，而是它会扩大任务范围和模型调用成本，因此需要进入 approval policy。

## A7. Plan mode 审批细节

Plan mode 的目标是只读探索，不直接改代码。

进入方式：

```bash
enigma --plan "任务描述"
/plan 任务描述
```

行为变化：

- 工具表过滤 risky tool。
- prefix 切换到 PLAN MODE。
- 模型被要求输出 `<plan>...</plan>`。
- `ask()` 返回 `PlanResult`，而不是普通 final 字符串。

审批选项：

```text
Approve -> 执行计划
Revise  -> 带反馈重新规划
Reject  -> 停止，不执行
```

计划持久化到 `.enigma/plan.md`，避免 history 压缩时计划正文丢失。

## A8. Orchestration trace 细节

`/orchestrate` 的每个主要阶段都会写 trace：

| 事件 | 含义 |
| --- | --- |
| `orchestration_started` | 创建 orchestration run |
| `orchestration_state_transition` | 状态变化 |
| `orchestration_plan_ready` | Planner 产出计划 |
| `orchestration_plan_accepted` | 计划可执行 |
| `orchestration_step_result` | Executor 完成一个 active step |
| `orchestration_reviewed` | Reviewer 完成审查 |
| `orchestration_repair_plan_ready` | RepairPlanner 产出修复计划 |
| `orchestration_finished` | 进入 DONE 或 ABORTED |

这些事件会进入 `.enigma/runs/<run_id>/trace.jsonl`，最终 report 中也会有 orchestration run 摘要。

## A9. 反思与语义记忆提升细节

语义记忆提升有两条路径。

### 用户显式触发

用户输入含“记住 / 保存 / record”等意图词，并且模型 final 中出现结构化结论，例如：

```text
Project convention: ...
Decision: ...
Dependency: ...
Preference: ...
```

系统可以直接提取，零额外模型调用。

### 自动反思触发

自动反思由子 agent 负责，触发点包括：

| 触发点 | 条件 |
| --- | --- |
| compact 前 | 用输入签名去重 |
| 工具循环若干次后 | 有新的 process note 或用户有记忆意图 |
| ask 正常结束 | 本轮有值得检查的新上下文 |
| ask 非正常结束 | 步数上限、重试上限等情况下仍可沉淀教训 |

反思原则：

- 宁缺毋滥，没有值得记的内容就返回空 JSON。
- 只提取跨会话仍成立的事实。
- 不把“agent 做了什么”当成语义记忆。
- 输出经过密钥、traceback、过长文本过滤。
- 每个 topic 有上限。

## A10. Durable memory topic 与增长控制

语义记忆 topic：

```text
project-conventions
key-decisions
dependency-facts
user-preferences
```

增长控制：

- 每个 topic 最多 25 条。
- 4 个 topic 最多约 100 条。
- `MEMORY.md` 启动读取限制为 200 行 / 25KB。
- 新旧笔记如果主语相同，可以替换而不是追加。
- compact 不允许重写 durable topics。

本轮补强后的 `MemoryNote` 增加：

```text
text
topic
problem
applies_when
```

面试重点：记忆不是“存了就有用”，而是要知道未来什么时候召回。

## A11. Checkpoint 可信度判断

checkpoint 用来判断旧任务状态还能不能信。

评估维度：

| 维度 | 说明 |
| --- | --- |
| schema version | checkpoint 格式是否兼容 |
| key files freshness | 摘要对应文件是否变更 |
| runtime identity | cwd、model、approval policy、feature flags 是否变更 |
| workspace fingerprint | 分支、状态、目录结构是否变化 |
| tool signature | 工具表是否变化 |

可能状态：

```text
no-checkpoint
full-valid
partial-stale
workspace-mismatch
schema-mismatch
```

这比单纯保存 history 更可靠，因为 history 不能告诉你“旧文件摘要是否仍然可信”。

## A12. RunStore 三件套

每次运行写入：

```text
.enigma/runs/<run_id>/
  task_state.json
  trace.jsonl
  report.json
```

用途：

- `task_state.json`：任务状态和停机原因。
- `trace.jsonl`：每一步事件，适合 debug 和面试展示。
- `report.json`：最终报告，适合自动化评估。

trace/report 会做 secret redaction，避免密钥进入本地工件。

## A13. SessionDB / FTS5 细节

SessionDB 是增强路径，不是主流程硬依赖。

核心表：

- sessions。
- messages。
- message FTS。
- session_state。

能力：

- 跨会话全文搜索。
- 按 session 过滤历史。
- 保存 rolling summary。
- 镜像当前 session history。

如果 SQLite 层失败，agent 仍然能依靠 SessionStore JSON 运行，只是跨会话检索能力退化。

## A14. 测试覆盖索引

关键测试方向可以按两层记：

- `tests/` 是单元和集成回归，约 231 个 pytest 用例，主要保证 harness 不变式。
- `benchmarks/coding_tasks.json` 是固定 coding task 测试集，当前 12 个任务，主要保证端到端运行工件、恢复路径和 durable memory contract。

| 测试 | 覆盖点 |
| --- | --- |
| `tests/test_enigma.py` | 主循环、parse、工具调用、模型 client、checkpoint、compact、trace、report、durable memory |
| `tests/test_safety_invariants.py` | 路径穿越、symlink 逃逸、审批拒绝、read_only、delegate、安全环境变量、secret redaction |
| `tests/test_context_manager.py` | prompt 分段预算、历史压缩、相关记忆召回、重复读取摘要 |
| `tests/test_memory.py` | 分层记忆、file freshness、durable memory、applies_when、topic limit |
| `tests/test_plan_mode.py` | plan mode、只读工具面、审批流程、plan 文件持久化 |
| `tests/test_review.py` | review 命令、git diff、changed files、static finding / severity |
| `tests/test_orchestration.py` | Plan / Execute / Review / Repair 状态机、blocking finding、retry limit、Reviewer 只读 |
| `tests/test_skills.py` | skill 发现、frontmatter、目录式 skill、模板加载 |
| `tests/test_session_db.py` | SQLite / FTS5 / rolling summary / startup memory / session 隔离 |
| `tests/test_evaluator.py` | benchmark schema、fixture copy、run metadata、failure category |
| `tests/test_metrics.py` | context / memory / recovery ablation 和 benchmark core report |
| `tests/test_run_store.py` | `task_state.json`、`trace.jsonl`、`report.json` |
| `tests/test_task_state.py` | TaskState 生命周期、stop reason、snapshot |

固定 benchmark 12 个任务可以按 5 类回答：

| 类别 | 任务 id | 面试回答重点 |
| --- | --- | --- |
| documentation | `readme_intro_locked`、`readme_schema_note` | 基本文档编辑闭环 |
| text-edit | `sample_beta_locked`、`sample_gamma_locked` | 精确补丁和 verifier 验证 |
| tool-boundary | `invalid_patch_recovery`、`path_escape_recovery`、`repeated_read_recovery` | 工具错误、路径拒绝、重复调用拒绝后仍能恢复 |
| recovery | `context_reduction_checkpoint`、`freshness_reanchor_resume`、`workspace_mismatch_resume` | checkpoint、freshness、runtime identity 恢复分类 |
| durable-contract | `durable_promotion_accept`、`durable_promotion_reject` | 长期记忆接受/拒绝规则可验证 |

本轮新增或补强的重点是：

- policy tests。
- memory `applies_when` tests。
- orchestration state transition tests。
- reviewer 只读工具面 tests。
- retry limit abort tests。

## A15. 面试时可以主动展示的证据

可以现场展示三类证据：

1. 代码结构：

```text
enigma/policy.py
enigma/orchestration.py
enigma/memory.py
enigma/context_manager.py
enigma/runtime.py
```

2. 测试：

```bash
uv run pytest -q tests/test_orchestration.py tests/test_memory.py tests/test_plan_mode.py tests/test_safety_invariants.py
```

3. 运行工件：

```text
.enigma/runs/<run_id>/task_state.json
.enigma/runs/<run_id>/trace.jsonl
.enigma/runs/<run_id>/report.json
```

这三类证据能把“我做了 agent”落到“它为什么安全、为什么可恢复、为什么可追踪”。
