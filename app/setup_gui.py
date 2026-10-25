#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设置向导：填「云笔记账号」+「DeepSeek 密钥」，保存到本机，并当场测试连通性。

设计取舍：
  · 用 tkinter（标准库），不引入任何第三方 UI 依赖 —— 以后打包 exe 不会因为 UI 库炸
  · 保存格式沿用现有文件，**不改变**控制端读取逻辑：
      %LOCALAPPDATA%\\ZhongYuToolBox\\zy_credentials.json
      %LOCALAPPDATA%\\ZhongYuToolBox\\deepseek_key.txt
  · 测试连接跑在后台线程，界面不卡死

用法：双击 run_setup.bat，或 .venv\\Scripts\\pythonw.exe setup_gui.py
"""

import datetime
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：__file__ 指向临时解包目录，真实位置要看 exe 本身
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                    "ZhongYuToolBox")
CRED = os.path.join(BASE, "zy_credentials.json")
KEYF = os.path.join(BASE, "deepseek_key.txt")
CONF = os.path.join(BASE, "config.json")
# 2026-10-17：服务地址不再绑定特定服务，由用户填写
DEFAULT_API = ""
PLACEHOLDER = ("REPLACE_ME", "PASTE_HERE")
TASK_NAME = "云笔记遥控器"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_cred():
    try:
        with open(CRED, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conf(d):
    try:
        os.makedirs(BASE, exist_ok=True)
        with open(CONF, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def load_key():
    try:
        with open(KEYF, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if (s and not s.startswith("#") and s.upper() not in PLACEHOLDER
                        and len(s) >= 20):
                    return s
    except Exception:
        pass
    return ""


def load_conf():
    d = {"mode": "control", "interval_min": 1}
    try:
        with open(CONF, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            d.update(got)
    except Exception:
        pass
    if d.get("mode") not in ("control", "full"):
        d["mode"] = "control"
    try:
        d["interval_min"] = max(1, min(60, int(d.get("interval_min") or 1)))
    except Exception:
        d["interval_min"] = 1
    return d


THINK_OFF = "关（最快最省）"
THINK_CHOICES = (THINK_OFF, "标准", "最高（最强也最慢）")
THINK_MAP = {THINK_OFF: "off", "标准": "high", "最高（最强也最慢）": "max"}
THINK_BACK = {v: k for k, v in THINK_MAP.items()}

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "ZhongYuRemote"


def autostart_on():
    """注册表里有没有开机自启动项"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
        return True
    except Exception:
        return False


