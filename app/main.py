#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py —— 云笔记遥控器（托盘常驻版）

一个程序管三件事，不再依赖 Windows 计划任务：

  1. **托盘图标**：右键菜单「打开设置 / 暂停 / 继续 / 打开日志 / 退出」
  2. **后台定时循环**：每 N 分钟跑一次 control_bot.run_once()
  3. **设置界面**：点「打开设置」时启动自己的 --settings 子进程
     （tkinter 需要独占主循环，塞进托盘进程里会打架）

退出 = 真的全停：停循环、停图标，进程消失。

工程上的取舍：
  · 用 Windows 命名互斥体保证单实例 —— 双击两次不会出现两个图标、两套循环
  · 循环里的睡眠切成 1 秒一片，这样点「退出」能立刻响应，不用等满一个周期
  · 打包后 sys.executable 就是 exe 本身，所以「打开设置」直接再启动自己
"""

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
import traceback

HERE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MUTEX_NAME = "Global\\ZhongYuRemoteTray"
ERROR_ALREADY_EXISTS = 183
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ZhongYuRemote"


def _autostart_value():
    """读注册表里的开机自启项；没有返回 None"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, APP_NAME)
            return v
    except Exception:
        return None


def _set_autostart(on):
    """写/删开机自启项（只有打包成 exe 才有意义）"""
    if not getattr(sys, "frozen", False):
        return False                    # 源码模式不开机自启，避免写进 python.exe
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                  '"%s"' % sys.executable)
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        C.log("设置开机自启失败 %r" % (e,))
        return False


def _self_cmd(extra):
    """拼出「再启动自己」的命令行（区分打包前后）"""
    if getattr(sys, "frozen", False):
        return [sys.executable] + extra
    return [sys.executable, os.path.abspath(__file__)] + extra


# ------------------------------------------------------------------ 设置模式
# 必须在导入 pystray 之前分派，避免多开一个托盘
if "--settings" in sys.argv:
    import setup_gui
    setup_gui.main()
    sys.exit(0)


# ------------------------------------------------------------------ 子进程分发
# ⚠️ 打包成 exe 之后，send_text / _shot_page / dsc_click 不再是以独立 .py 文件存在的，
#    所以统一走「同一个 exe + --child <名字>」。
#    否则 control_bot 会把 exe 当成解释器去跑 `-B xxx.py`，exe 不认识这些参数 →
#    argparse 直接 exit(2) —— 表现就是「指令能处理，但每条回执都发不出去」（2026-10-18 踩到）。
_CHILD_FILES = {
    "send_text": "send_text.py",
    "shot_page": "_shot_page.py",
    "dsc_click": "dsc_click.py",
}

if "--child" in sys.argv:
    _i = sys.argv.index("--child")
    _which = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else ""
    sys.argv = [_which] + sys.argv[_i + 2:]
    if _which == "send_text":
        import send_text
        send_text.main()
        sys.exit(0)
    if _which == "send_text_render":
        # send_text 内部渲染文字回执时调自己（Pillow 与主程序同包，只是个隔离）
        import send_text
        sys.argv = ["send_text", "--render-only"]
        send_text.main()
        sys.exit(0)
    if _which == "shot_page":
        import _shot_page
        sys.exit(_shot_page.main())
    if _which == "dsc_click":
        import dsc_click
        sys.exit(dsc_click.main())
    sys.stderr.write("未知子命令：%s\n" % _which)
    sys.exit(2)


import control_bot as C          # noqa: E402
import pystray                    # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


class _Args:
    """给 control_bot.run_once 用的伪参数对象"""
    dry = False
    force = False


