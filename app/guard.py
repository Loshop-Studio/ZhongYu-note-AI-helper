# -*- coding: utf-8 -*-
"""系统保护（守护）的统一管理。

为什么单独一个模块
------------------
设置界面要开关它、遥控器指令也要开关它。如果两边各写一份，
迟早会出现「界面说开着、指令说关着」这种割裂。所以集中在这里，
谁要用就 import 谁。

机制
----
    计划任务（每 30 分钟）→ guard_run.bat → remote_guard.py
    bat 和 py 都先看 `guard.enabled` 这个标志文件，没有就直接退出。

开关用**文件**而不是靠建/删计划任务：
    · 建删任务要权限、也不便携；文件谁都能删，改完立刻生效
    · 关掉时任务不用删 —— 跑起来读到没标志就自己退出，
      所以「关得掉、且不会过一会儿又反弹」
"""

import os
import subprocess
import sys

BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                    "ZhongYuToolBox")

GUARD_TASK = "ZhongYuRemoteGuard"
OLD_GUARD_TASK = "云笔记遥控器守护"        # 老版本的名字，发现就顺手清掉
GUARD_FLAG = os.path.join(BASE, "guard.enabled")
GUARD_BAT = os.path.join(BASE, "guard_run.bat")
GUARD_EVERY_MIN = 30


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def guard_on():
    """守护是否打开（看标志文件在不在）"""
    return os.path.exists(GUARD_FLAG)


def _guard_script():
    """守护脚本的**稳定**路径。

    源码运行时它就在 app/ 下；打包成 onefile 之后，app/ 目录不存在了，
    脚本会被解到 `sys._MEIPASS`（每次启动都是新的临时目录）——
    计划任务不能指向那种路径，否则下次启动就失效。

    所以这里做一次"搬移"：把脚本复制到 `%LOCALAPPDATA%\\ZhongYuToolBox\\`
    下长期保存，任务固定指向这份副本。
    """
    dst = os.path.join(BASE, "remote_guard.py")
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_guard.py")
    mei = getattr(sys, "_MEIPASS", None)
    src = None
    if os.path.exists(here):
        src = here
    elif mei and os.path.exists(os.path.join(mei, "remote_guard.py")):
        src = os.path.join(mei, "remote_guard.py")

    if not src:
        return dst if os.path.exists(dst) else None

    try:
        if os.path.abspath(src) == os.path.abspath(dst):
            return dst
        with open(src, "rb") as f:
            data = f.read()
        old = None
        if os.path.exists(dst):
            with open(dst, "rb") as f:
                old = f.read()
        if old != data:                     # 内容变了才重写，避免每次启动都动文件
            os.makedirs(BASE, exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        return dst
    except Exception:
        return src                          # 复制失败就直接用原位置（源码模式够用）


def _pythonw():
    for cand in (r"C:\Program Files\Python314\pythonw.exe",
                 os.path.join(os.path.dirname(sys.executable), "pythonw.exe")):
        if os.path.exists(cand):
            return cand
    return None


def _write_bat():
    """生成守护启动脚本。

    为什么绕一层 bat：schtasks 的 /TR 对「带引号又含空格的命令行」很挑，
    直接写 `"C:\\Program Files\\...\\pythonw.exe" -B "D:\\...\\有 空格\\x.py"`
    经常建失败；指向一个短路径的 bat 最稳。

    bat 里也检查一次标志文件，双保险：没标志就连 python 都不启动。
    """
    script, pyw = _guard_script(), _pythonw()
    lines = ["@echo off",
             'if not exist "%s" exit /b' % GUARD_FLAG]
    if script and pyw:
        lines.append('"%s" -B "%s"' % (pyw, script))
    else:
        # 没有 Python 环境就退回 exe 自带守护（慢一点：onefile 要解包）
        lines.append('start "" "%s" --guard' % os.path.abspath(sys.executable))
    os.makedirs(BASE, exist_ok=True)
    with open(GUARD_BAT, "w", encoding="gbk", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return GUARD_BAT


def _task_exists(name):
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name],
                           capture_output=True, timeout=15,
                           creationflags=_no_window())
        return r.returncode == 0
    except Exception:
        return False


def _del_task(name):
    try:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", name],
                       capture_output=True, timeout=20,
                       creationflags=_no_window())
    except Exception:
        pass


def set_guard(on):
    """开/关系统保护。返回 (成功, 说明)。

    开：写标志文件 + 确保计划任务在（每 30 分钟敲一次门）
    关：**只删标志文件**，任务留着 —— 它跑起来读到没标志就直接退出。
        这样"关"不需要管理员权限，也不会出现"删不掉任务所以关不掉"。
    """
    try:
        os.makedirs(BASE, exist_ok=True)

        if not on:
            # 这是程序自己的开关文件（不是用户资料），删掉它就等于关开关
            if os.path.exists(GUARD_FLAG):
                try:
                    os.remove(GUARD_FLAG)
                except Exception as e:
                    return False, "删开关文件失败：%r" % (e,)
            return True, "已关闭，不会再自动拉起"

        with open(GUARD_FLAG, "w", encoding="utf-8") as f:
            f.write("on\n")

        # 老版本还有一套「云笔记遥控器守护」（每小时跑 guard.bat），功能重复，
        # 留着会两套打架，发现就清掉
        if _task_exists(OLD_GUARD_TASK):
            _del_task(OLD_GUARD_TASK)

        bat = _write_bat()
        r = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", GUARD_TASK,
             "/TR", '"%s"' % bat,
             "/SC", "MINUTE", "/MO", str(GUARD_EVERY_MIN)],
            capture_output=True, timeout=30,
            creationflags=_no_window())
        if r.returncode == 0:
            return True, "每 %d 分钟检查一次，挂了自动拉起" % GUARD_EVERY_MIN

        # 任务没建成 → 把标志撤掉，避免「界面显示关、标志却还在」的错乱
        try:
            if os.path.exists(GUARD_FLAG):
                os.remove(GUARD_FLAG)
        except Exception:
            pass
        err = (r.stderr or r.stdout or b"").decode("mbcs", "replace").strip()[:160]
        return False, err or ("schtasks 返回 %d" % r.returncode)
    except Exception as e:
        return False, repr(e)