def set_autostart(on):
    """写/删开机自启动项。返回 (成功, 说明)"""
    if not getattr(sys, "frozen", False):
        return False, "源码模式下不支持（注册表会指向 python.exe）"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                exe = os.path.abspath(sys.executable)
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
                return True, exe
            try:
                winreg.DeleteValue(k, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
            return True, "已移除"
    except Exception as e:
        return False, repr(e)


GUARD_TASK = "云笔记遥控器守护"
GUARD_BAT = os.path.join(BASE, "guard.bat")


def guard_on():
    """守护计划任务是否已存在"""
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", GUARD_TASK],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def _write_guard_bat():
    """生成守护脚本：进程不在才把 exe 拉起来。

    ⚠️ 为什么不让任务直接调 exe：exe 是 onefile，每次启动要解包 30MB（实测 20~25 秒），
       每小时跑一次纯属浪费 IO。用 tasklist 先探一下只要 0.3 秒。
    """
    exe = os.path.abspath(sys.executable)
    name = os.path.basename(exe)
    lines = [
        "@echo off",
        'tasklist /FI "IMAGENAME eq %s" | find /I "%s" >nul 2>&1' % (name, name),
        "if errorlevel 1 (",
        '    start "" "%s" --guard' % exe,
        ")",
    ]
    os.makedirs(BASE, exist_ok=True)
    with open(GUARD_BAT, "w", encoding="gbk", newline="\r\n") as f:
        f.write("\n".join(lines) + "\r\n")
    return GUARD_BAT


def set_guard(on):
    """创建/删除守护计划任务（每小时跑一次 guard.bat）。返回 (成功, 说明)"""
    if not getattr(sys, "frozen", False):
        return False, "源码模式下不支持（任务会指向 python.exe）"
    try:
        if on:
            bat = _write_guard_bat()
            cmd = ["schtasks", "/Create", "/F", "/TN", GUARD_TASK,
                   "/TR", '"%s"' % bat, "/SC", "HOURLY"]
        else:
            cmd = ["schtasks", "/Delete", "/F", "/TN", GUARD_TASK]
        r = subprocess.run(cmd, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return True, ("每小时检查，挂了自动拉起" if on else "已移除")
        err = (r.stderr or r.stdout or b"").decode("mbcs", "replace").strip()[:120]
        return False, err or ("schtasks 返回 %d" % r.returncode)
    except Exception as e:
        return False, repr(e)


def _pids_of(exe_name):
    """列出名字等于 exe_name 的所有进程 PID（含自己）"""
    out = []
    try:
        import ctypes
        from ctypes import wintypes

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        pe = PE32()
        pe.dwSize = ctypes.sizeof(PE32)
        if k32.Process32First(snap, ctypes.byref(pe)):
            while True:
                n = (pe.szExeFile or b"").decode("mbcs", "replace").lower()
                if n == exe_name.lower():
                    out.append(int(pe.th32ProcessID))
                if not k32.Process32Next(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
    except Exception:
        pass
    return out


def _ago(sec):
    """把秒数说成人话"""
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分钟" % (sec // 60)
    return "%.1f 小时" % (sec / 3600.0)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("云笔记遥控器 · 设置")
        self.geometry("600x720")
        self.minsize(560, 660)
        self.configure(bg="#1e1e1e")
        self.q = queue.Queue()
        self._build()
        self._load()
        self.after(120, self._poll)
        self.after(200, self._tick_status)

    # ---------------- UI
    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background="#1e1e1e", foreground="#e6e6e6")
        st.configure("TLabel", background="#1e1e1e", foreground="#e6e6e6",
                     font=("Microsoft YaHei UI", 10))
        st.configure("H.TLabel", font=("Microsoft YaHei UI", 12, "bold"),
                     foreground="#4ec9b0")
        st.configure("TFrame", background="#1e1e1e")
        st.configure("TButton", font=("Microsoft YaHei UI", 10))
        st.configure("TEntry", fieldbackground="#2b2b2b", foreground="#e6e6e6")

        pad = {"padx": 18, "pady": 4}
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, pady=8)

        ttk.Label(root, text="云笔记遥控器", style="H.TLabel").pack(anchor="w", **pad)
        ttk.Label(root, text="填好两项就能用，保存后立即生效",
                  foreground="#9a9a9a").pack(anchor="w", padx=18)

        # 运行状态条：上次巡检 / 下次预计（每 5 秒自己刷新）
        self.lb_status = tk.Label(root, text="正在读取运行状态…", bg="#122b22",
                                  fg="#8fe0a8", anchor="w", padx=10, pady=6,
                                  font=("Microsoft YaHei UI", 9))
        self.lb_status.pack(fill="x", padx=18, pady=(8, 2))

        # --- 云笔记
        box1 = ttk.LabelFrame(root, text=" 云笔记账号 ")
        box1.pack(fill="x", padx=18, pady=(12, 6))

        self.v_user = tk.StringVar()
        self.v_pass = tk.StringVar()
        self.v_api = tk.StringVar(value=DEFAULT_API)

        for i, (label, var, show) in enumerate([
                ("账号", self.v_user, ""),
                ("密码", self.v_pass, "●"),
                ("服务器", self.v_api, "")]):
            ttk.Label(box1, text=label).grid(row=i, column=0, sticky="e",
                                             padx=(10, 8), pady=5)
            e = ttk.Entry(box1, textvariable=var, width=38,
                          show=show if show else "")
            e.grid(row=i, column=1, sticky="we", pady=5)
            if show:
                cb = tk.BooleanVar()
                ttk.Checkbutton(box1, text="显示", variable=cb,
                                command=lambda v=var, c=cb, w=e:
                                w.configure(show="" if c.get() else "●")
                                ).grid(row=i, column=2, padx=6)
        box1.columnconfigure(1, weight=1)

        # --- DeepSeek
        box2 = ttk.LabelFrame(root, text=" DeepSeek ")
        box2.pack(fill="x", padx=18, pady=6)
        self.v_key = tk.StringVar()
        ttk.Label(box2, text="密钥").grid(row=0, column=0, sticky="e",
                                          padx=(10, 8), pady=6)
        ek = ttk.Entry(box2, textvariable=self.v_key, width=38, show="●")
        ek.grid(row=0, column=1, sticky="we", pady=6)
        cbk = tk.BooleanVar()
        ttk.Checkbutton(box2, text="显示", variable=cbk,
                        command=lambda: ek.configure(
                            show="" if cbk.get() else "●")
                        ).grid(row=0, column=2, padx=6)
        box2.columnconfigure(1, weight=1)
        ttk.Label(box2, text="在 platform.deepseek.com 生成，sk- 开头",
                  foreground="#9a9a9a").grid(row=1, column=1, sticky="w",
                                             pady=(0, 6))

        # --- 运行
        box3 = ttk.LabelFrame(root, text=" 运行 ")
        box3.pack(fill="x", padx=18, pady=6)

        self.v_interval = tk.StringVar(value="1")
        self.v_maxtok = tk.StringVar(value="2000")
        self.v_think = tk.StringVar(value=THINK_OFF)
        self.v_mode = tk.StringVar(value="control")
        self.v_search = tk.IntVar(value=1)
        self.v_auto = tk.IntVar(value=0)
        self.v_guard = tk.IntVar(value=0)

        ttk.Label(box3, text="检查频率").grid(row=0, column=0, sticky="e",
                                              padx=(10, 8), pady=5)
        ttk.Spinbox(box3, from_=1, to=60, width=5,
                    textvariable=self.v_interval).grid(row=0, column=1,
                                                       sticky="w", pady=5)
        ttk.Label(box3, text="分钟一次（1~60，越短登录越频繁）",
                  foreground="#9a9a9a").grid(row=0, column=2, sticky="w", padx=6)

        ttk.Label(box3, text="回答上限").grid(row=0, column=3, sticky="e",
                                              padx=(18, 8))
        ttk.Spinbox(box3, from_=200, to=8000, increment=100, width=7,
                    textvariable=self.v_maxtok).grid(row=0, column=4,
                                                     sticky="w", pady=5)
        ttk.Label(box3, text="token（越大越长、越费钱）",
                  foreground="#9a9a9a").grid(row=0, column=5, sticky="w", padx=6)

        ttk.Label(box3, text="模式").grid(row=1, column=0, sticky="e",
                                          padx=(10, 8), pady=5)
        mf = ttk.Frame(box3)
        mf.grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(mf, text="仅控制", value="control",
                        variable=self.v_mode).pack(side="left")
        ttk.Radiobutton(mf, text="全功能", value="full",
                        variable=self.v_mode).pack(side="left", padx=(14, 0))

        ttk.Label(box3, text="仅控制：只认指令，不聊天不答题　｜　全功能：还能问答、聊天",
                  foreground="#9a9a9a").grid(row=2, column=1, columnspan=2,
                                             sticky="w", pady=(0, 6))

        self.cb_search = ttk.Checkbutton(
            box3, text=self._search_label(),
            variable=self.v_search, command=self._on_search_toggle)
        self.cb_search.grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 6))

        self.cb_auto = ttk.Checkbutton(
            box3, text=self._auto_label(),
            variable=self.v_auto, command=self._on_auto_toggle)
        self.cb_auto.grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 6))

        self.cb_guard = ttk.Checkbutton(
            box3, text=self._guard_label(),
            variable=self.v_guard, command=self._on_guard_toggle)
        self.cb_guard.grid(row=5, column=1, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Label(box3, text="思考强度").grid(row=6, column=0, sticky="e",
                                              padx=(10, 8), pady=5)
        ttk.Combobox(box3, textvariable=self.v_think, state="readonly", width=18,
                     values=THINK_CHOICES).grid(row=6, column=1, columnspan=2,
                                                sticky="w", pady=5)
        ttk.Label(box3, text="开思考更准，但更慢也更费钱",
                  foreground="#9a9a9a").grid(row=6, column=3, columnspan=3,
                                             sticky="w", padx=6)

        # --- 按钮
        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=18, pady=(10, 4))
        ttk.Button(bar, text="测试连接", command=self.on_test).pack(side="left")
        ttk.Button(bar, text="保存", command=self.on_save).pack(side="left", padx=8)
        ttk.Button(bar, text="保存并测试",
                   command=lambda: (self.on_save(), self.on_test())
                   ).pack(side="left")
        ttk.Button(bar, text="提示词…",
                   command=self.on_edit_prompt).pack(side="left", padx=8)
        tk.Button(bar, text="全部关闭退出", command=self.on_quit_all,
                  bg="#b3261e", fg="white", activebackground="#8c1d18",
                  activeforeground="white", relief="flat", padx=12, pady=2,
                  cursor="hand2",
                  font=("Microsoft YaHei UI", 9)).pack(side="right")
        ttk.Button(bar, text="关闭窗口", command=self.destroy).pack(side="right", padx=8)

        ttk.Label(root,
                  text="关闭窗口不会停用遥控器；要停用请双击桌面「云笔记遥控器 开关」",
                  foreground="#9a9a9a").pack(anchor="w", padx=18, pady=(0, 6))

        # --- 输出
        ttk.Label(root, text="状态").pack(anchor="w", padx=18)
        self.txt = tk.Text(root, height=12, bg="#141414", fg="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat",
                           font=("Consolas", 9), wrap="word")
        self.txt.pack(fill="both", expand=True, padx=18, pady=(2, 12))
        self.log("就绪。填好上面的两项，点「测试连接」验证，再点「保存」。")

    # ---------------- 数据
    def _load(self):
        c = load_cred()
        self.v_user.set(c.get("userName", ""))
        self.v_pass.set(c.get("password", ""))
        self.v_api.set(c.get("apiBase", DEFAULT_API))
        self.v_key.set(load_key())
        cf = load_conf()
        self.v_interval.set(str(cf.get("interval_min", 1)))
        self.v_maxtok.set(str(cf.get("max_tokens") or 2000))
        self.v_think.set(THINK_BACK.get(str(cf.get("thinking") or "off"), THINK_OFF))
        self.v_mode.set(cf.get("mode", "control"))
        self.v_search.set(1 if cf.get("web_search", True) else 0)
        self.v_auto.set(1 if autostart_on() else 0)
        self.v_guard.set(1 if guard_on() else 0)
        # 回填完要重画文字（否则还停在 _build 时的初始「关」）
        for cb, fn in ((getattr(self, "cb_search", None), self._search_label),
                       (getattr(self, "cb_auto", None), self._auto_label),
                       (getattr(self, "cb_guard", None), self._guard_label)):
            try:
                if cb is not None:
                    cb.configure(text=fn())
            except Exception:
                pass
        if c.get("userName"):
            self.log("已读取现有配置（账号 %s，每 %d 分钟检查，%s）"
                     % (c.get("userName"), cf.get("interval_min", 1),
                        "仅控制" if cf.get("mode") == "control" else "全功能"))

    def _collect(self):
        return {
            "apiBase": self.v_api.get().strip().rstrip("/"),
            "userName": self.v_user.get().strip(),
            "password": self.v_pass.get(),
        }

    def log(self, msg):
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")

    def _ui(self, fn):
        self.q.put(fn)

    def _poll(self):
        try:
            while True:
                fn = self.q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _guard_label(self):
        return "系统保护：%s（每小时检查一次，没开着就自动拉起）" % (
            "开" if self.v_guard.get() else "关")

    def _on_guard_toggle(self):
        on = bool(self.v_guard.get())
        ok, info = set_guard(on)
        if not ok:
            self.log("！系统保护没设置成功：%s" % info)
            self.v_guard.set(0 if on else 1)
        else:
            self.log("[OK] 系统保护：%s（%s）" % ("已开启" if on else "已关闭", info))
        try:
            self.cb_guard.configure(text=self._guard_label())
        except Exception:
            pass

    def _auto_label(self):
        return "开机自动启动：%s" % ("开" if self.v_auto.get() else "关")

    def _on_auto_toggle(self):
        on = bool(self.v_auto.get())
        ok, info = set_autostart(on)
        if not ok:
            self.log("！开机自启动没设置成功：%s" % info)
            self.v_auto.set(0 if on else 1)
        else:
            self.log("[OK] 开机自启动：%s（%s）" % ("已开启" if on else "已关闭", info))
        try:
            self.cb_auto.configure(text=self._auto_label())
        except Exception:
            pass

    def _search_label(self):
        """文字里直接写明开/关 —— 不依赖那个小方块的渲染
        （某些 Windows 主题下未勾选的框会画成一个叉，很容易误读）"""
        on = bool(self.v_search.get())
        return "联网搜索：%s　（发「搜索 xxx」时用，每次约 5 厘钱）" % ("开" if on else "关")

    def _on_search_toggle(self):
        try:
            self.cb_search.configure(text=self._search_label())
        except Exception:
            pass

    # ---------------- 运行状态
    def _status_text(self):
        try:
            st = os.path.join(BASE, "control_state.json")
            with open(st, encoding="utf-8") as f:
                d = json.load(f)
            lc = (d.get("lastCheck") or "").strip()
            if not lc:
                return "还没跑过一轮"
            t = datetime.datetime.strptime(lc, "%Y-%m-%dT%H:%M:%S")
            gap = (datetime.datetime.now() - t).total_seconds()
            try:
                n = max(1, int(float(self.v_interval.get())))
            except Exception:
                n = 1
            nxt = t + datetime.timedelta(minutes=n)
            s = "上次巡检 %s（%s前）　·　下次约 %s　·　每 %d 分钟" % (
                t.strftime("%m-%d %H:%M:%S"), _ago(gap),
                nxt.strftime("%H:%M:%S"), n)
            if gap > max(180, n * 180):
                s += "　⚠ 好像停了"
            return s
        except Exception:
            return "读不到运行状态（可能还没跑过）"

    def _tick_status(self):
        try:
            txt = self._status_text()
            self.lb_status.configure(
                text=txt,
                fg="#ff9d9d" if "好像停了" in txt else "#8fe0a8")
        except Exception:
            pass
        self.after(5000, self._tick_status)

    # ---------------- 动作
    def on_save(self):
        c = self._collect()
        if not c["userName"] or not c["password"]:
            self.log("！账号和密码都要填")
            return
        if not c["apiBase"]:
            self.log("！服务器地址要填（向服务提供方索取，形如 https://xxx）")
            return
        key = self.v_key.get().strip()
        try:
            os.makedirs(BASE, exist_ok=True)
            with open(CRED, "w", encoding="utf-8") as f:
                json.dump(c, f, ensure_ascii=False, indent=2)
            with open(KEYF, "w", encoding="utf-8") as f:
                f.write("# DeepSeek API key（由设置向导写入）\n")
                f.write((key or "REPLACE_ME") + "\n")
            self.log("已保存到：")
            self.log("  " + CRED)
            self.log("  " + KEYF)
            if not key:
                self.log("！DeepSeek 密钥没填，「点 xxx」和聪明一点的指令会用不了")
        except Exception as e:
            self.log("！保存失败：%r" % (e,))

        # 运行设置（频率 + 模式）
        try:
            n = max(1, min(60, int(float(self.v_interval.get()))))
        except Exception:
            n = 1
        mode = self.v_mode.get() or "control"
        try:
            cur = load_conf()               # 先读原配置，别把别的字段冲掉
            cur["mode"] = mode
            cur["interval_min"] = n
            cur["web_search"] = bool(self.v_search.get())
            try:
                cur["max_tokens"] = max(200, min(8000,
                                                 int(float(self.v_maxtok.get()))))
            except Exception:
                cur["max_tokens"] = 2000
            cur["thinking"] = THINK_MAP.get(self.v_think.get(), "off")
            with open(CONF, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            self.log("运行设置：每 %d 分钟检查一次，%s"
                     % (n, "仅控制" if mode == "control" else "全功能"))
        except Exception as e:
            self.log("！运行设置保存失败：%r" % (e,))
            return
        self._apply_interval(n)

    def _pyw(self):
        """当前解释器对应的无窗版本（python.exe → pythonw.exe），不写死路径"""
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            cand = exe[:-len("python.exe")] + "pythonw.exe"
            if os.path.exists(cand):
                return cand
        return exe

    def _apply_interval(self, minutes):
        """把检查频率**和脚本路径**一起写进计划任务。

        ⚠️ 打包成 exe 后，exe 旁边没有 control_bot.py（它只是设置界面），
        此时**只改频率、不动路径** —— 否则会把计划任务指到一个不存在的地方，
        直接把正在用的遥控器弄坏。
        """
        frozen = getattr(sys, "frozen", False)
        if frozen:
            ps = (
                '$t = New-ScheduledTaskTrigger -Once -At "2026-10-05T15:15:00" '
                '-RepetitionInterval (New-TimeSpan -Minutes %d);'
                'Set-ScheduledTask -TaskName "%s" -Trigger $t | Out-Null'
                % (minutes, TASK_NAME))
        else:
            py = os.path.join(HERE, "control_bot.py")
            ps = (
                '$exe = "%s";'
                '$py = "%s";'
                '$t = New-ScheduledTaskTrigger -Once -At "2026-10-05T15:15:00" '
                '-RepetitionInterval (New-TimeSpan -Minutes %d);'
                '$a = New-ScheduledTaskAction -Execute $exe -Argument (\'-B ""\' + $py + \'""\');'
                'Set-ScheduledTask -TaskName "%s" -Trigger $t -Action $a | Out-Null'
                % (self._pyw(), py, minutes, TASK_NAME))
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                capture_output=True, timeout=90, creationflags=NO_WINDOW)
            if r.returncode == 0:
                if frozen:
                    self.log("[OK] 检查频率已设为每 %d 分钟（路径保持不变）" % minutes)
                else:
                    self.log("[OK] 计划任务已同步：每 %d 分钟，脚本路径已指向当前目录"
                             % minutes)
            else:
                err = (r.stderr or b"").decode("mbcs", "replace").strip()
                self.log("！同步计划任务失败：%s" % (err[:220] or "未知原因"))
                self.log("  配置已存下，下次保存会再试一次")
        except Exception as e:
            self.log("！同步计划任务失败：%r" % (e,))

    def on_edit_prompt(self):
        """二级窗口：编辑「全功能」模式的聊天提示词"""
        import control_bot as _CB
        default = _CB.CHAT_SYSTEM

        win = tk.Toplevel(self)
        win.title("编辑全功能提示词")
        win.geometry("680x560")
        win.configure(bg="#1e1e1e")
        try:
            win.transient(self)
        except Exception:
            pass

        ttk.Label(
            win, justify="left",
            text=("这段提示词只在「全功能」模式下用于聊天和问答。\n"
                  "「仅控制」模式不调用模型，用不到它。\n"
                  "留空（或与默认一致）= 使用内置默认。"),
            foreground="#9a9a9a").pack(anchor="w", padx=16, pady=(14, 8))

        box = tk.Frame(win, bg="#161616")
        box.pack(fill="both", expand=True, padx=16)
        txt = tk.Text(box, wrap="word", bg="#161616", fg="#e8e8e8",
                      insertbackground="#8fe0a8", relief="flat",
                      font=("Microsoft YaHei UI", 10), padx=10, pady=8,
                      undo=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        cur = str(load_conf().get("prompt_full") or "").strip()
        txt.insert("1.0", cur or default)

        lb = ttk.Label(win, text="", foreground="#8fe0a8")
        lb.pack(anchor="w", padx=16, pady=(6, 0))

        def _count(_evt=None):
            n = len(txt.get("1.0", "end").strip())
            lb.configure(text="当前 %d 字%s" % (n, "" if n else "（空 = 用内置默认）"))
        txt.bind("<KeyRelease>", _count)
        _count()

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16, pady=12)

        def _save():
            val = txt.get("1.0", "end").strip()
            cfg = load_conf()
            if not val or val == default.strip():
                cfg.pop("prompt_full", None)
                self.log("[OK] 提示词已恢复为内置默认")
            else:
                cfg["prompt_full"] = val
                self.log("[OK] 提示词已保存（%d 字），下一轮生效" % len(val))
            save_conf(cfg)
            win.destroy()

        def _reset():
            txt.delete("1.0", "end")
            txt.insert("1.0", default)
            _count()

        ttk.Button(bar, text="恢复默认", command=_reset).pack(side="left")
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="保存", command=_save).pack(side="right", padx=8)
        try:
            win.grab_set()
        except Exception:
            pass

    def on_quit_all(self):
        """红色按钮：连托盘一起关掉（真的全停）"""
        from tkinter import messagebox

        if not messagebox.askyesno(
                "全部关闭退出",
                "要完全停止遥控器吗？\n\n"
                "· 托盘图标消失，后台巡检停止\n"
                "· 平板上的指令不会再被处理\n"
                "· 这个窗口也会关掉\n\n"
                "（只想关窗口的话，请点「关闭窗口」）",
                parent=self):
            return

        if not getattr(sys, "frozen", False):
            # 源码模式下 sys.executable 是 python.exe，按名字杀会连别的 Python 一起带走
            messagebox.showinfo(
                "源码模式",
                "源码模式下不能自动停止托盘（会误杀其它 Python 进程）。\n\n"
                "请到托盘图标右键「退出」，或关掉启动它的黑色窗口。",
                parent=self)
            self.destroy()
            return

        me = os.getpid()
        target = os.path.basename(sys.executable)
        killed = 0
        for pid in _pids_of(target):
            if pid == me:
                continue                       # 别把自己先杀了，窗口还要用来收尾
            try:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=10,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    killed += 1
            except Exception:
                pass
        try:
            import control_bot as _CB
            _CB.log("设置界面：全部关闭退出，已结束 %d 个进程" % killed)
        except Exception:
            pass
        self.destroy()

    def on_test(self):
        c = self._collect()
        key = self.v_key.get().strip()
        if not c["userName"] or not c["password"]:
            self.log("！先填账号和密码")
            return
        self.log("开始测试…")
        threading.Thread(target=self._worker, args=(c, key), daemon=True).start()

    def _worker(self, c, key):
        def out(m):
            self._ui(lambda: self.log(m))

        # 1) 云笔记登录
        try:
            import zy_client
            cli = zy_client.build_client(c)
            out("[OK] 云笔记登录成功")
        except Exception as e:
            out("[失败] 云笔记登录：%s" % (str(e)[:180]))
            return

        # 2) 读指令文件夹（按名字解析，不写死 ID）
        cdir = None
        try:
            import watch_channel as W
            import control_bot as _CB
            cdir = _CB.resolve_control_dir(cli)
            items = W.folder_items(cli, cdir)
            out("[OK] 指令文件夹可读（%d 条子项）" % len(items))
        except Exception as e:
            out("[失败] 读取文件夹：%s" % (str(e)[:160]))

        # 3) 发一份指令手册到平板 —— 既是说明书，也顺便证明「回执真能发出去」
        if cdir:
            try:
                import send_text
                import control_bot as _CB
                res = send_text.send_block(
                    _CB.report_help(),
                    title="遥控器说明书",
                    name="【连接测试】遥控器指令手册",
                    parent=cdir)
                out("[OK] 说明书已发到平板（%s）"
                    % str(res.get("fileId") or "")[:12])
            except Exception as e:
                out("[注意] 说明书没发出去：%s" % (str(e)[:160]))

        # 4) DeepSeek 余额
        if not key:
            out("[跳过] DeepSeek：没填密钥")
            return
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Accept": "application/json",
                         "Authorization": "Bearer " + key})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            infos = d.get("balance_infos") or []
            if infos:
                b = infos[0]
                out("[OK] DeepSeek 余额：%s %s"
                    % (b.get("currency"), b.get("total_balance")))
            else:
                out("[注意] DeepSeek 返回里没有余额信息")
        except Exception as e:
            out("[失败] DeepSeek 余额：%s" % (str(e)[:160]))

        out("测试结束。")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
