from enigma.memory import LayeredMemory, summarize_read_result, summarize_shell_result


def test_working_memory_tracks_summary_and_recent_files():
    memory = LayeredMemory()

    memory.set_task_summary("Investigate flaky tests")
    memory.remember_file("README.md")
    memory.remember_file("src/app.py")
    memory.remember_file("README.md")

    snapshot = memory.to_dict()

    assert snapshot["working"]["task_summary"] == "Investigate flaky tests"
    assert snapshot["working"]["recent_files"] == ["src/app.py", "README.md"]
    assert snapshot["task"] == "Investigate flaky tests"
    assert snapshot["files"] == ["src/app.py", "README.md"]


def test_episodic_notes_append_and_retrieve_deterministically():
    memory = LayeredMemory()

    memory.append_note("Exact tag note", tags=("recall",), created_at="2026-04-07T10:00:00+00:00")
    memory.append_note("Keyword overlap note about memory", created_at="2026-04-07T10:01:00+00:00")
    memory.append_note("Newest unrelated note", created_at="2026-04-07T10:02:00+00:00")
    memory.append_note("Older unrelated note", created_at="2026-04-07T09:59:00+00:00")

    snapshot = memory.to_dict()
    assert [note["text"] for note in snapshot["episodic_notes"]] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]
    assert snapshot["notes"] == [
        "Exact tag note",
        "Keyword overlap note about memory",
        "Newest unrelated note",
        "Older unrelated note",
    ]

    lines = [line for line in memory.retrieval_view("recall memory", limit=4).splitlines() if line.startswith("- ")]
    assert lines == [
        "- Exact tag note",
        "- Keyword overlap note about memory",
    ]


def test_file_summaries_use_canonical_paths_and_freshness(tmp_path):
    file_path = tmp_path / "sample.txt"
    file_path.write_text("alpha\n", encoding="utf-8")
    memory = LayeredMemory(workspace_root=tmp_path)

    memory.set_file_summary("./sample.txt", "sample.txt: alpha")
    memory.remember_file("./sample.txt")
    snapshot = memory.to_dict()["file_summaries"]["sample.txt"]

    assert snapshot["summary"] == "sample.txt: alpha"
    assert snapshot["freshness"]

    assert "sample.txt: alpha" in memory.render_memory_text()
    file_path.write_text("beta\n", encoding="utf-8")
    assert "sample.txt: alpha" not in memory.render_memory_text()

    memory.invalidate_file_summary("sample.txt")

    assert "sample.txt" not in memory.to_dict()["file_summaries"]


def test_summarize_read_result_extracts_code_structure_before_first_lines():
    result = "\n".join(
        [
            "# src/app.py",
            "   1: # copyright",
            "   2: boring intro",
            "   3: import os",
            "   4: from pathlib import Path",
            "   5: def build_agent(args):",
            "   6:     return args",
            "   7: class Runner:",
        ]
    )

    summary = summarize_read_result(result, limit=300)

    assert summary == "import os | from pathlib import Path | def build_agent(args): | class Runner:"


def test_summarize_read_result_extracts_markdown_and_config_structure():
    markdown = "\n".join(
        [
            "# README.md",
            "   1: opening paragraph",
            "   2: ## Usage",
            "   3: - run pytest",
            "   4: - inspect failures",
        ]
    )
    config = "\n".join(
        [
            "# pyproject.toml",
            "   1: [project]",
            "   2: name = \"enigma\"",
            "   3: dependencies = []",
        ]
    )

    assert summarize_read_result(markdown, limit=300) == "## Usage | - run pytest | - inspect failures"
    assert summarize_read_result(config, limit=300) == "[project] | name = \"enigma\" | dependencies = []"


def test_summarize_read_result_falls_back_to_first_lines_when_no_structure_matches():
    result = "\n".join(
        [
            "# notes.txt",
            "   1: alpha",
            "   2: beta",
            "   3: gamma",
            "   4: delta",
        ]
    )

    assert summarize_read_result(result, limit=300) == "1: alpha | 2: beta | 3: gamma"


def test_summarize_shell_result_extracts_test_failure_signal():
    result = "\n".join(
        [
            "exit_code: 1",
            "stdout:",
            "FAILED tests/test_alpha.py::test_alpha - AssertionError: nope",
            "1 failed, 2 passed in 0.42s",
            "stderr:",
            "(empty)",
        ]
    )

    assert summarize_shell_result(result, command="python -m pytest") == (
        "shell failed exit_code 1: "
        "FAILED tests/test_alpha.py::test_alpha - AssertionError: nope | "
        "1 failed, 2 passed in 0.42s"
    )


def test_summarize_shell_result_skips_plain_success_output():
    result = "\n".join(["exit_code: 0", "stdout:", "hello", "stderr:", "(empty)"])

    assert summarize_shell_result(result, command="echo hello") == ""


def test_process_notes_keep_kind_and_latest_duplicate_wins():
    memory = LayeredMemory()

    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:00:00+00:00",
        kind="process",
    )
    memory.append_note(
        "Shell partial success on README.md; inspect diff before retry",
        tags=("process", "partial_success"),
        created_at="2026-04-07T10:01:00+00:00",
        kind="process",
    )

    notes = memory.to_dict()["episodic_notes"]

    assert len(notes) == 1
    assert notes[0]["kind"] == "process"
    assert notes[0]["created_at"] == "2026-04-07T10:01:00+00:00"


