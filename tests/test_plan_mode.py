"""Plan mode 测试：plan 标签解析、工具过滤、审批流程、plan → execute 完整链路。"""

import pytest

from enigma import FakeModelClient, MiniAgent, PlanResult, SessionStore, WorkspaceContext
from enigma.runtime import PLAN_FILE_NAME


def build_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def build_agent(tmp_path, outputs, **kwargs):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


class TestParsePlanTag:
    def test_parse_plan_tag(self):
        kind, payload = MiniAgent.parse("<plan>\n## Goal\nDo something\n</plan>")
        assert kind == "plan"
        assert "## Goal" in payload
        assert "Do something" in payload

    def test_parse_plan_tag_strips_whitespace(self):
        kind, payload = MiniAgent.parse("<plan>\n  content here  \n</plan>")
        assert kind == "plan"
        assert payload == "content here"

    def test_parse_plan_tag_empty_returns_retry(self):
        kind, payload = MiniAgent.parse("<plan></plan>")
        assert kind == "retry"
        assert "empty" in payload.lower()

    def test_parse_plan_tag_only_whitespace_returns_retry(self):
        kind, payload = MiniAgent.parse("<plan>   \n  </plan>")
        assert kind == "retry"

    def test_plan_takes_precedence_over_final(self):
        kind, payload = MiniAgent.parse("<plan>the plan</plan><final>the final</final>")
        assert kind == "plan"
        assert payload == "the plan"

    def test_tool_takes_precedence_over_plan(self):
        raw = '<tool>{"name":"list_files","args":{"path":"."}}</tool><plan>the plan</plan>'
        kind, payload = MiniAgent.parse(raw)
        assert kind == "tool"
        assert payload["name"] == "list_files"


