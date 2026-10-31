# -*- coding: utf-8 -*-
"""遥控器看护 —— 被计划任务每 30 分钟调一次。

只做一件事：托盘版「云笔记遥控器」挂了就把它拉回来。

- **开关关着**（没有 `guard.enabled` 文件） → 立刻退出，什么都不做
- 进程不在                                → 启动
- 进程在、但 `lastCheck` 很久没刷新（卡死） → 结束它再启动
- 一切正常                                → 静默退出

为什么开关是个**文件**而不是计划任务
------------------------------------
建/删计划任务要权限，也不便携；而一个文件谁都能建、谁都能删，
改完立刻生效。所以：

    %LOCALAPPDATA%\\ZhongYuToolBox\\guard.enabled
    存在 = 开，不存在 = 关

计划任务只是每 30 分钟来"敲一下门"，要不要干活由这个文件决定。
这样即使任务还在、你也能一键关掉，不会被反复拉起。

全部标准库实现，用 pythonw 跑没有控制台窗口。
"""

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import subprocess
import sys
import time

BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                    "ZhongYuToolBox")
SWITCH = os.path.join(BASE, "guard.enabled")
CONFIG = os.path.join(BASE, "config.json")
STATE = os.path.join(BASE, "control_state.json")
LOG = os.path.join(BASE, "remote_guard.log")

# 遥控器 exe 可能叫这几个名字（新旧版本不同）
KNOWN_NAMES = ("云笔记遥控器.exe", "云笔记ai遥控器.exe")

DETACHED = 0x00000008
NEWGRP = 0x00000200
PROCESS_TERMINATE = 0x0001


def log(msg):
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n"
                    % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def switch_on():
    """守护开关是否打开（看那个标志文件）"""
    return os.path.exists(SWITCH)


def exe_candidates():
    """遥控器 exe 的候选位置。

    ⚠️ 原来这里写死了 `C:\\Users\\Administrator\\Desktop\\...`，
       换台机器就直接失效。改成从脚本位置反推 + 常见位置，
       并且允许 config.json 里用 `guard_exe` 自定义。
    """
    out = []
    try:
        with open(CONFIG, encoding="utf-8") as f:
            custom = str((json.load(f) or {}).get("guard_exe") or "").strip()
        if custom:
            out.append(custom)
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))     # .../app
    root = os.path.dirname(here)                          # 项目根
    home = os.path.expanduser("~")
    for d in (os.path.join(root, "dist"),
              os.path.join(home, "Desktop"),
              os.path.join(home, "桌面")):
        for n in KNOWN_NAMES:
            out.append(os.path.join(d, n))
    return out


def pick_exe():
    for p in exe_candidates():
        if p and os.path.exists(p):
            return p
    return None


def _names_for(exe):
    """要匹配的进程名集合（exe 可能被改名，所以把真实名也加进去）"""
    s = set(KNOWN_NAMES)
    if exe:
        s.add(os.path.basename(exe))
    return s


def find_tray_pids(names):
    """列出所有名字命中的进程 PID（不解析命令行，避免中文编码坑）"""
    TH32CS_SNAPPROCESS = 0x00000002
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD),
                    ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value or not snap:
        return []
    pids = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile in names:
                pids.append(int(e.th32ProcessID))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return pids


def last_check_age():
    """距上次巡检过去了多少秒；读不到返回 None"""
    try:
        with open(STATE, encoding="utf-8") as f:
            t = (json.load(f) or {}).get("lastCheck")
        if not t:
            return None
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(t)).total_seconds()
    except Exception:
        return None


def stale_seconds():
    """卡死判定阈值。

    ⚠️ 必须跟着「检查频率」走：巡检间隔是可配的（1~60 分钟）。
       如果用户设成 30 分钟，lastCheck 本来就 30 分钟才更新一次，
       固定 15 分钟阈值会天天误杀正常进程。
       这里取 max(15 分钟, 3 个巡检周期)。
    """
    iv = 1
    try:
        with open(CONFIG, encoding="utf-8") as f:
            iv = max(1, int((json.load(f) or {}).get("interval_min") or 1))
    except Exception:
        pass
    return max(900, iv * 3 * 60)


def kill(pid):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(k32.TerminateProcess(h, 0))
    finally:
        k32.CloseHandle(h)


def start(exe):
    # --guard：静默启动，不弹设置界面
    subprocess.Popen([exe, "--guard"], creationflags=DETACHED | NEWGRP,
                     close_fds=True, cwd=os.path.dirname(exe))
    log("已启动 %s（--guard）" % exe)


def main():
    # ---- 第一件事就是看开关，关着就什么都不做
    if not switch_on():
        log("开关关闭（没有 guard.enabled），本次跳过")
        return 0

    exe = pick_exe()
    if not exe:
        log("找不到遥控器 exe，候选都试过了，跳过")
        return 1

    pids = find_tray_pids(_names_for(exe))
    age = last_check_age()

    if not pids:
        log("进程不在（lastCheck=%s），拉起"
            % ("无记录" if age is None else "%.0f 秒前" % age))
        start(exe)
        return 0

    limit = stale_seconds()
    if age is not None and age > limit:
        log("进程在（pid=%s）但 lastCheck 已 %.0f 分钟没刷新（阈值 %.0f 分钟），"
            "判为卡死 → 重启"
            % (",".join(map(str, pids)), age / 60.0, limit / 60.0))
        for pid in pids:
            kill(pid)
        time.sleep(3)
        start(exe)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
