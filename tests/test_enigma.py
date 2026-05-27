import os
import json
import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import enigma as mini_pkg
from enigma import cli as mini_cli
from enigma import (
    AnthropicCompatibleModelClient,
    FakeModelClient,
    MiniAgent,
    OllamaModelClient,
    OpenAICompatibleModelClient,
    SessionStore,
    WorkspaceContext,
    build_welcome,
)
from enigma.web_search import SearchResult


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


def test_agent_runs_tool_then_final(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Read the file successfully.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Read the file successfully."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    assert "hello.txt" in agent.session["memory"]["files"]


def test_agent_updates_task_summary_on_each_request(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>First pass.</final>",
            "<final>Second pass.</final>",
        ],
    )

    assert agent.ask("First request") == "First pass."
    assert agent.session["memory"]["working"]["task_summary"] == "First request"

    assert agent.ask("Second request") == "Second pass."
    assert agent.session["memory"]["working"]["task_summary"] == "Second request"


def test_agent_only_stores_reusable_epistemic_notes(tmp_path):
    (tmp_path / "facts.txt").write_text("deploy key is red\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"facts.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
            "<final>It is red.</final>",
        ],
    )

    assert agent.ask("Read the file and remember the fact") == "Done."
    notes = agent.session["memory"]["episodic_notes"]
    assert any("deploy key is red" in note["text"] for note in notes)
    assert not any(note["text"] == "Done." for note in notes)
    assert not any(note["text"] == "Done." for note in notes)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>It is red.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("What color is the deploy key?") == "It is red."
    prompt = resumed.model_client.prompts[-1]
    assert "召回记忆" in prompt
    assert "deploy key is red" in prompt


def test_file_summary_cache_is_invalidated_on_out_of_band_edit_and_path_spelling(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    agent.memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    agent.memory.remember_file("./sample.txt")
    assert agent.memory.to_dict()["file_summaries"]["sample.txt"]["freshness"]

    assert "sample.txt: alpha" in agent.memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient([]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert "sample.txt: alpha" not in resumed.memory_text()
    resumed.memory.invalidate_file_summary("sample.txt")
    assert "sample.txt" not in resumed.memory.to_dict()["file_summaries"]


def test_agent_retries_after_empty_model_output(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "<final>Recovered after retry.</final>",
        ],
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after retry."
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("empty response" in item for item in notices)


def test_agent_retries_after_malformed_tool_payload(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":"bad"}</tool>',
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Recovered after malformed tool output.</final>",
        ],
    )

    answer = agent.ask("Inspect hello.txt")

    assert answer == "Recovered after malformed tool output."
    assert any(item["role"] == "tool" and item["name"] == "read_file" for item in agent.session["history"])
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("valid <tool> call" in item for item in notices)


def test_agent_accepts_xml_write_file_tool(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>',
            "<final>Done.</final>",
        ],
    )

    answer = agent.ask("Create hello.py")

    assert answer == "Done."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == 'print("hi")\n'


def test_retries_do_not_consume_the_whole_budget(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "",
            "",
            "<final>Recovered after several retries.</final>",
        ],
        max_steps=1,
    )

    answer = agent.ask("Do the task")

    assert answer == "Recovered after several retries."


def test_agent_saves_and_resumes_session(tmp_path):
    agent = build_agent(tmp_path, ["<final>First pass.</final>"])
    assert agent.ask("Start a session") == "First pass."

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.session["history"][0]["content"] == "Start a session"
    assert resumed.ask("Continue") == "Resumed."


def test_delegate_uses_child_agent(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"delegate","args":{"task":"inspect README","max_steps":2}}</tool>',
            "<final>Child result.</final>",
            "<final>Parent incorporated the child result.</final>",
        ],
    )

    answer = agent.ask("Use delegation")

    assert answer == "Parent incorporated the child result."
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[0]["name"] == "delegate"
    assert "delegate_result" in tool_events[0]["content"]


def test_patch_file_replaces_exact_match(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello world\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "patch_file",
        {
            "path": "sample.txt",
            "old_text": "world",
            "new_text": "agent",
        },
    )

    assert result == "patched sample.txt"
    assert file_path.read_text(encoding="utf-8") == "hello agent\n"


def test_invalid_risky_tool_does_not_prompt_for_approval(tmp_path):
    agent = build_agent(tmp_path, [], approval_policy="ask")

    with patch("builtins.input") as mock_input:
        result = agent.run_tool("write_file", {})

    assert result.startswith("error: invalid arguments for write_file: 'path'")
    assert 'example: <tool name="write_file"' in result
    mock_input.assert_not_called()


def test_list_files_hides_internal_agent_state(tmp_path):
    agent = build_agent(tmp_path, [])
    (tmp_path / ".enigma").mkdir(exist_ok=True)
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")

    result = agent.run_tool("list_files", {})

    assert ".enigma" not in result
    assert ".git" not in result
    assert "[F] hello.txt" in result


def test_repeated_identical_tool_call_is_rejected(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.append_session_history({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "1"})
    agent.append_session_history({"role": "tool", "name": "list_files", "args": {}, "content": "(empty)", "created_at": "2"})

    result = agent.run_tool("list_files", {})

    assert result == "error: repeated identical tool call for list_files; choose a different tool or return a final answer"


def test_web_search_is_advertised_in_prompt(tmp_path):
    agent = build_agent(tmp_path, [])

    prompt = agent.prompt("What is the latest Python release?")

    assert "- web_search(" in prompt
    assert "Use web_search for current information" in prompt
    assert "include source URLs" in prompt


def test_web_search_rejects_invalid_arguments(tmp_path):
    agent = build_agent(tmp_path, [])

    assert "query must not be empty" in agent.run_tool("web_search", {"query": ""})
    assert "max_results must be in [1, 10]" in agent.run_tool("web_search", {"query": "python", "max_results": 0})
    assert "max_results must be in [1, 10]" in agent.run_tool("web_search", {"query": "python", "max_results": 99})
    assert "query must be at most 300 characters" in agent.run_tool("web_search", {"query": "x" * 301})


def test_web_search_formats_mock_results(tmp_path):
    agent = build_agent(tmp_path, [])
    results = [
        SearchResult("Python 3.13 Released", "https://python.org/news", "Release notes are available."),
        SearchResult("Docs update", "https://docs.python.org/3/", "Documentation changed today."),
    ]

    with patch("enigma.web_search.default_search_client.search", return_value=results) as fake_search:
        output = agent.run_tool("web_search", {"query": "latest Python release", "max_results": 2})

    assert output == "\n".join(
        [
            "web_search_results:",
            "1. Python 3.13 Released",
            "   url: https://python.org/news",
            "   snippet: Release notes are available.",
            "2. Docs update",
            "   url: https://docs.python.org/3/",
            "   snippet: Documentation changed today.",
        ]
    )
    fake_search.assert_called_once_with(
        "latest Python release",
        2,
        allowed_domains=set(),
        blocked_domains=set(),
    )


def test_web_search_passes_domain_filters_to_client(tmp_path):
    agent = build_agent(tmp_path, [])

    with patch("enigma.web_search.default_search_client.search", return_value=[]) as fake_search:
        output = agent.run_tool(
            "web_search",
            {
                "query": "python",
                "allowed_domains": "python.org, docs.python.org",
                "blocked_domains": "spam.example",
            },
        )

    assert output == "web_search_results:\n(no results)"
    fake_search.assert_called_once_with(
        "python",
        5,
        allowed_domains={"python.org", "docs.python.org"},
        blocked_domains={"spam.example"},
    )


def test_web_search_backend_failure_is_readable(tmp_path):
    agent = build_agent(tmp_path, [])

    with patch("enigma.web_search.default_search_client.search", side_effect=TimeoutError("slow network")):
        output = agent.run_tool("web_search", {"query": "python"})

    assert output == "error: web_search failed: slow network"


def test_tool_call_message_is_parsed_and_displayed_before_tool(tmp_path):
    stream = io.StringIO()
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"message":"I will list the workspace first.","name":"list_files","args":{"path":"."}}</tool>',
            "<final>Done.</final>",
        ],
        show_tool_activity=True,
        tool_activity_stream=stream,
    )

    answer = agent.ask("Inspect the workspace")

    assert answer == "Done."
    output = stream.getvalue()
    assert "[enigma] list_files path=\".\"" in output
    assert "| I will list the workspace first." in output
    tool_events = [item for item in agent.session["history"] if item["role"] == "tool"]
    assert tool_events[-1]["message"] == "I will list the workspace first."


