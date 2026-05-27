"""SessionDB + MEMORY.md startup loader + rolling summary tests."""

from __future__ import annotations

from pathlib import Path

from enigma.memory import DurableMemoryStore, LayeredMemory
from enigma.storage import SessionDB


def test_session_db_creates_schema_and_roundtrips_messages(tmp_path):
    db_path = tmp_path / ".enigma" / "sessions" / "state.db"
    db = SessionDB(db_path)

    db.start_session("s1", title="demo", model="gpt-4", cwd=str(tmp_path))
    db.append_message("s1", "user", "message", "hello world foo")
    db.append_message("s1", "assistant", "message", "bar baz qux")
    db.append_message(
        "s1",
        "tool",
        "tool_result",
        "read file content here",
        tool_name="read_file",
        file_path="a.py",
    )

    recent = db.recent_messages("s1")
    assert len(recent) == 3
    assert recent[0]["content"] == "hello world foo"
    assert recent[-1]["tool_name"] == "read_file"


def test_session_db_fts_search_finds_by_keyword(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.start_session("s1")
    db.append_message("s1", "user", "message", "apple banana cherry")
    db.append_message("s1", "assistant", "message", "cherry pie recipe")
    db.start_session("s2")
    db.append_message("s2", "user", "message", "apple tart")

    results = db.search_messages("cherry")
    contents = [row["content"] for row in results]
    assert any("cherry pie recipe" in content for content in contents)
    assert len(results) >= 1


def test_session_db_fts_search_can_filter_by_session(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.start_session("a")
    db.append_message("a", "user", "message", "foo session a content")
    db.start_session("b")
    db.append_message("b", "user", "message", "foo session b content")

    rows = db.search_messages("foo", session_id="a")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "a"


def test_session_db_stores_rolling_summary_and_session_state(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.start_session("s1")

    db.update_session_state(
        "s1",
        rolling_summary="summary v1",
        recent_files=["a.py", "b.py"],
        open_tasks=["step 1"],
    )
    state = db.get_session_state("s1")
    assert state["rolling_summary"] == "summary v1"
    assert state["recent_files"] == ["a.py", "b.py"]
    assert state["open_tasks"] == ["step 1"]

    # partial update keeps prior fields
    db.update_session_state("s1", rolling_summary="summary v2")
    state = db.get_session_state("s1")
    assert state["rolling_summary"] == "summary v2"
    assert state["recent_files"] == ["a.py", "b.py"]


def test_session_db_fts_ignores_empty_queries(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    assert db.search_messages("") == []
    assert db.search_messages("   ") == []


def test_memory_md_default_is_written_when_missing(tmp_path):
    root = tmp_path / ".enigma" / "memory"
    store = DurableMemoryStore(root)
    assert not store.index_path.exists()

    store.ensure_index()
    assert store.index_path.exists()
    body = store.index_path.read_text(encoding="utf-8")
    assert "Enigma Memory" in body


def test_startup_memory_caps_at_line_and_byte_limits(tmp_path):
    root = tmp_path / ".enigma" / "memory"
    root.mkdir(parents=True)
    # 写 300 行，超过 200 行上限
    content = "\n".join(f"line {index}" for index in range(300))
    (root / "MEMORY.md").write_text(content, encoding="utf-8")
    store = DurableMemoryStore(root)

    text = store.load_startup_memory()
    assert text.count("\n") < 300  # 被截断
    assert text.splitlines()[0] == "line 0"
    assert len(text.splitlines()) <= 200


def test_startup_memory_returns_empty_when_memory_md_missing(tmp_path):
    memory = LayeredMemory(workspace_root=tmp_path)
    assert memory.startup_memory_text() == ""


def test_startup_memory_is_injected_into_context_manager_prompt(tmp_path):
    from enigma import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
    from enigma.context_manager import ContextManager

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    root = tmp_path / ".enigma" / "memory"
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text(
        "# Enigma Memory\n\nProject uses Python 3.13 and pytest.\n",
        encoding="utf-8",
    )

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    agent = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )

    prompt, metadata = ContextManager(agent).build("hello")
    assert "启动记忆 (MEMORY.md):" in prompt
    assert "Project uses Python 3.13" in prompt
    assert "startup_memory" in metadata["section_order"]


def test_rolling_summary_survives_compact_and_comes_from_session_db(tmp_path):
    """/compact 后，稳定记忆从磁盘重载，滚动摘要存进 SessionDB，下一轮 prompt 能拿到。"""
    import json
    from enigma import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
    from enigma.context_manager import ContextManager

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    root = tmp_path / ".enigma" / "memory"
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text(
        "# Enigma Memory\n\nStable project rule X.\n",
        encoding="utf-8",
    )

    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    compact_payload = {
        "compact_summary": "rolling summary after compact",
        "working_memory": {"task_summary": "continue task", "file_summaries": {}},
        "episodic_notes": [],
    }
    agent = MiniAgent(
        model_client=FakeModelClient([json.dumps(compact_payload)]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        feature_flags={"reflection": False},
    )
    agent.append_session_history({"role": "user", "content": "old", "created_at": "2026-04-07T10:00:00+00:00"})

    agent.compact_context()

    # 稳定记忆仍然可读
    assert "Stable project rule X" in agent.memory.startup_memory_text()
    # 滚动摘要写进 SessionDB
    state = agent.session_db.get_session_state(agent.session["id"])
    assert state is not None
    assert "rolling summary after compact" in state["rolling_summary"]

    # 下一轮 prompt 带上滚动摘要
    prompt, metadata = ContextManager(agent).build("next step")
    assert "会话滚动摘要:" in prompt
    assert "rolling summary after compact" in prompt


def test_history_is_mirrored_to_session_db(tmp_path):
    from enigma import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".enigma" / "sessions")
    agent = MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )
    agent.append_session_history({"role": "user", "content": "hello there"})
    agent.append_session_history(
        {"role": "tool", "name": "read_file", "args": {"path": "README.md"}, "content": "demo"}
    )

    rows = agent.session_db.recent_messages(agent.session["id"], limit=10)
    assert any(row["content"] == "hello there" for row in rows)
    assert any(row["tool_name"] == "read_file" and row["file_path"] == "README.md" for row in rows)


def test_session_isolation_keeps_rolling_summaries_separate(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.start_session("alpha")
    db.start_session("beta")
    db.update_session_state("alpha", rolling_summary="alpha summary")
    db.update_session_state("beta", rolling_summary="beta summary")

    assert db.get_session_state("alpha")["rolling_summary"] == "alpha summary"
    assert db.get_session_state("beta")["rolling_summary"] == "beta summary"