def test_durable_memory_index_and_topic_notes_are_loaded_and_retrieved(tmp_path):
    memory_root = tmp_path / ".enigma" / "memory"
    topics_dir = memory_root / "topics"
    topics_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "# Semantic Memory Index\n\n"
        "- [project-conventions](topics/project-conventions.md): Project Conventions\n"
        "  - summary: Stable repository conventions.\n"
        "  - tags: convention\n",
        encoding="utf-8",
    )
    (topics_dir / "project-conventions.md").write_text(
        "# Project Conventions\n\n"
        "- topic: project-conventions\n"
        "- summary: Stable repository conventions.\n"
        "- tags: convention\n"
        "- updated_at: 2026-04-12T08:14:49+00:00\n\n"
        "## Notes\n"
        "- Use constrained tools instead of guessing.\n"
        "- Preserve local agent state under .enigma/.\n",
        encoding="utf-8",
    )

    memory = LayeredMemory(workspace_root=tmp_path)

    snapshot = memory.to_dict()
    assert snapshot["durable_topics"] == ["project-conventions"]

    lines = [line for line in memory.retrieval_view("constrained tools", limit=4).splitlines() if line.startswith("- ")]
    assert any("Use constrained tools instead of guessing." in line for line in lines)


def test_promote_respects_topic_note_limit(tmp_path):
    from enigma.memory import DurableMemoryStore, TOPIC_NOTE_LIMIT

    memory_root = tmp_path / ".enigma" / "memory"
    memory_root.mkdir(parents=True)
    store = DurableMemoryStore(memory_root)
    store.ensure_index()

    # Fill up a topic to the limit
    promotions = [("user-preferences", f"Preference {i}: value {i}") for i in range(TOPIC_NOTE_LIMIT)]
    results, _ = store.promote(promotions)
    assert len(results) == TOPIC_NOTE_LIMIT

    # Next promotion should be rejected (topic full)
    results, _ = store.promote([("user-preferences", "Preference overflow: should be rejected")])
    assert len(results) == 0

    # Verify the topic still has exactly TOPIC_NOTE_LIMIT notes
    notes = store.load_topic_notes("user-preferences")
    assert len(notes) == TOPIC_NOTE_LIMIT


def test_promote_replacement_not_counted_against_limit(tmp_path):
    from enigma.memory import DurableMemoryStore, TOPIC_NOTE_LIMIT

    memory_root = tmp_path / ".enigma" / "memory"
    memory_root.mkdir(parents=True)
    store = DurableMemoryStore(memory_root)
    store.ensure_index()

    # Fill to limit (using "X is Y" pattern so _subject_key extracts subject)
    promotions = [("user-preferences", f"option {i} is value {i}") for i in range(TOPIC_NOTE_LIMIT)]
    store.promote(promotions)

    # Replace existing (same subject "option 0") should still work at the limit
    results, superseded = store.promote([("user-preferences", "option 0 is updated value")])
    assert len(results) == 1
    assert len(superseded) == 1

    notes = store.load_topic_notes("user-preferences")
    assert len(notes) == TOPIC_NOTE_LIMIT
    assert any("updated value" in note["text"] for note in notes)


def test_replace_topics_clips_to_limit(tmp_path):
    from enigma.memory import DurableMemoryStore, TOPIC_NOTE_LIMIT

    memory_root = tmp_path / ".enigma" / "memory"
    memory_root.mkdir(parents=True)
    store = DurableMemoryStore(memory_root)
    store.ensure_index()

    # Try to replace with more than the limit
    notes = [f"Convention {i}: value {i}" for i in range(TOPIC_NOTE_LIMIT + 10)]
    replaced = store.replace_topics({"project-conventions": notes})
    assert "project-conventions" in replaced

    stored = store.load_topic_notes("project-conventions")
    assert len(stored) == TOPIC_NOTE_LIMIT


def test_promote_skips_duplicate_exact_text(tmp_path):
    from enigma.memory import DurableMemoryStore

    memory_root = tmp_path / ".enigma" / "memory"
    memory_root.mkdir(parents=True)
    store = DurableMemoryStore(memory_root)
    store.ensure_index()

    store.promote([("user-preferences", "User likes dark mode")])
    store.promote([("user-preferences", "User likes dark mode")])

    notes = store.load_topic_notes("user-preferences")
    assert len(notes) == 1


def test_replace_topics_rejects_invalid_keys(tmp_path):
    from enigma.memory import DurableMemoryStore

    memory_root = tmp_path / ".enigma" / "memory"
    memory_root.mkdir(parents=True)
    store = DurableMemoryStore(memory_root)
    store.ensure_index()

    replaced = store.replace_topics({
        "project-conventions": ["Valid note"],
        "not-a-real-topic": ["Should be ignored"],
    })
    assert "project-conventions" in replaced
    assert "not-a-real-topic" not in replaced
    assert not (memory_root / "topics" / "not-a-real-topic.md").exists()