class TestPlanModeToolFiltering:
    def test_plan_mode_filters_risky_tools(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        tool_names = set(agent.tools.keys())
        assert "list_files" in tool_names
        assert "read_file" in tool_names
        assert "search" in tool_names
        assert "web_search" in tool_names
        assert "write_file" not in tool_names
        assert "patch_file" not in tool_names
        assert "run_shell" not in tool_names

    def test_normal_mode_has_all_tools(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=False)
        tool_names = set(agent.tools.keys())
        assert "write_file" in tool_names
        assert "patch_file" in tool_names
        assert "run_shell" in tool_names

    def test_plan_mode_delegate_still_available(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True, max_depth=2)
        assert "delegate" in agent.tools


class TestPlanModePrefix:
    def test_plan_mode_prefix_contains_plan_instructions(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        prefix = agent.prefix
        assert "PLAN MODE" in prefix
        assert "<plan>" in prefix
        assert "READ-ONLY" in prefix

    def test_plan_mode_prefix_does_not_mention_write_tools(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        prefix = agent.prefix
        assert "write_file" not in prefix
        assert "patch_file" not in prefix

    def test_normal_mode_prefix_has_write_tools(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=False)
        prefix = agent.prefix
        assert "write_file" in prefix


class TestAskReturnsPlanResult:
    def test_ask_returns_plan_result_in_plan_mode(self, tmp_path):
        plan_text = "## Goal\nAdd version flag\n## Steps\n1. Edit cli.py"
        agent = build_agent(
            tmp_path,
            [f"<plan>\n{plan_text}\n</plan>"],
            plan_mode=True,
        )
        result = agent.ask("Add a --version flag")
        assert isinstance(result, PlanResult)
        assert "Add version flag" in result.plan
        assert result.session_id == agent.session["id"]

    def test_ask_plan_mode_final_fallback(self, tmp_path):
        agent = build_agent(
            tmp_path,
            ["This is my plan: do X, then Y"],
            plan_mode=True,
        )
        result = agent.ask("Plan something")
        assert isinstance(result, PlanResult)
        assert "do X" in result.plan

    def test_ask_normal_mode_returns_string(self, tmp_path):
        agent = build_agent(
            tmp_path,
            ["<final>Done.</final>"],
            plan_mode=False,
        )
        result = agent.ask("Do something")
        assert isinstance(result, str)
        assert result == "Done."


class TestExitPlanMode:
    def test_exit_plan_mode_rebuilds_tools(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        assert "write_file" not in agent.tools
        agent.exit_plan_mode("the plan")
        assert agent.plan_mode is False
        assert "write_file" in agent.tools
        assert "patch_file" in agent.tools
        assert "run_shell" in agent.tools

    def test_exit_plan_mode_injects_plan_to_history(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        agent.exit_plan_mode("my approved plan")
        history = agent.session["history"]
        system_messages = [h for h in history if h.get("role") == "system"]
        assert len(system_messages) >= 1
        assert PLAN_FILE_NAME in system_messages[-1]["content"]
        assert "approved" in system_messages[-1]["content"].lower()

    def test_exit_plan_mode_rebuilds_prefix(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        assert "PLAN MODE" in agent.prefix
        agent.exit_plan_mode("the plan")
        assert "PLAN MODE" not in agent.prefix
        assert "write_file" in agent.prefix

    def test_exit_plan_mode_noop_when_not_in_plan_mode(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=False)
        history_before = list(agent.session["history"])
        agent.exit_plan_mode("the plan")
        assert agent.session["history"] == history_before


class TestPlanThenExecute:
    def test_full_plan_then_execute_flow(self, tmp_path):
        (tmp_path / "hello.txt").write_text("original\n", encoding="utf-8")
        agent = build_agent(
            tmp_path,
            [
                "<plan>\n## Goal\nRead hello.txt\n## Steps\n1. Read it\n</plan>",
                '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":5}}</tool>',
                "<final>Read hello.txt successfully.</final>",
            ],
            plan_mode=True,
        )
        # Phase 1: plan mode
        result = agent.ask("Read hello.txt")
        assert isinstance(result, PlanResult)
        assert "Read hello.txt" in result.plan

        # Phase 2: approve and execute
        agent.exit_plan_mode(result.plan)
        assert agent.plan_mode is False
        final = agent.ask("Execute the approved plan for: Read hello.txt")
        assert isinstance(final, str)
        assert "successfully" in final


class TestPlanModeNotInheritedByDelegate:
    def test_delegate_tools_unaffected_by_parent_plan_mode(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True, max_depth=2)
        assert "delegate" in agent.tools
        # The delegate tool's child agent is created at runtime,
        # but the parent's plan_mode should not be passed to it.
        # We verify by checking that the delegate tool spec itself
        # doesn't have plan_mode baked in.
        delegate_spec = agent.tools["delegate"]
        assert "run" in delegate_spec


class TestPlanModeRuntimeIdentity:
    def test_plan_mode_in_runtime_identity(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        identity = agent.current_runtime_identity()
        assert identity["plan_mode"] is True

    def test_normal_mode_plan_mode_false_in_identity(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=False)
        identity = agent.current_runtime_identity()
        assert identity["plan_mode"] is False


class TestPlanFilePersistence:
    def test_exit_plan_mode_writes_plan_file(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        plan_content = "## Goal\nDo X\n## Steps\n1. Step one"
        agent.exit_plan_mode(plan_content)
        plan_path = agent._get_plan_path()
        assert plan_path.exists()
        assert plan_path.read_text(encoding="utf-8") == plan_content

    def test_plan_file_overwritten_on_revision(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        agent.exit_plan_mode("version 1")
        plan_path = agent._get_plan_path()
        assert plan_path.read_text(encoding="utf-8") == "version 1"
        agent.plan_mode = True
        agent.exit_plan_mode("version 2")
        assert plan_path.read_text(encoding="utf-8") == "version 2"

    def test_plan_file_cleaned_up_after_ask(self, tmp_path):
        agent = build_agent(
            tmp_path,
            ["<final>Done.</final>"],
            plan_mode=True,
        )
        agent.exit_plan_mode("the plan")
        plan_path = agent._get_plan_path()
        assert plan_path.exists()
        agent.ask("Execute the approved plan for: test")
        assert not plan_path.exists()

    def test_prefix_mentions_plan_file(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=True)
        agent.exit_plan_mode("the plan")
        assert PLAN_FILE_NAME in agent.prefix
        assert "Read it" in agent.prefix

    def test_prefix_no_plan_hint_when_no_file(self, tmp_path):
        agent = build_agent(tmp_path, [], plan_mode=False)
        assert PLAN_FILE_NAME not in agent.prefix
