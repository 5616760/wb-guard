#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wb-guard 单文件启动器（替代 6 个 .bat）
=======================================

一个 exe，用子命令复现原来 6 个 bat 的行为：

  wb-guard.exe                 → 面板（等同「启动面板.bat」，推荐日常用）
  wb-guard.exe panel           → 同上
  wb-guard.exe watch           → 控制台监控（等同「启动监控.bat」）
  wb-guard.exe watch --dry-run → 只观察不发送（等同「启动监控-只观察.bat」）
  wb-guard.exe stop            → 停止守护（等同「停止守护.bat」）
  wb-guard.exe autostart on    → 安装开机自启（等同「开机自启-安装.bat」）
  wb-guard.exe autostart off   → 取消开机自启（等同「开机自启-取消.bat」）

设计要点：
  - 打包成 console 版本，窗口行为与 bat 一致（watch 时能看到滚动日志）。
  - panel / autostart 复用 wb_guard.py 的原有实现，不重写逻辑。
  - 结束时不立刻关窗（等按键），和 bat 的 pause 行为对齐，避免双击后"闪退"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_TITLE = "WorkBuddyGuard"

# PyInstaller 打包后 __file__ 在临时目录，必须用 exe 路径定位 config/logs。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# 关键：wb_guard 必须作为模块导入并直接在进程内调用 main()。
# 不能用 subprocess 起 wb_guard.py —— 打包成单文件 exe 后磁盘上没有 .py，
# 且 sys.executable 指向 exe 自身，会造成重新进入 launcher 的死循环。
try:
    import wb_guard
except ImportError as _e:  # pragma: no cover
    # windowed 打包下 stderr 可能为 None，不能直接 write
    if sys.stderr is not None:
        sys.stderr.write(f"[FATAL] 无法导入 wb_guard：{_e}\n")
    raise

BANNER = "=" * 60


def _pause(msg: str = "按任意键关闭窗口...") -> None:
    """对齐 bat 的 pause。非交互环境下静默跳过，避免卡住自动化调用。"""
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        input(f"\n{msg}")
    except (EOFError, KeyboardInterrupt):
        pass


def _run_guard(args: list[str]) -> int:
    """在进程内运行 wb_guard.py 的某个子命令（不做子进程调用）。"""
    # 长驻模式（gui / watch）写入 PID 文件，供 stop 精确定位；退出时清理。
    is_daemon = bool(args) and args[0] in ("gui", "watch") and "--once" not in args
    pid_file = BASE_DIR / "wb-guard.pid"

    prev_cwd = os.getcwd()
    try:
        os.chdir(BASE_DIR)
        if is_daemon:
            try:
                with open(pid_file, "a", encoding="utf-8") as f:
                    f.write(f"{os.getpid()}\n")
            except Exception:
                pass
        return wb_guard.main(args)
    finally:
        if is_daemon:
            # 只删自己那一行，避免并发多实例互相清掉
            try:
                me = str(os.getpid())
                if pid_file.exists():
                    left = [
                        ln for ln in pid_file.read_text(encoding="utf-8", errors="replace").splitlines()
                        if ln.strip() and ln.strip() != me
                    ]
                    if left:
                        pid_file.write_text("\n".join(left) + "\n", encoding="utf-8")
                    else:
                        pid_file.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            os.chdir(prev_cwd)
        except Exception:
            pass


def cmd_watch(dry_run: bool, extra_args: list[str] | None = None) -> int:
    if dry_run:
        print(BANNER)
        print("  WorkBuddy 断流守护 - 只观察模式")
        print("  会检测中断并打印，但绝不发送任何消息，也不切换会话。")
        print("  确认判定符合预期后，改用 wb-guard.exe watch。")
        print(BANNER)
    else:
        print(BANNER)
        print("  WorkBuddy 断流守护 - 控制台模式")
        print("  检测到模型中断会自动发送「继续」；关闭本窗口即停止。")
        print(BANNER)
    print()

    # 保留 wb_guard 原生参数（--once / --session-file / --config 等），
    # 不能只传 --dry-run，否则 --once 会丢失并退化成常驻循环。
    args = ["watch", *(extra_args or [])]
    if dry_run and "--dry-run" not in args:
        args.append("--dry-run")

    code = _run_guard(args)

    print()
    print("已退出只观察模式。" if dry_run else "守护已退出。")
    # --once 是一次性检查，不该停下来等按键
    if "--once" not in args:
        _pause()
    return code


