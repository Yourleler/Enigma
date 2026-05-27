# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**enigma** 是一个面向代码仓库的轻量本地 coding agent。它在终端中运行，工作于仓库根目录下，通过一组受限工具（读/写/补丁文件、搜索、执行 shell 命令、委派给子 agent）来完成工程任务。纯 Python 3.10+ 实现，零运行时依赖。

## 常用命令

```bash
# 安装开发依赖
uv sync                  # 或: pip install -e .

# 运行全部测试
uv run pytest            # 或: python -m pytest

# 运行单个测试文件或测试用例
uv run pytest tests/test_enigma.py
uv run pytest tests/test_enigma.py::test_function_name -k "keyword"

# 代码检查
uv run ruff check .

# 运行 agent
uv run enigma                                      # 交互式 REPL
uv run enigma "在这里描述你的任务"                     # 单次任务
uv run enigma --provider ollama --model qwen3.5:4b  # 本地 Ollama
uv run enigma --provider openai                     # OpenAI 兼容 API
uv run enigma --provider anthropic                  # Anthropic 兼容 API
```

## 架构

所有源码位于 `enigma/` 目录下（约 16 个模块）。`runtime.py` 中的 `Enigma` 类是核心调度器。

### 核心循环 (`runtime.py` — `Enigma.ask()`)

1. 将用户消息记录到 history
2. 循环执行，最多 `max_steps` 步：
   - 通过 `ContextManager` 构建 prompt（基于预算组装 prefix + memory + history + request）
   - 调用 `model_client.complete()` 获取模型输出（支持 `on_token` 流式回调）
   - 通过 `Enigma.parse()` 解析输出 — 提取 `<tool>...</tool>`、`<plan>...</plan>` 或 `<final>...</final>`
   - 工具调用：校验 → 审批检查 → 通过 `run_tool()` 执行 → 终端显示结果+diff → 更新记忆 → 创建检查点
   - Plan 模式：`<plan>` 标签返回 `PlanResult`，REPL 进入审批循环（approve/revise/reject）
   - 最终回答：记录、提升持久化记忆、写报告、返回

### 模块职责

- **cli.py** — CLI 参数解析、agent 组装、REPL 循环（双线程：spinner + 暂存消息 + Ctrl+C 中断）
- **runtime.py** — 核心 agent 循环 (`Enigma`)、会话持久化 (`SessionStore`)、Plan 模式、终端显示
- **models.py** — 模型后端适配器：`OllamaModelClient`、`OpenAIModelClient`、`AnthropicModelClient`、`FakeModelClient`。统一暴露 `complete(prompt, max_new_tokens, on_token=None)` 接口，支持流式输出
- **tools.py** — 工具定义、schema 校验、执行。七个基础工具（`list_files`、`read_file`、`search`、`web_search`、`run_shell`、`write_file`、`patch_file`）外加 `delegate`（有深度限制的子 agent）
- **display.py** — 终端显示工具：ANSI 颜色、步骤进度、结果预览、文件 diff 显示
- **repl.py** — REPL 交互组件：Spinner 动画、非阻塞输入、暂存消息队列
- **web_search.py** — Web 搜索工具实现，支持域名过滤
- **skills.py** — Skill 系统：三源发现（内置/用户/项目）、frontmatter 解析、prompt 组装
- **context_manager.py** — 基于预算的 prompt 组装，各部分有独立预算。压缩顺序：relevant_memory → history → memory → prefix
- **memory.py** — 三类记忆：工作记忆（当前任务和文件摘要）、情景记忆（会话内提炼笔记）和语义记忆（跨会话稳定事实，存储在 `.enigma/memory/topics/*.md`）
- **workspace.py** — 基于 git 的工作区快照、项目文档、指纹识别、git diff 获取
- **task_state.py** — `TaskState` 数据类 — 运行生命周期状态机
- **run_store.py** — 每次运行的产物持久化（task_state.json、trace.jsonl、report.json）
- **evaluator.py** — 评测框架（`BenchmarkEvaluator`、`run_fixed_benchmark`），对标 `benchmarks/coding_tasks.json`
- **metrics.py** — 实验套件：消融实验、安全性、provider 对比、上下文压力测试

### 安全模型

- 所有文件路径锚定在工作区根目录（防止路径穿越）
- 工具审批策略：`ask` / `auto` / `never`
- 子 agent / delegate 以只读模式运行
- 跟踪和报告中对密钥进行脱敏
- 重复相同工具调用检测，防止死循环

### 模型 I/O 协议

模型通过类 XML 标签在输出中通信：
- `<tool>{"name":"tool_name","args":{...}}</tool>` — 请求工具调用
- `<tool name="write_file" path="..."><content>...</content></tool>` — write_file/patch_file 的替代语法
- `<plan>## Goal\n## Steps\n...</plan>` — Plan 模式输出计划
- `<final>answer text</final>` — 标记任务完成

### 测试结构

`tests/` 下共 10 个测试文件。最大的是 `test_enigma.py`（约 50 个测试），覆盖核心 agent 循环。`test_safety_invariants.py` 覆盖路径穿越、审批拒绝和密钥脱敏。测试中使用 `models.py` 中的 `FakeModelClient`，无需真实模型调用。
