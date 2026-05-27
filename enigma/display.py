"""终端显示工具函数。

提供 ANSI 颜色、步骤进度、结果截断等纯函数，
不依赖 runtime 状态，供 runtime.py 的 display 方法调用。"""

import difflib
import os

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
CYAN = "\033[36m"
GRAY = "\033[38;2;188;195;207m"
BADGE_BG = "\033[48;2;91;78;255m\033[38;2;255;255;255m"
BADGE_RESET = "\033[0m"


def supports_color(stream):
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _format_elapsed(elapsed_ms):
    if elapsed_ms < 1000:
        return f"{elapsed_ms}ms"
    return f"{elapsed_ms / 1000:.1f}s"


def format_step_header(step, total, name, summary, elapsed_ms, success, use_color=True):
    """步骤结果标题行。
    示例: [1/6] read_file README.md  ✓ (12ms)
    """
    progress = f"{step}/{total}"
    elapsed = _format_elapsed(elapsed_ms)

    if success is True:
        icon = "✓"
        color = GREEN
    elif success is False:
        icon = "✗"
        color = RED
    else:
        icon = "~"
        color = YELLOW

    if use_color:
        return (
            f"{DIM}  {progress}{RESET}  "
            f"{color}{icon}{RESET} "
            f"{CYAN}{name}{RESET} "
            f"{GRAY}{summary}{RESET} "
            f"{DIM}({elapsed}){RESET}"
        )
    return f"  {progress}  {icon} {name} {summary} ({elapsed})"


def format_result_preview(result, max_lines=15, use_color=True):
    """截断工具结果，最多显示 max_lines 行。返回 (text, line_count)。"""
    lines = str(result).splitlines()
    if not lines:
        return "", 0
    truncated = len(lines) > max_lines
    shown = lines[:max_lines] if truncated else lines
    line_count = len(shown)
    if use_color:
        parts = []
        for line in shown:
            parts.append(f"{DIM}  {line}{RESET}")
        if truncated:
            remaining = len(lines) - max_lines
            parts.append(f"{DIM}  ({remaining} lines truncated){RESET}")
            line_count += 1
        return "\n".join(parts), line_count
    parts = ["  " + line for line in shown]
    if truncated:
        remaining = len(lines) - max_lines
        parts.append(f"  ({remaining} lines truncated)")
        line_count += 1
    return "\n".join(parts), line_count


def format_result_compact(result, max_len=80):
    """单行摘要：取第一行，截断到 max_len 字符。"""
    first_line = str(result).splitlines()[0].strip() if str(result).strip() else ""
    if len(first_line) > max_len:
        return first_line[:max_len - 3] + "..."
    return first_line


def format_diff(old_content, new_content, path="", max_lines=40, use_color=True):
    """unified diff 显示，红绿着色。返回 (text, line_count)。"""
    old_lines = (old_content or "").splitlines(keepends=True)
    new_lines = (new_content or "").splitlines(keepends=True)
    if old_lines == new_lines:
        return "", 0
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{path}" if path else "a",
        tofile=f"b/{path}" if path else "b",
        n=3,
    ))
    if not diff:
        return "", 0
    truncated = len(diff) > max_lines
    shown = diff[:max_lines] if truncated else diff
    line_count = len(shown)
    if truncated:
        line_count += 1
    if use_color:
        parts = []
        for line in shown:
            line = line.rstrip("\n")
            if line.startswith("+++") or line.startswith("---"):
                parts.append(f"{BOLD}  {line}{RESET}")
            elif line.startswith("+"):
                parts.append(f"{GREEN}  {line}{RESET}")
            elif line.startswith("-"):
                parts.append(f"{RED}  {line}{RESET}")
            elif line.startswith("@@"):
                parts.append(f"{CYAN}  {line}{RESET}")
            else:
                parts.append(f"{DIM}  {line}{RESET}")
        if truncated:
            remaining = len(diff) - max_lines
            parts.append(f"{DIM}  ({remaining} diff lines truncated){RESET}")
        return "\n".join(parts), line_count
    parts = ["  " + line.rstrip("\n") for line in shown]
    if truncated:
        remaining = len(diff) - max_lines
        parts.append(f"  ({remaining} diff lines truncated)")
    return "\n".join(parts), line_count
