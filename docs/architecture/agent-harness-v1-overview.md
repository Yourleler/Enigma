# Agent Harness v1 Overview

Agent Harness v1 is the deterministic evaluation layer used to exercise Enigma's runtime behavior without requiring a live model provider.

## Purpose

The harness gives the project a repeatable way to answer three questions:

- Did the agent finish the task?
- Did it stay within the expected tool and step budget?
- Did the expected artifact or state change actually appear?

## Core flow

1. Load task definitions from `benchmarks/coding_tasks.json`.
2. Copy the fixture repository into a fresh workspace.
3. Run Enigma with a scripted deterministic model client.
4. Capture task state, trace, report, and verifier results.
5. Summarize pass rate, verifier pass rate, and budget compliance.

## Task state

Each benchmark row is tied to task state generated during the run. The task state records the task id, run id, user request, current status, step count, stop reason, created checkpoints, and output artifact locations.

The state file is designed to make a run inspectable after completion, so a reviewer can distinguish a correct final answer from a lucky or incomplete stop condition.

## Reproducibility

Benchmark artifacts include metadata such as model name, decoding settings, fixture snapshot id, timezone, locale, and relative artifact paths. This keeps local paths out of the report while still making the result traceable to a specific fixture snapshot.
