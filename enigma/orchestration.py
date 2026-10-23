"""Bounded Plan/Execute/Review/Repair orchestration for Enigma.

This module deliberately implements a fixed, small state machine instead of an
open-ended agent swarm. Each role is a constrained prompt over the existing
runtime, so the feature stays inspectable and testable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from uuid import uuid4

from .static_review import format_static_findings, scan_python_files
from .task_state import TaskState
from .workspace import clip, now

STATE_PLANNING = "PLANNING"
STATE_PLAN_REVIEW = "PLAN_REVIEW"
STATE_EXECUTING = "EXECUTING"
STATE_STEP_FAILED = "STEP_FAILED"
STATE_REPAIR_PLANNING = "REPAIR_PLANNING"
STATE_REVIEWING = "REVIEWING"
STATE_DONE = "DONE"
STATE_ABORTED = "ABORTED"
STATE_UNVERIFIED = "UNVERIFIED"

CRITICAL_SEVERITIES = {"critical", "high", "error", "blocker"}


@dataclass
class PlanStep:
    id: str
    goal: str
    allowed_tools: list[str] = field(default_factory=list)
    expected_change: str = ""
    acceptance_check: str = ""
    verification: dict = field(default_factory=dict)

    @classmethod
    def from_text(cls, text, index):
        text = str(text).strip()
        return cls(
            id=f"step-{index}",
            goal=text or f"Execute step {index}.",
            allowed_tools=[],
            expected_change="Complete the described change.",
            acceptance_check="The step is complete without tool errors.",
        )

    def to_dict(self):
        return {
            "id": self.id,
            "goal": self.goal,
            "allowed_tools": list(self.allowed_tools),
            "expected_change": self.expected_change,
            "acceptance_check": self.acceptance_check,
            "verification": dict(self.verification),
        }


@dataclass
class StepResult:
    step_id: str
    status: str
    changed_files: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    verification_status: str = "unverified"
    verification_evidence: object = field(default_factory=dict)

    def to_dict(self):
        return {
            "step_id": self.step_id,
            "status": self.status,
            "changed_files": list(self.changed_files),
            "summary": self.summary,
            "error": self.error,
            "verification_status": self.verification_status,
            "verification_evidence": self.verification_evidence,
        }


@dataclass
class ReviewFinding:
    severity: str
    file: str = ""
    line: str = ""
    message: str = ""
    recommended_action: str = ""

    @classmethod
    def from_text(cls, text):
        text = str(text).strip()
        severity = "suggestion"
        match = re.search(r"(?i)\b(critical|high|warning|suggestion|error|blocker)\b", text)
        if match:
            severity = match.group(1).lower()
        else:
            for label, value in (("严重", "critical"), ("高危", "high"), ("阻断", "blocker"), ("错误", "error"), ("警告", "warning"), ("建议", "suggestion")):
                if label in text:
                    severity = value
                    break
        return cls(severity=severity, message=text, recommended_action="Address the finding if it is actionable.")

    def is_blocking(self):
        return self.severity.lower() in CRITICAL_SEVERITIES

    def to_dict(self):
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "recommended_action": self.recommended_action,
        }


@dataclass
class OrchestrationRun:
    task: str
    run_id: str = field(default_factory=lambda: "orch_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
    state: str = STATE_PLANNING
    plan: str = ""
    plan_steps: list[PlanStep] = field(default_factory=list)
    step_results: list[StepResult] = field(default_factory=list)
    review_findings: list[ReviewFinding] = field(default_factory=list)
    static_findings: list[dict] = field(default_factory=list)
    retries: int = 0
    retry_limit: int = 2
    final_answer: str = ""
    aborted_reason: str = ""

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "task": self.task,
            "state": self.state,
            "plan": self.plan,
            "plan_steps": [step.to_dict() for step in self.plan_steps],
            "step_results": [result.to_dict() for result in self.step_results],
            "review_findings": [finding.to_dict() for finding in self.review_findings],
            "static_findings": list(self.static_findings),
            "retries": self.retries,
            "retry_limit": self.retry_limit,
            "final_answer": self.final_answer,
            "aborted_reason": self.aborted_reason,
        }


class Orchestrator:
    def __init__(self, agent, retry_limit=2):
        self.agent = agent
        self.retry_limit = int(retry_limit)
        self.run = None
        self.task_state = None
        self.pending_steps = []

    def run_task(self, task):
        try:
            return self._run_task(task)
        except (Exception, KeyboardInterrupt) as exc:
            if self.run is None:
                raise
            self._abort("user_cancel" if isinstance(exc, KeyboardInterrupt) else str(exc))
            return self._finish()

    def _run_task(self, task):
        task = str(task or "").strip()
        if not task:
            raise ValueError("task must not be empty")
        self.run = OrchestrationRun(task=task, retry_limit=self.retry_limit)
        self.history_start = len(self.agent.session["history"])
        self.task_state = TaskState.create(run_id=self.run.run_id, task_id=self.run.run_id, user_request=task)
        self.agent.current_task_state = self.task_state
        self.agent.current_run_dir = self.agent.run_store.start_run(self.task_state)
        self._trace("orchestration_started", {"task": clip(task, 300)})

        self._planning(task)
        if self.run.state == STATE_ABORTED:
            return self._finish()
        self._plan_review()
        if self.run.state == STATE_ABORTED:
            return self._finish()

        while self.run.retries <= self.run.retry_limit:
            self._executing()
            if self.run.state == STATE_ABORTED:
                break
            if self.run.state == STATE_STEP_FAILED:
                if not self._repair("execution failure"):
                    break
                continue

            self._reviewing()
            if self.run.state in {STATE_DONE, STATE_UNVERIFIED}:
                break
            if self.run.state == STATE_REPAIR_PLANNING:
                if not self._repair("review finding"):
                    break
                continue
            break

        return self._finish()

    def _planning(self, task):
        self._transition(STATE_PLANNING, role="Planner")
        previous_plan_mode = self.agent.plan_mode
        self.agent.plan_mode = True
        self.agent.tools = self.agent.build_tools()
        self.agent.prefix_state = self.agent.build_prefix()
        self.agent.prefix = self.agent.prefix_state.text
        try:
            result = self.agent.ask(
                "Planner role: produce a concrete implementation plan with goal, files, steps, risks, and acceptance checks for:\n"
                + task
                + '\nUse <plan>{"steps":[{"goal":"...","allowed_tools":["read_file","patch_file","run_shell"],'
                  '"verification":{"files":{"relative/path":null},"command":"focused check"}}]}</plan> '
                  'for executable plans. files maps paths to exact expected UTF-8 text or null for existence. '
                  'Verification commands obey tool permissions and approval. '
                  'Markdown plans remain readable but are unverified without independent checks.'
            )
        finally:
            self.agent.plan_mode = previous_plan_mode
            self.agent.tools = self.agent.build_tools()
            self.agent.prefix_state = self.agent.build_prefix()
            self.agent.prefix = self.agent.prefix_state.text
        plan = getattr(result, "plan", None) or str(result)
        if self.agent.current_task_state.stop_reason == "user_cancel":
            self._abort("user_cancel")
            return
        if self.agent.current_task_state.stop_reason != "plan_ready":
            self._abort("planner did not produce a valid plan")
            return
        self.run.plan = plan
        self.run.plan_steps = parse_plan_steps(plan)
        self.pending_steps = list(self.run.plan_steps)
        self._trace("orchestration_plan_ready", {"plan": clip(plan, 700), "step_count": len(self.run.plan_steps)})

    def _plan_review(self):
        self._transition(STATE_PLAN_REVIEW, role="Planner")
        if not self.run.plan_steps:
            self._abort("planner produced no executable steps")
            return
        if not any(step.acceptance_check for step in self.run.plan_steps):
            for step in self.run.plan_steps:
                step.acceptance_check = "No tool errors and changed files match the step goal."
        self._trace("orchestration_plan_accepted", {"step_count": len(self.run.plan_steps)})

    def _executing(self):
        self._transition(STATE_EXECUTING, role="Executor")
        final = ""
        while self.pending_steps:
            step = self.pending_steps[0]
            prompt = (
                "Executor role: execute only this active plan step. "
                "Do not invent extra work. Return a concise final summary when the step is complete.\n\n"
                f"Step JSON:\n{json.dumps(step.to_dict(), ensure_ascii=False, indent=2)}\n\n"
                f"Original task:\n{self.run.task}"
            )
            old_allowed = self.agent.allowed_tools
            old_check = self.agent.completion_check
            if step.allowed_tools:
                self.agent.allowed_tools = set(step.allowed_tools) if old_allowed is None else old_allowed & set(step.allowed_tools)
            if step.verification:
                self.agent.completion_check = partial(verify_step, specification=step.verification)
            else:
                self.agent.completion_check = None
            self.agent.tools = self.agent.build_tools()
            self.agent.refresh_prefix(force=True)
            try:
                answer = str(self.agent.ask(prompt))
                state = self.agent.current_task_state
            finally:
                self.agent.allowed_tools = old_allowed
                self.agent.completion_check = old_check
                self.agent.tools = self.agent.build_tools()
                self.agent.refresh_prefix(force=True)
            final = answer
            if state.stop_reason == "user_cancel":
                self._abort("user_cancel")
                return
            status = "ok"
            error = ""
            if state.status in {"stopped", "failed"} or (state.verification_status != "verified" and re.match(r"(?i)^error:", answer)):
                status = "failed"
                error = answer
            changed_files = self._changed_files()
            result = StepResult(
                step_id=step.id,
                status=status,
                changed_files=changed_files,
                summary=clip(answer, 500),
                error=clip(error, 500),
                verification_status=state.verification_status,
                verification_evidence=state.verification_evidence,
            )
            self.run.step_results.append(result)
            self._trace("orchestration_step_result", result.to_dict())
            if status != "ok":
                self._transition(STATE_STEP_FAILED, role="Executor", reason=error)
                return
            self.pending_steps.pop(0)
        self.run.final_answer = final

    def _reviewing(self):
        self._transition(STATE_REVIEWING, role="Reviewer")
        diff, source = self.agent.workspace.git_diff() if self.agent.workspace.is_git_repo else ("", "workspace files")
        files = self._changed_files()
        static_findings = scan_python_files(files, self.agent.workspace.repo_root)
        self.run.static_findings = [finding.to_dict() for finding in static_findings]
        prompt = build_review_prompt(
            task=self.run.task,
            plan=self.run.plan,
            step_results=[result.to_dict() for result in self.run.step_results],
            diff=diff,
            source=source,
            files=files,
            static_findings=static_findings,
        )
        previous_read_only = self.agent.read_only
        previous_check = self.agent.completion_check
        self.agent.completion_check = None
        self.agent.read_only = True
        self.agent.tools = {
            name: spec
            for name, spec in self.agent.build_tools().items()
            if not spec.get("risky", False)
        }
        self.agent.prefix_state = self.agent.build_prefix()
        self.agent.prefix = self.agent.prefix_state.text
        try:
            review = str(self.agent.ask(prompt))
        finally:
            self.agent.read_only = previous_read_only
            self.agent.completion_check = previous_check
            self.agent.tools = self.agent.build_tools()
            self.agent.prefix_state = self.agent.build_prefix()
            self.agent.prefix = self.agent.prefix_state.text
        findings = parse_review_findings(review)
        if self.agent.current_task_state.stop_reason == "user_cancel":
            self._abort("user_cancel")
            return
        findings.extend(static_findings_to_blocking_review_findings(static_findings))
        self.run.review_findings = findings
        self._trace(
            "orchestration_reviewed",
            {
                "review": clip(review, 700),
                "findings": [f.to_dict() for f in findings],
                "static_findings": self.run.static_findings,
            },
        )
        if any(finding.is_blocking() for finding in findings):
            self._transition(STATE_REPAIR_PLANNING, role="Reviewer", reason="blocking review finding")
            return
        if previous_check is not None:
            passed, evidence = previous_check(self.agent)
            if self.agent.check_cancel():
                self._abort("user_cancel")
                return
            if not isinstance(passed, bool):
                raise TypeError("completion_check must return (bool, evidence)")
            self.task_state.verification_status = "verified" if passed else "failed"
            self.task_state.verification_evidence = self.agent.redact_artifact(evidence)
            if not passed:
                self.run.review_findings.append(ReviewFinding("blocker", message=f"Verification failed: {evidence}"))
                self._transition(STATE_REPAIR_PLANNING, role="Verifier", reason="acceptance failed")
                return
        elif not all(result.verification_status == "verified" for result in self.run.step_results if result.status == "ok"):
            self._transition(STATE_UNVERIFIED, role="Reviewer", reason="no executable acceptance evidence")
            return
        # Later steps and repair may invalidate an earlier check.
        for step in self.run.plan_steps:
            if step.verification:
                specification = dict(step.verification)
                if len(self.run.step_results) == 1 and not self.run.retries:
                    specification.pop("command", None)
                if not specification.get("files") and not specification.get("command"):
                    continue
                passed, evidence = verify_step(self.agent, specification)
                if not passed:
                    self._abort(f"acceptance no longer holds: {evidence}")
                    return
        self._transition(STATE_DONE, role="Verifier")

    def _repair(self, reason):
        if self.run.retries >= self.run.retry_limit:
            self._abort(f"retry limit reached after {reason}")
            return False
        self.run.retries += 1
        self._transition(STATE_REPAIR_PLANNING, role="RepairPlanner", reason=reason)
        repair_context = {
            "task": self.run.task,
            "plan": self.run.plan,
            "step_results": [result.to_dict() for result in self.run.step_results[-3:]],
            "review_findings": [finding.to_dict() for finding in self.run.review_findings],
            "retry": self.run.retries,
            "retry_limit": self.run.retry_limit,
        }
        previous_plan_mode = self.agent.plan_mode
        self.agent.plan_mode = True
        self.agent.tools = self.agent.build_tools()
        self.agent.prefix_state = self.agent.build_prefix()
        self.agent.prefix = self.agent.prefix_state.text
        try:
            result = self.agent.ask(
                "RepairPlanner role: create the smallest repair step needed. "
                "Base it only on the failure or review finding below.\n\n"
                + json.dumps(repair_context, ensure_ascii=False, indent=2)
            )
        finally:
            self.agent.plan_mode = previous_plan_mode
            self.agent.tools = self.agent.build_tools()
            self.agent.prefix_state = self.agent.build_prefix()
            self.agent.prefix = self.agent.prefix_state.text
        repair_plan = getattr(result, "plan", None) or str(result)
        if self.agent.current_task_state.stop_reason == "user_cancel":
            self._abort("user_cancel")
            return False
        if self.agent.current_task_state.stop_reason != "plan_ready":
            self._abort("repair planner did not produce a valid plan")
            return False
        repair_steps = parse_plan_steps(repair_plan)
        if not repair_steps:
            self._abort("repair planner produced no executable steps")
            return False
        self.run.plan = self.run.plan + "\n\n## Repair Plan\n" + repair_plan
        for index, step in enumerate(repair_steps):
            step.id = f"repair-{self.run.retries}-{index + 1}"
        self.pending_steps = repair_steps + self.pending_steps
        self._trace("orchestration_repair_plan_ready", {"repair_plan": clip(repair_plan, 700)})
        return True

    def _finish(self):
        self.agent.current_task_state = self.task_state
        self.agent.current_run_dir = self.agent.run_store.run_dir(self.task_state)
        if self.run.state == STATE_ABORTED:
            final = "Orchestration aborted: " + self.run.aborted_reason
            self.task_state.stop("user_cancel" if self.run.aborted_reason == "user_cancel" else "orchestration_aborted", final_answer=final)
        elif self.run.state == STATE_UNVERIFIED:
            final = "Orchestration unverified: executable acceptance checks are required to confirm completion."
            self.task_state.stop("verification_missing", final_answer=final)
        elif self.run.state == STATE_DONE:
            final = build_final_summary(self.run)
            self.task_state.finish_success(final)
            self.task_state.verification_status = "verified"
        else:
            final = "Orchestration aborted: incomplete state " + self.run.state
            self.run.state = STATE_ABORTED
            self.task_state.stop("orchestration_aborted", final_answer=final)
        self.run.final_answer = final
        self.agent.run_store.write_task_state(self.task_state)
        self._trace("orchestration_finished", self.run.to_dict())
        self.agent.run_store.write_report(self.task_state, self.agent.redact_artifact({"orchestration": self.run.to_dict(), "task_state": self.task_state.to_dict()}))
        return final

    def _transition(self, state, role="", reason=""):
        self.run.state = state
        self._trace("orchestration_state_transition", {"state": state, "role": role, "reason": reason})

    def _abort(self, reason):
        self.run.state = STATE_ABORTED
        self.run.aborted_reason = str(reason)
        if self.task_state is not None:
            self.task_state.verification_status = "unverified" if reason == "user_cancel" else "failed"
        self._trace("orchestration_aborted", {"reason": self.run.aborted_reason})

    def _trace(self, event, payload):
        payload = dict(payload or {})
        payload["orchestration_state"] = self.run.state if self.run else ""
        payload["created_at"] = now()
        payload["event"] = event
        self.agent.run_store.append_trace(self.task_state, self.agent.redact_artifact(payload))

    def _changed_files(self):
        paths = set()
        for item in self.agent.session["history"][self.history_start:]:
            paths.update(item.get("metadata", {}).get("affected_paths", []))
        try:
            paths.update(self.agent.workspace.changed_files())
        except Exception:
            pass
        return sorted(paths)


def parse_plan_steps(plan_text):
    text = str(plan_text).strip()
    if text.startswith("{"):
        data = json.loads(text)
        steps = []
        for index, item in enumerate(data["steps"]):
            if not isinstance(item, dict) or not isinstance(item.get("goal"), str):
                raise ValueError("plan steps require a goal")
            tools = item.get("allowed_tools", [])
            verification = item.get("verification", {})
            if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools) or not isinstance(verification, dict):
                raise ValueError("invalid plan tool/verification schema")
            step = PlanStep.from_text(item["goal"], index + 1)
            step.allowed_tools = tools
            step.verification = verification
            steps.append(step)
        return steps[:8]
    raw_lines = str(plan_text).splitlines()
    in_steps_section = False
    saw_steps_heading = False
    lines = []
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        if re.match(r"^\s*#{1,6}\s+", raw_line):
            heading = raw_line.strip("# \t").lower()
            if heading.startswith("steps"):
                in_steps_section = True
                saw_steps_heading = True
                continue
            if saw_steps_heading:
                in_steps_section = False
                continue
        if not saw_steps_heading or in_steps_section:
            lines.append(raw_line)
    steps = []
    for line in lines:
        if line[:1].isspace():
            continue
        line = line.strip()
        match = re.match(r"^(?:[-*]|\d+[.)])\s+(.+)$", line)
        if not match:
            continue
        text = match.group(1).strip()
        if not text or text.lower().startswith(("goal", "risk", "file", "acceptance")):
            continue
        steps.append(PlanStep.from_text(text, len(steps) + 1))
    if not steps and str(plan_text).strip():
        steps.append(PlanStep.from_text(str(plan_text).strip(), 1))
    return steps[:8]


def parse_review_findings(review_text):
    text = str(review_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
            items = data.get("findings") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("findings must be a list")
            findings = []
            for item in items:
                if not isinstance(item, dict) or item.get("severity") not in CRITICAL_SEVERITIES | {"warning", "suggestion"} or not item.get("message"):
                    raise ValueError("invalid finding")
                findings.append(ReviewFinding(**{key: str(item.get(key, "")) for key in ("severity", "file", "line", "message", "recommended_action")}))
            return findings
        except (ValueError, TypeError):
            return [ReviewFinding("blocker", message="Review output could not be validated.")]
    findings = []
    for line in text.splitlines():
        line = line.strip(" -\t")
        if not line:
            continue
        if re.search(r"(?i)\b(critical|high|warning|suggestion|error|blocker)\b|严重|高危|警告|建议|错误|阻断", line):
            findings.append(ReviewFinding.from_text(line))
    if not findings and re.search(r"(?i)\b(bug|risk|fail|incorrect|broken)\b", text):
        findings.append(ReviewFinding("blocker", message=text, recommended_action="Return a structured severity before accepting this review."))
    if findings:
        return findings
    if re.fullmatch(r"(?i)(no findings|looks good|all good|no issues|no pending changes|无问题|未发现问题|没有发现问题)[.!。\s]*", text):
        return []
    return [ReviewFinding("blocker", message="Review returned no validated conclusion.")]


def verify_step(agent, specification):
    """Execute declared acceptance checks; descriptive prose is not a check."""
    evidence = {}
    files = specification.get("files", {})
    if not isinstance(files, dict):
        return False, {"error": "verification.files must map paths to exact content or null"}
    for name, expected in files.items():
        path = agent.path(name)
        passed = path.is_file() and (expected is None or path.read_text(encoding="utf-8") == expected)
        evidence[name] = passed
        if not passed:
            return False, evidence
    command = specification.get("command")
    if command:
        result = agent.run_tool("run_shell", {"command": command, "timeout": 120}, check_repetition=False)
        evidence["command"] = {"result": result, **agent._last_tool_result_metadata}
        if agent._last_tool_result_metadata.get("tool_status") != "ok":
            return False, evidence
    return bool(files or command), evidence


def static_findings_to_blocking_review_findings(static_findings):
    findings = []
    for finding in static_findings or []:
        if str(finding.severity).lower() not in CRITICAL_SEVERITIES:
            continue
        findings.append(
            ReviewFinding(
                severity=finding.severity,
                file=finding.file,
                line=str(finding.line),
                message=f"{finding.rule_id}: {finding.message}",
                recommended_action="Triage this static finding before marking orchestration done.",
            )
        )
    return findings


def build_review_prompt(task, plan, step_results, diff, source, files, static_findings=None):
    file_list = "\n".join(f"- {path}" for path in files) if files else "(no changed files detected)"
    static_text = format_static_findings(static_findings or [])
    return (
        "Reviewer role: review the completed execution against the approved plan. "
        'Do not modify files. Prefer JSON {"findings":[{"severity":"critical|high|warning|suggestion|error|blocker",'
        '"file":"relative path","line":"number","message":"finding","recommended_action":"action"}]}. '
        'Return {"findings":[]} or exactly "No findings." when there are no issues.\n\n'
        f"Task:\n{task}\n\n"
        f"Approved / active plan:\n{plan}\n\n"
        f"Step results:\n{json.dumps(step_results, ensure_ascii=False, indent=2)}\n\n"
        f"Changed files:\n{file_list}\n\n"
        f"Static findings:\n{static_text}\n\n"
        "Treat static findings as evidence to triage, not guaranteed bugs. "
        "For each static finding, decide whether it looks real, a false positive, or needs a regression test.\n\n"
        f"Diff source: {source}\n"
        f"Diff:\n```\n{diff or '(empty)'}\n```"
    )


def build_final_summary(run):
    return (
        "Orchestration complete.\n"
        f"- state: {run.state}\n"
        f"- retries: {run.retries}/{run.retry_limit}\n"
        f"- steps: {len(run.step_results)}\n"
        f"- review_findings: {len(run.review_findings)}"
    )