class TrayApp:
    def __init__(self):
        self.stop = threading.Event()
        self.paused = False
        self.cfg = C.load_config()
        self.worker = None
        self.icon = None

    # ---------------- 图标
    def _image(self, paused=False):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        outer = (150, 150, 150, 255) if paused else (78, 201, 176, 255)
        d.ellipse((6, 6, 58, 58), fill=outer)
        d.ellipse((24, 24, 40, 40), fill=(25, 25, 25, 255))
        if paused:
            d.rectangle((28, 20, 36, 44), fill=(25, 25, 25, 255))
        return img

    def _title(self):
        if self.paused:
            return "云笔记遥控器（已暂停）"
        return "云笔记遥控器（运行中 · 每 %d 分钟）" % max(
            1, int(self.cfg.get("interval_min", 1)))

    # ---------------- 后台循环
    def _sleep(self, seconds):
        """切片睡眠：随时能被打断，退出不用等满一轮"""
        for _ in range(int(seconds)):
            if self.stop.is_set():
                return False
            time.sleep(1)
        return not self.stop.is_set()

    def _loop(self):
        # 启动先等一下，别和开机时的系统繁忙抢资源
        if not self._sleep(3):
            return
        while not self.stop.is_set():
            if not self.paused:
                try:
                    C.run_once(_Args(), force=False, dry=False)
                except Exception:
                    C.log("托盘循环异常：%s" % traceback.format_exc()[-500:])
            # 每轮重新读配置：在设置里改了频率，下一轮就生效
            try:
                self.cfg = C.load_config()
            except Exception:
                pass
            interval = max(15, int(self.cfg.get("interval_min", 1)) * 60)
            if not self._sleep(interval):
                break

    # ---------------- 菜单动作
    def _on_settings(self, icon=None, item=None):
        try:
            subprocess.Popen(_self_cmd(["--settings"]),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            C.log("打开设置失败：%r" % (e,))

    def _on_pause(self, icon=None, item=None):
        self.paused = not self.paused
        self.icon.icon = self._image(self.paused)
        self.icon.title = self._title()
        C.log("托盘：%s" % ("已暂停" if self.paused else "继续运行"))

    def _on_autostart(self, icon=None, item=None):
        """勾选/取消「开机自动启动」"""
        on = _autostart_value() is None
        if _set_autostart(on):
            C.log("开机自启：" + ("已开启" if on else "已关闭"))
        else:
            C.log("开机自启：设置失败（源码模式不支持，需用打包后的 exe）")

    def _on_log(self, icon=None, item=None):
        try:
            p = os.path.join(C._base_dir(), "control_bot.log")
            if os.path.exists(p):
                subprocess.Popen(["notepad.exe", p])
        except Exception as e:
            C.log("打开日志失败：%r" % (e,))

    def _on_quit(self, icon=None, item=None):
        C.log("托盘：用户选择退出，停止所有循环")
        self.stop.set()
        if self.icon:
            self.icon.stop()

    # ---------------- 启动
    def run(self):
        self.worker = threading.Thread(target=self._loop, daemon=True,
                                       name="patrol-loop")
        self.worker.start()

        menu = pystray.Menu(
            pystray.MenuItem("打开设置", self._on_settings, default=True),
            pystray.MenuItem(lambda i: "继续" if self.paused else "暂停",
                             self._on_pause),
            pystray.MenuItem("开机自动启动", self._on_autostart,
                             checked=lambda i: _autostart_value() is not None),
            pystray.MenuItem("打开日志", self._on_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出（全停）", self._on_quit),
        )
        self.icon = pystray.Icon("ZhongYuRemote", self._image(False),
                                 self._title(), menu)
        self.icon.run()

        # icon.run() 返回 = 已经退出
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=5)
        return 0


def _single_instance():
    """命名互斥体：已经有一个在跑就直接退出，避免双循环"""
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return True                      # 拿不到就算了，别挡住启动
    if k32.GetLastError() == ERROR_ALREADY_EXISTS:
        return False
    keep = h                            # 持有到进程结束
    return True


def main():
    ap = argparse.ArgumentParser(description="云笔记遥控器（托盘常驻）")
    ap.add_argument("--settings", action="store_true", help="只打开设置界面")
    ap.add_argument("--once", action="store_true", help="只巡检一次就退出（调试用）")
    ap.add_argument("--guard", action="store_true",
                    help="守护模式：没在跑就拉起来，已在跑就静默退出（给计划任务用）")
    ap.parse_args()

    if "--once" in sys.argv:
        C.run_once(_Args(), force=False, dry=False)
        return 0

    # --guard：计划任务每小时调一次，只负责「没在跑就拉起来」。
    # 已经在跑就静默退出（不弹设置窗），保证守护过程完全不打扰用户。
    guard_mode = "--guard" in sys.argv

    if not _single_instance():
        if guard_mode:
            return 0                    # 已经活着，什么都不用做
        # 已经有托盘在跑了 —— 这次双击只负责把设置界面叫出来，
        # 不然用户双击后「什么都没发生」，会以为程序坏了。
        try:
            subprocess.Popen(_self_cmd(["--settings"]),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            C.log("唤起设置界面失败 %r" % (e,))
        return 0

    C.log("托盘版启动（每 %d 分钟一轮）%s"
          % (max(1, int(C.load_config().get("interval_min", 1))),
             "（守护模式拉起）" if guard_mode else ""))
    app = TrayApp()

    if not guard_mode:
        # 双击启动时顺手把主界面打开（用户要求：不要只留一个托盘图标）。
        # 守护模式拉起时不弹窗，免得每小时糊你一脸。
        # 稍等一下再开，让托盘图标先出现，视觉上更顺。
        def _open_settings():
            try:
                subprocess.Popen(_self_cmd(["--settings"]),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:
                C.log("自动打开设置失败 %r" % (e,))
        threading.Timer(0.8, _open_settings).start()

    return app.run()


if __name__ == "__main__":
    sys.exit(main())