def test_tool_activity_uses_default_message_when_model_omits_one(tmp_path):
    stream = io.StringIO()
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"web_search","args":{"query":"latest Python release","max_results":1}}</tool>',
            "<final>Done.</final>",
        ],
        show_tool_activity=True,
        tool_activity_stream=stream,
    )

    with patch("enigma.web_search.default_search_client.search", return_value=[]):
        answer = agent.ask("Search current info")

    assert answer == "Done."
    output = stream.getvalue()
    assert "[enigma] web_search query=\"latest Python release\"" in output
    assert "| Searching the web for latest Python release." in output


def test_token_usage_metadata_is_displayed_during_conversation(tmp_path):
    class UsageFakeModelClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.last_completion_metadata = {
                "input_tokens": 100,
                "output_tokens": 12,
                "total_tokens": 112,
                "cached_tokens": 80,
                "cache_hit": True,
            }
            return super().complete(prompt, max_new_tokens, **kwargs)

    stream = io.StringIO()
    agent = MiniAgent(
        model_client=UsageFakeModelClient(["<final>Done.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=SessionStore(tmp_path / ".enigma" / "sessions"),
        approval_policy="auto",
        show_token_usage=True,
        tool_activity_stream=stream,
        feature_flags={"reflection": False},
    )

    assert agent.ask("Show usage") == "Done."

    assert "[usage] in=100  out=12  total=112  cached=80  cache_hit=true" in stream.getvalue()


def test_welcome_screen_keeps_box_shape_for_long_paths(tmp_path):
    deep = tmp_path / "very" / "long" / "path" / "for" / "the" / "mini" / "agent" / "welcome" / "screen"
    deep.mkdir(parents=True)
    agent = build_agent(deep, [])

    welcome = build_welcome(agent, model="qwen3.5:4b", host="http://127.0.0.1:11434")
    lines = welcome.splitlines()

    assert len(lines) >= 5
    assert len({len(line) for line in lines}) == 1
    assert "..." in welcome
    assert "(  o o  )" in welcome
    assert "MINI-CODING-AGENT" not in welcome
    assert "MINI CODING AGENT" not in welcome
    assert "enigma" in welcome
    assert "local coding agent" in welcome
    assert "// READY" not in welcome
    assert "SLASH" not in welcome
    assert "READY      " not in welcome
    assert "commands: Commands:" not in welcome


def test_ollama_client_posts_expected_payload():
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"response": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OllamaModelClient(
        model="qwen3.5:4b",
        host="http://127.0.0.1:11434",
        temperature=0.2,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == 30
    assert captured["body"]["model"] == "qwen3.5:4b"
    assert captured["body"]["prompt"] == "hello"
    assert captured["body"]["stream"] is False


def test_openai_compatible_client_posts_expected_responses_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"output_text": "<final>ok</final>"}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://right.codes/v1/responses"
    assert captured["timeout"] == 30
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "right.codes/codex-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_output_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_openai_compatible_client_sends_prompt_cache_fields_and_records_usage():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output_text": "<final>ok</final>",
                    "usage": {
                        "input_tokens": 2048,
                        "input_tokens_details": {"cached_tokens": 1536},
                        "output_tokens": 32,
                        "total_tokens": 2080,
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete(
            "hello",
            42,
            prompt_cache_key="prefix-hash-123",
            prompt_cache_retention="in_memory",
        )

    assert result == "<final>ok</final>"
    assert captured["body"]["prompt_cache_key"] == "prefix-hash-123"
    assert captured["body"]["prompt_cache_retention"] == "in_memory"
    assert client.last_completion_metadata["prompt_cache_supported"] is True
    assert client.last_completion_metadata["cached_tokens"] == 1536
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["input_tokens"] == 2048


def test_openai_compatible_client_extracts_text_from_event_stream():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'data: {"type":"response.created","response":{"id":"resp_1","output":[]}}\n'
                'data: {"type":"response.completed","response":{"output":[{"content":[{"text":"<final>stream ok</final>"}]}]}}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>stream ok</final>"


def test_openai_compatible_client_extracts_text_from_event_stream_deltas():
    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"<final>"}\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"OK"}\n'
                'event: response.output_text.done\n'
                'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n'
                "data: [DONE]\n"
            ).encode("utf-8")

    client = OpenAICompatibleModelClient(
        model="right.codes/codex-mini",
        base_url="https://right.codes/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>OK</final>"


def test_anthropic_compatible_client_posts_expected_messages_payload():
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "<final>ok</final>",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", fake_urlopen):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"
    assert captured["url"] == "https://www.right.codes/claude-aws/v1/messages"
    assert captured["timeout"] == 30
    assert captured["headers"]["X-api-key"] == "sk-test"
    assert captured["headers"]["Anthropic-version"] == "2023-06-01"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == {
        "model": "claude-sonnet-4-5-20250929",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hello",
                    }
                ],
            }
        ],
        "max_tokens": 42,
        "stream": False,
        "temperature": 0.2,
    }


