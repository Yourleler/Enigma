"""Lightweight deterministic checks used by /review.

This is intentionally small: it gives the reviewer concrete evidence without
trying to become a full static analyzer.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


SECRET_NAME_RE = re.compile(r"(api[_-]?key|token|secret|password|passwd|private[_-]?key)", re.I)
SECRET_VALUE_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9_]{12,}|AKIA[0-9A-Z]{12,})")


@dataclass(frozen=True)
class StaticFinding:
    rule_id: str
    severity: str
    category: str
    file: str
    line: int
    message: str
    evidence: str = ""

    def to_prompt_line(self):
        location = f"{self.file}:{self.line}" if self.line > 0 else self.file
        evidence = f" evidence={self.evidence!r}" if self.evidence else ""
        return (
            f"- [{self.severity}] {self.rule_id} ({self.category}) "
            f"{location}: {self.message}{evidence}"
        )

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "evidence": self.evidence,
        }


def scan_python_file(path, repo_root):
    path = Path(path)
    repo_root = Path(repo_root)
    rel = _relative_path(path, repo_root)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [
            StaticFinding(
                rule_id="PY_READ_ERROR",
                severity="warning",
                category="static-scan",
                file=rel,
                line=0,
                message=f"Could not read file for static review: {exc}",
            )
        ]

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        return [
            StaticFinding(
                rule_id="PY_SYNTAX_ERROR",
                severity="warning",
                category="syntax",
                file=rel,
                line=exc.lineno or 0,
                message=f"Python syntax error prevented AST review: {exc.msg}",
                evidence=(exc.text or "").strip(),
            )
        ]

    visitor = _StaticReviewVisitor(rel, source.splitlines())
    visitor.visit(tree)
    return visitor.findings


def scan_python_files(paths, repo_root):
    findings = []
    seen = set()
    for path in paths:
        candidate = Path(repo_root) / path if not Path(path).is_absolute() else Path(path)
        if candidate.suffix != ".py" or not candidate.exists() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        findings.extend(scan_python_file(resolved, repo_root))
    return findings


def format_static_findings(findings):
    findings = list(findings or [])
    if not findings:
        return "No static findings."
    return "\n".join(finding.to_prompt_line() for finding in findings)


class _StaticReviewVisitor(ast.NodeVisitor):
    def __init__(self, file, lines):
        self.file = file
        self.lines = lines
        self.findings = []

    def visit_FunctionDef(self, node):
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self._check_mutable_defaults(node)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.type is None:
            self._add(
                "PY_BARE_EXCEPT",
                "warning",
                "error-handling",
                node,
                "Bare except catches BaseException and can hide interrupts or system exits.",
            )
        self.generic_visit(node)

    def visit_Call(self, node):
        call_name = _call_name(node.func)
        if call_name in {"eval", "exec"}:
            self._add(
                "PY_DYNAMIC_EXEC",
                "critical",
                "security",
                node,
                f"Use of {call_name} can execute untrusted code.",
            )
        if call_name.startswith("subprocess.") and _keyword_is_true(node, "shell"):
            self._add(
                "PY_SUBPROCESS_SHELL_TRUE",
                "critical",
                "security",
                node,
                "subprocess call uses shell=True; validate inputs or avoid the shell.",
            )
        if call_name == "open" and not _has_keyword(node, "encoding"):
            self._add(
                "PY_OPEN_WITHOUT_ENCODING",
                "suggestion",
                "portability",
                node,
                "open() without encoding can behave differently across environments.",
            )
        self.generic_visit(node)

    def visit_Assign(self, node):
        self._check_secret_assignment(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._check_secret_assignment([node.target], node.value)
        self.generic_visit(node)

    def _check_mutable_defaults(self, node):
        for default in list(node.args.defaults) + list(node.args.kw_defaults):
            if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    "PY_MUTABLE_DEFAULT",
                    "warning",
                    "bug-risk",
                    default,
                    "Mutable default argument is shared across calls.",
                )

    def _check_secret_assignment(self, targets, value):
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return
        names = [_target_name(target) for target in targets]
        secret_name = any(name and SECRET_NAME_RE.search(name) for name in names)
        secret_value = bool(SECRET_VALUE_RE.search(value.value))
        if secret_name and (secret_value or len(value.value) >= 12):
            self._add(
                "PY_HARDCODED_SECRET",
                "critical",
                "security",
                value,
                "Possible hardcoded secret assigned in source code.",
                evidence=_redact(value.value),
            )

    def _add(self, rule_id, severity, category, node, message, evidence=None):
        self.findings.append(
            StaticFinding(
                rule_id=rule_id,
                severity=severity,
                category=category,
                file=self.file,
                line=getattr(node, "lineno", 0) or 0,
                message=message,
                evidence=evidence or self._line(getattr(node, "lineno", 0)),
            )
        )

    def _line(self, lineno):
        if not lineno or lineno < 1 or lineno > len(self.lines):
            return ""
        return self.lines[lineno - 1].strip()


def _relative_path(path, repo_root):
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def _call_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parent = _call_name(func.value)
        return f"{parent}.{func.attr}" if parent else func.attr
    return ""


def _has_keyword(node, name):
    return any(keyword.arg == name for keyword in node.keywords)


def _keyword_is_true(node, name):
    for keyword in node.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value is True
    return False


def _target_name(target):
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        parent = _target_name(target.value)
        return f"{parent}.{target.attr}" if parent else target.attr
    return ""


def _redact(value):
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]
