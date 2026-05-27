# Enigma Review Pack

This folder collects the short materials that are useful when reviewing Enigma as an internship portfolio project.

## Project pitch

Enigma is a terminal-based local coding agent runtime for Python repositories. It focuses on the engineering pieces behind an agent: workspace discovery, model backend abstraction, controlled tool execution, memory, context budgeting, traceability, and recovery.

## Architecture map

- `enigma/cli.py`: command-line parsing, model client selection, session bootstrap, and REPL wiring.
- `enigma/runtime.py`: the main agent loop, tool dispatch, approval policy, trace writing, and memory updates.
- `enigma/context_manager.py`: prompt assembly, token budgeting, and context compaction.
- `enigma/memory.py`: working, episodic, and semantic memory state.
- `enigma/tools.py`: file, search, shell, patch, web search, and delegate tools.
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
