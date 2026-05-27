"""REPL 交互终端组件。

封装 spinner、非阻塞输入、消息队列显示、中断处理，
供 cli.py 的主循环调用。"""

import sys
import threading
import time

SPINNER_FRAMES = ["◐", "◓", "◑", "◒"]
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
ERASE_LINE = "\033[2K"
CURSOR_UP = "\033[A"


def _is_windows():
    return sys.platform == "win32"


class Spinner:
    """后台线程：每 100ms 更新一次 spinner 动画。"""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr
        self._stop_event = threading.Event()
        self._thread = None
        self._start_time = 0
        self.frame = 0

    def start(self):
        self._stop_event.clear()
        self._start_time = time.monotonic()
        self.frame = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=0.3)
            self._thread = None
        self._clear_line()

    def _run(self):
        while not self._stop_event.is_set():
            self._draw()
            self._stop_event.wait(0.1)

    def _draw(self):
        elapsed = time.monotonic() - self._start_time
        icon = SPINNER_FRAMES[self.frame % len(SPINNER_FRAMES)]
        self.frame += 1
        elapsed_str = _format_elapsed(elapsed)
        line = f"\r{ERASE_LINE}{DIM}  {icon} Working... ({elapsed_str}){RESET}"
        try:
            self.stream.write(line)
            self.stream.flush()
        except Exception:
            pass

    def _clear_line(self):
        try:
            self.stream.write(f"\r{ERASE_LINE}")
            self.stream.flush()
        except Exception:
            pass


def _format_elapsed(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s"


def read_key_nonblocking():
    """非阻塞读取一个键。返回字符或 None。"""
    if _is_windows():
        return _read_key_windows()
    return _read_key_unix()


def _read_key_windows():
    try:
        import msvcrt
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            # 功能键前缀（如方向键）需要读两次
            if ch in ("\x00", "\xe0"):
                msvcrt.getwch()
                return None
            return ch
    except ImportError:
        pass
    return None


def _read_key_unix():
    try:
        import select
        import tty
        import termios
        if select.select([sys.stdin], [], [], 0)[0]:
            ch = sys.stdin.read(1)
            return ch
    except Exception:
        pass
    return None


def draw_pending_message(pending, stream=None):
    """在当前行显示暂存消息和提示。"""
    stream = stream or sys.stderr
    if pending:
        stream.write(f"\r{ERASE_LINE}{DIM}  > {pending}{RESET}")
        stream.write(f"\n{ERASE_LINE}{DIM}    [Esc] cancel{RESET}")
        stream.write(f"\r{DIM}  > {pending}{RESET}")
    else:
        stream.write(f"\r{ERASE_LINE}")
    stream.flush()


def clear_pending_display(lines=2, stream=None):
    """清除暂存消息的显示行。"""
    stream = stream or sys.stderr
    for _ in range(lines):
        stream.write(f"{CURSOR_UP}{ERASE_LINE}")
    stream.write(f"\r{ERASE_LINE}")
    stream.flush()


def prompt_idle():
    """空闲状态的 prompt。"""
    return f"\n{GREEN}enigma>{RESET} "


def prompt_running():
    """运行状态的 prompt（灰色）。"""
    return f"{DIM}enigma⟳{RESET} "
