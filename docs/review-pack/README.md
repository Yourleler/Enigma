# Enigma Review Pack

This folder collects the short materials that are useful when reviewing Enigma as an internship portfolio project.

## Project pitch

Enigma is a terminal-based local coding agent harness for local repositories. It focuses on the engineering pieces behind an agent: workspace discovery, model backend abstraction, controlled tool execution, memory, context budgeting, bounded orchestration, traceability, and recovery.

In interview terms, the project should be presented as an application-layer agent runtime, not as model training work and not as a chat UI. The key story is: deterministic harness rules control unsafe boundaries, lightweight memory reduces repeated exploration, and Plan / Execute / Review / Repair orchestration creates a traceable loop for coding tasks.

## Interview delta: this round

The following items are the changes made in this interview-preparation round.

- **[This round] Policy layer**: Added `HarnessPolicy` / `ToolPolicy` so rules like UTF-8 file IO, workspace path anchoring, risky-tool approval, and plan-mode read-only tool exposure are enforced by the harness instead of prompt wording.
- **[This round] Coding loop tool surface**: Added `glob` and lightweight `symbol_search`, then made the normal and Plan prompts explicitly follow a locate -> read -> reason -> edit -> test/check -> observe -> revise workflow.
- **[This round] Memory applicability**: Durable memory now carries `problem` and `applies_when`, so a stored fact explains both the problem it solves and the future scenario where it should be recalled.
- **[This round] Bounded multi-agent orchestration**: Added `/orchestrate <task>` with Planner, Executor, Reviewer, and RepairPlanner roles. The state machine supports `Plan -> Execute -> Review -> Done`, `Execute -> RepairPlan -> Execute`, `Review -> RepairPlan -> Execute -> Review`, and retry-limit abort.
- **[This round] Interview narrative**: Updated README to frame the project as `Problem -> Design -> Evidence`, directly addressing interviewer concerns about real coding pain points, prompt overuse, memory trigger conditions, and agent harness maturity.
- **[This round] Evidence tests**: Added orchestration tests and strengthened memory / plan mode / safety tests so the claims are backed by executable checks.
- **[This round] Real-call validation**: Verified with Ollama `qwen3.6:35b-a3b` that both `model -> tool -> model -> final` and `/orchestrate -> DONE` paths run end to end.

## Interviewer concern mapping

| Interviewer concern | What Enigma now shows | Short answer |
| --- | --- | --- |
| The project feels like a practice clone, not a real problem-driven design. | README now maps repeated exploration, unsafe local tools, unclear memory triggers, and long-task failure recovery to concrete design choices. | "I reframed it as a coding agent harness and tied each feature to a real runtime problem." |
| Too many rules are prompt-level soft constraints. | Policy and tool layers enforce deterministic boundaries. | "Prompt handles semantic judgment; harness policy handles rules that should never depend on model memory." |
| The tool surface does not fully match the coding loop. | `glob`, `search`, `symbol_search`, `read_file`, `write_file` / `patch_file`, and `run_shell` now support locate, read, edit, test, observe, and revise. | "The loop is both in the prompt and backed by callable tools." |
| Memory does not explain when it is useful. | Durable notes include `problem` and `applies_when`, and prompt rendering exposes applicability. | "A memory is only valuable if the agent knows when to recall it." |
| Multi-agent orchestration sounds too light. | `/orchestrate` is not a one-way pipeline; review and execution failures can route back to repair planning. | "It is bounded orchestration, not open-ended swarm." |
| There is not enough evidence. | Tests cover policy, memory, orchestration state transitions, reviewer tool boundaries, retry abort, and trace/report output. | "The point is not scale; it is a small but testable agent harness." |

## Architecture map

- `enigma/cli.py`: command-line parsing, model client selection, session bootstrap, and REPL wiring.
- `enigma/runtime.py`: the main agent loop, tool dispatch, approval policy, trace writing, and memory updates.
- `enigma/policy.py`: deterministic harness and tool policy rules.
- `enigma/orchestration.py`: bounded Plan / Execute / Review / Repair orchestration.
- `enigma/context_manager.py`: prompt assembly, token budgeting, and context compaction.
- `enigma/memory.py`: working, episodic, and semantic memory state, including durable note applicability.
- `enigma/tools.py`: file, glob, search, lightweight symbol search, shell, patch, web search, and delegate tools with safety policy integration.
- `enigma/storage/session_db.py`: SQLite-backed session metadata and full-text search.
- `tests/`: regression tests for runtime behavior, safety invariants, memory, planning, review, metrics, and storage.

## Benchmark evidence

The fixed benchmark definition lives in `benchmarks/coding_tasks.json`. The evaluator code in `enigma/evaluator.py` runs deterministic tasks against copied fixtures and records per-task status, budget usage, verifier status, and reproducibility metadata.

Local benchmark artifacts should be generated under `artifacts/` or `docs/review-pack/*.json` as needed. Generated JSON artifacts are intentionally not required for reading the project source.

## Sample run artifact list

A normal Enigma run writes local artifacts under `.enigma/runs/<run_id>/`:

- `task_state.json`
- `trace.jsonl`
- `report.json`

These files are useful for debugging but are ignored by Git because they describe local execution state.

## Validation snapshot

Latest local validation from this round:

```bash
uv run pytest -q
# 231 passed

uv run ruff check .
# All checks passed!

uv run enigma --provider ollama --model qwen3.6:35b-a3b --approval never --max-steps 4 --max-new-tokens 512 \
  "请先调用 read_file 工具读取 README.md 的第 1 到 20 行，然后根据工具结果用中文简要总结这个项目是什么。必须先调用工具，不要直接回答。"
# Verified: model -> read_file tool -> model -> final

uv run enigma --provider ollama --model qwen3.6:35b-a3b --approval never --max-steps 6 --max-new-tokens 700 \
  "/orchestrate 只读检查 README.md 的项目定位，并给出是否符合 coding agent harness 叙事的简短结论。不要修改任何文件。"
# Orchestration complete.
# - state: DONE
# - retries: 0/2
# - steps: 3
# - review_findings: 0
```
