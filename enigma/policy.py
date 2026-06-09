"""Runtime policy for deterministic agent harness constraints.

The policy layer is intentionally small: it captures rules that should not
depend on the model remembering a prompt instruction, such as text encoding,
workspace anchoring, and risky-tool categories.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolPolicy:
    """Deterministic defaults used by concrete tool implementations."""

    text_encoding: str = "utf-8"
    read_errors: str = "replace"
    write_errors: str = "strict"
    anchor_paths_to_workspace: bool = True
    risky_tools: frozenset[str] = frozenset({"write_file", "patch_file", "run_shell", "delegate"})

    def text_read_kwargs(self):
        return {"encoding": self.text_encoding, "errors": self.read_errors}

    def text_write_kwargs(self):
        return {"encoding": self.text_encoding, "errors": self.write_errors}

    def is_risky(self, tool_name):
        return str(tool_name) in self.risky_tools


@dataclass(frozen=True)
class HarnessPolicy:
    """Top-level harness policy surfaced to runtime, tools, and docs."""

    tool: ToolPolicy = field(default_factory=ToolPolicy)
    semantic_rules_live_in_prompt: bool = True

    @classmethod
    def default(cls):
        return cls()

    def render_prompt_block(self):
        return (
            "Harness policy:\n"
            f"- Text file reads/writes use {self.tool.text_encoding.upper()} at the tool layer.\n"
            "- File paths are resolved inside the workspace before tool execution.\n"
            "- Risky tools require the configured approval policy unless the agent is read-only.\n"
            "- Deterministic rules belong in tools/policy; prompt and memory are reserved for semantic judgment."
        )