def test_anthropic_compatible_client_extracts_first_text_block():
    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "<final>ok</final>"},
                    ]
                }
            ).encode("utf-8")

    client = AnthropicCompatibleModelClient(
        model="claude-sonnet-4-5-20250929",
        base_url="https://www.right.codes/claude-aws/v1",
        api_key="sk-test",
        temperature=0.2,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        result = client.complete("hello", 42)

    assert result == "<final>ok</final>"


def test_build_agent_uses_openai_provider_and_model_override(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "openai",
            "model": "override-model",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_BASE": "https://www.right.codes/codex/v1",
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_MODEL": "env-model",
        },
        clear=False,
    ):
        with patch(
            "enigma.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("enigma.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = mini_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["model"] == "override-model"
    assert mock_openai.call_args.kwargs["base_url"] == "https://www.right.codes/codex/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


def test_build_arg_parser_defaults_provider_to_openai(tmp_path):
    args = mini_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    assert args.provider == "openai"


def test_build_arg_parser_accepts_anthropic_provider(tmp_path):
    args = mini_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    assert args.provider == "anthropic"


def test_build_agent_uses_anthropic_provider_and_openai_key_fallback(tmp_path):
    args = type(
        "Args",
        (),
        {
            "cwd": str(tmp_path),
            "provider": "anthropic",
            "model": "claude-sonnet-4-5-20250929",
            "base_url": None,
            "host": "http://127.0.0.1:11434",
            "ollama_timeout": 300,
            "openai_timeout": 300,
            "temperature": 0.2,
            "top_p": 0.9,
            "resume": None,
            "approval": "ask",
            "secret_env_names": [],
            "max_steps": 6,
            "max_new_tokens": 512,
        },
    )()

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-openai-fallback",
        },
        clear=True,
    ):
        with patch(
            "enigma.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch(
            "enigma.cli.OpenAICompatibleModelClient",
            side_effect=AssertionError("openai client should not be used"),
        ), patch("enigma.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            fake_client = mock_anthropic.return_value
            agent = mini_pkg.build_agent(args)

    mock_anthropic.assert_called_once()
    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-5-20250929"
    assert mock_anthropic.call_args.kwargs["base_url"] == "https://www.right.codes/claude/v1"
    assert mock_anthropic.call_args.kwargs["api_key"] == "sk-openai-fallback"
    assert agent.model_client is fake_client


def test_build_agent_uses_anthropic_default_model_when_env_is_missing(tmp_path):
    args = mini_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--provider", "anthropic"])

    with patch.dict(
        os.environ,
        {},
        clear=False,
    ):
        os.environ.pop("ANTHROPIC_MODEL", None)
        with patch("enigma.cli.AnthropicCompatibleModelClient") as mock_anthropic:
            mini_pkg.build_agent(args)

    assert mock_anthropic.call_args.kwargs["model"] == "claude-sonnet-4-6"


def test_build_agent_uses_openai_provider_by_default(tmp_path):
    args = mini_pkg.build_arg_parser().parse_args(["--cwd", str(tmp_path)])

    with patch.dict(
        os.environ,
        {
            "OPENAI_API_BASE": "https://www.right.codes/codex/v1",
            "OPENAI_API_KEY": "sk-test",
        },
        clear=False,
    ):
        with patch(
            "enigma.cli.OllamaModelClient",
            side_effect=AssertionError("ollama client should not be used"),
        ), patch("enigma.cli.OpenAICompatibleModelClient") as mock_openai:
            fake_client = mock_openai.return_value
            agent = mini_pkg.build_agent(args)

    mock_openai.assert_called_once()
    assert mock_openai.call_args.kwargs["model"] == "gpt-5.4"
    assert mock_openai.call_args.kwargs["base_url"] == "https://www.right.codes/codex/v1"
    assert mock_openai.call_args.kwargs["api_key"] == "sk-test"
    assert agent.model_client is fake_client


def test_successful_run_persists_run_artifacts_and_stop_reason(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":2}}</tool>',
            "<final>Finished.</final>",
        ],
    )

    assert agent.ask("Do the thing") == "Finished."

    runs_root = tmp_path / ".enigma" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    task_state = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()

    assert task_state["task_id"] != task_state["run_id"]
    assert run_dir.name == task_state["run_id"]
    assert (run_dir / "task_state.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.json").exists()
    assert task_state["stop_reason"] == "final_answer_returned"
    assert task_state["final_answer"] == "Finished."
    assert report["stop_reason"] == "final_answer_returned"
    assert report["task_state"]["stop_reason"] == "final_answer_returned"
    assert report["run_id"] == task_state["run_id"]
    trace_events = [json.loads(line)["event"] for line in trace_lines]
    assert trace_events[0] == "run_started"
    assert trace_events[-1] == "run_finished"
    assert trace_events.count("prompt_built") == 2
    assert "tool_executed" in trace_events


