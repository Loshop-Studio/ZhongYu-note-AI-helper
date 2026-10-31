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


CONFIG_SCHEMA = 2          # 必须和 control_bot.CONFIG_SCHEMA 一致
MODE_NAMES = {"control": "仅控制", "chat": "普通模式", "full": "全功能"}

def load_conf():
    d = {"mode": "control", "interval_min": 1}
    try:
        with open(CONF, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            d.update(got)
    except Exception:
        pass
    # 与 control_bot.migrate_config() 保持一致：
    # 老版本的 "full" 真实含义是「关键词动作 + 聊天兜底」，也就是现在的
    # 「普通模式(chat)」。这里只做内存迁移（不写盘，避免和常驻进程抢文件），
    # 但保存时会把这个迁移结果落下去。
    try:
        ver = int(d.get("config_schema") or 0)
    except Exception:
        ver = 0
    if ver < CONFIG_SCHEMA and str(d.get("mode") or "").lower() == "full":
        d["mode"] = "chat"
    if d.get("mode") not in ("control", "full", "chat"):
        d["mode"] = "control"
    try:
        d["interval_min"] = max(1, min(60, int(d.get("interval_min") or 1)))
    except Exception:
        d["interval_min"] = 1
    return d


THINK_OFF = "关（最快最省）"
THINK_LOW = "低（快一些）"
THINK_MAX = "最高（最强最慢，不限长度）"
THINK_CHOICES = (THINK_OFF, THINK_LOW, "标准", THINK_MAX)
THINK_MAP = {THINK_OFF: "off", THINK_LOW: "low", "标准": "high", THINK_MAX: "max"}
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


# ---------------------------------------------------------------- 系统保护（守护）
# 具体实现在 app/guard.py —— 设置界面和遥控器指令共用同一份，
# 免得两边各写一遍，最后出现「界面说开着、指令说关着」这种割裂。
from guard import (guard_on, set_guard,            # noqa: E402,F401
                   GUARD_TASK, GUARD_FLAG, GUARD_EVERY_MIN)


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


class SymbolCheck(tk.Frame):
    """用方块字符当勾选框：选中 ■、未选 □。

    为什么不用 ttk.Checkbutton：`clam` 主题把选中标记画成一个**大叉 ✗**，
    又丑又看不清（用户 2026-10-31 反馈"这个 X 也太厉害了吧"）。
    ttk 的 indicator 形状由主题决定，改不了，所以干脆自己画。
    """

    def __init__(self, parent, text, variable, command=None, wraplength=330):
        bg = parent.cget("bg") if "bg" in parent.keys() else "#1e1e1e"
        super().__init__(parent, bg=bg)
        self.var = variable
        self._cmd = command
        self._bg = bg
        self.mark = tk.Label(self, text="", bg=bg,
                             font=("Microsoft YaHei UI", 12), cursor="hand2")
        self.mark.pack(side="left", anchor="n")
        # wraplength：窗口只有 600 出头，长说明（比如联网搜索那条）靠换行消化
        self.lab = tk.Label(self, text=text, bg=bg, fg="#e6e6e6", anchor="w",
                            justify="left", wraplength=wraplength,
                            font=("Microsoft YaHei UI", 10), cursor="hand2")
        self.lab.pack(side="left", padx=(5, 0))
        for w in (self, self.mark, self.lab):
            w.bind("<Button-1>", self._toggle)
        # ⚠️ 必须监听变量变化：ttk.Checkbutton 会自动跟着 variable 走，
        #    自己画的 Label 不会 —— 不监听的话 `v.set(1)` 之后方块还停在 □
        #    （2026-10-31 实测：文字显示"开"、方块却是空的）。
        try:
            self.var.trace_add("write", lambda *_a: self._refresh())
        except Exception:
            pass
        self._refresh()

    def _refresh(self):
        on = bool(self.var.get())
        # ■/□ 在雅黑里都有字形，不会变方块乱码（比 ✔ 靠谱）
        self.mark.configure(text="■" if on else "□",
                            fg="#4ec9b0" if on else "#8a8a8a")

    def _toggle(self, _e=None):
        self.var.set(0 if self.var.get() else 1)
        self._refresh()
        if self._cmd:
            self._cmd()

    def set_text(self, text):
        """只改文字，勾选标记照旧（替代 Checkbutton 的 configure(text=)）"""
        self.lab.configure(text=text)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("云笔记遥控器 · 设置")
        # 宽度保持和原来一样（600 出头），内容长了靠**换行 + 滚动**消化，
        # 不靠把窗口拉宽（用户 2026-10-31："宽度太厉害了，跟原来一样，多换换行"）。
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = min(620, max(560, sw - 200))
        h = max(520, min(900, sh - 120))
        self.geometry("%dx%d" % (w, h))
        self.minsize(560, 460)
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
        st.configure("TEntry", fieldbackground="#2b2b2b", foreground="#e6e6e6",
                     insertcolor="#e6e6e6", selectbackground="#264f78")
        # ⚠️ TEntry 的样式**不会**自动套到 Spinbox / Combobox 上。
        #    不单独设的话它们用 clam 默认的浅灰底，在深色主题里几乎看不清字
        #    （用户 2026-10-31 截图反馈"文字都看不见"）。
        for _sty in ("TSpinbox", "TCombobox"):
            st.configure(_sty, fieldbackground="#2b2b2b", foreground="#e6e6e6",
                         background="#343434", arrowcolor="#d8d8d8",
                         insertcolor="#e6e6e6", bordercolor="#4a4a4a",
                         lightcolor="#4a4a4a", darkcolor="#4a4a4a")
        # readonly 状态的 Combobox 用的是另一套颜色，要单独映射，否则是灰底黑字
        st.map("TCombobox",
               fieldbackground=[("readonly", "#2b2b2b")],
               foreground=[("readonly", "#e6e6e6")],
               selectbackground=[("readonly", "#2b2b2b")],
               selectforeground=[("readonly", "#e6e6e6")])
        # 下拉弹出的列表不走 ttk style，只能设进 option 数据库
        self.option_add("*TCombobox*Listbox.background", "#2b2b2b")
        self.option_add("*TCombobox*Listbox.foreground", "#e6e6e6")
        self.option_add("*TCombobox*Listbox.selectBackground", "#264f78")
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        pad = {"padx": 18, "pady": 4}
        # ---- 滚动容器 ----------------------------------------------------
        # 内容实测需要 709×975，而小屏幕（或系统开了 125%/150% 缩放）往往只有
        # 800 出头的高度，直接 pack 会把底部的按钮裁掉（用户 2026-10-31 反馈"有遮挡"）。
        # 所以整页塞进 Canvas，高度不够就滚动，怎么都能看全。
        outer = ttk.Frame(self)
        self._canvas = tk.Canvas(outer, bg="#1e1e1e", highlightthickness=0,
                                 bd=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        root = ttk.Frame(self._canvas)
        self._canvas.create_window((0, 0), window=root, anchor="nw", tags="inner")
        root.bind("<Configure>", self._sync_scroll)
        # 画布自己变宽时也要重新同步（否则只靠 root 的 Configure 永远等不到）
        self._canvas.bind("<Configure>", self._sync_scroll)
        self._canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.after_idle(self._sync_scroll)

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
                                              padx=(10, 8), pady=(8, 1))
        ttk.Spinbox(box3, from_=1, to=60, width=6,
                    textvariable=self.v_interval).grid(row=0, column=1,
                                                       sticky="w", pady=(8, 1))
        ttk.Label(box3, text="分钟一次（1~60，越短登录越频繁）", foreground="#9a9a9a",
                  wraplength=420).grid(row=1, column=0, columnspan=5,
                                       sticky="w", padx=(24, 10), pady=(0, 6))

        ttk.Label(box3, text="回答上限").grid(row=2, column=0, sticky="e",
                                              padx=(10, 8), pady=(4, 1))
        ttk.Spinbox(box3, from_=200, to=8000, increment=100, width=8,
                    textvariable=self.v_maxtok).grid(row=2, column=1,
                                                     sticky="w", pady=(4, 1))
        ttk.Label(box3, text="token（越大越长、越费钱）", foreground="#9a9a9a",
                  wraplength=420).grid(row=3, column=0, columnspan=5,
                                       sticky="w", padx=(24, 10), pady=(0, 6))

        ttk.Label(box3, text="模式").grid(row=4, column=0, sticky="e",
                                          padx=(10, 8), pady=(6, 1))
        mf = ttk.Frame(box3)
        mf.grid(row=4, column=1, columnspan=4, sticky="w", pady=(6, 1))
        ttk.Radiobutton(mf, text="仅控制", value="control",
                        variable=self.v_mode).pack(side="left")
        ttk.Radiobutton(mf, text="普通模式", value="chat",
                        variable=self.v_mode).pack(side="left", padx=(16, 0))
        ttk.Radiobutton(mf, text="全功能", value="full",
                        variable=self.v_mode).pack(side="left", padx=(16, 0))
        ttk.Label(box3, text="全功能 = agent 模式，一句话能组合多个动作\n（更费钱、慢几秒）",
                  foreground="#9a9a9a").grid(
                      row=5, column=0, columnspan=5, sticky="w",
                      padx=(24, 10), pady=(0, 8))

        self.cb_search = SymbolCheck(
            box3, self._search_label(), self.v_search, self._on_search_toggle)
        self.cb_search.grid(row=6, column=1, columnspan=4, sticky="w", pady=4)

        self.cb_auto = SymbolCheck(
            box3, self._auto_label(), self.v_auto, self._on_auto_toggle)
        self.cb_auto.grid(row=7, column=1, columnspan=4, sticky="w", pady=4)

        self.cb_guard = SymbolCheck(
            box3, self._guard_label(), self.v_guard, self._on_guard_toggle)
        self.cb_guard.grid(row=8, column=1, columnspan=4, sticky="w", pady=4)

        ttk.Label(box3, text="思考强度").grid(row=9, column=0, sticky="e",
                                              padx=(10, 8), pady=(8, 1))
        ttk.Combobox(box3, textvariable=self.v_think, state="readonly", width=22,
                     values=THINK_CHOICES).grid(row=9, column=1, columnspan=2,
                                                sticky="w", pady=(8, 1))
        ttk.Label(box3, text="越强越准也越慢越贵；选「最高」时不限回答长度\n看图始终不思考",
                  foreground="#9a9a9a").grid(
                      row=10, column=0, columnspan=5, sticky="w",
                      padx=(24, 10), pady=(0, 8))

        # --- 按钮：固定在窗口底部，不跟着内容滚
        # ⚠️ 必须**先 pack 底部按钮、最后 pack 滚动区**，否则 expand 的滚动区
        #    会先吃掉全部空间，按钮就被挤出去看不见了。
        # 危险操作放最底下一行、字号小一号、靠右 —— 别让它长得像普通按钮。
        bar3 = ttk.Frame(self)
        bar3.pack(side="bottom", fill="x", padx=18, pady=(2, 8))
        tk.Button(bar3, text="全部关闭退出", command=self.on_quit_all,
                  bg="#8c1d18", fg="#f0c4c0", activebackground="#b3261e",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  padx=8, pady=1, cursor="hand2",
                  font=("Microsoft YaHei UI", 8)).pack(side="right")

        bar2 = ttk.Frame(self)
        bar2.pack(side="bottom", fill="x", padx=18, pady=(4, 0))
        ttk.Button(bar2, text="关闭窗口", command=self.destroy).pack(side="left")

        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=18, pady=(8, 0))
        ttk.Button(bar, text="测试连接", command=self.on_test).pack(side="left")
        ttk.Button(bar, text="保存", command=self.on_save).pack(side="left", padx=6)
        ttk.Button(bar, text="保存并测试",
                   command=lambda: (self.on_save(), self.on_test())
                   ).pack(side="left")
        ttk.Button(bar, text="提示词…",
                   command=self.on_edit_prompt).pack(side="left", padx=6)

        # 最后才让滚动区占满剩下的空间
        outer.pack(fill="both", expand=True)

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
                    cb.set_text(fn())
            except Exception:
                pass
        if c.get("userName"):
            self.log("已读取现有配置（账号 %s，每 %d 分钟检查，%s）"
                     % (c.get("userName"), cf.get("interval_min", 1),
                        MODE_NAMES.get(cf.get("mode"), "仅控制")))

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

    def _sync_scroll(self, _e=None):
        """内容尺寸变化时刷新滚动范围，并让内层跟随画布宽度。

        ⚠️ 必须在画布**已经布局**（宽度 > 100）时才写 width，
           否则首次触发拿到的是 1，会把整页挤成 1px 宽（内容全看不见）。
        """
        try:
            cw = self._canvas.winfo_width()
            if cw > 100:
                self._canvas.itemconfigure("inner", width=cw)
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except Exception:
            pass

    def _on_wheel(self, e):
        try:
            self._canvas.yview_scroll(int(-e.delta / 120), "units")
        except Exception:
            pass

    def _poll(self):
        try:
            while True:
                fn = self.q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _guard_label(self):
        return ("系统保护：%s\n每 %d 分钟检查，挂了自动拉起" 
                % ("开" if self.v_guard.get() else "关", GUARD_EVERY_MIN))

    def _on_guard_toggle(self):
        on = bool(self.v_guard.get())
        ok, info = set_guard(on)
        if not ok:
            self.log("！系统保护没设置成功：%s" % info)
            self.v_guard.set(0 if on else 1)
        else:
            self.log("[OK] 系统保护：%s（%s）" % ("已开启" if on else "已关闭", info))
        try:
            self.cb_guard.set_text(self._guard_label())
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
            self.cb_auto.set_text(self._auto_label())
        except Exception:
            pass

    def _search_label(self):
        """文字里直接写明开/关 —— 不依赖那个小方块的渲染
        （某些 Windows 主题下未勾选的框会画成一个叉，很容易误读）"""
        on = bool(self.v_search.get())
        return ("联网搜索：%s\n（发「/搜索 xxx」时用，每次约 5 厘钱）"
                % ("开" if on else "关"))

    def _on_search_toggle(self):
        try:
            self.cb_search.set_text(self._search_label())
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
            cur["config_schema"] = CONFIG_SCHEMA   # 标记已是新结构，别再迁移
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
                     % (n, MODE_NAMES.get(mode, "仅控制")
                        + ("（agent 模式，每条指令都会调 DeepSeek）"
                           if mode == "full" else "")))
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
        """二级窗口：编辑「普通模式」下聊天用的提示词"""
        import control_bot as _CB
        default = _CB.CHAT_SYSTEM

        win = tk.Toplevel(self)
        win.title("编辑聊天提示词")
        win.geometry("680x560")
        win.configure(bg="#1e1e1e")
        try:
            win.transient(self)
        except Exception:
            pass

        ttk.Label(
            win, justify="left",
            text=("这段提示词只在「普通模式」下聊天时用。\n"
                  "「仅控制」完全不调模型；「全功能(agent)」用的是另一套内置调度提示词。\n"
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
                "⚠ 确认全部关闭？",
                "这一步会把遥控器**彻底停掉**，不是关窗口：\n\n"
                "· 托盘图标消失，后台巡检停止\n"
                "· 平板上发指令不会再有任何反应\n"
                "· 这个设置窗口也会一起关掉\n\n"
                "确认要停，就点「是」。\n"
                "只是想关掉这个窗口的话，点「关闭窗口」就行。",
                parent=self, icon="warning", default="no"):
            self.log("已取消「全部关闭退出」")
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
