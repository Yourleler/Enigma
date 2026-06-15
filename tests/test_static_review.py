"""Tests for lightweight static review rules."""

from enigma.static_review import scan_python_file


def rule_ids(findings):
    return {finding.rule_id for finding in findings}


def test_detects_mutable_default_argument(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("def add_item(items=[]):\n    items.append(1)\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_MUTABLE_DEFAULT" in rule_ids(findings)


def test_detects_bare_except(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("try:\n    work()\nexcept:\n    pass\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_BARE_EXCEPT" in rule_ids(findings)


def test_detects_subprocess_shell_true(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("import subprocess\nsubprocess.run('echo hi', shell=True)\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_SUBPROCESS_SHELL_TRUE" in rule_ids(findings)


def test_detects_open_without_encoding(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("with open('data.txt') as fh:\n    data = fh.read()\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_OPEN_WITHOUT_ENCODING" in rule_ids(findings)


def test_ignores_open_with_encoding(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("with open('data.txt', encoding='utf-8') as fh:\n    data = fh.read()\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_OPEN_WITHOUT_ENCODING" not in rule_ids(findings)


def test_detects_hardcoded_secret(tmp_path):
    code = tmp_path / "code.py"
    code.write_text("API_KEY = 'sk-abcdefghijklmnop'\n", encoding="utf-8")

    findings = scan_python_file(code, tmp_path)

    assert "PY_HARDCODED_SECRET" in rule_ids(findings)
