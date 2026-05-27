"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass


def estimate_tokens(text):
    """估算文本的 token 数。中英混合约 3.5 字符/token。"""
    return max(1, int(len(str(text)) / 3.5))


def tokens_to_chars(tokens):
    """token 数转回字符估算（用于裁剪）。"""
    return max(1, int(tokens * 3.5))


# 默认预算（token）：适用于本地小模型（8K-32K 上下文）
DEFAULT_TOTAL_BUDGET = 4000
# 各 section 占总预算的比例（总和 = 1.0）
# startup_memory / rolling_summary 是稳定记忆 + 跨轮任务总结，
# 独立于 memory 的工作记忆，体积小但必须每轮都注入。
SECTION_RATIO = {
    "prefix": 0.28,
    "startup_memory": 0.06,
    "memory": 0.12,
    "relevant_memory": 0.09,
    "rolling_summary": 0.05,
    "history": 0.40,
}
# 各 section 最低保留比例（相对自身预算）
SECTION_FLOOR_RATIO = 0.25


def _compute_section_budgets(total_budget):
    """根据总预算按比例分配各 section 的 token 预算。"""
    return {section: max(10, int(total_budget * ratio)) for section, ratio in SECTION_RATIO.items()}


def _compute_section_floors(section_budgets):
    """根据 section 预算计算最低保留值。"""
    return {section: max(5, int(budget * SECTION_FLOOR_RATIO)) for section, budget in section_budgets.items()}


# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "rolling_summary", "startup_memory", "prefix")
SECTION_ORDER = ("prefix", "startup_memory", "memory", "relevant_memory", "rolling_summary", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 4


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)

    @property
    def raw_tokens(self):
        return estimate_tokens(self.raw)

    @property
    def rendered_tokens(self):
        return estimate_tokens(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = _compute_section_budgets(self.total_budget)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        召回记忆 + history + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Enigma.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "startup_memory": self._collect_startup_memory(),
            "memory": "工作记忆:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "rolling_summary": self._collect_rolling_summary(),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = checkpoint_text + "\n\n" + section_texts["prefix"]
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        # 旧调用方可能只传 4 个 key（prefix/memory/relevant_memory/history），
        # 这里补齐新加的 section，避免 KeyError 或 budget=0 导致文本被裁光。
        default_budgets = _compute_section_budgets(self.total_budget)
        for section, default_value in default_budgets.items():
            budgets.setdefault(section, default_value)
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while estimate_tokens(prompt) > self.total_budget:
            overflow = estimate_tokens(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_tokens": current_budget,
                        "after_tokens": new_budget,
                        "overflow_tokens": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        # 预算裁剪到底了还超限 → 自动调用 compact 深度压缩（防重入）。
        # 小幅超支（< 25%）不值得触发 compact——compact 自身要调模型、成本更高，
        # 让 prompt 小超一点走出去反而代价更小。
        overflow_ratio = (estimate_tokens(prompt) - self.total_budget) / max(1, self.total_budget)
        if (
            estimate_tokens(prompt) > self.total_budget
            and overflow_ratio > 0.25
            and not getattr(self, "_auto_compacting", False)
        ):
            compact = getattr(self.agent, "compact_context", None)
            if callable(compact):
                self._auto_compacting = True
                try:
                    compact()
                    # compact 后 memory、history 已被压缩，重新组装
                    section_texts["startup_memory"] = self._collect_startup_memory()
                    section_texts["memory"] = str(self.agent.memory_text())
                    section_texts["rolling_summary"] = self._collect_rolling_summary()
                    section_texts["prefix"] = str(getattr(self.agent, "prefix", ""))
                    section_texts["history"] = ""
                    rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                    prompt = self._assemble_prompt(rendered)
                    reduction_log.append({"section": "__auto_compact__", "trigger": "budget_overflow"})
                except Exception as exc:
                    reduction_log.append({"section": "__auto_compact__", "trigger": "budget_overflow", "error": str(exc)})
                finally:
                    self._auto_compacting = False

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
        )
        return prompt, metadata

    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        selected_notes = selected_notes or []
        relevant_lines = ["召回记忆:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "startup_memory": SectionRender(raw=section_texts.get("startup_memory", ""), budget=len(section_texts.get("startup_memory", "")), rendered=section_texts.get("startup_memory", ""), details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "rolling_summary": SectionRender(raw=section_texts.get("rolling_summary", ""), budget=len(section_texts.get("rolling_summary", "")), rendered=section_texts.get("rolling_summary", ""), details={}),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_floors(self):
        floors = _compute_section_floors(self.section_budgets)
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts.get(section, "")
                char_limit = tokens_to_chars(int(budget)) if budget is not None else len(raw)
                rendered_text = _tail_clip(raw, char_limit) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _collect_startup_memory(self):
        """从 agent.memory 读 MEMORY.md 头部作为启动记忆。"""
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "startup_memory_text"):
            return ""
        try:
            text = str(memory.startup_memory_text() or "").strip()
        except Exception:
            return ""
        if not text:
            return ""
        return "启动记忆 (MEMORY.md):\n" + text

    def _collect_rolling_summary(self):
        """从 session 或 agent 读滚动摘要。"""
        text = ""
        getter = getattr(self.agent, "rolling_summary_text", None)
        if callable(getter):
            try:
                text = str(getter() or "").strip()
            except Exception:
                text = ""
        if not text:
            session = getattr(self.agent, "session", None) or {}
            summary_dict = session.get("compact_summary") or {}
            text = str(summary_dict.get("text") or "").strip()
        if not text:
            return ""
        return "会话滚动摘要:\n" + text

    def _render_relevant_memory(self, selected_notes, budget):
        header = "召回记忆:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            char_limit = tokens_to_chars(per_note_budget)
            rendered_notes = [_tail_clip(text, char_limit) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if estimate_tokens(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if estimate_tokens(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, tokens_to_chars(budget))
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = estimate_tokens(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "history:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["history:", *candidate_entries])
            if estimate_tokens(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available_tokens = budget - estimate_tokens("history:")
                if rendered_entries:
                    available_tokens -= sum(estimate_tokens(line) + 1 for line in rendered_entries)
                available_chars = tokens_to_chars(max(20, available_tokens - 1))
                candidate_lines = [_tail_clip(line, available_chars) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["history:", *candidate_entries])
                if estimate_tokens(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 70) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["history:", *smaller_entries])
                if estimate_tokens(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["history:", *rendered_entries])

        if estimate_tokens(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, tokens_to_chars(budget))

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                char_limit = tokens_to_chars(300)  # ~300 tokens for recent entries
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, char_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 200)})

        return entries, details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "history:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["history:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        parts = []
        for section in SECTION_ORDER:
            render = rendered.get(section)
            if render is None:
                continue
            text = render.rendered
            if not text:
                continue
            parts.append(text)
        return "\n\n".join(parts).strip()

    def _metadata(self, prompt, rendered, budgets, reduction_log, selected_notes, user_message, section_texts):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "raw_tokens": estimate_tokens(rendered[section].raw),
                "budget_tokens": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
                "rendered_tokens": estimate_tokens(rendered[section].rendered),
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "raw_tokens": estimate_tokens(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_tokens": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            "rendered_tokens": estimate_tokens(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_tokens": estimate_tokens(prompt),
            "prompt_budget_tokens": self.total_budget,
            "prompt_over_budget": estimate_tokens(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "raw_tokens": estimate_tokens(rendered["relevant_memory"].raw),
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_tokens": estimate_tokens(rendered["relevant_memory"].rendered),
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "raw_tokens": estimate_tokens(rendered["history"].raw),
                "rendered_chars": rendered["history"].rendered_chars,
                "rendered_tokens": estimate_tokens(rendered["history"].rendered),
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "raw_tokens": estimate_tokens(user_message),
                "rendered_chars": len(user_message),
                "rendered_tokens": estimate_tokens(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
                "section_tokens": estimate_tokens(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
