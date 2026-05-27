"""Skill 系统测试：发现、加载、渐进式加载、列表、prompt 组装。"""

from pathlib import Path

import pytest

from enigma import Skill, build_skill_metadata_block, build_skill_prompt, discover_skills, list_skills
from enigma.skills import _parse_frontmatter


class TestParseFrontmatter:
    def test_parses_valid_frontmatter(self):
        text = "---\nname: test\nDescription: a test\n---\nBody here."
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "test"
        assert meta["Description"] == "a test"
        assert body == "Body here."

    def test_no_frontmatter_returns_full_body(self):
        text = "Just a body."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == "Just a body."

    def test_empty_body_after_frontmatter(self):
        text = "---\nname: x\n---\n"
        meta, body = _parse_frontmatter(text)
        assert meta["name"] == "x"
        assert body == ""


class TestDiscoverSkills:
    def test_discovers_builtin_skills(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        assert len(skills) >= 5
        assert "test-writer" in skills
        assert "reviewer" in skills
        assert "refactor" in skills
        assert "explain" in skills
        assert "doc-writer" in skills

    def test_builtin_skills_have_correct_source(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        for skill in skills.values():
            assert skill.source == "builtin"

    def test_project_skills_override_builtin(self, tmp_path):
        project_dir = tmp_path / ".enigma" / "skills"
        project_dir.mkdir(parents=True)
        (project_dir / "test-writer.md").write_text(
            "---\nname: test-writer\ndescription: custom\ntrigger: test\n---\nCustom body.",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path, tmp_path)
        assert skills["test-writer"].description == "custom"
        assert skills["test-writer"].source == "project"

    def test_user_skills_discovered(self, tmp_path):
        user_dir = tmp_path / "home" / ".enigma" / "skills"
        user_dir.mkdir(parents=True)
        (user_dir / "my-skill.md").write_text(
            "---\nname: my-skill\ndescription: user skill\ntrigger: my\n---\nUser body.",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path, tmp_path / "home")
        assert "my-skill" in skills
        assert skills["my-skill"].source == "user"

    def test_missing_dirs_dont_error(self, tmp_path):
        skills = discover_skills(tmp_path / "no-such-dir", tmp_path / "no-home")
        assert isinstance(skills, dict)


class TestDirectoryBasedSkills:
    def test_loads_skill_from_directory(self, tmp_path):
        skill_dir = tmp_path / ".enigma" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: dir skill\ntrigger: my\n---\nDir body.",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path, tmp_path)
        assert "my-skill" in skills
        assert skills["my-skill"].body == "Dir body."

    def test_directory_with_references(self, tmp_path):
        skill_dir = tmp_path / ".enigma" / "skills" / "deep-skill"
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: deep-skill\ndescription: has refs\n---\nBody.",
            encoding="utf-8",
        )
        (refs_dir / "api.md").write_text("# API Reference\n", encoding="utf-8")
        skills = discover_skills(tmp_path, tmp_path)
        assert skills["deep-skill"].has_references is True
        assert "api.md" in skills["deep-skill"].reference_files()

    def test_directory_skill_overrides_single_file(self, tmp_path):
        project_dir = tmp_path / ".enigma" / "skills"
        project_dir.mkdir(parents=True)
        (project_dir / "my-skill.md").write_text(
            "---\nname: my-skill\ndescription: old\n---\nOld body.",
            encoding="utf-8",
        )
        skill_dir = project_dir / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\ndescription: new\n---\nNew body.",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path, tmp_path)
        assert skills["my-skill"].description == "new"
        assert skills["my-skill"].body == "New body."

    def test_single_file_skill_no_references(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        for skill in skills.values():
            assert skill.has_references is False
            assert skill.reference_files() == []


class TestListSkills:
    def test_empty_skills(self):
        result = list_skills({})
        assert "No skills" in result

    def test_lists_all_skills(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        result = list_skills(skills)
        for name in skills:
            assert f"/{name}" in result

    def test_shows_source(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        result = list_skills(skills)
        assert "[builtin]" in result

    def test_shows_references_hint(self, tmp_path):
        skill_dir = tmp_path / ".enigma" / "skills" / "ref-skill"
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: ref-skill\ndescription: has refs\n---\nBody.",
            encoding="utf-8",
        )
        (refs_dir / "doc.md").write_text("# Doc\n", encoding="utf-8")
        skills = discover_skills(tmp_path, tmp_path)
        result = list_skills(skills)
        assert "+references/" in result


class TestBuildSkillMetadataBlock:
    def test_empty_skills(self):
        assert build_skill_metadata_block({}) == ""

    def test_contains_all_skills(self, tmp_path):
        skills = discover_skills(tmp_path, tmp_path)
        block = build_skill_metadata_block(skills)
        for name in skills:
            assert f"/{name}" in block

    def test_mentions_references(self, tmp_path):
        skill_dir = tmp_path / ".enigma" / "skills" / "ref-skill"
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: ref-skill\ndescription: has refs\n---\nBody.",
            encoding="utf-8",
        )
        (refs_dir / "doc.md").write_text("# Doc\n", encoding="utf-8")
        skills = discover_skills(tmp_path, tmp_path)
        block = build_skill_metadata_block(skills)
        assert "has references/" in block


class TestBuildSkillPrompt:
    def test_basic_prompt(self):
        skill = Skill(
            name="test", description="d", trigger="t",
            body="Do stuff.", source="builtin", path=Path("."),
        )
        prompt = build_skill_prompt(skill, "write tests")
        assert "[Skill: test]" in prompt
        assert "Do stuff." in prompt
        assert "User request: write tests" in prompt

    def test_no_redundant_args_field(self):
        skill = Skill(
            name="test", description="d", trigger="t",
            body="Do stuff.", source="builtin", path=Path("."),
        )
        prompt = build_skill_prompt(skill, "auth.py")
        assert "Context:" not in prompt

    def test_with_references(self, tmp_path):
        refs_dir = tmp_path / "references"
        refs_dir.mkdir()
        (refs_dir / "api.md").write_text("# API\n", encoding="utf-8")
        skill = Skill(
            name="test", description="d", trigger="t",
            body="Do stuff.", source="builtin", path=Path("."),
            references_dir=refs_dir,
        )
        prompt = build_skill_prompt(skill, "do something")
        assert "Reference docs are available" in prompt
        assert "api.md" in prompt
