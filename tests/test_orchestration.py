import json
from unittest.mock import patch

from enigma import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from enigma.orchestration import parse_plan_steps, parse_review_findings


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    feature_flags = kwargs.pop("feature_flags", {})
    feature_flags.setdefault("reflection", False)
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        feature_flags=feature_flags,
        **kwargs,
    )


def orchestration_events(agent):
    return [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_parse_plan_steps_extracts_numbered_steps():
    steps = parse_plan_steps("## Steps\n1. Update README narrative\n2. Run tests\n## Risks\n- keep it small")

    assert [step.id for step in steps] == ["step-1", "step-2"]
    assert steps[0].goal == "Update README narrative"


def test_parse_plan_steps_ignores_nested_bullets():
    steps = parse_plan_steps(
        "## Steps\n"
        "1. Inspect README narrative\n"
        "   - Check harness positioning\n"
        "   - Check safety policy wording\n"
        "2. Produce conclusion\n"
        "\n"
        "## Risks / open questions\n"
        "- Keep it read-only\n"
    )

    assert [step.goal for step in steps] == ["Inspect README narrative", "Produce conclusion"]


def test_parse_review_findings_detects_blocking_severity():
    findings = parse_review_findings("- critical: README omits the review loop\n- suggestion: tighten wording")

    assert len(findings) == 2
    assert findings[0].is_blocking() is True
    assert findings[1].is_blocking() is False


def test_orchestrate_normal_plan_execute_review_done(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Inspect README\n</plan>",
            "<final>Executor completed the active step.</final>",
            "<final>No findings.</final>",
        ],
    )

    result = agent.orchestrate("Inspect README")

    assert "Orchestration complete." in result
    events = orchestration_events(agent)
    states = [event.get("state") or event.get("orchestration_state") for event in events]
    assert "PLANNING" in states
    assert "EXECUTING" in states
    assert "REVIEWING" in states
    assert events[-1]["event"] == "orchestration_finished"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    assert report["orchestration"]["state"] == "DONE"
    assert report["orchestration"]["step_results"][0]["status"] == "ok"


def test_orchestrate_execution_failure_enters_repair_plan(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Patch README\n</plan>",
            "<final>error: patch failed</final>",
            "<plan>\n## Steps\n1. Read README before patching\n</plan>",
            "<final>Executor completed repair.</final>",
            "<final>No findings.</final>",
        ],
    )

    result = agent.orchestrate("Patch README")

    assert "Orchestration complete." in result
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    assert report["orchestration"]["retries"] == 1
    assert any(result["status"] == "failed" for result in report["orchestration"]["step_results"])
    assert any(event["event"] == "orchestration_repair_plan_ready" for event in orchestration_events(agent))


def test_orchestrate_review_finding_blocks_done_until_repair(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Update README\n</plan>",
            "<final>Executor completed the active step.</final>",
            "<final>critical: README update missed the acceptance check.</final>",
            "<plan>\n## Steps\n1. Add the missing acceptance check\n</plan>",
            "<final>Executor completed repair.</final>",
            "<final>No findings.</final>",
        ],
    )

    result = agent.orchestrate("Update README")

    assert "Orchestration complete." in result
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    assert report["orchestration"]["retries"] == 1
    assert report["orchestration"]["review_findings"] == []
    events = orchestration_events(agent)
    assert any(event.get("reason") == "blocking review finding" for event in events)


def test_orchestrate_aborts_after_retry_limit(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Patch README\n</plan>",
            "<final>error: patch failed</final>",
        ],
    )

    result = agent.orchestrate("Patch README", retry_limit=0)

    assert result.startswith("Orchestration aborted:")
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    assert report["orchestration"]["state"] == "ABORTED"


def test_reviewer_role_uses_read_only_tool_surface(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Inspect README\n</plan>",
            "<final>Executor completed the active step.</final>",
            "<final>No findings.</final>",
        ],
    )

    agent.orchestrate("Inspect README")

    reviewer_prompt = next(prompt for prompt in agent.model_client.prompts if "Reviewer role:" in prompt)
    assert "write_file" not in reviewer_prompt
    assert "patch_file" not in reviewer_prompt
    assert "run_shell" not in reviewer_prompt


def test_orchestrate_static_critical_finding_blocks_done(tmp_path):
    (tmp_path / "code.py").write_text("eval(user_input)\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            "<plan>\n## Steps\n1. Inspect code\n</plan>",
            "<final>Executor completed the active step.</final>",
            "<final>No findings.</final>",
        ],
    )

    with patch.object(WorkspaceContext, "changed_files", return_value=["code.py"]):
        result = agent.orchestrate("Inspect code", retry_limit=0)

    assert result.startswith("Orchestration aborted:")
    reviewer_prompt = next(prompt for prompt in agent.model_client.prompts if "Reviewer role:" in prompt)
    assert "PY_DYNAMIC_EXEC" in reviewer_prompt
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    assert report["orchestration"]["static_findings"][0]["rule_id"] == "PY_DYNAMIC_EXEC"
    assert report["orchestration"]["review_findings"][0]["severity"] == "critical"
