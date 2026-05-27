"""Skill 系统：发现、加载和调用可复用的 agent 技能。

三级渐进加载：
  1. Metadata（name + description）— 始终在 prefix 中，模型知道有哪些 skill
  2. SKILL.md 正文 — skill 被触发时注入 prompt
  3. references/ — 模型按需通过 read_file 读取

支持两种存储格式：
  - 目录式：skill-name/SKILL.md + 可选 references/
  - 单文件：skill-name.md（向后兼容，无 references）
"""

import re
from dataclasses import dataclass
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KEY_VALUE_RE = re.compile(r"^(\w[\w-]*):\s*(.+)$")


@dataclass
class Skill:
    name: str
    description: str
    trigger: str
    body: str
    source: str  # "builtin" | "user" | "project"
    path: Path  # SKILL.md 或 skill-name.md 的路径
    references_dir: Path | None = None  # references/ 目录，单文件格式为 None

    @property
    def has_references(self):
        return self.references_dir is not None and self.references_dir.is_dir()

    def metadata_line(self):
        """用于 prefix 注入的单行摘要。"""
        ref_hint = " [has references/]" if self.has_references else ""
        return f"- /{self.name}: {self.description}{ref_hint}"

    def reference_files(self):
        """列出 references/ 下的文件名，供模型按需读取。"""
        if not self.has_references:
            return []
        return sorted(f.name for f in self.references_dir.iterdir() if f.is_file())


def _parse_frontmatter(text):
    """解析简单的 YAML frontmatter，返回 (meta_dict, body)。"""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    raw = match.group(1)
    meta = {}
    for line in raw.splitlines():
        m = _KEY_VALUE_RE.match(line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
    body = text[match.end():].strip()
    return meta, body


def _load_skill_from_dir(skill_dir, source):
    """从目录式 skill 加载：skill-name/SKILL.md + references/。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name", skill_dir.name)
    description = meta.get("description", "")
    trigger = meta.get("trigger", name)
    if not body:
        return None
    refs_dir = skill_dir / "references"
    return Skill(
        name=name,
        description=description,
        trigger=trigger,
        body=body,
        source=source,
        path=skill_md,
        references_dir=refs_dir if refs_dir.is_dir() else None,
    )


def _load_skill_from_file(path, source):
    """从单文件 skill 加载：skill-name.md（向后兼容）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    name = meta.get("name", path.stem)
    description = meta.get("description", "")
    trigger = meta.get("trigger", name)
    if not body:
        return None
    return Skill(
        name=name,
        description=description,
        trigger=trigger,
        body=body,
        source=source,
        path=path,
        references_dir=None,
    )


def _scan_directory(directory, source):
    """扫描目录，发现目录式和单文件 skill。优先目录式。"""
    skills = {}
    if not directory.is_dir():
        return skills
    # 先扫描目录式 skill（子目录含 SKILL.md）
    for child in sorted(directory.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            skill = _load_skill_from_dir(child, source)
            if skill:
                skills[skill.name] = skill
    # 再扫描单文件 skill（*.md，排除已在目录式中加载的同名）
    for md_path in sorted(directory.glob("*.md")):
        if md_path.stem not in skills:
            skill = _load_skill_from_file(md_path, source)
            if skill:
                skills[skill.name] = skill
    return skills


def discover_skills(workspace_root, user_home):
    """扫描三个来源，返回 {name: Skill} 字典。优先级：项目 > 用户 > 内置。"""
    builtin_dir = Path(__file__).parent / "skills"
    user_dir = Path(user_home) / ".enigma" / "skills"
    project_dir = Path(workspace_root) / ".enigma" / "skills"

    skills = {}
    skills.update(_scan_directory(builtin_dir, "builtin"))
    skills.update(_scan_directory(user_dir, "user"))
    skills.update(_scan_directory(project_dir, "project"))
    return skills


def list_skills(skills):
    """格式化输出 /skills 列表。"""
    if not skills:
        return "No skills available."
    lines = ["Available skills:"]
    for name in sorted(skills):
        s = skills[name]
        ref = " +references/" if s.has_references else ""
        lines.append(f"  /{name:<16} {s.description}  [{s.source}{ref}]")
    return "\n".join(lines)


def build_skill_metadata_block(skills):
    """生成 skill 元数据摘要，用于注入 prefix。无 skill 时返回空串。"""
    if not skills:
        return ""
    lines = ["Available skills (invoke with /<name>):"]
    for name in sorted(skills):
        lines.append(skills[name].metadata_line())
    return "\n".join(lines)


def build_skill_prompt(skill, user_message):
    """组装 skill 指令 + 用户消息（二级加载）。"""
    parts = [f"[Skill: {skill.name}]\n", skill.body]
    if skill.has_references:
        ref_files = skill.reference_files()
        if ref_files:
            ref_list = ", ".join(ref_files)
            parts.append(
                f"\n---\nReference docs are available at {skill.references_dir}/"
                f" ({ref_list}). Use read_file to load them as needed."
            )
    parts.append(f"\n---\nUser request: {user_message}")
    return "\n".join(parts)