def test_trace_and_report_redact_secret_env_values(tmp_path):
    secret = "sk-test-secret-123"
    with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True):
        agent = build_agent(
            tmp_path,
            [
                '<tool>{"name":"run_shell","args":{"command":"echo sk-test-secret-123","timeout":20}}</tool>',
                "<final>Masked.</final>",
            ],
        )

        assert agent.ask("Mask the secret") == "Masked."

    runs_root = tmp_path / ".enigma" / "runs"
    run_dirs = [path for path in runs_root.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1

    run_dir = run_dirs[0]
    trace_text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    report_text = (run_dir / "report.json").read_text(encoding="utf-8")
    trace_events = [json.loads(line) for line in trace_text.splitlines()]

    assert secret not in trace_text
    assert secret not in report_text

    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events
    assert prompt_events[0]["prompt_metadata"]["secret_env_count"] >= 1
    assert "OPENAI_API_KEY" in prompt_events[0]["prompt_metadata"]["secret_env_names"]

    tool_events = [event for event in trace_events if event["event"] == "tool_executed"]
    assert tool_events
    assert "<redacted>" in tool_events[0]["args"]["command"]
    assert "<redacted>" in tool_events[0]["result"]


def test_prompt_budget_metadata_records_budget_decisions(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    agent.memory.append_note("alpha episodic note " + ("A" * 120), tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    agent.memory.append_note("beta episodic recall note " + ("B" * 120), created_at="2026-04-07T10:01:00+00:00")
    agent.memory.append_note("gamma episodic note " + ("C" * 120), tags=("recall",), created_at="2026-04-07T10:02:00+00:00")

    for index in range(4):
        agent.append_session_history(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 240),
                "created_at": f"2026-04-07T10:0{index}:00+00:00",
            }
        )

    agent.context_manager.total_budget = 1000
    agent.context_manager.section_budgets = {
        "prefix": 80,
        "memory": 80,
        "relevant_memory": 80,
        "history": 80,
    }

    assert agent.ask("recall") == "Done."

    trace_events = [
        json.loads(line)
        for line in (agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines())
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events
    metadata = prompt_events[0]["prompt_metadata"]
    relevant_section = agent.model_client.prompts[0].split("召回记忆:\n", 1)[1].split("\n\nhistory:", 1)[0]

    assert metadata["relevant_memory"]["selected_count"] == 3
    assert len(metadata["relevant_memory"]["rendered_notes"]) == 3
    assert len([line for line in relevant_section.splitlines() if line.startswith("- ")]) == 3
    assert "alpha episodic" in relevant_section
    assert "beta episodic" in relevant_section
    assert "gamma episodic" in relevant_section
    assert metadata["current_request"]["text"] == "recall"
    assert metadata["current_request"]["rendered_chars"] == len("recall")


def test_prompt_metadata_refreshes_prefix_when_workspace_changes(tmp_path):
    agent = build_agent(tmp_path, [])

    first = agent.prompt_metadata("first", "")
    second = agent.prompt_metadata("second", "")

    assert first["prefix_hash"] == second["prefix_hash"]
    assert second["prefix_changed"] is False
    assert second["workspace_changed"] is False

    (tmp_path / "README.md").write_text("demo changed\n", encoding="utf-8")

    third = agent.prompt_metadata("third", "")

    assert third["prefix_hash"] != second["prefix_hash"]
    assert third["prefix_changed"] is True
    assert third["workspace_changed"] is True
    assert "demo changed" in agent.prefix


def test_agent_creates_checkpoint_when_context_reduction_happens_and_artifacts_only_reference_it(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done after checkpoint.</final>"])
    for index in range(10):
        agent.append_session_history(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("A" * 260),
                "created_at": f"2026-04-07T10:{index:02d}:00+00:00",
            }
        )
    agent.memory.append_note("checkpoint note " + ("B" * 220), tags=("checkpoint",), created_at="2026-04-07T11:00:00+00:00")
    agent.context_manager.total_budget = 900
    agent.context_manager.section_budgets = {
        "prefix": 120,
        "memory": 120,
        "relevant_memory": 120,
        "history": 160,
    }

    assert agent.ask("Resume the long task") == "Done after checkpoint."

    checkpoint_state = agent.session["checkpoints"]
    checkpoint = checkpoint_state["items"][checkpoint_state["current_id"]]
    assert checkpoint["checkpoint_id"] == checkpoint_state["current_id"]
    assert checkpoint["schema_version"] == "phase1-v1"
    assert checkpoint["current_goal"] == "Resume the long task"
    assert checkpoint["key_files"] == []
    assert checkpoint["current_blocker"] == ""
    assert checkpoint["current_plan"] == ["Task completed; preserve the final answer for follow-up context."]
    assert checkpoint["open_questions"] == []
    assert checkpoint["confirmed_findings"] == ["Final answer: Done after checkpoint."]
    assert checkpoint["blocked_on"] == ""
    assert checkpoint["next_action"] == checkpoint["next_step"]
    assert checkpoint["next_step"]

    task_state = json.loads(agent.run_store.task_state_path(agent.current_task_state).read_text(encoding="utf-8"))
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]

    assert task_state["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert report["task_state"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in task_state
    assert "current_goal" not in report
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]
    assert checkpoint_events
    assert checkpoint_events[-1]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert "current_goal" not in checkpoint_events[-1]


def test_resume_prompt_uses_checkpoint_state_not_just_history(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_manual",
        "items": {
            "ckpt_manual": {
                "checkpoint_id": "ckpt_manual",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix failing resume flow",
                "current_plan": ["Re-read stale anchors", "Refresh checkpoint"],
                "open_questions": ["Is runtime.py still stale?"],
                "confirmed_findings": ["Read runtime.py"],
                "completed": ["Read runtime.py"],
                "excluded": ["Do not add branch summary"],
                "current_blocker": "Need to re-anchor stale file facts",
                "blocked_on": "stale runtime.py summary",
                "next_action": "Re-read runtime.py and refresh the checkpoint",
                "next_step": "Re-read runtime.py and refresh the checkpoint",
                "key_files": [{"path": "runtime.py", "freshness": "abc"}],
                "freshness": {"runtime.py": "abc"},
                "summary": "Resume from the latest checkpoint",
                "runtime_identity": {"workspace_fingerprint": "old-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    prompt = resumed.model_client.prompts[-1]
    assert "Task checkpoint:" in prompt
    assert "Current goal: Fix failing resume flow" in prompt
    assert "Current blocker: Need to re-anchor stale file facts" in prompt
    assert "Current plan: Re-read stale anchors | Refresh checkpoint" in prompt
    assert "Confirmed findings: Read runtime.py" in prompt
    assert "Open questions: Is runtime.py still stale?" in prompt
    assert "Blocked on: stale runtime.py summary" in prompt
    assert "Next action: Re-read runtime.py and refresh the checkpoint" in prompt
    assert "Next step: Re-read runtime.py and refresh the checkpoint" in prompt


def test_resume_invalidates_stale_file_summaries_and_marks_partial_stale(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_stale",
        "items": {
            "ckpt_stale": {
                "checkpoint_id": "ckpt_stale",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Fix stale summary handling",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py is important",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."

    assert "runtime.py" not in resumed.memory.to_dict()["file_summaries"]
    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events[0]["prompt_metadata"]["resume_status"] == "partial-stale"
    assert prompt_events[0]["prompt_metadata"]["stale_summary_invalidations"] == 1
    assert resumed.last_prompt_metadata["resume_status"] == "full-valid"


def test_run_shell_nonzero_with_workspace_change_is_recorded_as_partial_success(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    assert "exit_code: 1" in result
    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["affected_paths"] == ["README.md"]
    assert agent._last_tool_result_metadata["workspace_changed"] is True


def test_resume_marks_workspace_mismatch_when_checkpoint_runtime_identity_is_stale(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_workspace",
        "items": {
            "ckpt_workspace": {
                "checkpoint_id": "ckpt_workspace",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after drift",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime state",
                "key_files": [],
                "freshness": {},
                "summary": "workspace changed",
                "runtime_identity": {"workspace_fingerprint": "outdated-fingerprint"},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events[0]["prompt_metadata"]["resume_status"] == "workspace-mismatch"
    assert resumed.last_prompt_metadata["resume_status"] == "full-valid"


def test_write_file_trace_records_minimum_tool_contract_fields(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"write_file","args":{"path":"notes.txt","content":"hello\\n"}}</tool>',
            "<final>Done.</final>",
        ],
    )

    assert agent.ask("Create notes.txt") == "Done."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    tool_event = [event for event in trace_events if event["event"] == "tool_executed"][-1]

    assert tool_event["name"] == "write_file"
    assert tool_event["risk_level"] == "high"
    assert tool_event["read_only"] is False
    assert tool_event["tool_status"] == "ok"
    assert tool_event["affected_paths"] == ["notes.txt"]
    assert tool_event["workspace_changed"] is True
    assert tool_event["diff_summary"] == ["created:notes.txt"]


def test_resume_marks_schema_mismatch_when_checkpoint_version_is_incompatible(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_schema",
        "items": {
            "ckpt_schema": {
                "checkpoint_id": "ckpt_schema",
                "parent_checkpoint_id": "",
                "schema_version": "legacy-v0",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Continue after schema change",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Migrate checkpoint",
                "key_files": [],
                "freshness": {},
                "summary": "schema changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "schema-mismatch"


def test_resume_marks_no_checkpoint_when_session_has_no_checkpoint_state(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session.pop("checkpoints", None)
    agent.session_store.save(agent.session)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
    )

    assert resumed.ask("Continue the task") == "Resumed."
    assert resumed.last_prompt_metadata["resume_status"] == "no-checkpoint"
    assert "Task checkpoint:" not in resumed.model_client.prompts[-1]


def test_freshness_mismatch_creates_checkpoint_before_model_completion(tmp_path):
    file_path = tmp_path / "runtime.py"
    file_path.write_text("alpha\n", encoding="utf-8")
    agent = build_agent(tmp_path, ["<final>Resumed.</final>"])
    agent.memory.set_file_summary("runtime.py", "runtime.py: alpha")
    freshness = agent.memory.to_dict()["file_summaries"]["runtime.py"]["freshness"]
    agent.session["checkpoints"] = {
        "current_id": "ckpt_freshness",
        "items": {
            "ckpt_freshness": {
                "checkpoint_id": "ckpt_freshness",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Handle freshness mismatch",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Re-read runtime.py",
                "key_files": [{"path": "runtime.py", "freshness": freshness}],
                "freshness": {"runtime.py": freshness},
                "summary": "runtime.py changed",
                "runtime_identity": {"workspace_fingerprint": agent.workspace.fingerprint()},
            }
        },
    }
    agent.session_store.save(agent.session)
    file_path.write_text("beta\n", encoding="utf-8")

    assert agent.ask("Continue the task") == "Resumed."

    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    checkpoint_events = [event for event in trace_events if event["event"] == "checkpoint_created"]

    assert checkpoint_events
    assert checkpoint_events[0]["trigger"] == "freshness_mismatch"


def test_runtime_identity_persists_key_execution_metadata(tmp_path):
    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    agent = MiniAgent(
        model_client=FakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False, "reflection": False},
    )

    runtime_identity = agent.session["runtime_identity"]

    assert runtime_identity["session_id"] == agent.session["id"]
    assert runtime_identity["cwd"] == str(tmp_path)
    assert runtime_identity["approval_policy"] == "never"
    assert runtime_identity["read_only"] is False
    assert runtime_identity["max_steps"] == 9
    assert runtime_identity["max_new_tokens"] == 1024
    assert runtime_identity["feature_flags"]["memory"] is True
    assert runtime_identity["feature_flags"]["relevant_memory"] is False
    assert runtime_identity["shell_env_allowlist"] == list(agent.shell_env_allowlist)


def test_resume_records_runtime_identity_mismatch_fields_in_metadata_and_trace(tmp_path):
    agent = build_agent(tmp_path, ["<final>checkpoint ready.</final>"])
    agent.session["checkpoints"] = {
        "current_id": "ckpt_identity",
        "items": {
            "ckpt_identity": {
                "checkpoint_id": "ckpt_identity",
                "parent_checkpoint_id": "",
                "schema_version": "phase1-v1",
                "created_at": "2026-04-14T09:00:00+00:00",
                "current_goal": "Resume with a different runtime identity",
                "completed": [],
                "excluded": [],
                "current_blocker": "",
                "next_step": "Rebuild runtime identity",
                "key_files": [],
                "freshness": {},
                "summary": "identity changed",
                "runtime_identity": {
                    "workspace_fingerprint": agent.workspace.fingerprint(),
                    "approval_policy": "auto",
                    "read_only": False,
                    "max_steps": 6,
                    "max_new_tokens": 512,
                    "model": "old-model",
                    "model_client": "FakeModelClient",
                    "feature_flags": {"memory": True, "relevant_memory": True},
                    "shell_env_allowlist": ["PATH"],
                    "session_id": agent.session["id"],
                    "cwd": str(tmp_path),
                },
            }
        },
    }
    agent.session_store.save(agent.session)

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>Resumed.</final>"]),
        workspace=build_workspace(tmp_path),
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="never",
        max_steps=9,
        max_new_tokens=1024,
        feature_flags={"memory": True, "relevant_memory": False, "reflection": False},
    )

    resumed.ask("Continue the task")

    trace_events = [
        json.loads(line)
        for line in resumed.run_store.trace_path(resumed.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    prompt_events = [event for event in trace_events if event["event"] == "prompt_built"]
    assert prompt_events[0]["prompt_metadata"]["resume_status"] == "workspace-mismatch"
    assert prompt_events[0]["prompt_metadata"]["runtime_identity_mismatch_fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]
    assert resumed.last_prompt_metadata["resume_status"] == "full-valid"

    mismatch_events = [event for event in trace_events if event["event"] == "runtime_identity_mismatch"]
    assert mismatch_events
    assert mismatch_events[0]["fields"] == [
        "approval_policy",
        "feature_flags",
        "max_new_tokens",
        "max_steps",
        "model",
        "shell_env_allowlist",
    ]


def test_partial_success_creates_process_note_for_exploration_history(tmp_path):
    agent = build_agent(tmp_path, [])

    agent.run_tool(
        "run_shell",
        {
            "command": "printf 'changed\\n' > README.md && exit 1",
            "timeout": 20,
        },
    )

    process_notes = [
        note
        for note in agent.memory.to_dict()["episodic_notes"]
        if note.get("kind") == "process"
    ]

    assert process_notes
    assert process_notes[-1]["text"] == "run_shell partial_success on README.md; inspect diff before retry"
    assert "partial_success" in process_notes[-1]["tags"]
    assert "README.md" in process_notes[-1]["tags"]


def test_run_shell_failure_summarizes_key_output_into_memory(tmp_path):
    agent = build_agent(tmp_path, [])

    result = agent.run_tool(
        "run_shell",
        {
            "command": "echo FAILED tests/test_alpha.py::test_alpha - AssertionError: nope && echo 1 failed, 2 passed in 0.42s && exit 1",
            "timeout": 20,
        },
    )

    assert "exit_code: 1" in result
    process_notes = [
        note
        for note in agent.memory.to_dict()["episodic_notes"]
        if note.get("kind") == "process"
    ]
    assert process_notes
    assert process_notes[-1]["text"] == (
        "shell failed exit_code 1: "
        "FAILED tests/test_alpha.py::test_alpha - AssertionError: nope | "
        "1 failed, 2 passed in 0.42s"
    )
    assert "shell" in process_notes[-1]["tags"]
    assert "test" in process_notes[-1]["tags"]
    assert "exit_code_1" in process_notes[-1]["tags"]


def test_explicit_memory_promotion_persists_durable_memory_topics(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.\n"
            "Project convention: Preserve local agent state under .enigma/.\n"
            "Decision: Keep semantic memory topic-based and lightweight.</final>",
        ],
    )

    answer = agent.ask(
        "Capture the stable facts you already discovered as semantic memory. "
        "Respond with exactly the long-term facts."
    )

    assert "Project convention:" in answer

    index_path = tmp_path / ".enigma" / "memory" / "MEMORY.md"
    conventions_path = tmp_path / ".enigma" / "memory" / "topics" / "project-conventions.md"
    decisions_path = tmp_path / ".enigma" / "memory" / "topics" / "key-decisions.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))

    assert index_path.exists()
    assert conventions_path.exists()
    assert decisions_path.exists()
    assert "project-conventions" in index_path.read_text(encoding="utf-8")
    assert "Use constrained tools instead of guessing." in conventions_path.read_text(encoding="utf-8")
    assert "Keep semantic memory topic-based and lightweight." in decisions_path.read_text(encoding="utf-8")
    assert report["durable_promotions"] == [
        "project-conventions: Use constrained tools instead of guessing.",
        "project-conventions: Preserve local agent state under .enigma/.",
        "key-decisions: Keep semantic memory topic-based and lightweight.",
    ]


def test_explicit_memory_promotion_supports_chinese_intent_and_labels(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>项目约定：优先使用受约束工具，不要靠猜。\n"
            "决策：语义记忆保持轻量、按 topic 管理。</final>",
        ],
    )

    answer = agent.ask("请把下面这些稳定事实记住，作为长期记忆保存下来。")

    assert "项目约定：" in answer

    conventions_path = tmp_path / ".enigma" / "memory" / "topics" / "project-conventions.md"
    decisions_path = tmp_path / ".enigma" / "memory" / "topics" / "key-decisions.md"

    assert "优先使用受约束工具，不要靠猜。" in conventions_path.read_text(encoding="utf-8")
    assert "语义记忆保持轻量、按 topic 管理。" in decisions_path.read_text(encoding="utf-8")


def test_explicit_memory_promotion_rejects_secret_shaped_and_transient_lines(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.\n"
            "Dependency: API key is sk-live-secret-abc.\n"
            "Decision: Current goal is fix flaky tests.\n"
            "Dependency: stdout: FAIL test_one FAIL test_two FAIL test_three.</final>",
        ],
    )

    agent.ask("Capture these stable facts into semantic memory.")

    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    conventions_path = tmp_path / ".enigma" / "memory" / "topics" / "project-conventions.md"
    dependency_path = tmp_path / ".enigma" / "memory" / "topics" / "dependency-facts.md"

    assert report["durable_promotions"] == [
        "project-conventions: Use constrained tools instead of guessing.",
    ]
    assert report["durable_rejections"] == [
        "dependency-facts:secret_shaped",
        "key-decisions:transient_task_state",
        "dependency-facts:noisy_output",
    ]
    assert "Use constrained tools instead of guessing." in conventions_path.read_text(encoding="utf-8")
    assert not dependency_path.exists()


def test_explicit_memory_promotion_supersedes_matching_durable_fact(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Dependency: Python runtime is 3.11.</final>",
            "<final>Dependency: Python runtime is 3.12.</final>",
        ],
    )

    assert agent.ask("Capture this stable dependency fact into semantic memory.") == "Dependency: Python runtime is 3.11."
    assert agent.ask("Save the updated dependency fact into semantic memory.") == "Dependency: Python runtime is 3.12."

    dependency_path = tmp_path / ".enigma" / "memory" / "topics" / "dependency-facts.md"
    report = json.loads(agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8"))
    text = dependency_path.read_text(encoding="utf-8")

    assert "Python runtime is 3.12." in text
    assert "Python runtime is 3.11." not in text
    assert report["durable_superseded"] == [
        "dependency-facts: Python runtime is 3.11. -> Python runtime is 3.12.",
    ]


def test_explicit_memory_promotion_dedupes_duplicate_durable_note(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            "<final>Project convention: Use constrained tools instead of guessing.</final>",
            "<final>Project convention: Use constrained tools instead of guessing.</final>",
        ],
    )

    agent.ask("Capture the stable fact into semantic memory.")
    agent.ask("Capture the stable fact into semantic memory again.")

    conventions_path = tmp_path / ".enigma" / "memory" / "topics" / "project-conventions.md"
    text = conventions_path.read_text(encoding="utf-8")

    assert text.count("Use constrained tools instead of guessing.") == 1


def test_agent_records_model_cache_metadata_in_last_prompt_metadata(tmp_path):
    class CacheAwareFakeModelClient(FakeModelClient):
        def complete(self, prompt, max_new_tokens, **kwargs):
            self.last_completion_metadata = {
                "prompt_cache_supported": True,
                "cached_tokens": 512,
                "cache_hit": True,
                "input_tokens": 1024,
            }
            return super().complete(prompt, max_new_tokens, **kwargs)

    workspace = build_workspace(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    agent = MiniAgent(
        model_client=CacheAwareFakeModelClient(["<final>Done.</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags={"reflection": False},
    )

    assert agent.ask("Cache aware run") == "Done."

    assert agent.last_prompt_metadata["prompt_cache_supported"] is True
    assert agent.last_prompt_metadata["cached_tokens"] == 512
    assert agent.last_prompt_metadata["cache_hit"] is True
    assert agent.last_prompt_metadata["prefix_hash"]
    assert agent.last_prompt_metadata["prompt_cache_key"] == agent.last_prompt_metadata["prefix_hash"]


def test_recent_transcript_entries_stay_richer_than_older_ones(tmp_path):
    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    old_text = "OLD-" + ("A" * 320)
    recent_text = "RECENT-" + ("B" * 320)

    agent.append_session_history({"role": "user", "content": old_text, "created_at": "2026-04-07T09:00:00+00:00"})
    agent.append_session_history({"role": "assistant", "content": old_text, "created_at": "2026-04-07T09:01:00+00:00"})
    agent.append_session_history({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:02:00+00:00"})
    agent.append_session_history({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:03:00+00:00"})
    agent.append_session_history({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:04:00+00:00"})
    agent.append_session_history({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:05:00+00:00"})
    agent.append_session_history({"role": "user", "content": recent_text, "created_at": "2026-04-07T09:06:00+00:00"})
    agent.append_session_history({"role": "assistant", "content": recent_text, "created_at": "2026-04-07T09:07:00+00:00"})

    assert agent.ask("Check the transcript") == "Done."

    prompt = agent.model_client.prompts[-1]

    assert recent_text in prompt
    assert old_text not in prompt


def test_compact_context_updates_summary_memory_and_history_tail(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\nbeta\n", encoding="utf-8")
    compact_payload = {
        "compact_summary": "Goal: keep the compact state small. Next: inspect recent tail.",
        "working_memory": {
            "task_summary": "Compact the current session.",
            "file_summaries": {"sample.txt": "sample.py style summary"},
        },
        "episodic_notes": [
            {
                "text": "Older work was consolidated into this 情景记忆 note.",
                "tags": ["compact"],
                "source": "compact",
                "kind": "episodic",
            },
            {
                "text": "Recent tail should stay available.",
                "tags": ["tail"],
                "source": "compact",
                "kind": "episodic",
            },
        ],
        "semantic_memory": {
            "project-conventions": ["Keep compact summaries structured."],
            "dependency-facts": ["API key is sk-should-not-persist"],
        },
    }
    agent = build_agent(tmp_path, [json.dumps(compact_payload)])
    agent.memory.set_task_summary("Long task before compact")
    agent.memory.set_file_summary("sample.txt", "def old_summary")
    agent.memory.remember_file("sample.txt")
    original_freshness = agent.memory.to_dict()["file_summaries"]["sample.txt"]["freshness"]
    for index in range(10):
        agent.append_session_history(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}",
                "created_at": f"2026-04-07T10:{index:02d}:00+00:00",
            }
        )

    message = agent.compact_context(focus="memory naming")

    assert "compact complete" in message
    assert agent.session["compact_summary"]["text"] == compact_payload["compact_summary"]
    assert agent.session["compact_summary"]["focus"] == "memory naming"
    assert "memory naming" in agent.model_client.prompts[0]
    snapshot = agent.memory.to_dict()
    assert snapshot["working"]["task_summary"] == "Compact the current session."
    assert snapshot["working"]["recent_files"] == ["sample.txt"]
    assert snapshot["file_summaries"]["sample.txt"]["summary"] == "sample.py style summary"
    assert snapshot["file_summaries"]["sample.txt"]["freshness"] == original_freshness
    assert [note["text"] for note in snapshot["episodic_notes"]] == [
        "Older work was consolidated into this 情景记忆 note.",
        "Recent tail should stay available.",
    ]
    assert len(agent.session["history"]) == 9
    assert agent.session["history"][2]["kind"] == "compact_summary"
    assert [item["content"] for item in agent.session["history"][-2:]] == ["history-8", "history-9"]
    conventions_path = tmp_path / ".enigma" / "memory" / "topics" / "project-conventions.md"
    # 新设计：compact 不再改写稳定记忆（语义主题），只产出滚动摘要。
    # 下一轮 prompt 会从磁盘重新加载 MEMORY.md 与主题，确保稳定记忆不丢失。
    assert not conventions_path.exists() or "Keep compact summaries structured." not in conventions_path.read_text(encoding="utf-8")
    saved = agent.session_store.load(agent.session["id"])
    assert saved["compact_summary"]["text"] == compact_payload["compact_summary"]


def test_compact_context_iteratively_includes_previous_summary(tmp_path):
    first = {
        "compact_summary": "First compact summary.",
        "working_memory": {"task_summary": "First", "file_summaries": {}},
        "episodic_notes": [{"text": "first compact note", "tags": ["first"]}],
        "semantic_memory": {},
    }
    second = {
        "compact_summary": "Second compact summary keeps first and adds new work.",
        "working_memory": {"task_summary": "Second", "file_summaries": {}},
        "episodic_notes": [{"text": "second compact note", "tags": ["second"]}],
        "semantic_memory": {},
    }
    agent = build_agent(tmp_path, [json.dumps(first), json.dumps(second)])
    agent.append_session_history({"role": "user", "content": "old", "created_at": "2026-04-07T10:00:00+00:00"})

    agent.compact_context()
    agent.append_session_history({"role": "user", "content": "new", "created_at": "2026-04-07T10:01:00+00:00"})
    agent.compact_context(focus="next pass")

    assert "First compact summary." in agent.model_client.prompts[1]
    assert "next pass" in agent.model_client.prompts[1]
    assert agent.session["compact_summary"]["text"] == second["compact_summary"]


def test_compact_context_invalid_json_does_not_mutate_session(tmp_path):
    agent = build_agent(tmp_path, ["not json"])
    agent.memory.set_task_summary("Before compact")
    agent.append_session_history({"role": "user", "content": "keep me", "created_at": "2026-04-07T10:00:00+00:00"})
    before_memory = json.loads(json.dumps(agent.memory.to_dict()))
    before_history = list(agent.session["history"])

    with pytest.raises(RuntimeError, match="invalid JSON"):
        agent.compact_context()

    assert agent.memory.to_dict() == before_memory
    assert agent.session["history"] == before_history


def test_compact_context_rejects_malformed_shape_before_mutating(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            json.dumps(
                {
                    "compact_summary": "bad shape",
                    "working_memory": {"task_summary": "bad", "file_summaries": []},
                    "episodic_notes": [],
                    "semantic_memory": {},
                }
            )
        ],
    )
    agent.memory.set_task_summary("Before compact")
    agent.append_session_history({"role": "user", "content": "keep me", "created_at": "2026-04-07T10:00:00+00:00"})
    before_memory = json.loads(json.dumps(agent.memory.to_dict()))
    before_history = list(agent.session["history"])

    with pytest.raises(RuntimeError, match="file_summaries"):
        agent.compact_context()

    assert agent.memory.to_dict() == before_memory
    assert agent.session["history"] == before_history
    assert agent.session["compact_summary"] == {}


def test_compact_context_does_not_rewrite_stable_semantic_topics(tmp_path):
    """新设计：compact 不再负责改写稳定记忆，稳定主题在磁盘上保持原样。"""
    agent = build_agent(
        tmp_path,
        [
            json.dumps(
                {
                    "compact_summary": "semantic topic unchanged",
                    "working_memory": {"task_summary": "Clean", "file_summaries": {}},
                    "episodic_notes": [],
                    "semantic_memory": {"dependency-facts": []},
                }
            )
        ],
    )
    agent.memory.promote_durable([("dependency-facts", "Python runtime is 3.13.")])
    dependency_path = tmp_path / ".enigma" / "memory" / "topics" / "dependency-facts.md"
    assert "Python runtime is 3.13." in dependency_path.read_text(encoding="utf-8")

    agent.compact_context()

    text = dependency_path.read_text(encoding="utf-8")
    assert "Python runtime is 3.13." in text
    assert "## Notes" in text


def test_reset_clears_compact_summary(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.session["compact_summary"] = {"text": "old compact summary"}
    agent.append_session_history({"role": "user", "content": "old", "created_at": "2026-04-07T10:00:00+00:00"})

    agent.reset()

    assert agent.session["compact_summary"] == {}
    assert agent.session_store.load(agent.session["id"])["compact_summary"] == {}


def test_cli_compact_builtin_calls_agent_compact_with_focus(capsys):
    class StubAgent:
        def __init__(self):
            self.focus = None

        def compact_context(self, focus=""):
            self.focus = focus
            return "compact complete"

    agent = StubAgent()

    assert mini_cli.handle_builtin_command(agent, "/compact focus on naming") is True

    assert agent.focus == "focus on naming"
    assert "compact complete" in capsys.readouterr().out


def test_public_api_exports_resolve_through_package_path():
    assert callable(build_welcome)
    assert FakeModelClient is not None
    assert MiniAgent is not None
    assert OllamaModelClient is not None
    assert SessionStore is not None
    assert WorkspaceContext is not None
    assert Path(mini_pkg.__file__).as_posix().endswith("/enigma/__init__.py")


def test_reflection_writes_valid_topics_to_markdown(tmp_path):
    """反思子 agent 返回有效 JSON 时，语义记忆应写入 topic markdown 文件。"""
    reflection_response = json.dumps({
        "project-conventions": ["We use pytest for testing"],
        "key-decisions": ["Decision: Use ruff over flake8"],
        "dependency-facts": [],
        "user-preferences": ["Preference: user likes concise output"],
    })
    agent = build_agent(
        tmp_path,
        [
            # failing shell → produces kind=process note → triggers reflection at ask-end
            '<tool>{"name":"run_shell","args":{"command":"exit 1"}}</tool>',
            "<final>Done.</final>",
            reflection_response,
        ],
        feature_flags={"reflection": True},
    )
    (tmp_path / ".enigma" / "memory").mkdir(parents=True)
    agent.memory.durable_store.ensure_index()

    agent.ask("Set up the project")

    store = agent.memory.durable_store
    conventions = store.load_topic_notes("project-conventions")
    assert any("pytest" in note["text"] for note in conventions)
    decisions = store.load_topic_notes("key-decisions")
    assert any("ruff" in note["text"] for note in decisions)
    prefs = store.load_topic_notes("user-preferences")
    assert any("concise" in note["text"] for note in prefs)


def test_reflection_rejects_invalid_topic_keys(tmp_path):
    """反思子 agent 返回非法 topic key 时，该 key 应被忽略。"""
    reflection_response = json.dumps({
        "project-conventions": ["Valid convention"],
        "lessons_learned": ["This should be ignored"],
    })
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"exit 1"}}</tool>',
            "<final>Done.</final>",
            reflection_response,
        ],
        feature_flags={"reflection": True},
    )
    (tmp_path / ".enigma" / "memory").mkdir(parents=True)
    agent.memory.durable_store.ensure_index()

    agent.ask("Do something")

    store = agent.memory.durable_store
    conventions = store.load_topic_notes("project-conventions")
    assert any("Valid convention" in note["text"] for note in conventions)
    assert not (tmp_path / ".enigma" / "memory" / "topics" / "lessons_learned.md").exists()


def test_reflection_filters_secret_content(tmp_path):
    """反思子 agent 返回含密钥的条目时，应被 reject_durable_reason 过滤。"""
    reflection_response = json.dumps({
        "project-conventions": ["API key is sk-abc123def456ghi789"],
        "key-decisions": ["Use token-based auth"],
    })
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"exit 1"}}</tool>',
            "<final>Done.</final>",
            reflection_response,
        ],
        feature_flags={"reflection": True},
    )
    (tmp_path / ".enigma" / "memory").mkdir(parents=True)
    agent.memory.durable_store.ensure_index()

    agent.ask("Configure auth")

    store = agent.memory.durable_store
    conventions = store.load_topic_notes("project-conventions")
    assert not any("sk-abc" in note["text"] for note in conventions)


def test_reflection_all_empty_json_skips_write(tmp_path):
    """反思子 agent 返回全空 JSON 时，不应写入任何 topic。"""
    reflection_response = json.dumps({
        "project-conventions": [],
        "key-decisions": [],
        "dependency-facts": [],
        "user-preferences": [],
    })
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"exit 1"}}</tool>',
            "<final>Done.</final>",
            reflection_response,
        ],
        feature_flags={"reflection": True},
    )
    (tmp_path / ".enigma" / "memory").mkdir(parents=True)
    agent.memory.durable_store.ensure_index()

    agent.ask("Nothing interesting happened")

    store = agent.memory.durable_store
    for topic in ["project-conventions", "key-decisions", "dependency-facts", "user-preferences"]:
        assert len(store.load_topic_notes(topic)) == 0


def test_reflection_enforces_topic_note_limit(tmp_path):
    """反思子 agent 返回超过 TOPIC_NOTE_LIMIT 条目时，应被截断。"""
    from enigma.memory import TOPIC_NOTE_LIMIT

    too_many = [f"Convention {i}: value {i}" for i in range(TOPIC_NOTE_LIMIT + 10)]
    reflection_response = json.dumps({
        "project-conventions": too_many,
        "key-decisions": [],
        "dependency-facts": [],
        "user-preferences": [],
    })
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"run_shell","args":{"command":"exit 1"}}</tool>',
            "<final>Done.</final>",
            reflection_response,
        ],
        feature_flags={"reflection": True},
    )
    (tmp_path / ".enigma" / "memory").mkdir(parents=True)
    agent.memory.durable_store.ensure_index()

    agent.ask("Do something")

    store = agent.memory.durable_store
    notes = store.load_topic_notes("project-conventions")
    assert len(notes) == TOPIC_NOTE_LIMIT


def test_reflection_disabled_by_default_in_tests(tmp_path):
    """reflection feature flag 默认为 False 时，不调用模型做反思。"""
    agent = build_agent(
        tmp_path,
        ["<final>Done.</final>"],
        feature_flags={"reflection": False},
    )
    result = agent.ask("Simple task")
    assert result == "Done."
    assert len(agent.model_client.prompts) == 1


def test_reviewer_skeleton_docs_exist():
    review_pack = Path("docs/review-pack/README.md")
    architecture = Path("docs/architecture/agent-harness-v1-overview.md")

    assert review_pack.exists()
    assert architecture.exists()

    review_text = review_pack.read_text(encoding="utf-8")
    assert "Project pitch" in review_text
    assert "Architecture map" in review_text
    assert "Benchmark evidence" in review_text
    assert "Sample run artifact list" in review_text

    architecture_text = architecture.read_text(encoding="utf-8")
    assert "Agent Harness v1" in architecture_text
    assert "task state" in architecture_text.lower()


def test_package_import_surface_includes_cli_entrypoints():
    assert callable(mini_pkg.main)
    assert callable(mini_pkg.build_agent)
    assert callable(mini_pkg.build_arg_parser)


def test_module_execution_help_works():
    result = subprocess.run(
        [sys.executable, "-m", "enigma", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()

