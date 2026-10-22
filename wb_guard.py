#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 断流守护（wb-guard）
==============================

监控 WorkBuddy 会话是否被上游模型中断（上游 503 / stream read error / 长时间无响应），
一旦判定中断，自动把「继续」发送到 WorkBuddy 输入框，让它接着干活。

判定依据（读取 WorkBuddy 自己的会话记录，不是猜日志）：
  %USERPROFILE%\\.workbuddy\\projects\\<项目>\\<sessionId>.jsonl
  - 最后一条 assistant 消息 status=incomplete 且 providerData.error 存在 → 明确中断
  - 最后一条是 function_call / reasoning / user，且文件超过 stall_timeout 没长 → 卡死中断

续跑动作：
  激活 WorkBuddy 主窗口 → 点击输入框 → 粘贴「继续」→ 回车 → 校验是否产生新记录

纯标准库实现（ctypes 调 Win32），不需要 pip 安装任何东西。

用法：
  python wb_guard.py status            看一眼现在什么状态
  python wb_guard.py watch             常驻监控（默认动作）
  python wb_guard.py watch --dry-run   只报告，不真的发消息
  python wb_guard.py gui               带界面的监控面板
  python wb_guard.py selftest          自检（合成样本，验证判定逻辑）
  python wb_guard.py test-input        安全测试输入通道（只往记事本粘贴，不碰 WorkBuddy）
  python wb_guard.py install-autostart 开机自启
  python wb_guard.py remove-autostart  取消开机自启
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from ctypes import wintypes
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------

APP_NAME = "WorkBuddy"
EXE_NAME = "WorkBuddy.exe"
HOME = Path(os.path.expanduser("~"))
WB_HOME = HOME / ".workbuddy"
PROJECTS_DIR = WB_HOME / "projects"

# PyInstaller 打包后 __file__ 指向临时解包目录（_MEIPASS，进程退出即删），
# 配置/状态/日志必须落在 exe 所在目录，否则重启就丢。frozen 时优先用 exe 路径。
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_DIR = HERE / "logs"
LOG_PATH = LOG_DIR / "wb_guard.log"

LOG_MAX_BYTES = 2 * 1024 * 1024

DEFAULT_CONFIG: Dict[str, Any] = {
    # 目标进程名（一般不用改；自测时可指向 notepad.exe）
    "target_exe": "WorkBuddy.exe",
    # 续跑内容
    "continue_prompt": "继续",
    # 轮询间隔（秒）
    "poll_interval": 5,
    # 卡死判定：最后事件是「思考/工具已返回」，且这么久没有新记录 → 判定中断（秒）
    "stall_timeout": 420,
    # 工具正在执行（function_call 无结果）时的阈值：长命令要留足时间（秒）
    "tool_stall_timeout": 900,
    # 已发出用户消息但模型一直没有首个响应 → 判定中断（秒）
    "first_token_timeout": 180,
    # 同一个中断最多重试几次
    "max_attempts": 6,
    # 重试退避：base * 2^(n-1)，上限 backoff_max
    "backoff_base": 20,
    "backoff_max": 300,
    # 用户最近这么多秒内有键鼠操作，就先不抢焦点（避免打断你打字）
    "user_idle_seconds": 15,
    # 发消息前是否先点一下输入框（更稳；如遇误点可设 false）
    "click_composer": True,
    # 输入框位置：距窗口底部的比例（0.12），并限制在 [min,max] 像素之间
    "composer_offset_ratio": 0.12,
    "composer_offset_min": 100,
    "composer_offset_max": 180,
    # 输入框水平位置（占客户区宽度的比例）：0.45 在「单栏」和「右侧开文档面板」两种版式下都在输入框内
    "composer_x_ratio": 0.45,
    # 发送后等待多少秒校验是否产生新记录
    "verify_seconds": 25,
    # 只监控最近这么多小时内活跃过的会话
    "session_freshness_hours": 8,
    # 一次最多检查多少个会话（多任务并行时防止开销过大）
    "max_sessions": 12,
    # 中断时是否自动切到目标任务再发指令
    "switch_session": True,
    # 切换会话的总时限（秒）：超时就放弃，别把主循环卡死
    "switch_timeout_seconds": 40,
    # 救完别的任务后，是否把界面切回用户原来那个对话（避免把人拽走）
    "switch_back_after_recover": True,
    # 深链切换后等待界面响应的秒数
    "switch_wait_seconds": 4.0,
    # 深链失败时的兜底：左侧任务列表几何参数（首个任务项 y 坐标 / 行高 / 点击 x 坐标）
    "max_expand_rounds": 3,
    "list_first_item_y": 96,
    "list_row_height": 40,
    "list_x": 110,
    # 只监控 cwd 含该子串的会话（留空 = 不限制）
    "project_filter": "",
    # 运行期间挡住系统睡眠 / 显示器休眠。
    # 锁屏后所有输入通道都失效（实测，见 keep_awake 上方注释），
    # 这是唯一纯软件能做、又不用管理员权限的预防手段。
    "keep_awake": True,
    "keep_display_on": True,
    # 只报告不执行
    "dry_run": False,
    # 连续失败这么多次 → 认为模型整体不可用，暂停自动续跑
    "max_consecutive_failures": 8,
    "pause_minutes": 15,
    # WorkBuddy 没在运行时是否自动拉起
    "auto_restart_app": False,
    "restart_command": "",
    # 成功后响一声
    "beep": True,
}

IGNORED_EVENT_TYPES = {
    "session-meta",
    "ai-title",
    "credit-usage",
    "model-usage",
    "file-history-snapshot",
    "resend-fork-notice",
}

STATE_IDLE = "idle"            # 上一轮已正常结束，无需动作
STATE_WORKING = "working"      # 正在跑
STATE_INTERRUPTED = "interrupted"  # 明确报错中断
STATE_STALLED = "stalled"      # 疑似卡死
STATE_NO_SESSION = "no_session"
STATE_APP_DOWN = "app_down"
STATE_PAUSED = "paused"        # 连续失败后主动暂停

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------


class Logger:
    def __init__(self, path: Path = LOG_PATH, echo: bool = True):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _rotate(self):
        try:
            if self.path.exists() and self.path.stat().st_size > LOG_MAX_BYTES:
                backup = self.path.with_suffix(".1.log")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
        except Exception:
            pass

    def __call__(self, msg: str, level: str = "INFO"):
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [{level:<5}] {msg}"
        with self._lock:
            if self.echo:
                try:
                    print(line, flush=True)
                except Exception:
                    pass
            try:
                self._rotate()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass


log = Logger()

# --------------------------------------------------------------------------
# Win32 基础封装
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t

VK_MENU = 0x12
VK_CONTROL = 0x11
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9
CREATE_NO_WINDOW = 0x08000000
TH32CS_SNAPPROCESS = 0x00000002

# SetThreadExecutionState 标志：挡住显示器休眠 / 系统睡眠
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ES_AWAYMODE_REQUIRED = 0x00000040


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ULONG_PTR),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ULONG_PTR]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def enable_dpi_awareness() -> None:
    """避免高分屏下坐标换算偏差。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def pids_of(exe_name: str = EXE_NAME) -> List[int]:
    pids: List[int] = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value or not snap:
        return pids
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == exe_name.lower():
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_rect(hwnd: int) -> RECT:
    rc = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rc))
    return rc


def find_main_window(exe_name: str = EXE_NAME) -> Optional[int]:
    """在目标进程的可见顶层窗口里挑面积最大的那个（WorkBuddy 主窗口）。"""
    pids = set(pids_of(exe_name))
    if not pids:
        return None
    found: List[tuple] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if window_pid(hwnd) not in pids:
                return True
            title = window_title(hwnd)
            if not title:
                return True
            rc = window_rect(hwnd)
            area = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
            if area < 400 * 300:
                return True
            found.append((area, hwnd, title))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    if not found:
        return None
    found.sort(key=lambda x: x[0], reverse=True)
    # 优先标题里带 WorkBuddy 的
    for area, hwnd, title in found:
        if APP_NAME.lower() in title.lower():
            return hwnd
    return found[0][1]


def app_running(exe_name: str = EXE_NAME) -> bool:
    return bool(pids_of(exe_name))


def _desktop_locked_by_window() -> bool:
    """判据 A：看前台窗口是不是锁屏界面。

    实测（Win11 26200 / 本机，2026-10-22）：锁屏时前台窗口是
    `Windows.UI.Core.CoreWindow`，标题 `Windows Default Lock Screen`。
    这是 XAML 锁屏，不是经典的 LogonUI 密码框，所以 ClassName 里没有
    `LogonUI` 字样，必须匹配标题。

    锁屏界面存在时前台一定被它占着 —— 也就是说 WorkBuddy 必然拿不到
    前台，模拟输入必然落空。这个判据最直接、开销最小（两次 Win32 调用）。
    """
    try:
        user32.GetForegroundWindow.restype = wintypes.HWND
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg, cls, 256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(fg, title, 256)
        c, t = cls.value, (title.value or "")
        if "Lock Screen" in t or "锁屏" in t:
            return True
        if c in ("LockScreenBackstopFrame", "LockScreenControllerProxy"):
            return True
        # 经典登录界面（UAC / 安全桌面）
        if c.startswith("LogonUI"):
            return True
    except Exception:
        pass
    return False


def _desktop_locked_by_process() -> bool:
    """判据 B：LogonUI.exe 存在（Win10/11 通用）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq LogonUI.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=6,
            creationflags=CREATE_NO_WINDOW,
        )
        if out.stdout and "LogonUI.exe" in out.stdout:
            return True
    except Exception:
        pass
    return False


def _desktop_locked_by_name() -> bool:
    """判据 C（兜底，可靠性最低）：当前输入桌面名不是 Default。

    注意：实测本机锁屏时 OpenInputDesktop 依然返回 `Default`
    （Win11 把锁屏做成了普通桌面上的一个 XAML 窗口），所以这条判据
    不能反向使用 —— 「拿到 Default」不等于「没锁屏」。
    只保留「拿到非 Default」→ 判定锁屏 这一个方向。
    """
    try:
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        hdesk = user32.OpenInputDesktop(0, True, 0x0001)  # DESKTOP_READOBJECTS
        if not hdesk:
            return False  # 拿不到句柄不足以判定，交给其它判据
        try:
            buf = ctypes.create_unicode_buffer(256)
            n = user32.GetUserObjectInformationW(hdesk, 2, buf, 512, None)  # UOI_NAME
            if n:
                return buf.value.strip().lower() != "default"
        finally:
            user32.CloseDesktop(hdesk)
    except Exception:
        pass
    return False


def workstation_locked() -> bool:
    """检测工作站是否已锁屏。

    锁屏时模拟输入（点击、粘贴）全部无效，窗口也切不到前台，
    表现为「日志说发了但没反应」。这是守护最常见的无效原因之一，
    必须在发送前就查出来，而不是等激活失败再猜。

    三条判据取并集（任一命中即锁屏）：
      A 前台窗口是锁屏界面（Windows Default Lock Screen / LogonUI*）
      B LogonUI.exe 进程存在
      C 当前输入桌面不是 Default

    历史坑：旧版把 C 当作兜底，且写成「OpenInputDesktop 失败 → 锁屏」。
    本机实测锁屏时 OpenInputDesktop 成功且返回 Default，所以旧兜底永远
    返回 False；一旦 LogonUI 判据在某些 Win11 版本上不成立，就会误判
    「没锁屏」→ 盲目发送 → 用户看到「日志说发了但没反应」。这版修掉。
    """
    return _desktop_locked_by_window() or _desktop_locked_by_process() or _desktop_locked_by_name()


# --------------------------------------------------------------------------
# 锁屏规避：唯一「程序自己能做到」的一件事
# --------------------------------------------------------------------------
#
# 实测结论（Win11 26200，2026-10-22，逐条验证过）：
#
#   通道                        锁屏时是否可用   说明
#   ------------------------------------------------------------------------
#   SendInput 模拟键鼠           ✗             前台被锁屏界面占用，输入不落到目标窗口
#   SetForegroundWindow          ✗             返回 0，err=5（拒绝访问）
#   UIA（uiautomation）          ✗             树塌成几个空 Pane，读到 0 个任务项
#   剪贴板 OpenClipboard         ✗             全部失败，读回 None
#   PostMessage 到标准 Win32 控件 ✓             记事本 Edit 实测成功写入
#   PostMessage 到 Electron      ✗             WorkBuddy 主窗口 0 个子窗口，无可投递目标
#   OpenInputDesktop 判桌面名     ✗（不可信）    锁屏时仍返回 "Default"，不能用它反推「没锁」
#
# 所以「锁屏状态下还能发指令」这件事，纯软件手段做不到 —— 这是 Windows 的
# 安全边界，不是实现不够聪明。能做的是「别让机器进入锁屏/熄屏」：
#
#   1) SetThreadExecutionState 挡住显示器休眠与系统睡眠（本函数，无需提权）
#   2) 关闭屏保 / 调长电源计划超时（需要用户自己设置，见 README）
#   3) 组策略 NoLockScreen（需管理员，且只挡锁屏不挡熄屏）
#
# 注意 1) 只能挡「因空闲触发的睡眠/熄屏」，挡不住用户手动 Win+L。

_keeper_state = {"on": False}


def keep_awake(enable: bool = True, with_display: bool = True) -> bool:
    """挡住系统睡眠 / 显示器休眠，返回是否成功。

    用 SetThreadExecutionState 设 ES_CONTINUOUS，效果绑定在调用线程上：
    线程退出即失效。守护是常驻线程，所以适合在这里调。

    只挡空闲触发的睡眠；用户手动锁屏（Win+L）不受影响。
    """
    kernel32.SetThreadExecutionState.restype = wintypes.ULONG
    flags = ES_CONTINUOUS
    if enable:
        flags |= ES_SYSTEM_REQUIRED
        if with_display:
            flags |= ES_DISPLAY_REQUIRED
    try:
        ret = kernel32.SetThreadExecutionState(flags)
    except Exception:
        return False
    ok = bool(ret)
    if ok:
        _keeper_state["on"] = enable
    return ok


