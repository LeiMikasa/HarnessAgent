"""Probe run_bash in a private hidden console, never the user's TUI console."""

import ctypes
from ctypes import wintypes
import json
import msvcrt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Settings
from agent.tools.basic import run_bash
from agent.tools.registry import ToolContext


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetStdHandle.argtypes = [wintypes.DWORD]
kernel.GetStdHandle.restype = wintypes.HANDLE
kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
workspace = Path(sys.argv[1])
ctx = ToolContext(settings=Settings(provider="mock", workdir=workspace), workdir=workspace)

with open("CONIN$", "r") as console:
    handle = msvcrt.get_osfhandle(console.fileno())

    def mode():
        value = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.value

    original_mode = mode()
    original_input = kernel.GetStdHandle(0xFFFFFFF6)
    try:
        # Match Textual's virtual-terminal input mode; echo must remain off.
        if not kernel.SetStdHandle(0xFFFFFFF6, handle) or not kernel.SetConsoleMode(handle, 0x0200):
            raise ctypes.WinError(ctypes.get_last_error())
        before = mode()
        output = run_bash({"command": "echo ready", "timeout": 10}, ctx)
        result = {"before": before, "after": mode(), "output": str(output)}
    finally:
        kernel.SetConsoleMode(handle, original_mode)
        kernel.SetStdHandle(0xFFFFFFF6, original_input)
    print(json.dumps(result))