def cmd_panel() -> int:
    """打开 GUI 面板。

    单文件 exe 下不再 spawn 子进程 —— 实测 onefile 解包 + 无 console 启动时，
    子进程会在父进程退出后被回收，面板留不住。
    这里直接**在本进程内同步运行 GUI**：双击 exe 就是面板，关掉面板就结束，
    语义最直观，也不依赖任何进程托管技巧。
    """
    return _run_guard(["gui"])


def _list_guard_pids() -> list[int]:
    """列出所有 wb-guard 进程 PID。

    用 ctypes 直接枚举 Toolhelp 快照，不依赖 tasklist：
      - 在 Git Bash / 受限环境下 tasklist 过滤可能失效或被路径转换打断；
      - onefile 会同时存在引导进程与实际进程，两者名字都是 wb-guard.exe。
    同时兼容源码态（python.exe 跑 wb_guard.py）。"""
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            # 用显式 260 字节数组而非 WCHAR*260：打包后 argtypes 未声明时
            # ctypes 对宽字符数组的隐式转换会失败，导致整次枚举返回空。
            ("szExeFile", ctypes.c_char * 260),
        ]

    pids: list[int] = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return pids
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32First(snap, ctypes.byref(pe))
            while ok:
                raw = bytes(pe.szExeFile).split(b"\x00", 1)[0]
                name = raw.decode("mbcs", "replace").lower()
                if "wb-guard" in name:
                    pids.append(int(pe.th32ProcessID))
                ok = k32.Process32Next(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return pids


def cmd_stop() -> int:
    """停止所有 wb_guard 守护进程（gui / watch），不误杀 stop 自身。

    匹配策略按可靠性排序，任一命中即杀：
      1) PID 文件（最精确，由 gui/watch 启动时写入）
      2) 进程名 wb-guard.exe / pythonw.exe（打包态 / 旧 bat 态）
      3) WMI 命令行兜底（源码态 `python wb_guard.py gui`）
    """
    print("正在停止 WorkBuddy 断流守护...", flush=True)

    self_pid = os.getpid()
    # onefile 模式下存在「引导进程 → 实际进程」两层，只排除 getpid() 会漏掉父引导进程，
    # 导致 stop 杀掉自己的父进程后中途消失。把整条祖先链都排除。
    exclude: set[int] = set()
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        k32 = ctypes.windll.kernel32
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap and snap != -1:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            parents: dict[int, int] = {}
            if k32.Process32First(snap, ctypes.byref(pe)):
                while True:
                    parents[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                    if not k32.Process32Next(snap, ctypes.byref(pe)):
                        break
            k32.CloseHandle(snap)
            cur = self_pid
            for _ in range(8):  # 防御性上限，避免环
                exclude.add(cur)
                nxt = parents.get(cur, 0)
                if not nxt or nxt in exclude:
                    break
                cur = nxt
    except Exception:
        exclude.add(self_pid)

    targets: set[int] = set()

    # 1) PID 文件
    pid_file = BASE_DIR / "wb-guard.pid"
    if pid_file.exists():
        try:
            for ln in pid_file.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if ln.isdigit():
                    targets.add(int(ln))
        except Exception:
            pass

    # 2) 进程名匹配（打包态最可靠；onefile 的 CommandLine 常为空，只能靠名字）。
    #    注意排除自身：stop 自己也叫 wb-guard.exe，误杀会导致命令中途消失。
    for pid in _list_guard_pids():
        if pid not in exclude:
            targets.add(pid)

    # 3) 旧 bat 态：pythonw.exe 里标题为 WorkBuddyGuard 的
    subprocess.call(
        ["taskkill", "/f", "/im", "pythonw.exe", "/fi", f"WINDOWTITLE eq {APP_TITLE}*"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # 4) WMI 兜底：源码态 python wb_guard.py <cmd>
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine -match 'wb_guard\\.py') } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
        )
        for ln in (out.stdout or "").splitlines():
            ln = ln.strip()
            if ln.isdigit():
                targets.add(int(ln))
    except Exception:
        pass

    # 先记录清理前的目标数与存活 PID（用于准确报数）
    targets_to_kill = [p for p in sorted(targets) if p not in exclude]
    alive_before = [p for p in _list_guard_pids() if p not in exclude]

    for pid in targets_to_kill:
        subprocess.call(
            ["taskkill", "/f", "/t", "/pid", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # 兜底：再枚举一次清残留（覆盖引导进程，以及 PID 文件与实际进程不一致的情况）
    for pid in _list_guard_pids():
        if pid in exclude:
            continue
        subprocess.call(
            ["taskkill", "/f", "/t", "/pid", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # 报数：清理前存活、清理后已消失的 PID 数量（最贴近用户直觉的「停掉了几个」）
    alive_after = {p for p in _list_guard_pids() if p not in exclude}
    vanished = [p for p in alive_before if p not in alive_after]
    killed = len(vanished)
    remaining = len(alive_after)

    # 清理 PID 文件
    try:
        pid_file.unlink(missing_ok=True)
    except Exception:
        pass

    if remaining > 0:
        print(f"已停止 {killed} 个守护进程，仍有 {remaining} 个残留（权限不足？）。", flush=True)
    elif killed > 0:
        print(f"已停止 {killed} 个守护进程。", flush=True)
    else:
        print("没有正在运行的守护进程。", flush=True)

    log = BASE_DIR / "logs" / "wb_guard.log"
    print(f"日志：{log}")
    _pause()
    return 0


def cmd_autostart(enable: bool) -> int:
    code = _run_guard(["install-autostart" if enable else "remove-autostart"])
    print()
    _pause()
    return code


def print_usage(stream=None) -> None:
    # 默认值不能写成 stream=sys.stdout：windowed 打包下导入时它是 None，
    # 默认参数会被永久固化成 None。改为调用时取。
    if stream is None:
        stream = sys.stdout
    print(
        """wb-guard 断流守护
用法：
  wb-guard.exe                打开面板（默认，等价于原「启动面板」）
  wb-guard.exe panel          同上
  wb-guard.exe watch          控制台监控，自动发「继续」
  wb-guard.exe watch --dry-run 只观察，不发消息
  wb-guard.exe stop           停止守护
  wb-guard.exe autostart on   安装开机自启
  wb-guard.exe autostart off  取消开机自启

其他高级命令直接透传给 wb_guard.py：
  status / probe / lockcheck / selftest / replay / test-input
""",
        file=stream,
    )


def _init_console() -> None:
    """确保有可用的输出流，并把控制台切到 UTF-8。

    windowed（无控制台）打包下 sys.stdout / sys.stderr 为 None，
    print() 会直接抛 AttributeError。这里兜底重定向到空设备，
    保证 CLI 子命令不会因为「没有窗口」而崩溃。
    """
    if sys.stdout is None:
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr is None:
        try:
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
        except Exception:
            pass

    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _init_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    # windowed 打包没有控制台，未捕获异常会静默消失、看起来像「双击没反应」。
    # 这里兜底写进日志，并在 GUI 模式弹窗提示，保证故障可诊断。
    def _fatal(exc_type, exc_val, exc_tb):
        import traceback

        msg = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        try:
            wb_guard.log("未捕获异常：\n" + msg, "ERROR")
        except Exception:
            try:
                err_file = BASE_DIR / "logs" / "launcher-crash.log"
                err_file.parent.mkdir(parents=True, exist_ok=True)
                with open(err_file, "a", encoding="utf-8") as f:
                    f.write(msg + "\n")
            except Exception:
                pass
        try:
            import tkinter as tk
            from tkinter import messagebox

            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("wb-guard 启动失败", msg[-1500:])
            r.destroy()
        except Exception:
            pass

    sys.excepthook = _fatal

    if argv and argv[0] in ("-h", "--help", "help", "/?"):
        print_usage()
        _pause()
        return 0

    cmd = argv[0].lower() if argv else "panel"
    rest = argv[1:]

    if cmd == "panel":
        return cmd_panel()

    if cmd == "gui":
        # 与 panel 等价：都在本进程内同步跑 GUI。
        return _run_guard(["gui"])

    if cmd == "watch":
        return cmd_watch(
            dry_run="--dry-run" in rest or "--dryrun" in rest,
            extra_args=rest,
        )

    if cmd in ("stop", "kill"):
        return cmd_stop()

    if cmd == "autostart":
        sub = rest[0].lower() if rest else "on"
        if sub in ("on", "install", "enable", "1", "true"):
            return cmd_autostart(True)
        if sub in ("off", "remove", "disable", "0", "false"):
            return cmd_autostart(False)
        print(f"[ERROR] 未知的 autostart 参数：{sub}（应为 on/off）")
        _pause()
        return 2

    # 其余命令透传给 wb_guard.py，保持向后兼容
    passthrough = {
        "status", "probe", "lockcheck", "selftest", "replay", "test-input",
        "install-autostart", "remove-autostart",
    }
    if cmd in passthrough:
        code = _run_guard(argv)
        _pause()
        return code

    print(f"[ERROR] 未知命令：{argv[0]}\n")
    print_usage(sys.stderr)
    _pause()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