def display_settings_hint() -> Dict[str, Any]:
    """读当前电源/屏保设置，给出「还能怎么调」的具体数值。"""
    info: Dict[str, Any] = {"display_off_seconds": None, "sleep_seconds": None,
                            "screensaver": None}
    try:
        out = subprocess.run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"],
                             capture_output=True, text=True, errors="replace",
                             timeout=8, creationflags=CREATE_NO_WINDOW)
        for line in (out.stdout or "").splitlines():
            s = line.strip()
            if "AC Power Setting Index" in s or "当前交流电源设置索引" in s:
                info["display_off_seconds"] = int(s.split(":")[-1].strip() or s.split("：")[-1].strip(), 16)
    except Exception:
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
            info["screensaver"] = winreg.QueryValueEx(k, "ScreenSaveActive")[0]
    except Exception:
        pass
    return info


def activate_window(hwnd: int) -> bool:
    """把窗口拉到前台并确认成功。"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.4)
    # Alt 轻点一下，解除前台锁定限制
    _send_inputs([_key_input(VK_MENU, False), _key_input(VK_MENU, True)])
    time.sleep(0.05)
    user32.BringWindowToTop(hwnd)
    if user32.GetForegroundWindow() != hwnd:
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        cur_tid = kernel32.GetCurrentThreadId()
        attached = user32.AttachThreadInput(cur_tid, fg_tid, True)
        try:
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.35)
    return user32.GetForegroundWindow() == hwnd


def _key_input(vk: int, up: bool) -> INPUT:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = KEYBDINPUT(wVk=vk, wScan=0,
                         dwFlags=KEYEVENTF_KEYUP if up else 0,
                         time=0, dwExtraInfo=0)
    return item


def _send_inputs(items: List[INPUT]) -> int:
    if not items:
        return 0
    arr = (INPUT * len(items))(*items)
    return int(user32.SendInput(len(items), arr, ctypes.sizeof(INPUT)))


def tap_key(vk: int) -> None:
    _send_inputs([_key_input(vk, False), _key_input(vk, True)])


def press_ctrl_v() -> None:
    _send_inputs([
        _key_input(VK_CONTROL, False),
        _key_input(0x56, False),  # V
        _key_input(0x56, True),
        _key_input(VK_CONTROL, True),
    ])


def press_enter() -> None:
    tap_key(VK_RETURN)


def click_client(hwnd: int, x: int, y: int) -> bool:
    pt = POINT(x, y)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return False
    user32.SetCursorPos(pt.x, pt.y)
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    time.sleep(0.06)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
    time.sleep(0.25)
    return True


# --------------------------------------------------------------------------
# UIA：读取/操作 WorkBuddy 的 Web 界面（任务列表、对话标题）
# --------------------------------------------------------------------------
# WorkBuddy 界面是 Electron 渲染的，没有原生子控件，但 Windows 无障碍层（UIA）
# 能读到完整的元素树。左侧任务列表每项都是一个 Button，Name 就是任务标题，
# 所以可以「按标题精确点击」来切换会话 —— 这比按坐标盲点可靠得多。
#
# 依赖 uiautomation 库；缺失时自动降级（切换会话返回失败，不会发错会话）。

_uia_lock = threading.Lock()
_uia_worker: Optional["UiaWorker"] = None


class UiaWorker:
    """常驻 UIA 工作线程。

    为什么不用「每次起一个线程」：uiautomation 底层是 comtypes，
    在同一进程里反复创建/销毁带 COM 上下文的线程会触发段错误（Python 3.14 实测必现）。
    所以改成单个常驻线程，所有 UIA 调用排队串行执行。
    """

    def __init__(self):
        self.q: "queue.Queue" = queue.Queue()
        self.ready = threading.Event()
        self.alive = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="uia-worker")
        self.thread.start()
        self.ready.wait(5.0)

    def _loop(self):
        init = None
        try:
            import uiautomation as auto
            init = auto.UIAutomationInitializerInThread(debug=False)
            init.__enter__()
            self.alive = True
        except Exception as e:
            log(f"UIA 线程初始化失败：{e}", "WARN")
            self.alive = False
            self.ready.set()
            return
        self.ready.set()
        try:
            while True:
                fn, box, done = self.q.get()
                if fn is None:
                    break
                try:
                    box["ok"] = fn()
                except Exception as e:
                    box["err"] = e
                finally:
                    done.set()
        finally:
            try:
                if init is not None:
                    init.__exit__(None, None, None)
            except Exception:
                pass

    def call(self, fn, timeout: float = 12.0):
        if not self.alive:
            raise RuntimeError("UIA 线程不可用")
        box: Dict[str, Any] = {}
        done = threading.Event()
        self.q.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError(f"UIA 操作超时（{timeout}s）")
        if "err" in box:
            raise box["err"]
        return box.get("ok")

    def stop(self):
        try:
            self.q.put((None, {}, threading.Event()))
        except Exception:
            pass


def _uia_run(fn, timeout: float = 12.0):
    """把 UIA 操作交给常驻工作线程执行（串行、不阻塞主循环、不会段错误）。"""
    global _uia_worker
    with _uia_lock:
        if _uia_worker is None or not _uia_worker.alive:
            _uia_worker = UiaWorker()
        worker = _uia_worker
    return worker.call(fn, timeout)


def uia_available() -> bool:
    try:
        import uiautomation  # noqa: F401
        return True
    except Exception:
        return False


def _uia_main_ctrl():
    """拿到 WorkBuddy 主窗口的 UIA 控件。"""
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(2)
    hwnd = find_main_window()
    if not hwnd:
        return None
    return auto.ControlFromHandle(hwnd)


def _uia_elements(pred, max_depth: int = 14, limit: int = 6000, with_rect: bool = False):
    """遍历 UIA 树，收集满足 pred(ctrl) 的元素。

    注意：Electron 的整棵树可能有上万节点，遍历必须给 limit，否则会卡死。
    """
    out = []
    count = [0]
    ctrl = _uia_main_ctrl()
    if ctrl is None:
        return out

    stack = [(ctrl, 0)]
    while stack:
        c, depth = stack.pop()
        if depth > max_depth or count[0] > limit:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            continue
        for k in kids:
            count[0] += 1
            if count[0] > limit:
                break
            try:
                if pred(k):
                    out.append(k)
            except Exception:
                pass
            if depth + 1 <= max_depth:
                stack.append((k, depth + 1))
    return out


def uia_task_items() -> List[dict]:
    """读出左侧「任务」列表的每一项：{title, name(含时间), x, y, cx, cy}。

    列表每项是一个 Button（Name = 标题 + 相对时间）。窗口按钮、工具栏按钮
    也在同一个可访问性树里，所以必须按「左栏区域 + 行高」过滤。
    """
    def job():
        # 每次都重新取根元素：界面切换后旧元素会失效（返回 0 个子节点）
        for attempt in range(3):
            items = _collect_task_items_once()
            if items:
                return items
            time.sleep(0.4)
        return []

    return _uia_run(job, timeout=15.0) or []


def _collect_task_items_once() -> List[dict]:
    """读出左栏任务条目（含「查看更多」入口、空间区里的项目）。

    踩坑记录（这版是第三版，前两版都不稳）：
    1) 最早用 `_uia_elements(is_button, limit=6000)` —— 列表滚动后全树节点上万，
       全局预算在爬到左栏之前就耗尽，表现为「有时读得到、有时读出空列表」。
    2) 改成「按矩形裁剪子树」—— 无效。Electron 各级 Pane 的 BoundingRectangle
       都是整个客户区，祖先永远跨全窗，剪枝条件永远不成立（或误剪），结果直接读空。

    现在：全树 DFS，**只保留 ButtonControl 进入候选**（文本、消息正文等类型直接跳过判定，
    但继续下钻），预算提到 60000。左栏条目一定在树里较早的位置，实际耗时 0.3~1s。
    """
    ctrl = _uia_main_ctrl()
    if ctrl is None:
        return []

    hwnd = find_main_window()
    win = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(win))
    left0, top0 = win.left, win.top
    win_w = win.right - win.left
    win_h = win.bottom - win.top

    raw = []
    stack = [(ctrl, 0)]
    guard = 0
    while stack and guard < 60000:
        c, depth = stack.pop()
        guard += 1
        if depth > 16:
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            continue
        for k in kids:
            try:
                is_btn = k.ControlTypeName == "ButtonControl"
            except Exception:
                is_btn = False
            if is_btn:
                try:
                    nm = (k.Name or "").strip()
                    r = k.BoundingRectangle
                    if nm and len(nm) <= 200:
                        rel_x = r.left - left0
                        rel_y = r.top - top0
                        if (0 <= rel_x <= win_w * 0.30
                                and 100 <= rel_y <= win_h - 40
                                and 80 <= r.width() <= win_w * 0.30
                                and 18 <= r.height() <= 60):
                            raw.append({
                                "name": nm,
                                "x": int(rel_x), "y": int(rel_y),
                                "cx": int(r.left + r.width() / 2),
                                "cy": int(r.top + r.height() / 2),
                                "w": int(r.width()), "h": int(r.height()),
                            })
                except Exception:
                    pass
            stack.append((k, depth + 1))

    if not raw:
        return []
    raw.sort(key=lambda d: d["y"])
    # 同一列（x 相近）、同高的一簇才是列表项；取最大的那簇
    groups: Dict[tuple, List[dict]] = {}
    for it in raw:
        key = (round(it["x"] / 8), round(it["h"] / 4))
        groups.setdefault(key, []).append(it)
    best = max(groups.values(), key=len)
    if len(best) < 2:
        best = raw
    best.sort(key=lambda d: d["y"])
    return best



def uia_left_rail_items() -> List[dict]:
    """读出左栏所有可点击条目（含「查看更多」入口、空间区里的项目）。"""
    return _uia_run(lambda: _collect_task_items_once(), timeout=15.0) or []


def uia_click_named(name: str, exact: bool = True) -> bool:
    """按名字点击左栏某个条目（用于「查看更多」入口和项目名）。"""
    def job():
        raws = _collect_task_items_once()
        for it in raws:
            nm = it["name"]
            if (exact and nm == name) or (not exact and name in nm):
                hwnd = find_main_window()
                if hwnd and not activate_window(hwnd):
                    return False
                time.sleep(0.2)
                pt = POINT(it["cx"], it["cy"])
                user32.ScreenToClient(hwnd, ctypes.byref(pt))
                click_client(hwnd, pt.x, pt.y)
                time.sleep(0.9)
                return True
        return False

    return bool(_uia_run(job, timeout=15.0))


def _scroll_rail(delta: int, hwnd=None) -> None:
    """把鼠标放到左侧列表上滚轮（正数向上、负数向下）。"""
    hwnd = hwnd or find_main_window()
    if not hwnd:
        return
    sp = POINT(130, 520)
    user32.ClientToScreen(hwnd, ctypes.byref(sp))
    ctypes.windll.user32.SetCursorPos(sp.x, sp.y)
    time.sleep(0.05)
    mouse_wheel(delta)


def uia_find_and_click(title: str, session: Optional[Session] = None,
                       max_expand: int = 3, budget: float = 40.0) -> bool:
    """在左栏里找到某个任务并点开。

    WorkBuddy 左栏是分组 + 可滚动的：
      任务 (N)   —— 最近若干条，其余藏在「查看更多」
      空间 (N)   —— 按项目分组（PPJH / REA / 钢筋算量 …），项目下缩进列出任务

    查找顺序：
      1) 当前可见列表（最快）
      2) 展开「查看更多」
      3) 上下滚动侧栏（覆盖藏在滚动区外的条目）
      4) 目标项目分组若出现，点开它再看

    性能要点：整轮用**一个总时限 budget**（默认 40s）。早期版本每个候选标题都跑一遍
    完整滚动（6+12 次滚轮），三个候选加起来 90 秒以上，把守护主循环整个卡死。
    """
    if not title:
        return False
    deadline = time.time() + budget

    def time_left() -> bool:
        return time.time() < deadline

    def click_item(hit) -> bool:
        hwnd = find_main_window()
        if hwnd and not activate_window(hwnd):
            return False
        time.sleep(0.25)
        fp_before = header_fingerprint()
        pt = POINT(hit["cx"], hit["cy"])
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        click_client(hwnd, pt.x, pt.y)
        time.sleep(1.3)
        # 首选：读标题文字确认（最可靠）。之前只靠指纹，导致「本来就已经在这个会话、
        # 点一下没有任何视觉变化」被误判成失败 —— 这是 9 次 switch-failed 的根因之一。
        for _ in range(3):
            cur = ""
            try:
                cur = uia_header_text()
            except Exception:
                pass
            if cur and _title_similar(cur, title):
                return True
            time.sleep(0.5)
        cur = ""
        try:
            cur = uia_header_text()
        except Exception:
            pass
        if cur:
            log(f"点开后标题是「{cur[:24]}」，与目标「{title[:24]}」不符", "WARN")
            return False
        # 标题读不到（窗口在后台时 Electron 报空矩形）才退回指纹
        fp_after = header_fingerprint()
        if fp_before and fp_after and _fp_changed(fp_before, fp_after):
            return True
        time.sleep(1.2)
        fp2 = header_fingerprint()
        return bool(fp_before and fp2 and _fp_changed(fp_before, fp2))

    def find_in(raws):
        for it in raws:
            if _title_similar(it["name"], title):
                return it
        return None

    def click_at(item) -> None:
        hwnd = find_main_window()
        pt = POINT(item["cx"], item["cy"])
        user32.ScreenToClient(hwnd, ctypes.byref(pt))
        click_client(hwnd, pt.x, pt.y)
        time.sleep(1.0)

    proj = os.path.basename((session.cwd or "").rstrip("\\/")) if (session and session.cwd) else ""

    def job():
        # ① 先看当前可见（覆盖绝大多数情况，最快）
        hit = find_in(_collect_task_items_once())
        if hit:
            return click_item(hit)

        # ② 展开「查看更多」
        raws = _collect_task_items_once()
        more = next((it for it in raws if it["name"].startswith("查看更多")), None)
        if more:
            click_at(more)
            hit = find_in(_collect_task_items_once())
            if hit:
                return click_item(hit)

        # ③ 滚动侧栏查找（带总时限，别把主循环卡死）
        for direction, times in ((-5, 5), (5, 10)):
            for _ in range(times):
                if not time_left():
                    return False
                _scroll_rail(direction)
                time.sleep(0.4)
                raws = _collect_task_items_once()
                hit = find_in(raws)
                if hit:
                    return click_item(hit)
                more = next((it for it in raws if it["name"].startswith("查看更多")), None)
                if more:
                    click_at(more)
                    hit = find_in(_collect_task_items_once())
                    if hit:
                        return click_item(hit)
                # ④ 顺路看目标项目分组是否出现，点开它
                if proj and len(proj) >= 3:
                    pj = next((it for it in raws
                               if it["name"].strip().lower() == proj.lower()), None)
                    if pj:
                        click_at(pj)
                        hit = find_in(_collect_task_items_once())
                        if hit:
                            return click_item(hit)
        return False

    return bool(_uia_run(job, timeout=budget + 30.0))



def uia_task_titles() -> List[str]:
    return [it["name"] for it in uia_task_items()]


def header_fingerprint() -> str:
    """对话标题区域的像素指纹。

    WorkBuddy 顶部标题多是绘制文本，UIA 读不到 Name；此时用像素指纹判断
    「界面是否已经换了一个会话」：同一会话重绘后指纹基本不变，换会话必变。
    """
    try:
        hwnd = find_main_window()
        if not hwnd:
            return ""
        win = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(win))
        region = (win.left + 280, win.top + 48, 520, 34)
        tmp = LOG_PATH.parent / "_hdr_fp.png"
        capture_screen(tmp, region)
        from PIL import Image

        im = Image.open(tmp).convert("L")
        px = list(im.getdata())
        w, h = im.size
        n = len(px)
        if not n:
            return ""
        mean = sum(px) / n
        var = sum((p - mean) ** 2 for p in px) / n
        edges = 0
        for y in range(h):
            base = y * w
            for x in range(w - 1):
                if abs(px[base + x] - px[base + x + 1]) > 28:
                    edges += 1
        denom = max(1, h * (w - 1))
        return f"{mean:.1f}|{var ** 0.5:.1f}|{edges / denom:.4f}"
    except Exception:
        return ""


def _fp_changed(a: str, b: str, tol: float = 0.06) -> bool:
    """两个指纹是否显著不同（相对差异超过 tol）。"""
    if not a or not b:
        return False
    try:
        va = [float(x) for x in a.split("|")]
        vb = [float(x) for x in b.split("|")]
    except Exception:
        return a != b
    scale = max(1.0, abs(va[0]), abs(va[1]))
    d = abs(va[0] - vb[0]) / scale + abs(va[1] - vb[1]) / scale + abs(va[2] - vb[2])
    return d > tol


def uia_header_text() -> str:
    """读出内容区顶部那条「会话标题」文字。

    定位这个标题踩过三个坑，都记在这里免得再走弯路：
    1) 它**层级很深（实测第 9 层）**。早期版本限 depth<=8、只扫 1500 节点 → 永远扫不到 → 一直返回空，
       直接导致「点击后无法确认是否切换成功」，把所有"本来就已经在这个会话"的情况误判成切换失败。
    2) 用统一的 DFS + 全局节点预算也不行：预算会在消息列表里耗尽
       （实测 12000 预算下只拿到 333 个 TextControl，标题不在其中）。
    3) 按矩形裁剪很快，但**取不到矩形时不能剪** —— Electron 偶发在中间节点上抛异常，
       一剪就把标题的整条祖先链剪没了，表现为"有时读得到、有时读不到"。

    所以：DFS + 矩形裁剪（取不到矩形就照常下钻）+ 若一条都没找到，再做一次**无剪枝**的全树兜底扫描。
    实测标题节点：屏幕 (304, 51)、512x21，相对窗口 (312, 59)。
    """
    def job():
        ctrl = _uia_main_ctrl()
        if ctrl is None:
            return ""
        hwnd = find_main_window()
        win = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(win))
        win_left, win_top = win.left, win.top
        win_w = win.right - win.left
        band_bottom = win_top + 110          # 头部横带下沿
        band_left = win_left + 270           # 左栏右边界

        # 前置判据：WorkBuddy 不在前台时，Electron 给标题等控件报空矩形，
        # 怎么扫都是空（实测要白扫 6~19 秒）。直接返回，让调用方先去置前。
        try:
            fg = user32.GetForegroundWindow()
            if fg != hwnd:
                fg_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
                own_pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(own_pid))
                if fg_pid.value != own_pid.value:
                    return ""
        except Exception:
            pass

        def rect_of(k):
            try:
                return k.BoundingRectangle
            except Exception:
                return None

        def scan(prune: bool, max_nodes: int) -> str:
            cands = []
            stack = [(ctrl, 0)]
            seen = 0
            while stack and seen < max_nodes:
                c, depth = stack.pop()
                seen += 1
                if depth > 18:
                    continue
                try:
                    kids = c.GetChildren()
                except Exception:
                    continue
                for k in kids:
                    r = rect_of(k)
                    # 仅在有可靠矩形、且整块都在头部下方时剪枝；取不到矩形一律照常下钻
                    if prune and r is not None and r.top >= band_bottom:
                        continue
                    try:
                        nm = (k.Name or "").strip()
                        ct = k.ControlTypeName
                    except Exception:
                        nm, ct = "", ""
                    if nm and ct == "TextControl" and r is not None:
                        try:
                            if (band_left <= r.left <= win_left + win_w * 0.80
                                    and 24 <= (r.top - win_top) <= 96
                                    and r.width() >= 60
                                    and 14 <= r.height() <= 48
                                    and 2 <= len(nm) <= 200):
                                cands.append(((r.top - win_top), -r.width(), nm))
                        except Exception:
                            pass
                    stack.append((k, depth + 1))
            if not cands:
                return ""
            cands.sort()
            return cands[0][2]

        # 快扫（带剪枝）成本 <1s；找不到时做**一次**全树兜底，其余轮次只重试快扫。
        # 窗口刚置前时 Electron 可能还没重绘完、控件矩形为空，表现为「第一次读是空、
        # 过一会儿就有了」，所以需要重试，但重试必须是便宜的，否则会把主循环拖死。
        for attempt in range(6):
            got = scan(True, 40000)
            if got:
                return got
            if attempt == 2:
                got = scan(False, 60000)
                if got:
                    return got
            time.sleep(0.5)
        return ""

    return _uia_run(job, timeout=30.0) or ""


def uia_current_conversation() -> str:
    """当前会话标题（保留旧名字，兼容既有调用）。"""
    return uia_header_text()



_REL_TIME_TAIL = re.compile(
    r"\s*(?:刚刚|\d+\s*秒前|\d+\s*分钟前|\d+\s*小时前|昨天|前天|\d+\s*天前|"
    r"\d+\s*月\s*\d+\s*日|\d{4}-\d{2}-\d{2})\s*$"
)


def _norm_title(t: str) -> str:
    """归一化标题：去空白、去尾部省略号、去「3小时前」这类相对时间后缀。

    左栏条目和内容区标题都会被 UI 截断成 `...`，还常带相对时间后缀，
    不先洗掉这两样，任何前缀/包含判断都会失败。
    """
    s = re.sub(r"\s+", " ", (t or "")).strip().lower()
    for _ in range(3):
        before = s
        s = s.rstrip()
        s = re.sub(r"(?:\.{2,}|\u2026+)$", "", s).strip()   # 去掉尾部 ... 或 …
        s = _REL_TIME_TAIL.sub("", s).strip()                  # 去掉「3小时前」
        if s == before:
            break
    return s


def _title_similar(a: str, b: str) -> bool:
    """标题模糊匹配。

    必须容忍三种噪声（都是实际遇到的）：
      1) UI 截断：`I://电子书//机械工业出版社6000 再帮我挑30本和…」`；
      2) 相对时间后缀：`安装 opendataloader-pdf 技能 昨天`；
      3) 全角/半角标点差异。
    """
    if not a or not b:
        return False
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 6 and (na.startswith(nb) or nb.startswith(na)):
        return True
    if len(na) >= 8 and (na in nb or nb in na):
        return True
    # 只保留字母数字汉字后再比前 8 个字符（应对标点差异）
    ca = re.sub(r"[^\w\u4e00-\u9fff]", "", na)[:8]
    cb = re.sub(r"[^\w\u4e00-\u9fff]", "", nb)[:8]
    if not ca or not cb:
        return False
    n = min(len(ca), len(cb))
    return n >= 4 and ca[:n] == cb[:n]


def uia_click_task(title: str, timeout: float = 4.0) -> bool:
    """在左侧任务列表里按标题点击，并用「标题文字 + 像素指纹」双重校验是否切换。"""
    if not title:
        return False
    deadline = time.time() + timeout

    while time.time() < deadline:
        items = uia_task_items()
        hit = None
        for it in items:
            if _title_similar(it["name"], title):
                hit = it
                break
        if hit:
            hwnd = find_main_window()
            if hwnd and not activate_window(hwnd):
                return False
            time.sleep(0.15)

            fp_before = header_fingerprint()
            txt_before = ""
            try:
                txt_before = uia_current_conversation()
            except Exception:
                pass

            pt = POINT()
            pt.x, pt.y = hit["cx"], hit["cy"]
            user32.ScreenToClient(hwnd, ctypes.byref(pt))
            click_client(hwnd, pt.x, pt.y)
            time.sleep(1.2)

            # 校验①：顶部标题文字能读到且匹配
            cur = ""
            try:
                cur = uia_current_conversation()
            except Exception:
                pass
            if cur and _title_similar(cur, title):
                return True
            # 校验②：标题读不到 → 用像素指纹是否变化判断
            if not cur:
                fp_after = header_fingerprint()
                if fp_before and fp_after and _fp_changed(fp_before, fp_after):
                    return True
                # 再等一轮（界面可能还在重绘）
                time.sleep(1.2)
                fp_after2 = header_fingerprint()
                return bool(fp_before and fp_after2 and _fp_changed(fp_before, fp_after2))
            # 标题读到了但不匹配 → 说明点错了，不再重试同一项
            log(f"点击后标题为「{cur}」，与目标「{title}」不符", "WARN")
            return False

        # 目标任务可能在「查看更多」里，滚动列表再找
        try:
            hwnd = find_main_window()
            if hwnd:
                sp = POINT(130, 400)
                user32.ClientToScreen(hwnd, ctypes.byref(sp))
                ctypes.windll.user32.SetCursorPos(sp.x, sp.y)
                mouse_wheel(-3)
        except Exception:
            pass
        time.sleep(0.6)
    return False
    """标题模糊匹配：列表里会截断，所以用前缀/包含判断。"""
    if not a or not b:
        return False
    na = re.sub(r"\s+", " ", a).strip().lower()
    nb = re.sub(r"\s+", " ", b).strip().lower()
    if na == nb:
        return True
    if len(na) >= 6 and (na.startswith(nb) or nb.startswith(na)):
        return True
    if len(na) >= 8 and (na in nb or nb in na):
        return True
    # 中文短标题：去掉标点后前 6 字相同即可
    ca = re.sub(r"[^\w\u4e00-\u9fff]", "", na)[:6]
    cb = re.sub(r"[^\w\u4e00-\u9fff]", "", nb)[:6]
    return bool(ca) and ca == cb and len(ca) >= 4




def mouse_wheel(delta: int) -> None:
    """在鼠标当前位置滚轮（负值向下）。"""
    item = INPUT()
    item.type = 0  # INPUT_MOUSE
    item.mi = MOUSEINPUT(dx=0, dy=0, mouseData=delta * 120,
                         dwFlags=0x0800,  # MOUSEEVENTF_WHEEL
                         time=0, dwExtraInfo=0)
    _send_inputs([item])


def idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 1e9
    now = kernel32.GetTickCount()
    return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def get_clipboard_text() -> Optional[str]:
    for _ in range(6):
        if user32.OpenClipboard(None):
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return None
                handle = user32.GetClipboardData(CF_UNICODETEXT)
                if not handle:
                    return None
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    return None
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        time.sleep(0.1)
    return None


def set_clipboard_text(text: str) -> bool:
    for _ in range(6):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                size = (len(text) + 1) * 2
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
                if not handle:
                    return False
                ptr = kernel32.GlobalLock(handle)
                if not ptr:
                    kernel32.GlobalFree(handle)
                    return False
                ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), size)
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                    kernel32.GlobalFree(handle)
                    return False
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.1)
    return False


# --------------------------------------------------------------------------
# 截屏（纯标准库，用于位置探针与自检）
# --------------------------------------------------------------------------

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]


def capture_screen(path, region=None, marks=None) -> Path:
    """截屏存 PNG。region=(x,y,w,h)；marks=[(x,y,(r,g,b))] 用于画标记（窗口坐标）。"""
    path = Path(path)
    x = user32.GetSystemMetrics(76)
    y = user32.GetSystemMetrics(77)
    w = user32.GetSystemMetrics(78)
    h = user32.GetSystemMetrics(79)
    if region:
        x, y, w, h = region

    hdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    gdi32.BitBlt(mdc, 0, 0, w, h, hdc, x, y, SRCCOPY)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = w
    bi.biHeight = -h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(None, hdc)

    data = bytearray(buf.raw)
    for mx, my, color in (marks or []):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                px, py = mx + dx, my + dy
                if 0 <= px < w and 0 <= py < h:
                    i = (py * w + px) * 4
                    data[i:i + 4] = bytes((color[2], color[1], color[0], 255))

    stride = w * 3
    rows = bytearray()
    for yy in range(h):
        base = yy * w * 4
        row = bytearray(stride)
        for xx in range(w):
            i = base + xx * 4
            row[xx * 3] = data[i + 2]
            row[xx * 3 + 1] = data[i + 1]
            row[xx * 3 + 2] = data[i]
        rows += b"\x00" + bytes(row)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(rows), 6))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


# --------------------------------------------------------------------------
# 会话读取与判定
# --------------------------------------------------------------------------


@dataclass
class Session:
    path: Path
    session_id: str
    cwd: str
    mtime: float
    size: int

    @property
    def age(self) -> float:
        return time.time() - self.mtime


def list_sessions() -> List[Session]:
    out: List[Session] = []
    if not PROJECTS_DIR.exists():
        return out
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        try:
            files = list(project_dir.glob("*.jsonl"))
        except Exception:
            continue
        for f in files:
            try:
                st = f.stat()
            except Exception:
                continue
            if st.st_size < 64:
                continue
            cwd = ""
            sid = f.stem
            out.append(Session(path=f, session_id=sid, cwd=cwd,
                               mtime=st.st_mtime, size=st.st_size))
    out.sort(key=lambda s: s.mtime, reverse=True)
    return out


def session_cwd(path: Path, max_bytes: int = 65536) -> str:
    """从记录里捞 cwd（只读开头一小段）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            chunk = f.read(max_bytes)
    except Exception:
        return ""
    m = re.search(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return m.group(1)


def read_tail_records(path: Path, max_bytes: int = 768 * 1024) -> List[dict]:
    """读文件尾部若干条 JSON 记录。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # 丢掉半行
            raw = f.read()
    except Exception:
        return []
    recs: List[dict] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            recs.append(obj)
    return recs


@dataclass
class Diagnosis:
    state: str
    detail: str = ""
    session: Optional[Session] = None
    record_id: str = ""
    error_code: str = ""
    error_message: str = ""
    last_event_type: str = ""
    last_event_age: float = 0.0
    turn_started: float = 0.0
    records: int = 0

    @property
    def needs_action(self) -> bool:
        return self.state in (STATE_INTERRUPTED, STATE_STALLED)


def _text_of(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


# 界面上会以「红字错误条」出现、且必须重发才能继续的文案特征。
# 注意：这些词只在「整段正文很短且没有正常回复结构」时才判定，避免误伤正常回答。
ERROR_TEXT_PATTERNS = (
    "服务暂时不可用",
    "请稍后重试",
    "当前共享版请求较多",
    "算力不足",
    "模型已在网关中关闭",
    "模型不存在",
    "无可用渠道",
    "请求超时",
    "连接超时",
    "网络错误",
    "stream error",
    "rate limit",
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "invalid api key",
    "insufficient quota",
)


def _guess_error_code(text: str) -> str:
    """从错误正文里推断一个便于识别和去重的错误码。"""
    low = text.lower()
    m = re.search(r"\b(4\d\d|5\d\d)\b", text)
    http_code = m.group(1) if m else ""
    if "模型已在网关中关闭" in text:
        return "model-disabled" + (f"/{http_code}" if http_code else "")
    if "服务暂时不可用" in text:
        return "service-unavailable" + (f"/{http_code}" if http_code else "")
    if "算力不足" in text or "请求较多" in text:
        return "overloaded" + (f"/{http_code}" if http_code else "")
    for kw in ("rate limit", "quota", "api key", "overloaded", "timeout",
               "stream error", "internal server error", "bad gateway"):
        if kw in low:
            return kw.replace(" ", "-") + (f"/{http_code}" if http_code else "")
    return http_code


def _looks_like_error_text(body: str) -> bool:
    """判断 assistant 正文是否「整体就是一条错误提示」。

    只看短文本（<= 300 字），避免把正常长回答里顺带提到的词误判。
    """
    t = (body or "").strip()
    if not t or len(t) > 300:
        return False
    low = t.lower()
    hits = sum(1 for p in ERROR_TEXT_PATTERNS if (p.lower() in low))
    if hits == 0:
        return False
    # 只命中一个泛化词时，要求文本很短，防止「请求超时」这类正常讨论被误伤
    return hits >= 2 or len(t) <= 120


_SYSTEM_TAGS = (
    "system-reminder",
    "user_info",
    "project_context",
    "project_layout",
    "connector-status",
    "identity_context",
    "memory",
    "craft_mode",
    "additional-data",
)


def _clean_user_text(raw: str) -> str:
    """把用户消息里的系统壳剥掉，只留人写的指令。

    WorkBuddy 会把用户在界面里看不到的一大段上下文塞进 user 消息，
    取标题时必须剔除，否则标题会变成「<system-reminder data-role...」。
    注意：真正的用户输入通常包在 </system-reminder> 之后的
    <user_query>...</user_query> 里，必须先把它捞出来再洗。
    """
    t = (raw or "").strip()
    if not t:
        return ""
    # ① 先捞 <user_query>（真正的用户输入），有就直接用它的内容
    m = re.search(r"<user_query\b[^>]*>(.*?)</user_query\s*>", t, flags=re.S)
    if m:
        t = m.group(1)
    else:
        # 没有 user_query：砍掉 </system-reminder> 之前的所有上下文
        pos = t.rfind("</system-reminder>")
        if pos >= 0:
            t = t[pos + len("</system-reminder>"):]
    # ② 去掉所有 XML 风格的块（循环处理嵌套）
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"<([a-zA-Z_][\w-]*)\b[^>]*>.*?</\1\s*>", " ", t, flags=re.S)
        t = re.sub(r"<[a-zA-Z_][\w-]*\b[^>]*/>", " ", t)
    # ③ 去掉残留的开闭标签
    t = re.sub(r"</?[a-zA-Z_][\w-]*\b[^>]*>", " ", t)
    # ④ 去掉图片/附件占位
    t = re.sub(r"@(?:image|wedrive|file)#\S*", " ", t)
    t = re.sub(r"\[Image[^\]]*\]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if any(t.startswith("<" + tag) for tag in _SYSTEM_TAGS):
        return ""
    # 纯英文的工具/系统指令残留（例如 "Use the TaskOutput tool with the following..."）
    # 标题应反映人话，遇到这类就跳过，让调用方继续找下一条
    if t and not re.search(r"[\u4e00-\u9fff]", t):
        if re.match(r"^(use|call|run|invoke|read|write|please|the|you|note|important)\b",
                    t, flags=re.I):
            return ""
        # 全英文且太短，多半也是系统碎片
        if len(t) < 6:
            return ""
    return t



def pick_session(cfg: Dict[str, Any], explicit: Optional[Path] = None) -> Optional[Session]:
    """挑「最新一个」会话（兼容旧调用）。多会话场景请用 scan_sessions。"""
    if explicit is not None:
        try:
            st = explicit.stat()
        except Exception:
            return None
        return Session(path=explicit, session_id=explicit.stem,
                       cwd=session_cwd(explicit), mtime=st.st_mtime, size=st.st_size)

    sessions = scan_sessions(cfg)
    return sessions[0] if sessions else None


def scan_sessions(cfg: Dict[str, Any]) -> List[Session]:
    """扫描所有「可能还活着」的会话，按最近活动倒序。

    关键点：WorkBuddy 可以同时开多个任务，每个任务一个 jsonl。
    只盯最新那个会漏掉其它任务的中断 —— 这正是「有任务停了却没监控到」的根因。
    """
    sessions = list_sessions()
    horizon = float(cfg.get("session_freshness_hours", 8)) * 3600
    flt = str(cfg.get("project_filter") or "").strip().lower()
    cap = int(cfg.get("max_sessions", 12))

    out: List[Session] = []
    for s in sessions:
        if time.time() - s.mtime > horizon:
            continue
        cwd = session_cwd(s.path)
        if flt and flt not in cwd.lower():
            continue
        s.cwd = cwd
        out.append(s)
        if len(out) >= cap:
            break
    return out


def diagnose(session: Optional[Session], cfg: Dict[str, Any]) -> Diagnosis:
    if session is None:
        return Diagnosis(STATE_NO_SESSION, "没有找到活跃会话")

    recs = read_tail_records(session.path)
    if not recs:
        return Diagnosis(STATE_NO_SESSION, "会话文件为空或无法解析", session=session)

    meaningful = [r for r in recs if r.get("type") not in IGNORED_EVENT_TYPES]
    if not meaningful:
        return Diagnosis(STATE_NO_SESSION, "会话里还没有有效事件", session=session)

    last = meaningful[-1]
    last_type = str(last.get("type") or "")
    last_ts = float(last.get("timestamp") or 0) / 1000.0
    last_age = max(0.0, time.time() - last_ts) if last_ts else session.age
    stall_timeout = float(cfg.get("stall_timeout", 420))
    first_token_timeout = float(cfg.get("first_token_timeout", 180))

    diag = Diagnosis(state=STATE_WORKING, session=session,
                     last_event_type=last_type, last_event_age=last_age,
                     records=len(recs))

    # 情况 A：最后一条是 assistant 消息 —— 直接看它的 status
    if last_type == "message" and last.get("role") == "assistant":
        status = str(last.get("status") or "")
        prov = last.get("providerData") or {}
        err = prov.get("error") if isinstance(prov, dict) else None
        diag.record_id = str(last.get("id") or "")
        body = _text_of(last)
        if status == "incomplete":
            # error.code 有时是整数 0（占位），真实错误码在 logStatus/status 或正文里
            code = ""
            msg = ""
            if isinstance(err, dict):
                kc = err.get("code")
                if kc not in (None, "", 0, "0"):
                    code = str(kc)
                elif err.get("logStatus") not in (None, "", 0, "0"):
                    code = str(err.get("logStatus"))
                elif err.get("status") not in (None, "", 0, "0"):
                    code = str(err.get("status"))
                msg = str(err.get("message") or "")
            if not msg:
                msg = body
            if not code:
                code = _guess_error_code(msg)
            # 用户在界面上点了「停止」：有意为之，不是模型故障，不自动续跑
            if "interrupted by user" in (msg or "").lower():
                diag.state = STATE_IDLE
                diag.detail = "用户主动中断，不自动续跑"
                return diag
            diag.state = STATE_INTERRUPTED
            diag.error_code = code or "incomplete"
            diag.error_message = msg
            diag.detail = f"上游中断 [{diag.error_code}] {msg[:120]}"
            return diag
        # status 显示正常，但正文是纯错误提示 —— 界面同样会卡住，也必须续跑
        if _looks_like_error_text(body):
            diag.state = STATE_INTERRUPTED
            diag.error_code = _guess_error_code(body) or "error-text"
            diag.error_message = body
            diag.detail = f"消息内容是错误提示 [{diag.error_code}] {body[:110]}"
            return diag
        diag.state = STATE_IDLE
        diag.detail = "上一轮已正常结束"
        return diag

    # 情况 B：最后一条是工具调用/思考/用户消息 —— 可能是还在跑，也可能卡死
    if last_type == "function_call":
        diag.detail = f"等待工具 {last.get('name') or ''} 返回"
        limit = float(cfg.get("tool_stall_timeout", 900))
    elif last_type == "reasoning":
        diag.detail = "模型思考中"
        limit = stall_timeout
    elif last_type == "function_call_result":
        diag.detail = "工具已返回，等待模型继续"
        limit = stall_timeout
    elif last_type == "message" and last.get("role") == "user":
        diag.detail = "已发出指令，等待模型首个响应"
        limit = first_token_timeout
        diag.record_id = str(last.get("id") or "")
    else:
        diag.state = STATE_IDLE
        diag.detail = f"空闲（最后事件 {last_type}）"
        return diag

    if last_age > limit:
        diag.state = STATE_STALLED
        diag.error_code = "stall"
        diag.detail = f"超过 {int(limit)} 秒无新记录（{diag.detail}）"
        return diag

    diag.state = STATE_WORKING
    return diag


# --------------------------------------------------------------------------
# 状态持久化
# --------------------------------------------------------------------------


class StateStore:
    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("handled", {})
                d.setdefault("history", [])
                d.setdefault("stats", {"recovered": 0, "failed": 0, "checks": 0})
                return d
        except Exception:
            pass
        return {"handled": {}, "history": [], "stats": {"recovered": 0, "failed": 0, "checks": 0}}

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            log(f"状态保存失败: {e}", "WARN")

    def entry(self, key: str) -> dict:
        return self.data["handled"].setdefault(key, {"attempts": 0, "next_ts": 0, "sent": []})

    def note_send(self, key: str, ok: bool, detail: str = "") -> None:
        with self.lock:
            e = self.entry(key)
            e["attempts"] = int(e.get("attempts", 0)) + 1
            base = float(self.data.get("backoff_base", 20))
            cap = float(self.data.get("backoff_max", 300))
            n = e["attempts"]
            delay = min(cap, base * (2 ** (n - 1)))
            e["next_ts"] = time.time() + delay
            e["last_ts"] = time.time()
            e["last_detail"] = detail
            self.data["history"].append({
                "ts": time.time(), "key": key, "ok": ok, "detail": detail,
            })
            self.data["history"] = self.data["history"][-200:]
            if ok:
                self.data["stats"]["recovered"] = int(self.data["stats"].get("recovered", 0)) + 1
            else:
                self.data["stats"]["failed"] = int(self.data["stats"].get("failed", 0)) + 1
            self.save()

    def note_blocked(self, key: str, detail: str = "") -> None:
        """记录一次「环境阻塞」（如锁屏）：只记账，不消耗 attempts。

        note_send 每次都会 attempts+1。锁屏这类环境问题不是续跑失败，
        若照常累计，锁屏几分钟就把 attempts 耗到 max_attempts，
        桌面真正解锁后反而被「已达最大重试次数」挡住，永远发不出去。
        """
        with self.lock:
            e = self.entry(key)
            e["last_ts"] = time.time()
            e["last_detail"] = detail
            e["next_ts"] = time.time() + 10.0
            self.data["history"].append({
                "ts": time.time(), "key": key, "ok": False, "detail": detail,
            })
            self.data["history"] = self.data["history"][-200:]
            self.save()

    def mark_success(self, key: str) -> None:
        with self.lock:
            self.data["handled"].pop(key, None)
            self.data["consecutive_failures"] = 0
            self.save()

    def bump_failure(self) -> int:
        with self.lock:
            n = int(self.data.get("consecutive_failures", 0)) + 1
            self.data["consecutive_failures"] = n
            self.save()
            return n


# --------------------------------------------------------------------------
# 守护核心
# --------------------------------------------------------------------------


class Guard:
    def __init__(self, cfg: Dict[str, Any], session_file: Optional[Path] = None,
                 dry_run: Optional[bool] = None, on_event=None):
        self.cfg = cfg
        self.session_file = session_file
        self.dry_run = cfg.get("dry_run", False) if dry_run is None else dry_run
        self.state = StateStore()
        self.state.data["backoff_base"] = cfg.get("backoff_base", 20)
        self.state.data["backoff_max"] = cfg.get("backoff_max", 300)
        self.on_event = on_event or (lambda *_: None)
        self.paused_until = 0.0
        self.stop_flag = threading.Event()
        self.last_diag: Optional[Diagnosis] = None
        self.last_diags: List[Diagnosis] = []
        # 刚救完的那个会话 ID：切回时避免又切回去造成来回横跳
        self._last_recovered_id = ""

    # ---- 单个轮次 -------------------------------------------------------
    def tick(self) -> Diagnosis:
        """扫描全部活跃会话，只处理「确实需要续跑」的那一个。"""
        cfg = self.cfg
        self.state.data["stats"]["checks"] = int(self.state.data["stats"].get("checks", 0)) + 1

        if not app_running(str(cfg.get("target_exe") or EXE_NAME)):
            if cfg.get("auto_restart_app"):
                self._restart_app()
                d = Diagnosis(STATE_APP_DOWN, "WorkBuddy 未运行，已尝试拉起")
            else:
                d = Diagnosis(STATE_APP_DOWN, "WorkBuddy 未运行")
            self.last_diag = d
            self.on_event("diag", d)
            return d

        diags = self.scan_all()
        self.last_diags = diags
        if not diags:
            d = Diagnosis(STATE_NO_SESSION, "没有找到活跃会话")
            self.last_diag = d
            self.on_event("diag", d)
            return d

        # 汇总：展示用「最紧急」的那个；动作只挑一个待处理的
        urgent = self._pick_actionable(diags)
        d_display = urgent or diags[0]
        self.last_diag = d_display
        self.on_event("diag", d_display)

        if urgent is None:
            self._clear_stale(diags)
            return d_display

        return self._handle_one(urgent)

    def scan_all(self) -> List[Diagnosis]:
        """对每个活跃会话做一次判定。"""
        cfg = self.cfg
        sessions = ([pick_session(cfg, self.session_file)] if self.session_file
                    else scan_sessions(cfg))
        out: List[Diagnosis] = []
        for s in sessions:
            if s is None:
                continue
            try:
                out.append(diagnose(s, cfg))
            except Exception:
                log(f"诊断会话 {s.session_id[:8]} 失败：{traceback.format_exc()}", "ERROR")
        return out

    def _pick_actionable(self, diags: List[Diagnosis]) -> Optional[Diagnosis]:
        """挑选一个「该续跑且未被限流」的会话：中断优先于卡死，同级别取最新的。"""
        cands = [d for d in diags if d.needs_action and d.session]
        if not cands:
            return None
        cands.sort(key=lambda d: (0 if d.state == STATE_INTERRUPTED else 1,
                                  -float(getattr(d, "last_event_age", 0) * -1)))
        for d in cands:
            if self._blocked_reason(d) is None:
                return d
        return None

    def _blocked_reason(self, d: Diagnosis) -> Optional[str]:
        """这个会话现在能不能发？不能就返回原因（用于展示）。"""
        cfg = self.cfg
        key = f"{d.session.session_id}:{d.record_id or d.error_code or 'unknown'}"
        entry = self.state.entry(key)
        if time.time() < self.paused_until:
            return "连续失败，自动续跑暂停中"
        if int(entry.get("attempts", 0)) >= int(cfg.get("max_attempts", 6)):
            return f"已达最大重试次数({entry['attempts']})，请人工介入"
        if time.time() < float(entry.get("next_ts", 0)):
            wait = int(float(entry["next_ts"]) - time.time())
            return f"退避中，{wait}s 后再试"
        idle = idle_seconds()
        need_idle = float(cfg.get("user_idle_seconds", 15))
        if idle < need_idle:
            return f"你正在操作（空闲 {idle:.0f}s < {need_idle:.0f}s），暂缓"
        return None

    def _clear_stale(self, diags: List[Diagnosis]) -> None:
        """会话恢复正常后，清掉它的去重记录，便于下次真正中断时能再发。"""
        changed = False
        for d in diags:
            if d.state == STATE_IDLE and d.session:
                prefix = f"{d.session.session_id}:"
                stale = [k for k in list(self.state.data["handled"]) if k.startswith(prefix)]
                for k in stale:
                    self.state.data["handled"].pop(k, None)
                    changed = True
        if changed:
            self.state.save()

    def _handle_one(self, d: Diagnosis) -> Diagnosis:
        """对单个中断会话执行续跑。"""
        cfg = self.cfg
        key = f"{d.session.session_id}:{d.record_id or d.error_code or 'unknown'}"
        entry = self.state.entry(key)
        reason = self._blocked_reason(d)
        if reason:
            d.detail += f"｜{reason}"
            return d

        if self.dry_run:
            log(f"[DRY-RUN] 判定中断，本应切到该任务并发送「{cfg.get('continue_prompt')}」："
                f"{d.session.session_id[:8]}｜{d.detail}", "WARN")
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["next_ts"] = time.time() + float(cfg.get("backoff_base", 20))
            self.state.save()
            return d

        self._recover(d, key, entry)
        return d

    def force_recover(self, sid_prefix: str, prompt: str = "") -> bool:
        """GUI 手动触发：对指定会话立即续跑。

        和 tick() 里的自动处理有三点不同：
        1. 由人点按钮触发，忽略 backoff / pause 的冷却限制；
        2. 会话不一定是当前「最新」的那个，所以按 sessionId 前缀在扫描结果里找；
        3. 明确告诉调用方到底做了什么，好显示到界面上。

        返回 True 表示已走到「发送」那一步（不保证模型一定会恢复）。
        """
        want = (sid_prefix or "").strip().lower()
        if not want:
            log("未指定会话 ID，无法续跑", "WARN")
            return False

        target: Optional[Session] = None
        for s in scan_sessions(self.cfg):
            if s.session_id.lower().startswith(want):
                target = s
                break
        if target is None:
            log(f"找不到会话 {sid_prefix}（可能已超过 session_freshness_hours 不再活跃）", "WARN")
            return False

        d = diagnose(target, self.cfg)
        title = self._session_title(target) or target.session_id[:8]
        if not d.needs_action:
            log(f"「{title}」当前无需续跑（{d.state}：{d.detail}）", "INFO")
            return False

        key = f"{d.session.session_id}:{d.record_id or d.error_code or 'unknown'}"
        entry = self.state.entry(key)
        if prompt:
            self.cfg = dict(self.cfg)
            self.cfg["continue_prompt"] = prompt
        if self.dry_run:
            log(f"[DRY-RUN] 手动续跑：本应切到「{title}」并发送「{self.cfg.get('continue_prompt')}」", "WARN")
            return True
        log(f"手动续跑「{title}」｜{d.detail}", "WARN")
        self._recover(d, key, entry)
        return True

    # ---- 切换会话（UIA 精确点击任务列表）--------------------------------
    def switch_session(self, session: Session) -> bool:
        """把界面切到指定会话。

        关键前提：**必须先置前**。WorkBuddy 在后台时，Electron 会给标题等控件报空矩形，
        标题读出来是空的，于是"是否已经在这个会话"判断失效、点击后的校验也失效
        —— 这会把它误判成切换失败（线上 9 次 switch-failed 就是这一条）。

        策略：
        1. 置前 → 读标题 → 已经是它就直接成功（不做任何点击，避免"点了没变化=失败"的假失败）；
        2. 按候选标题（aiTitle → 首句 → 目录名）在左栏找并点击，点完**再用标题文字确认**；
        3. 全都不成返回 False —— 宁可不动，也不能把「继续」发到别的任务里。
        """
        cands = self._session_title_candidates(session)
        if not cands:
            log("拿不到该会话的标题，无法安全切换，跳过", "WARN")
            return False
        want = cands[0]
        t0 = time.time()

        exe = str(self.cfg.get("target_exe") or EXE_NAME)
        hwnd = find_main_window(exe)
        if hwnd and user32.GetForegroundWindow() != hwnd:
            activate_window(hwnd)
            time.sleep(0.5)

        cur = uia_header_text()
        if cur and any(_title_similar(cur, c) for c in cands):
            log(f"界面已在目标任务（{cur[:24]}），无需切换")
            return True
        if not cur:
            log("读不到当前会话标题（可能窗口不在前台），将直接按键匹配查找", "WARN")

        budget = float(self.cfg.get("switch_timeout_seconds", 40))
        for i, c in enumerate(cands):
            left = budget - (time.time() - t0)
            if left < 6:
                log(f"切换已超时（{budget:.0f}s），放弃剩余候选", "WARN")
                break
            try:
                ok = uia_find_and_click(c, session=session,
                                        max_expand=int(self.cfg.get("max_expand_rounds", 3)),
                                        budget=min(left, budget))
            except Exception as e:
                log(f"UIA 查找/点击异常（{c[:20]}）：{e}", "WARN")
                ok = False
            if ok:
                how = "标题" if i == 0 else ("首句" if i == 1 else "目录名")
                log(f"已切到任务「{c}」（按{how}匹配）")
                return True
            log(f"候选标题未命中：「{c[:22]}」", "INFO")

        log(f"切换失败（目标「{want[:24]}」）。请确认该任务在左栏可见："
            f"若属于某个项目，先在「空间」里展开该项目一次", "ERROR")
        return False

    def _window_title_now(self) -> str:
        hwnd = find_main_window(str(self.cfg.get("target_exe") or EXE_NAME))
        return window_title(hwnd) if hwnd else ""

    def _session_title(self, session: Session) -> str:
        """取会话标题，用于在左栏里找并点击它。

        必须用 ai-title 的 aiTitle —— 那正是 WorkBuddy 左栏显示的摘要文字。
        早期版本误取了用户首句，和左栏文字毫无共同前缀，导致永远匹配不上。
        匹配失败的兜底逻辑见 switch_session：会再用「其他候选标题」多试几次。
        """
        return _session_display_title(session)

    def _session_title_candidates(self, session: Session) -> List[str]:
        """返回该会话所有可用作标题的候选（aiTitle / 首句 / 目录名），去重保序。"""
        head = _read_head_records(session.path)
        out: List[str] = []
        ai_title = ""
        first_user = ""
        for rec in head:
            if not ai_title and rec.get("type") == "ai-title":
                ai_title = str(rec.get("aiTitle") or rec.get("title") or "").strip()
            if first_user or rec.get("type") != "message" or rec.get("role") != "user":
                continue
            t = _clean_user_text(_text_of(rec))
            if t:
                first_user = t
        cwd_name = os.path.basename((session.cwd or "").rstrip("\\/"))
        for c in (ai_title, first_user, cwd_name):
            if c and c not in out:
                out.append(c)
        return out


    def _switch_by_deeplink(self, sid: str) -> bool:
        """深链兜底：workbuddy://chat/<id>，以对话标题变化为准判断成败。"""
        before = uia_current_conversation()
        try:
            shell32 = ctypes.WinDLL("shell32")
            ret = shell32.ShellExecuteW(None, "open", f"workbuddy://chat/{sid}",
                                        None, None, 1)
            if ret <= 32:
                log(f"深链调用失败（ShellExecute 返回 {ret}）", "WARN")
                return False
        except Exception as e:
            log(f"深链调用异常：{e}", "WARN")
            return False
        time.sleep(1.5)
        after = uia_current_conversation()
        return bool(after and after != before)

    # ---- 续跑动作 -------------------------------------------------------
    def _composer_point(self, hwnd: int) -> tuple:
        """按比例估算输入框位置：底部往上 12% 高度处，横向 60% 宽度。"""
        cfg = self.cfg
        rc = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        h = max(1, rc.bottom - rc.top)
        w = max(1, rc.right - rc.left)
        offset = int(h * float(cfg.get("composer_offset_ratio", 0.12)))
        offset = max(int(cfg.get("composer_offset_min", 100)),
                     min(int(cfg.get("composer_offset_max", 180)), offset))
        x = int(w * float(cfg.get("composer_x_ratio", 0.60)))
        y = max(10, rc.bottom - offset)
        return x, y

    def _recover(self, d: Diagnosis, key: str, entry: dict) -> bool:
        cfg = self.cfg
        prompt = str(cfg.get("continue_prompt") or "继续")
        attempt = int(entry.get("attempts", 0)) + 1
        exe = str(cfg.get("target_exe") or EXE_NAME)
        log(f"检测到中断（第 {attempt} 次尝试续跑）｜会话 {d.session.session_id[:8]}｜{d.detail}", "WARN")

        # 锁屏时点击/粘贴/切窗口全都无效，先查出来，别等激活失败再猜
        if workstation_locked():
            log("工作站已锁屏：模拟键鼠、UIA 读取、剪贴板全部不可用，本轮不发送。"
                "这不是续跑失败，是环境阻塞 —— 解锁后会自动重试", "ERROR")
            # 走 note_blocked：只记账，不消耗 attempts。
            # 否则锁屏一段时间（约3分钟）就把 attempts 耗到 max_attempts，
            # 真正解锁后反而被「已达最大重试次数」挡住，再也发不出去。
            self.state.note_blocked(key, "workstation-locked")
            return False

        hwnd = find_main_window(exe)
        if not hwnd:
            log("找不到目标窗口，跳过本次", "ERROR")
            self.state.note_send(key, False, "no-window")
            return False

        if not activate_window(hwnd):
            log("窗口无法置前（可能被系统限制），跳过本次", "ERROR")
            self.state.note_send(key, False, "activate-failed")
            return False

        # ① 先切到出问题的那个任务（多任务场景的核心）
        back_to: Optional[Session] = None
        if cfg.get("switch_session", True):
            # 记住切换前在哪个任务，发完要切回去 —— 否则用户会被拽到别的对话里
            back_to = (self._home_session(exclude=d.session)
                       if cfg.get("switch_back_after_recover", True) else None)
            if not self.switch_session(d.session):
                log("未能切到目标任务，本次不发送（避免发错会话）", "ERROR")
                self.state.note_send(key, False, "switch-failed")
                entry["attempts"] = attempt
                entry["next_ts"] = time.time() + float(cfg.get("backoff_base", 20))
                self.state.save()
                return False
            hwnd = find_main_window(exe)
            if not hwnd or not activate_window(hwnd):
                log("切换后再置前失败，跳过本次", "ERROR")
                self.state.note_send(key, False, "activate-failed-2")
                return False

        before = self._record_count(d.session)
        # 策略轮换：前两次「点击 + 粘贴」，之后退化为「直接粘贴」，避免一直卡在同一动作上
        do_click = bool(cfg.get("click_composer", True)) and attempt <= 2
        if do_click:
            x, y = self._composer_point(hwnd)
            click_client(hwnd, x, y)
            if user32.GetForegroundWindow() != hwnd:
                log("点击后焦点丢失，放弃本次", "ERROR")
                self.state.note_send(key, False, "focus-lost")
                return False
            log(f"已点击输入框 ({x},{y})")
        else:
            log("本轮不点击，直接粘贴（策略轮换）")

        old_clip = get_clipboard_text()
        if not set_clipboard_text(prompt):
            log("写剪贴板失败", "ERROR")
            self.state.note_send(key, False, "clipboard")
            return False
        try:
            press_ctrl_v()
            time.sleep(0.35)
            press_enter()
            log(f"已发送「{prompt}」到 WorkBuddy")
            self._last_recovered_id = d.session.session_id
        finally:
            time.sleep(0.2)
            if old_clip is not None:
                set_clipboard_text(old_clip)

        # 校验
        ok = self._verify(d.session, before, float(cfg.get("verify_seconds", 25)))
        if ok:
            log("续跑成功：会话已产生新记录", "OK")
            self.state.mark_success(key)
            if cfg.get("beep"):
                try:
                    import winsound
                    winsound.MessageBeep(0x40)
                except Exception:
                    pass
            self.state.note_send(key, True, "verified")
            self._switch_back(back_to)
            return True

        log("续跑未生效（没有新记录）", "WARN")
        self.state.note_send(key, False, "verify-failed")
        n = self.state.bump_failure()
        limit = int(cfg.get("max_consecutive_failures", 8))
        if n >= limit:
            self.paused_until = time.time() + float(cfg.get("pause_minutes", 15)) * 60
            log(f"连续 {n} 次续跑无效，疑似模型整体不可用，暂停 {cfg.get('pause_minutes')} 分钟", "ERROR")
            self.state.data["consecutive_failures"] = 0
            self.state.save()
        self._switch_back(back_to)
        return False

    def _home_session(self, exclude: Optional[Session] = None) -> Optional[Session]:
        """猜用户原本在哪个任务：最近写入的那个会话（排除掉正在救的）。"""
        try:
            for s in scan_sessions(self.cfg):
                if exclude and s.session_id == exclude.session_id:
                    continue
                return s
        except Exception:
            pass
        return None

    def _switch_back(self, target: Optional[Session]) -> None:
        """救完别的任务后把界面切回用户原来那个对话。

        失败只警告，不回滚已完成的续跑 —— 消息已经发出去了，切换回去只是体验优化。
        """
        if not target:
            return
        if target.session_id == self._last_recovered_id:
            return
        try:
            if self.switch_session(target):
                log(f"已切回原任务「{self._session_title(target)}」")
            else:
                log("切回原任务失败（不影响本次续跑结果）", "WARN")
        except Exception as e:
            log(f"切回原任务异常（不影响续跑结果）：{e}", "WARN")

    def _record_count(self, session: Optional[Session]) -> int:
        if not session:
            return 0
        try:
            with open(session.path, "rb") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _verify(self, session: Optional[Session], before: int, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            time.sleep(2.0)
            if not session:
                return False
            now = self._record_count(session)
            if now > before:
                return True
        return False

    def _restart_app(self) -> None:
        cmd = str(self.cfg.get("restart_command") or "").strip()
        if not cmd:
            default = HOME / "AppData" / "Local" / "Programs" / "WorkBuddy" / "WorkBuddy.exe"
            if not default.exists():
                log("未配置 restart_command，且找不到默认安装路径", "ERROR")
                return
            cmd = f'"{default}"'
        try:
            subprocess.Popen(cmd, shell=True)
            log(f"已尝试启动 WorkBuddy: {cmd}")
        except Exception as e:
            log(f"启动 WorkBuddy 失败: {e}", "ERROR")

    # ---- 常驻循环 -------------------------------------------------------
    def watch(self) -> None:
        log("=" * 62)
        log(f"WorkBuddy 守护启动｜dry_run={self.dry_run}｜轮询 {self.cfg.get('poll_interval')}s")
        log(f"会话目录：{PROJECTS_DIR}")

        # 先挡住睡眠/熄屏，再开始轮询 —— 一旦锁屏就什么也发不出去了
        if self.cfg.get("keep_awake", True):
            if keep_awake(True, with_display=bool(self.cfg.get("keep_display_on", True))):
                mode = "系统+显示器" if self.cfg.get("keep_display_on", True) else "仅系统"
                log(f"已阻止空闲休眠（{mode}），避免锁屏导致发送失效")
            else:
                log("阻止空闲休眠失败（不影响监控，但空闲后可能锁屏）", "WARN")

        log("=" * 62)
        last_state = ""
        last_lock_state: Optional[bool] = None

        while not self.stop_flag.is_set():
            try:
                # 每轮捎带一次锁屏检测：状态变化时记一条，避免刷屏
                locked = workstation_locked()
                if locked != last_lock_state:
                    if locked:
                        log("工作站进入锁屏：检测继续，但无法发送输入（解锁后自动恢复）", "WARN")
                    else:
                        log("工作站已解锁：恢复正常发送能力", "OK")
                    last_lock_state = locked
                    # SetThreadExecutionState 绑定线程，某些情况下会被系统重置，顺手续一下
                    if self.cfg.get("keep_awake", True):
                        keep_awake(True, with_display=bool(self.cfg.get("keep_display_on", True)))

                d = self.tick()
                line = self.format_status(d)
                if d.state != last_state:
                    log(line)
                    last_state = d.state
                elif d.needs_action:
                    log(line, "WARN")
            except Exception:
                log("轮询异常：\n" + traceback.format_exc(), "ERROR")
            self.stop_flag.wait(float(self.cfg.get("poll_interval", 5)))
        log("守护已停止")

    def format_status(self, d: Diagnosis) -> str:
        s = d.session
        head = f"状态={d.state}"
        if s:
            head += f"｜会话={s.session_id[:8]}｜cwd={s.cwd or '?'}"
        head += f"｜{d.detail}"
        return head


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            log(f"配置读取失败，使用默认值: {e}", "WARN")
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
            log(f"已生成默认配置：{path}")
        except Exception:
            pass
    return cfg


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------


def cmd_status(cfg: Dict[str, Any], session_file: Optional[Path]) -> int:
    print()
    print("WorkBuddy 断流守护 · 当前状态")
    print("-" * 66)
    running = app_running(str(cfg.get("target_exe") or EXE_NAME))
    hwnd = find_main_window(str(cfg.get("target_exe") or EXE_NAME)) if running else None
    print(f"  进程         : {'运行中' if running else '未运行'}"
          + (f"（{len(pids_of(str(cfg.get('target_exe') or EXE_NAME)))} 个进程）" if running else ""))
    print(f"  主窗口       : {hwnd if hwnd else '未找到'}"
          + (f"  «{window_title(hwnd)}»" if hwnd else ""))
    print(f"  空闲时长     : {idle_seconds():.0f} 秒")
    locked = workstation_locked()
    if locked:
        why = []
        if _desktop_locked_by_window():
            why.append("前台是锁屏界面")
        if _desktop_locked_by_process():
            why.append("LogonUI.exe 存在")
        if _desktop_locked_by_name():
            why.append("输入桌面非 Default")
        print(f"  桌面状态     : 已锁屏（{'、'.join(why) or '未知原因'}）")
        print(f"  可发送       : 否 —— 锁屏下模拟键鼠 / UIA / 剪贴板全部不可用")
        print(f"  提示         : 检测照常进行，中断会被记录；解锁后自动重试，无需重开")
    else:
        print(f"  桌面状态     : 可交互")

    # 多任务：逐个检查所有活跃会话，而不是只看最新的那个
    if session_file:
        sess = Session(path=session_file, session_id=session_file.stem,
                       mtime=session_file.stat().st_mtime, size=session_file.stat().st_size,
                       cwd=session_cwd(session_file))
        sessions = [sess]
    else:
        sessions = scan_sessions(cfg)

    if not sessions:
        print("  活跃会话     : 无（最近 8 小时没有活动会话）")
        print("-" * 66)
        return 1

    diags: List[Diagnosis] = []
    for s in sessions:
        try:
            diags.append(diagnose(s, cfg))
        except Exception as e:
            log(f"诊断失败 {s.session_id[:8]}：{e}", "WARN")

    if not diags:
        print("  活跃会话     : 全部诊断失败，见日志")
        print("-" * 66)
        return 1

    need = [d for d in diags if d.needs_action]
    working = [d for d in diags if d.state == STATE_WORKING]

    print(f"  活跃任务     : {len(diags)} 个"
          f"（需处理 {len(need)} / 运行中 {len(working)} / 空闲 {len(diags) - len(need) - len(working)}）")
    print("-" * 66)

    # 表头
    print("  状态     任务                             距上次事件   说明")
    print("  " + "-" * 64)
    order = {STATE_INTERRUPTED: 0, STATE_STALLED: 1, STATE_WORKING: 2, STATE_IDLE: 3}
    diags.sort(key=lambda d: (order.get(d.state, 9), -d.session.mtime))
    for d in diags:
        label = {STATE_INTERRUPTED: "中断", STATE_STALLED: "卡死",
                 STATE_WORKING: "运行中"}.get(d.state, "空闲")
        title = _session_display_title(d.session)[:30]
        age = f"{d.last_event_age:.0f}s"
        detail = (d.detail or "").replace("\n", " ")[:34]
        print(f"  {label:<6} {title:<30} {age:>9}   {detail}")

    print("-" * 66)
    if need:
        for d in need[:3]:
            if d.error_code:
                print(f"  · {_session_display_title(d.session)[:26]}  错误码 {d.error_code}")
    if need:
        print("  >>> 有任务需要续跑。执行 `python wb_guard.py watch` 自动切换并续跑，")
        print("      或 `python wb_guard.py watch --once` 立刻处理一次。")
    elif working:
        print("  >>> 有任务正在运行，无需动作。")
    else:
        print("  >>> 全部空闲，无需动作。")
    print()
    return 0


def _read_head_records(path: Path, max_records: int = 260) -> List[dict]:
    """从头读少量记录。首条 user 消息和 ai-title 都在文件开头，读尾部是读不到的。"""
    recs: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    recs.append(obj)
                if len(recs) >= max_records:
                    break
    except Exception:
        pass
    return recs


def _session_display_title(session: Session) -> str:
    """给会话取一个人类可读的短标题。

    优先级：ai-title 的 aiTitle（= 左栏实际显示的文字，切换会话必须用它匹配）
            → 首条干净的用户消息 → 目录名。
    """
    head = _read_head_records(session.path)
    ai_title = ""
    first_user = ""
    for rec in head:
        if not ai_title and rec.get("type") == "ai-title":
            ai_title = str(rec.get("aiTitle") or rec.get("title") or "").strip()
        if first_user or rec.get("type") != "message" or rec.get("role") != "user":
            continue
        t = _clean_user_text(_text_of(rec))
        if t:
            first_user = t
    cwd_name = os.path.basename((session.cwd or "").rstrip("\\/"))
    return (ai_title or first_user or cwd_name or session.session_id[:8])[:40]


def cmd_lockcheck(cfg: Dict[str, Any]) -> int:
    """逐条实测「锁屏下哪些输入通道还活着」，给出能/不能的硬结论。

    这个命令存在的原因：锁屏问题反复出现，每次都靠人肉猜太慢。
    把所有候选通道一次跑完，输出可复现的证据。
    只做「读」和「无害探测」，不往 WorkBuddy 里发任何东西。
    """
    exe = str(cfg.get("target_exe") or EXE_NAME)
    print()
    print("锁屏通道实测")
    print("=" * 64)

    locked = workstation_locked()
    print(f"  锁定状态      : {'已锁屏' if locked else '未锁屏'}")
    for name, fn in (("前台是锁屏界面", _desktop_locked_by_window),
                     ("LogonUI.exe 存在", _desktop_locked_by_process),
                     ("输入桌面非 Default", _desktop_locked_by_name)):
        try:
            print(f"    · {name:<18}: {fn()}")
        except Exception as e:
            print(f"    · {name:<18}: 异常 {e}")

    # 前台窗口
    try:
        user32.GetForegroundWindow.restype = wintypes.HWND
        fg = user32.GetForegroundWindow()
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(fg, cls, 256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(fg, title, 256)
        print(f"  前台窗口      : {cls.value} / {title.value!r}")
    except Exception as e:
        print(f"  前台窗口      : 读取失败 {e}")

    hwnd = find_main_window(exe)
    print(f"  目标主窗口    : {hwnd if hwnd else '未找到'}")
    print()

    rows: List[tuple] = []

    # 1 SetForegroundWindow
    if hwnd:
        try:
            ok = bool(user32.SetForegroundWindow(hwnd))
            err = ctypes.get_last_error()
            got = user32.GetForegroundWindow()
            rows.append(("SetForegroundWindow 置前", "可用" if (ok and got == hwnd) else "不可用",
                         f"返回 {ok}，err={err}，前台仍是 {got}"))
        except Exception as e:
            rows.append(("SetForegroundWindow 置前", "不可用", str(e)))
    else:
        rows.append(("SetForegroundWindow 置前", "无法测试", "未找到主窗口"))

    # 2 剪贴板
    try:
        ok = set_clipboard_text("wbguard-lockcheck")
        back = get_clipboard_text()
        rows.append(("剪贴板写入", "可用" if ok and back else "不可用",
                     f"写入={ok} 读回={back!r}"))
    except Exception as e:
        rows.append(("剪贴板写入", "不可用", str(e)))

    # 3 UIA
    try:
        t0 = time.time()
        items = uia_task_items()
        hdr = uia_header_text()
        rows.append(("UIA 读取任务列表", "可用" if items else "不可用",
                     f"{len(items)} 项，标题={hdr!r}，耗时 {time.time()-t0:.1f}s"))
    except Exception as e:
        rows.append(("UIA 读取任务列表", "不可用", str(e)))

    # 4 PostMessage 到目标窗口的子控件（Electron 通常没有）
    if hwnd:
        try:
            n_child = [0]

            def _count(h, lp):
                n_child[0] += 1
                return True

            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumChildWindows.argtypes = [wintypes.HWND, EnumProc, wintypes.LPARAM]
            user32.EnumChildWindows(hwnd, EnumProc(_count), 0)
            rows.append(("PostMessage 注入字符", "可用" if n_child[0] else "不可用",
                         f"主窗口子控件数 {n_child[0]}（Electron 无原生编辑控件可投递）"))
        except Exception as e:
            rows.append(("PostMessage 注入字符", "不可用", str(e)))

    # 5 显示器状态
    try:
        import tempfile
        tmp = LOG_DIR / "_lockcheck_shot.png"
        capture_screen(tmp, None)
        from PIL import Image
        im = Image.open(tmp).convert("L")
        hist = im.histogram()
        mean = sum(i * n for i, n in enumerate(hist)) / max(1, im.width * im.height)
        tmp.unlink(missing_ok=True)
        rows.append(("截屏亮度", "可用" if mean > 30 else "偏暗/黑屏",
                     f"均值 {mean:.1f}（<30 通常表示熄屏或锁屏遮罩）"))
    except Exception as e:
        rows.append(("截屏亮度", "无法测试", str(e)))

    # 6 阻止休眠
    try:
        ok = keep_awake(True, with_display=True)
        rows.append(("阻止空闲休眠", "可用" if ok else "失败",
                     "SetThreadExecutionState 已设置（本命令退出即释放）"))
    except Exception as e:
        rows.append(("阻止空闲休眠", "失败", str(e)))

    print(f"  {'通道':<26}{'结论':<12}证据")
    print("  " + "-" * 62)
    for name, verdict, note in rows:
        print(f"  {name:<26}{verdict:<12}{note}")

    print()
    info = display_settings_hint()
    print("  当前电源设置：")
    sec = info.get("display_off_seconds")
    print(f"    · 显示器超时  : {sec} 秒（{sec//60 if isinstance(sec,int) else '?'} 分钟）")
    print(f"    · 屏保启用    : {info.get('screensaver')}")
    print()
    print("  结论：")
    if locked:
        print("    已锁屏。可用通道为 0 —— 模拟键鼠 / UIA / 剪贴板全部失效，")
        print("    Electron 又没有可被 PostMessage 投递的原生控件，因此此刻无法发送指令。")
        print("    这是 Windows 的安全边界，不是程序 bug。")
        print("    可行的只有两条：① 解锁后自动恢复；② 提前阻止锁屏（见 README「锁屏」一节）。")
    else:
        print("    当前未锁屏，正常通道可用。若担心离开后锁屏，把 config.json 的")
        print("    keep_awake / keep_display_on 保持 true，并关掉屏保。")
    print()
    return 0


def cmd_probe(cfg: Dict[str, Any]) -> int:
    """截图并在图上标出「续跑时会点击的位置」，用于核对输入框定位是否准确。"""
    exe = str(cfg.get("target_exe") or EXE_NAME)
    hwnd = find_main_window(exe)
    if not hwnd:
        print(f"找不到 {exe} 的主窗口")
        return 1

    rc = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    h = max(1, rc.bottom - rc.top)
    w = max(1, rc.right - rc.left)
    offset = int(h * float(cfg.get("composer_offset_ratio", 0.12)))
    offset = max(int(cfg.get("composer_offset_min", 100)),
                 min(int(cfg.get("composer_offset_max", 180)), offset))
    cx = int(w * float(cfg.get("composer_x_ratio", 0.60)))
    cy = max(10, rc.bottom - offset)

    # 客户区坐标 → 窗口坐标（截图是按窗口矩形裁的）
    pt = POINT(cx, cy)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    wr = window_rect(hwnd)
    mark = (pt.x - wr.left, pt.y - wr.top, (220, 30, 30))

    out = LOG_DIR / "probe.png"
    capture_screen(out, (wr.left, wr.top, wr.right - wr.left, wr.bottom - wr.top), marks=[mark])
    print(f"窗口      : {hwnd} «{window_title(hwnd)}» 客户区 {w}x{h}")
    print(f"点击位置  : 客户区 ({cx}, {cy})  ← 距底部 {offset}px，横向 {cx/w:.0%}")
    print(f"标记图    : {out}")
    print("红点应落在输入框内部；若不在，调整 config.json 里的 composer_offset_ratio / composer_x_ratio。")
    return 0


def cmd_watch(cfg: Dict[str, Any], args) -> int:
    guard = Guard(cfg, session_file=args.session_file, dry_run=args.dry_run)
    if args.once:
        d = guard.tick()
        print(guard.format_status(d))
        return 0
    try:
        guard.watch()
    except KeyboardInterrupt:
        guard.stop_flag.set()
        print("\n已退出")
    return 0


def cmd_replay(cfg: Dict[str, Any], path: Path) -> int:
    session = Session(path=path, session_id=path.stem,
                      cwd=session_cwd(path), mtime=path.stat().st_mtime,
                      size=path.stat().st_size)
    d = diagnose(session, cfg)
    print(json.dumps({
        "state": d.state, "detail": d.detail, "last_event_type": d.last_event_type,
        "last_event_age": round(d.last_event_age, 1), "error_code": d.error_code,
        "record_id": d.record_id, "records": d.records,
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(cfg: Dict[str, Any]) -> int:
    """用合成样本验证判定逻辑，不依赖真实会话。"""
    import tempfile

    def write(recs: List[dict]) -> Path:
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        p = Path(name)
        with open(p, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    now_ms = int(time.time() * 1000)
    old_ms = int((time.time() - 3600) * 1000)

    cases = []
    # 1 正常结束
    p1 = write([
        {"type": "message", "role": "user", "id": "u1", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a1", "status": "completed",
         "content": [{"type": "output_text", "text": "done"}], "timestamp": old_ms},
    ])
    cases.append((p1, STATE_IDLE))
    # 2 明确中断
    p2 = write([
        {"type": "message", "role": "user", "id": "u2", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a2", "status": "incomplete",
         "providerData": {"error": {"code": "upstream_http2_stream_error", "message": "503"}},
         "content": [{"type": "output_text", "text": "上游服务繁忙"}], "timestamp": old_ms},
    ])
    cases.append((p2, STATE_INTERRUPTED))
    # 3 工具调用卡死
    p3 = write([
        {"type": "message", "role": "user", "id": "u3", "timestamp": old_ms},
        {"type": "function_call", "name": "Bash", "id": "f3", "timestamp": old_ms},
    ])
    cases.append((p3, STATE_STALLED))
    # 4 工具刚调用，还在跑
    p4 = write([
        {"type": "message", "role": "user", "id": "u4", "timestamp": now_ms},
        {"type": "function_call", "name": "Bash", "id": "f4", "timestamp": now_ms},
    ])
    cases.append((p4, STATE_WORKING))
    # 5 用户消息刚发出，等待首字
    p5 = write([
        {"type": "message", "role": "user", "id": "u5", "timestamp": now_ms},
    ])
    cases.append((p5, STATE_WORKING))
    # 6 用户消息很久没响应
    p6 = write([
        {"type": "message", "role": "user", "id": "u6", "timestamp": old_ms},
    ])
    cases.append((p6, STATE_STALLED))
    # 7 尾部是计费/元数据，实际已结束
    p7 = write([
        {"type": "message", "role": "user", "id": "u7", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a7", "status": "completed",
         "content": [{"type": "output_text", "text": "ok"}], "timestamp": old_ms},
        {"type": "credit-usage", "timestamp": old_ms},
    ])
    cases.append((p7, STATE_IDLE))
    # 8 「服务暂时不可用」但 error.code 是占位 0（本次漏检的真实形态）
    p8 = write([
        {"type": "message", "role": "user", "id": "u8", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a8", "status": "incomplete",
         "providerData": {"skipRun": True,
                          "error": {"code": 0, "status": 503, "isRetryable": False,
                                    "message": "503 当前共享版请求较多、算力不足，请稍后重试。"
                                               "如需优先使用算力，请将 API 密钥的分组切换为"
                                               "「标准版」或「高性能版」"}},
         "content": [{"type": "output_text",
                      "text": "服务暂时不可用，请稍后重试"}], "timestamp": old_ms},
    ])
    cases.append((p8, STATE_INTERRUPTED))
    # 9 模型被网关关闭（404）—— 同样是必须重发才能继续
    p9 = write([
        {"type": "message", "role": "user", "id": "u9", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a9", "status": "incomplete",
         "providerData": {"error": {"code": 0, "status": 404,
                                    "message": "404 模型已在网关中关闭: xxx"}},
         "content": [{"type": "output_text", "text": "404 模型已在网关中关闭"}],
         "timestamp": old_ms},
    ])
    cases.append((p9, STATE_INTERRUPTED))
    # 10 status 正常但正文就是错误提示 —— 界面卡住，也要续跑
    p10 = write([
        {"type": "message", "role": "user", "id": "u10", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a10", "status": "completed",
         "content": [{"type": "output_text", "text": "服务暂时不可用，请稍后重试"}],
         "timestamp": old_ms},
    ])
    cases.append((p10, STATE_INTERRUPTED))
    # 11 正常长回答里顺带提到「请求超时」，不得误判
    p11 = write([
        {"type": "message", "role": "user", "id": "u11", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a11", "status": "completed",
         "content": [{"type": "output_text",
                      "text": "关于重试策略，我建议把请求超时设为 30 秒，并在失败后做指数退避。" * 8}],
         "timestamp": old_ms},
    ])
    cases.append((p11, STATE_IDLE))
    # 12 短回答里恰好含一个泛化词（如「超时」），仍需按正常处理
    p12 = write([
        {"type": "message", "role": "user", "id": "u12", "timestamp": old_ms},
        {"type": "message", "role": "assistant", "id": "a12", "status": "completed",
         "content": [{"type": "output_text", "text": "已修复超时问题。"}],
         "timestamp": old_ms},
    ])
    cases.append((p12, STATE_IDLE))

    ok = True
    print("自检：")
    for i, (path, expect) in enumerate(cases, 1):
        s = Session(path=path, session_id=path.stem, cwd="", mtime=path.stat().st_mtime,
                    size=path.stat().st_size)
        d = diagnose(s, cfg)
        flag = "PASS" if d.state == expect else "FAIL"
        if d.state != expect:
            ok = False
        print(f"  [{flag}] 用例{i}: 期望 {expect:<12} 实际 {d.state:<12} {d.detail[:50]}")
        path.unlink(missing_ok=True)
    print("\n结果：" + ("全部通过" if ok else "存在失败用例"))
    return 0 if ok else 1


def cmd_test_input(cfg: Dict[str, Any], prompt: str) -> int:
    """安全测试输入通道：启动记事本，把文本粘贴进去，不碰 WorkBuddy。"""
    print("即将启动记事本做输入通道测试（不会影响 WorkBuddy）…")
    proc = subprocess.Popen(["notepad.exe"])
    time.sleep(1.6)
    hwnd = None
    for _ in range(20):
        pids = set(pids_of("notepad.exe"))
        found = []

        @WNDENUMPROC
        def _cb(h, _l):
            if user32.IsWindowVisible(h) and window_pid(h) in pids and window_title(h):
                found.append(h)
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            hwnd = found[0]
            break
        time.sleep(0.3)

    if not hwnd:
        print("找不到记事本窗口")
        proc.terminate()
        return 1

    if not activate_window(hwnd):
        print("窗口置前失败")
        proc.terminate()
        return 1

    old = get_clipboard_text()
    if not set_clipboard_text(prompt):
        print("剪贴板写入失败")
        proc.terminate()
        return 1
    press_ctrl_v()
    time.sleep(0.4)
    if old is not None:
        set_clipboard_text(old)
    print(f"已向记事本粘贴：{prompt}")
    print("请肉眼确认记事本里出现了这段文字。3 秒后自动关闭记事本。")
    time.sleep(3)
    try:
        proc.terminate()
    except Exception:
        pass
    return 0


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "WorkBuddyGuard"


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        # 打包成 exe：直接让 exe 自己以 watch 模式自启，不需要 python 解释器。
        return f'"{Path(sys.executable).resolve()}" watch'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    return f'"{runner}" "{Path(__file__).resolve()}" watch'


def _autostart_enabled() -> bool:
    """读注册表判断当前是否已设置开机自启（供面板勾选框初始化）。"""
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False
    except Exception:
        return False


def cmd_autostart(enable: bool) -> int:
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_ALL_ACCESS)
    except Exception:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY)
    try:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, _autostart_command())
            print(f"已设置开机自启：{_autostart_command()}")
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
                print("已取消开机自启")
            except FileNotFoundError:
                print("本来就没有设置开机自启")
    finally:
        winreg.CloseKey(key)
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------


def cmd_gui(cfg: Dict[str, Any]) -> int:
    import tkinter as tk
    from tkinter import ttk

    # 面板本身绝不能静默崩掉：任何未捕获异常都写进日志再弹窗说明。
    try:
        root = tk.Tk()
    except Exception as e:
        log(f"GUI 启动失败（Tk 初始化）：{e}", "ERROR")
        raise
    root.title("WorkBuddy 断流守护")
    root.geometry("760x520")
    root.minsize(640, 440)

    def _gui_exc(exc, val, tb):
        import traceback as _tb
        msg = "".join(_tb.format_exception(exc, val, tb))
        log("GUI 未捕获异常：\n" + msg, "ERROR")
        try:
            tk.messagebox.showerror("WorkBuddy 守护面板出错", msg[-1200:])
        except Exception:
            pass

    root.report_callback_exception = _gui_exc

    state_var = tk.StringVar(value="未启动")
    session_var = tk.StringVar(value="-")
    detail_var = tk.StringVar(value="-")
    stat_var = tk.StringVar(value="检查 0 次｜续跑成功 0 次｜失败 0 次")
    dry_var = tk.BooleanVar(value=bool(cfg.get("dry_run")))
    prompt_var = tk.StringVar(value=str(cfg.get("continue_prompt", "继续")))
    stall_var = tk.StringVar(value=str(cfg.get("stall_timeout", 420)))

    guard_holder: Dict[str, Optional[Guard]] = {"g": None}
    worker: Dict[str, Optional[threading.Thread]] = {"t": None}
    q: "queue.Queue[str]" = queue.Queue()

    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    ttk.Label(top, text="状态：", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
    state_label = ttk.Label(top, textvariable=state_var, font=("Microsoft YaHei UI", 12, "bold"))
    state_label.grid(row=0, column=1, sticky="w", padx=(0, 16))
    ttk.Label(top, textvariable=stat_var).grid(row=0, column=2, sticky="e")
    top.columnconfigure(2, weight=1)

    ttk.Label(top, text="会话：").grid(row=1, column=0, sticky="nw", pady=(6, 0))
    ttk.Label(top, textvariable=session_var, wraplength=600).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Label(top, text="说明：").grid(row=2, column=0, sticky="nw", pady=(4, 0))
    ttk.Label(top, textvariable=detail_var, wraplength=600).grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 0))

    # 环境阻塞提示：锁屏时检测照常，但发不出去。不写清楚用户只会看到「监控中」发呆。
    env_var = tk.StringVar(value="")
    env_label = ttk.Label(top, textvariable=env_var, wraplength=680,
                          font=("Microsoft YaHei UI", 9, "bold"))
    env_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def _refresh_env():
        try:
            if workstation_locked():
                env_var.set("⚠ 桌面已锁屏：检测正常，但锁屏下无法发送输入（模拟键鼠 / UIA / 剪贴板均不可用）。"
                            "解锁后自动重试，无需重启面板。")
                env_label.configure(foreground="#c0392b")
            else:
                env_var.set("")
        except Exception:
            pass
        root.after(3000, _refresh_env)

    opts = ttk.Frame(root, padding=(10, 0))
    opts.pack(fill="x")
    ttk.Label(opts, text="续跑内容").pack(side="left")
    ttk.Entry(opts, textvariable=prompt_var, width=14).pack(side="left", padx=(6, 14))
    ttk.Label(opts, text="卡死阈值(秒)").pack(side="left")
    ttk.Entry(opts, textvariable=stall_var, width=7).pack(side="left", padx=(6, 14))
    ttk.Checkbutton(opts, text="只观察不发送", variable=dry_var).pack(side="left")

    # 开机自启开关：直接读写 HKCU Run，等价于原来的「开机自启-安装/取消.bat」
    autostart_var = tk.BooleanVar(value=_autostart_enabled())

    def _toggle_autostart():
        """勾/取消勾选即写入或删除注册表项，失败则回滚界面状态。"""
        want = bool(autostart_var.get())
        try:
            cmd_autostart(want)
            push("已设为开机自启" if want else "已取消开机自启")
        except Exception as e:
            autostart_var.set(not want)
            push(f"开机自启设置失败：{e}")

    ttk.Checkbutton(
        opts, text="开机自启", variable=autostart_var, command=_toggle_autostart
    ).pack(side="left", padx=(18, 0))

    btns = ttk.Frame(root, padding=10)
    btns.pack(fill="x")
    start_btn = ttk.Button(btns, text="开始监控")
    start_btn.pack(side="left")
    once_btn = ttk.Button(btns, text="立即检查")
    once_btn.pack(side="left", padx=8)
    stop_btn = ttk.Button(btns, text="停止", state="disabled")
    stop_btn.pack(side="left")
    ttk.Button(btns, text="打开日志", command=lambda: _open(LOG_PATH)).pack(side="right")

    # ---- 任务列表：一眼看清每个任务的状态（多任务并行时最关键）----
    listbox_frame = ttk.Frame(root, padding=(10, 0, 10, 6))
    listbox_frame.pack(fill="both", expand=False)
    ttk.Label(listbox_frame, text="任务状态（按最近活动排序）",
              font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
    cols = ("state", "title", "detail")
    tree = ttk.Treeview(listbox_frame, columns=cols, show="headings", height=7)
    tree.heading("state", text="状态")
    tree.heading("title", text="任务")
    tree.heading("detail", text="说明")
    tree.column("state", width=88, anchor="center", stretch=False)
    tree.column("title", width=280, anchor="w", stretch=False)
    tree.column("detail", width=380, anchor="w")
    tree.pack(fill="both", expand=True)
    tree.tag_configure("interrupted", background="#ffe3e3")
    tree.tag_configure("stalled", background="#fff1d6")
    tree.tag_configure("working", background="#e6f4ea")
    tree.tag_configure("idle", background="#f2f2f2")

    def _selected_sid() -> str:
        """取表格里当前选中行的会话 ID（存在 values 的第 4 列）。"""
        sel = tree.selection()
        if not sel:
            return ""
        vals = tree.item(sel[0], "values") or []
        return str(vals[3]) if len(vals) > 3 else ""

    def recover_selected():
        """手动续跑表格里选中的那个任务（忽略冷却，立即执行）。"""
        sid = _selected_sid()
        if not sid:
            push("请先在「任务状态」表格里选中一行（中断/卡死的）")
            return
        push(f"手动续跑 {sid} …")
        try:
            ok = guard_force_recover(sid)
            push(f"手动续跑结果：{'已发送' if ok else '未发送（原因见上方日志）'}")
        except Exception as e:
            push(f"手动续跑异常：{e}")
        _refresh_tree()

    def guard_force_recover(sid: str) -> bool:
        g = guard_holder.get("g") or Guard(cfg, dry_run=cfg["dry_run"])
        guard_holder["g"] = g
        return g.force_recover(sid, prompt=prompt_var.get().strip())

    ttk.Button(btns, text="立即续跑选中任务", command=recover_selected).pack(side="left")

    box = ttk.Frame(root, padding=(10, 0, 10, 10))
    box.pack(fill="both", expand=True)
    txt = tk.Text(box, height=10, wrap="none", font=("Consolas", 9))
    txt.pack(side="left", fill="both", expand=True)
    sb = ttk.Scrollbar(box, command=txt.yview)
    sb.pack(side="right", fill="y")
    txt.configure(yscrollcommand=sb.set)

    def _open(path: Path):
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            pass

    def push(line: str):
        q.put(line)

    def apply_cfg():
        cfg["continue_prompt"] = prompt_var.get().strip() or "继续"
        try:
            cfg["stall_timeout"] = max(60, int(float(stall_var.get())))
        except Exception:
            pass
        cfg["dry_run"] = bool(dry_var.get())

    def start():
        if worker["t"] and worker["t"].is_alive():
            return
        apply_cfg()
        g = Guard(cfg, dry_run=cfg["dry_run"], on_event=lambda kind, d: push(f"__DIAG__{d.state}|{d.detail}"))
        guard_holder["g"] = g
        t = threading.Thread(target=g.watch, daemon=True)
        worker["t"] = t
        t.start()
        state_var.set("监控中")
        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        push(f"开始监控｜dry_run={cfg['dry_run']}")

    def stop():
        g = guard_holder.get("g")
        if g:
            g.stop_flag.set()
        state_var.set("已停止")
        start_btn.configure(state="normal")
        stop_btn.configure(state="disabled")

    def once():
        apply_cfg()
        g = Guard(cfg, dry_run=cfg["dry_run"])
        d = g.tick()
        state_var.set(d.state)
        session_var.set(f"{d.session.path}（{d.session.cwd}）" if d.session else "-")
        detail_var.set(d.detail)
        push(g.format_status(d))

    start_btn.configure(command=start)
    stop_btn.configure(command=stop)
    once_btn.configure(command=once)

    def pump():
        try:
            while True:
                line = q.get_nowait()
                if line.startswith("__DIAG__"):
                    payload = line[len("__DIAG__"):]
                    st, _, det = payload.partition("|")
                    state_var.set(st)
                    detail_var.set(det)
                    color = {"interrupted": "#c0392b", "stalled": "#d35400",
                             "working": "#1f6feb", "idle": "#2e7d32"}.get(st, "#333")
                    state_label.configure(foreground=color)
                    _refresh_tree()
                    continue
                txt.insert("end", line + "\n")
                txt.see("end")
        except queue.Empty:
            pass
        try:
            st = guard_holder["g"].state.data["stats"] if guard_holder.get("g") else None
            if st:
                stat_var.set(f"检查 {st.get('checks', 0)} 次｜续跑成功 {st.get('recovered', 0)} 次｜失败 {st.get('failed', 0)} 次")
        except Exception:
            pass
        root.after(400, pump)

    def _refresh_tree():
        """把「所有任务」的判定结果刷到表格里。"""
        g = guard_holder.get("g")
        if not g:
            return
        diags = getattr(g, "last_diags", None) or []
        tree.delete(*tree.get_children())
        label = {"interrupted": "中断", "stalled": "卡死", "working": "运行中",
                 "idle": "已完成", "no_session": "无会话",
                 "app_down": "未运行", "paused": "暂停"}
        for d in diags:
            sid = d.session.session_id[:8] if d.session else "-"
            try:
                title = g._session_title(d.session) if d.session else "-"
            except Exception:
                title = sid
            tag = d.state if d.state in ("interrupted", "stalled", "working", "idle") else ""
            tree.insert("", "end",
                        values=(label.get(d.state, d.state), title or sid, d.detail[:90], sid),
                        tags=(tag,))
        session_var.set(f"共 {len(diags)} 个活跃任务"
                        f"｜需处理 {sum(1 for d in diags if d.needs_action)} 个"
                        if diags else "-")

    root.after(300, pump)
    root.after(100, _refresh_env)
    root.protocol("WM_DELETE_WINDOW", lambda: (stop(), root.destroy()))

    # 打开面板即开始监控。
    # 原来的行为是「面板开着但不动」，用户看到界面就以为在工作，实际一次检查都没跑
    # （实测：面板存活 40+ 分钟，日志零新增）。双击「启动面板」的语义是「开始守护」，
    # 不是「打开一个空壳给我点按钮」。
    root.after(800, start)

    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=CONFIG_PATH)
    common.add_argument("--session-file", type=Path, default=None,
                        help="指定会话 jsonl，默认自动挑最新的")
    common.add_argument("--target-exe", default=None, help="目标进程名，默认 WorkBuddy.exe")
    common.add_argument("--prompt", default=None, help="续跑时发送的内容，默认读配置")

    ap = argparse.ArgumentParser(description="WorkBuddy 断流守护：检测中断并自动续跑",
                                 parents=[common])
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="打印当前状态", parents=[common])
    w = sub.add_parser("watch", help="常驻监控（默认）", parents=[common])
    w.add_argument("--dry-run", action="store_true", help="只报告不发送")
    w.add_argument("--once", action="store_true", help="只检查一次")
    sub.add_parser("gui", help="图形面板", parents=[common])
    sub.add_parser("probe", help="截图核对点击位置", parents=[common])
    sub.add_parser("lockcheck", help="实测锁屏下哪些输入通道还活着", parents=[common])
    sub.add_parser("selftest", help="判定逻辑自检", parents=[common])
    r = sub.add_parser("replay", help="对指定 jsonl 跑一次判定", parents=[common])
    r.add_argument("file", type=Path)
    t = sub.add_parser("test-input", help="输入通道测试（用记事本）", parents=[common])
    t.add_argument("--text", default="wb-guard 输入通道测试")
    sub.add_parser("install-autostart", help="开机自启", parents=[common])
    sub.add_parser("remove-autostart", help="取消开机自启", parents=[common])

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.target_exe:
        cfg["target_exe"] = args.target_exe
    if args.prompt:
        cfg["continue_prompt"] = args.prompt
    enable_dpi_awareness()

    cmd = args.cmd or "watch"
    if cmd == "status":
        return cmd_status(cfg, args.session_file)
    if cmd == "watch":
        return cmd_watch(cfg, args)
    if cmd == "gui":
        return cmd_gui(cfg)
    if cmd == "probe":
        return cmd_probe(cfg)
    if cmd == "lockcheck":
        return cmd_lockcheck(cfg)
    if cmd == "selftest":
        return cmd_selftest(cfg)
    if cmd == "replay":
        return cmd_replay(cfg, args.file)
    if cmd == "test-input":
        return cmd_test_input(cfg, args.text)
    if cmd == "install-autostart":
        return cmd_autostart(True)
    if cmd == "remove-autostart":
        return cmd_autostart(False)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
