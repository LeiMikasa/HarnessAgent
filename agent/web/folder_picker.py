"""Open an OS directory chooser in an isolated, short-lived GUI process."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ..shell import noninteractive_process_options


PICKER_SCRIPT = r'''
import json
import sys
import tkinter as tk
from tkinter import filedialog

request = json.loads(sys.stdin.read())
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
    selected = filedialog.askdirectory(
        parent=root,
        title="选择 minicode 项目文件夹",
        initialdir=request["initial_path"],
        mustexist=True,
    )
    print(json.dumps({"path": selected or None}, ensure_ascii=True))
finally:
    root.destroy()
'''


def pick_directory(initial_path: str) -> str | None:
    """Only called by the user's local HTTP action; never registered as an Agent tool."""
    candidate = Path(initial_path).expanduser()
    try:
        initial = candidate.resolve() if candidate.is_dir() else Path.home()
    except (OSError, ValueError):
        initial = Path.home()
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", PICKER_SCRIPT],
            input=json.dumps({"initial_path": str(initial)}, ensure_ascii=True),
            capture_output=True, text=True, encoding="utf-8", timeout=180,
            **noninteractive_process_options(input_provided=True),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("选择窗口已超时关闭，请重新选择或手动填写路径") from exc
    except OSError as exc:
        raise RuntimeError("无法打开系统文件夹选择窗口，请手动填写路径") from exc
    if result.returncode:
        raise RuntimeError("无法打开系统文件夹选择窗口，请确认本机有桌面环境和 Tkinter，或手动填写路径")
    try:
        selected = json.loads(result.stdout)["path"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("读取文件夹选择结果失败，请重试或手动填写路径") from exc
    if selected is not None and not isinstance(selected, str):
        raise RuntimeError("文件夹选择结果无效，请重新选择")
    return selected or None
