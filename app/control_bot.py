#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
control_bot.py —— 云笔记「控制」遥控器

干什么：
  扫「猫cat」频道下的「控制」子文件夹，**按【笔记名】认指令**，执行完把结果写回同一文件夹。
  全程**零 AI、零积分**：只用本地进程检查 / 本地数据库 / Python 标准库，
  连云端只用 zy_client（登录）+ send_text.py（渲染上传）。

为什么这么设计：
  - 用户在学校只有中育学习平板，唯一能用的双向通道就是云笔记；
  - 指令写成【笔记名】是因为他在平板上习惯这么写；
  - 只读「控制」子文件夹 → 和问答频道彻底隔离，接力链看不见，不会互相污染。

用法：
    python control_bot.py             # 扫一次（正常由 Windows 计划任务调用）
    python control_bot.py --dry       # 只打印将要发的内容，不写回云端
    python control_bot.py --force     # 忽略"已处理"记录，强制重跑最新一条
    python control_bot.py -h          # 命令行参数说明（指令字典在云笔记里发「帮助」）
"""

import argparse
import ctypes
import datetime
import json
import os
import re
import shutil
import base64
import datetime
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ------------------------------------------------------------------ 常量

# ⚠️ 这里**不能**写死文件夹 ID（开源要让别人也能用）。
# 运行时由 resolve_control_dir() 按名字找；找不到就用父目录。
CONTROL_DIR = None
CONTROL_SUBDIR_NAME = "控制"      # 指令所在的子文件夹名，可通过 config.json 的 control_subdir 改
_LAST_DIR_ERROR = ""              # 上次定位指令文件夹失败的原因（给告警笔记用）
DIRFAIL_MARKER = "dirfail.marker"  # 告警节流标记文件
REPLY_PREFIX = "【电脑】"                            # 我们自己发的回执，必须跳过，否则会自我循环
def _pick_python():
    """挑一个同时有 pycryptodome 和 Pillow 的解释器。

    2026-10-17：不再写死路径。当前进程能用就用自己的（打包成 exe 后 sys.executable 就是它），
    否则退回系统 Python314。
    """
    try:
        import Crypto  # noqa: F401
        import PIL     # noqa: F401
        return sys.executable
    except Exception:
        pass
    for p in (r"C:\Program Files\Python314\python.exe",
              os.path.join(HERE, ".venv", "Scripts", "python.exe")):
        if os.path.exists(p):
            return p
    return sys.executable


PY = _pick_python()

_CHILD_FILES = {
    "send_text": "send_text.py",
    "shot_page": "_shot_page.py",
    "dsc_click": "dsc_click.py",
}


def _child_cmd(name, extra):
    """构造子进程命令。

    ⚠️ 打包成 exe 后，子脚本不再是独立文件，必须让 exe 自己分发（main.py 的 --child）。
    否则会变成 `云笔记遥控器.exe -B send_text.py ...` → argparse exit(2)，
    表现是「指令能处理，回执全发不出去」（2026-10-18 实际踩到）。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--child", name] + list(extra)
    return [PY, "-B", os.path.join(HERE, _CHILD_FILES[name])] + list(extra)
# ⚠️ 子进程一律不弹控制台。
# 计划任务用 pythonw（无窗）启动本脚本，但子进程是 python.exe（控制台程序），
# Windows 会给它分配一个新的黑窗口 —— 截图时正好把它拍进去（2026-10-17 用户发现）。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
def _find_workbuddy():
    """找 WorkBuddy.exe。

    这里故意**不写死用户名**：早期版本写的是 `C:\\Users\\<某个人>\\...`，
    换台机器就跑不了，而且把本机用户名带进了源码。
    """
    cands = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, "Programs", "WorkBuddy", "WorkBuddy.exe"))
    for key in ("ProgramFiles", "ProgramFiles(x86)"):
        p = os.environ.get(key)
        if p:
            cands.append(os.path.join(p, "WorkBuddy", "WorkBuddy.exe"))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return cands[0] if cands else "WorkBuddy.exe"


WORKBUDDY_EXE = _find_workbuddy()
ACCEL_PORTS = (7897, 7890, 10809, 10808)
MAX_HISTORY = 300                                   # 状态文件里最多记多少条已处理


def _base_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ZhongYuToolBox")


def state_path():
    return os.path.join(_base_dir(), "control_state.json")


def log_path():
    return os.path.join(_base_dir(), "control_bot.log")


def log(msg):
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ------------------------------------------------------------------ 指令识别

# 指令表：顺序即优先级；同义词写宽松点，用户怎么写都能认出来
CMDS = [
    ("dashboard", ("仪表盘", "控制台", "遥控器设置", "看设置", "看配置", "面板")),
    ("help", ("帮助", "说明", "字典", "怎么用", "咋用", "咋弄", "菜单", "指令", "help",
              "忘了", "有哪些", "能干什么", "怎么操作", "怎么使", "啥意思", "不明白")),
    ("calib", ("校准", "设定余额", "设置余额", "余额设为")),
    ("dsbal", ("deepseek", "ds余额", "api余额", "api账户", "api钱")),
    ("quota", ("额度", "剩余", "余额", "还能撑", "还能用多久", "够花", "钱还")),
    ("daily", ("日报", "开销", "花了多少", "消耗", "统计", "账")),
    ("shot", ("截图", "屏幕", "看看电脑", "在干嘛", "在干什么", "拍张")),
    ("click", ("点一下", "点击", "点开", "戳", "click")),
    ("restart", ("重启", "拉起来", "救一下", "复活", "唤醒")),
    ("nolink", ("补链", "接力", "修一下", "重建", "继续跑")),
    ("state", ("状态", "情况", "怎么样", "咋样", "还好吗", "在吗", "正常吗",
               "体检", "报告", "活着吗", "还在吗", "挂了没")),
]


def norm(s):
    """把笔记名规范化：去掉空白、包裹符号、标点，方便宽松匹配。"""
    s = str(s or "")
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.strip("【】[]()（）<>《》「」『』\"'“”‘’:：?？!！。.,，、-_/\\|~～*#")
    return s.lower()


def match_cmd(name):
    """返回指令名 / None"""
    n = norm(name)
    if not n:
        return None
    if n.startswith("思考") or "思考强度" in n or "思考模式" in n:
        return "set_think"
    # WorkBuddy 开/关（放在词表之前，别被「关闭」「开启」这类通用词抢走）
    # ⚠️ 兼容用户常写的 workbody（拼写变体）
    if any(k in n for k in ("开启workbuddy", "开启workbody", "打开workbuddy",
                            "打开workbody", "启动workbuddy", "启动workbody")):
        return "wb_start"
    if any(k in n for k in ("关闭workbuddy", "关闭workbody", "关掉workbuddy",
                            "关掉workbody", "退出workbuddy", "退出workbody",
                            "停止workbuddy", "停止workbody")):
        return "wb_stop"
    # 联网搜索的开关（平板也能发）
    if any(k in n for k in ("关闭搜索", "关掉搜索", "禁用搜索", "停用搜索")):
        return "search_off"
    if any(k in n for k in ("开启搜索", "打开搜索", "启用搜索", "允许搜索")):
        return "search_on"
    # ⚠️ 「搜索 xxx」必须**最先**判断：
    #    否则「帮我搜 DeepSeek」里的 deepseek 会先命中 dsbal（余额）被抢走。
    if n.startswith(("帮我搜索", "帮我搜一下", "帮我搜", "搜索一下", "搜一下",
                     "搜索", "搜", "查一下", "查查")):
        return "search"
    for cmd, words in CMDS:
        for w in words:
            if w in n:
                return cmd
    # 模式切换 / 频率调整：放在常规词表之后判断，
    # 免得「设置」这类通用词先把它们抢走
    if "全功能" in n or "开启聊天" in n or "允许聊天" in n:
        return "mode_full"
    if "仅控制" in n or "只控制" in n or "关闭聊天" in n or "禁止聊天" in n:
        return "mode_control"
    if ("频率" in n or "间隔" in n) and pick_number(n) is not None:
        return "set_freq"
    if any(k in n for k in ("token", "字数", "长度", "回答上限")) \
            and pick_number(n) is not None:
        return "set_maxtok"
    if n.startswith("双击"):
        return "dclick"
    if n.startswith("右键"):
        return "rclick"
    # 「点保存」「点那个按钮」这种以「点」开头的，也算点击。
    # ⚠️ 特意不把单字「点」放进词表 —— 否则「现在几点」会被误判成点击。
    if n.startswith("点"):
        return "click"
    return None


def pick_number(s):
    """从笔记名里抠出第一个数字（给「校准 4800」用）"""
    m = re.search(r"(\d+(?:\.\d+)?)", str(s or ""))
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ 云端意图识别（DeepSeek）
# 本地 0.5B 模型已于 2026-10-17 按要求删除。
# 关键词认不出来时，改让 deepseek-flash 做一次**纯文本**意图分类
# （不传图，一次约 0.1 分钱）。key 从同一个文件读，不写进代码。

DS_KEY_FILE = os.path.join(_base_dir(), "deepseek_key.txt")
DS_API = "https://api.deepseek.com/v1/chat/completions"
DS_TIMEOUT = 25
DS_TOOLS = ("state", "daily", "quota", "shot", "click", "restart", "help",
            "dashboard", "mode_full", "mode_control", "set_freq")

DS_SYSTEM = """你是家庭电脑遥控器的指令分类器。根据用户在云笔记标题里写的那句话，
从下面的动作里选一个最合适的。只输出 JSON，不要任何解释：{"tool":"名字"}

动作表：
- state    查看电脑状态（CPU、内存、WorkBuddy 是否在跑）
- daily    看今天和昨天各花了多少积分
- quota    看还剩多少积分
- shot     拍一张电脑屏幕发过来
- click    点击屏幕上的某个东西
- restart  重启 WorkBuddy 程序
- help     查看指令字典
- dashboard    查看/打开设置面板（当前模式、检查频率、上次巡检时间）
- mode_full    切换到全功能模式（允许聊天、答题）
- mode_control 切换到仅控制模式（只执行指令，不聊天、不答题）
- set_freq     调整检查频率（用户在句子里说了具体几分钟）
- none     都不属于上面任何一类

如果是 click，必须带上目标，输出 {"tool":"click","target":"要点的那个东西"}，
target 要保留用户原话里的描述（例如「任务栏的浏览器图标」）。
只输出 JSON。"""


def _load_ds_key():
    """读 DeepSeek key（一行）；没填/还是占位符就返回 None"""
    try:
        with open(DS_KEY_FILE, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if (s and not s.startswith("#")
                        and s.upper() not in ("REPLACE_ME", "PASTE_HERE")
                        and len(s) >= 20):
                    return s
    except Exception:
        pass
    return None


def ds_intent(text):
    """把一句模糊中文判成指令。返回 (指令名, 目标)；失败返回 (None, None)。

    ⚠️ 只做分类，不执行任何动作 —— 分类结果仍然要走下面的白名单分支。
    """
    key = _load_ds_key()
    if not key:
        return None, None
    body = {
        "model": "deepseek-flash",
        "temperature": 0,
        "max_tokens": 120,
        "thinking": {"type": "disabled"},     # 分类不需要思考，关掉更快更省
        "messages": [{"role": "system", "content": DS_SYSTEM},
                     {"role": "user", "content": str(text)[:200]}],
    }
    try:
        req = urllib.request.Request(
            DS_API, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连
        with op.open(req, timeout=DS_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        txt = (msg.get("content") or "").strip() \
            or (msg.get("reasoning_content") or "").strip()
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            return None, None
        d = json.loads(m.group(0))
        tool = d.get("tool")
        if tool not in DS_TOOLS:
            return None, None
        return tool, (str(d.get("target") or "").strip() or None)
    except Exception as e:
        log("DeepSeek 意图识别失败 %r" % (e,))
        return None, None


CLICK_WORDS = ("双击", "右键单击", "右键", "点一下", "点击", "点开",
               "戳一下", "戳", "click", "点")

def click_target(name):
    """从「点 保存按钮」里抠出「保存按钮」"""
    t = str(name or "")
    for w in CLICK_WORDS:
        i = t.find(w)
        if i >= 0:
            t = t[i + len(w):]
            break
    t = re.sub(r"^[\s:：,，、\-—]+", "", t).strip()
    return t


# ------------------------------------------------------------------ 运行模式（仅控制 / 全功能）
# 配置存在 %LOCALAPPDATA%\ZhongYuToolBox\config.json
#   {"mode": "control" | "full", "interval_min": 1..60}
# 仅控制：只认白名单动作，其他话一律拒绝（不聊天、不答题）
# 全功能：认不出来的话交给 DeepSeek 回答，可以聊天、问问题

def config_path():
    return os.path.join(_base_dir(), "config.json")


DEFAULT_CONFIG = {"mode": "control", "interval_min": 1, "web_search": True}


def load_config():
    d = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), encoding="utf-8") as f:
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


def _notify_dir_failure(cli, name, interval=3600):
    """定位不到指令文件夹时，往**根目录**发一条告警笔记。

    · 为什么发根目录：连指令文件夹都没有，没有别的地方可发
    · 为什么节流：每轮都是同一个错误，不掐住会每分钟刷一条（默认 1 小时一条）
    """
    marker = os.path.join(_base_dir(), DIRFAIL_MARKER)
    try:
        if os.path.exists(marker) and (time.time() - os.path.getmtime(marker)) < interval:
            return False
    except Exception:
        pass
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        open(marker, "a").close()
    except Exception:
        pass

    now = datetime.datetime.now().strftime("%m%d-%H%M")
    detail = _LAST_DIR_ERROR or "（没拿到更具体的原因）"
    text = "\n".join([
        "# 配置失败：找不到指令文件夹",
        "",
        "程序在云笔记里找不到名为「%s」的文件夹，自动创建也没成功。" % name,
        "",
        "【报错】",
        detail,
        "",
        "【后果】",
        "现在它**什么都不做** —— 你发的指令不会被处理。",
        "（它不会退回去读整个根目录，否则会把普通笔记当成指令。）",
        "",
        "【怎么修】",
        "1. 在云笔记里手动新建一个叫「%s」的文件夹（和这条笔记放在同一层）" % name,
        "2. 然后回电脑上打开设置界面，点「测试连接」看更详细的报错",
        "",
        "—— 这条发在根目录，因为已经不知道该往哪发了。",
        "（同类告警 1 小时内只发一次，修好后会自动恢复）",
    ])
    try:
        send_back("配置失败", "%s配置失败-%s" % (REPLY_PREFIX, now), text, parent="0")
        log("已在根目录发出「配置失败」告警")
        return True
    except Exception as e:
        log("发送「配置失败」告警也失败了 %r" % (e,))
        return False


def save_config(d):
    """写回 config.json（保留其他字段）"""
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log("写配置失败 %r" % (e,))
        return False


def _list_children(cli, parent_id):
    """列某文件夹的直接子项（含子文件夹，type=0）"""
    q = cli.enc("parentid=%s&isNoteNode=true" % parent_id)
    text = cli._authed_get(
        cli.api_base + "/CloudNotes/api/Notes/GetByParentId?" + q, timeout=60)
    inner = json.loads(cli.dec(json.loads(text)["data"]))
    return inner.get("noteList", []) or []


def _clear_dirfail():
    """定位成功后清掉告警节流标记，下次再出事能立刻通知"""
    try:
        p = os.path.join(_base_dir(), DIRFAIL_MARKER)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def resolve_control_dir(cli, force=False):
    """定位「指令文件夹」；**找不到就返回 None，绝不退回根目录**。

    ⚠️ 2026-10-17 血的教训：
    早先的版本「找不到就退回父目录」，而父目录默认是根目录 ——
    结果把用户几千条普通笔记全当成指令处理，一轮误发了 8 条垃圾回执。
    **宁可这一轮什么都不做，也绝不能乱认笔记。**

    查找顺序：
      1. config.json 里的 control_dir（直接给 ID，最准）
      2. work_dir（默认根目录）下名为 control_subdir（默认「控制」）的子文件夹
      3. 都没有 → None
    """
    global CONTROL_DIR, _LAST_DIR_ERROR
    if CONTROL_DIR and not force:
        return CONTROL_DIR
    cfg = load_config()

    cached = str(cfg.get("control_dir") or "").strip()
    if cached:
        CONTROL_DIR = cached
        _LAST_DIR_ERROR = ""
        _clear_dirfail()
        log("指令文件夹：用配置里的 control_dir = %s" % cached)
        return cached

    parent = str(cfg.get("work_dir") or "0")
    name = str(cfg.get("control_subdir") or CONTROL_SUBDIR_NAME).strip()
    try:
        items = _list_children(cli, parent)
    except Exception as e:
        _LAST_DIR_ERROR = "列父目录失败（parentId=%s）：%r" % (parent, e)
        log(_LAST_DIR_ERROR)
        return None
    for it in items:
        if it.get("type") == 0 and (it.get("fileName") or "").strip() == name:
            CONTROL_DIR = it.get("fileId")
            _LAST_DIR_ERROR = ""
            _clear_dirfail()
            log("指令文件夹：%s → %s" % (name, CONTROL_DIR))
            return CONTROL_DIR

    # 找不到就**自动建一个**：第一次用的人不该卡在「文件夹不存在」这一步
    try:
        import zy_upload as U
        fid = U.create_folder(cli, name, parent)
        CONTROL_DIR = fid
        _LAST_DIR_ERROR = ""
        _clear_dirfail()
        log("没有「%s」文件夹，已自动创建 → %s" % (name, fid))
        cfg["control_dir"] = fid
        save_config(cfg)
        return fid
    except Exception as e:
        _LAST_DIR_ERROR = ("找到父目录（parentId=%s），但没有名为「%s」的子文件夹；"
                           "自动创建也失败：%r" % (parent, name, e))
        log("自动创建「%s」失败：%r" % (name, e))
        log("本轮跳过 —— 不会退回去读父目录，避免把普通笔记当成指令")
        return None


CHAT_SYSTEM = """你是家里那台电脑上的小助手。用户在学校，用平板通过云笔记给你留言。

回答要求：
· 直接给结论，别客套：不要用「好的」「当然可以」「我理解」这类开头
· 长度按需要来：简单问题一两句说清；知识点、解题、分析这类要展开就写完整，
  不要为了凑字数硬扯，也不要为了短把关键步骤砍掉
· 用短段落和分点，别写成一大坨；关键公式、结论单独占一行
· 不要用 emoji
· 遇到危险请求（删文件、关机、改密码、付款），直接说做不到，让他回家自己弄"""


THINK_LEVELS = ("off", "high", "max")
THINK_LABELS = {"off": "关（最快最省）", "high": "标准", "max": "最高（最强也最慢）"}


def thinking_value():
    """思考强度：off / high / max。

    DeepSeek 的 low、medium 都会被映射成 high，所以只保留三档，免得选项看着多其实重复。
    ⚠️ 思考模式下 temperature 不生效（不报错，但也不起作用）。
    """
    try:
        v = str(load_config().get("thinking") or "off").lower()
    except Exception:
        v = "off"
    return v if v in THINK_LEVELS else "off"


def _parse_think(name):
    """从「思考 最高」这类标题里解析强度"""
    s = str(name or "").lower()
    if any(k in s for k in ("关", "off", "不思考", "禁用")):
        return "off"
    if any(k in s for k in ("max", "最高", "最强")):
        return "max"
    return "high"          # 只说「思考」「思考 高」都算标准档


def max_tokens_value():
    """回答长度上限：读 config.max_tokens，默认 2000，夹在 200~8000"""
    try:
        v = int(load_config().get("max_tokens") or 2000)
    except Exception:
        v = 2000
    return max(200, min(8000, v))


def chat_system():
    """全功能模式实际用的聊天提示词。

    优先读 config.json 的 prompt_full（用户在设置里自己写的），
    没写或写空就退回上面的内置默认。
    """
    try:
        p = str(load_config().get("prompt_full") or "")
        if p.strip():
            return p
    except Exception:
        pass
    return CHAT_SYSTEM


DS_SEARCH_API = "https://api.deepseek.com/v1/tools/search"


def ds_search(query, count=5):
    """调 DeepSeek Search API（2026-10-16 上线）。返回 (items, 错误信息)。

    计费：0.005 元/次成功请求；空结果/失败不计费。
    返回单条字段：title / site_name / authority / rerank_score / date / url /
                  summary（约 600~800 字，给模型读）/ snippet / content
    """
    key = _load_ds_key()
    if not key:
        return None, "还没有配置 DeepSeek key"
    body = {"query": str(query)[:120], "count": max(1, min(10, int(count)))}
    try:
        req = urllib.request.Request(
            DS_SEARCH_API, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return None, repr(e)
    items = data.get("results") or []
    if not items:
        return None, "搜索没有返回结果"
    return items, ""


def format_search(query, items, limit=5):
    """把搜索结果排成一张能直接看的图（不做模型总结，避免它编）"""
    lines = ["# 搜索：%s" % query, ""]
    for i, it in enumerate(items[:limit], 1):
        title = (it.get("title") or "无标题").strip()
        lines.append("%d. %s" % (i, title))
        s = (it.get("summary") or it.get("snippet") or "").strip()
        if s:
            lines.append("   " + re.sub(r"\s+", " ", s)[:200])
        site = (it.get("site_name") or "").strip()
        dt = (it.get("date") or "").strip()[:10]
        tail = "　".join(x for x in (site, dt) if x)
        if tail:
            lines.append("   （%s）" % tail)
        lines.append("")
    lines.append("—— 来自 DeepSeek 搜索，共 %d 条" % min(len(items), limit))
    return "\n".join(lines)


def pick_query(name):
    """从「搜索 xxx」里抠出 xxx（保留原始空格，搜索词需要）"""
    s = str(name or "").strip()
    s = re.sub(r"^[【\[(（]*", "", s).strip()
    for p in ("帮我搜索", "帮我搜一下", "帮我搜", "搜索一下", "搜一下", "搜索", "搜",
              "查一下", "查查"):
        if s.startswith(p):
            return s[len(p):].strip(" ：:，,。")
    return s.strip(" ：:，,。")


VISION_HINTS = ("看图", "解题", "这道题", "这道", "做题", "题目", "这题",
                "图片", "照片", "识别", "拍题")


def scan_root_vision(cli, done):
    """扫**根目录**里「标题带看图意图 + 有图片」的笔记。

    为什么需要：云笔记里新建笔记默认就落在根目录，用户经常直接把题目照片
    建在根目录，而程序只扫「控制」子文件夹 → 表现为「发图没反应」。
    条件卡得很死（标题必须含看图词 + 必须真的有图），避免误伤学习笔记。
    """
    out = []
    try:
        for it in _list_children(cli, "0"):
            if it.get("type") == 0:          # 文件夹，跳过
                continue
            nm = str(it.get("fileName") or "").strip()
            if not nm or not any(h in nm for h in VISION_HINTS):
                continue
            fid = it.get("fileId")
            if not fid or fid in done:
                continue
            urls = pick_image_urls(cli, fid)
            if urls:
                out.append((fid, nm, urls))
    except Exception as e:
        log("扫描根目录看图笔记失败 %r" % (e,))
    return out


def _split_image(raw, max_side=1000, max_parts=3):
    """把大图切成 n×n 块，返回切块后的 JPEG bytes 列表（小图返回空列表）。

    为什么要切：DeepSeek 会把每张图缩到约 800×800（每张最多 384 token）。
    实测一张 2176×2929 的题目照片，压缩后清晰度只剩 27%，小字必糊。
    切成 3×3 后每块约 725×976，缩放后清晰度约 82% —— 字终于看得清了。
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        side = max(w, h)
        if side <= max_side:
            return []
        n = max(2, min(max_parts, (side + max_side - 1) // max_side))
        out = []
        for r in range(n):
            for c in range(n):
                piece = img.crop((c * w // n, r * h // n,
                                  (c + 1) * w // n, (r + 1) * h // n))
                buf = io.BytesIO()
                piece.save(buf, "JPEG", quality=88)
                out.append(buf.getvalue())
        return out
    except Exception as e:
        log("图片切片失败 %r" % (e,))
        return []


def pick_image_urls(cli, file_id, limit=3):
    """取笔记里**真正的图片**地址。

    ⚠️ 一条笔记的资源里混着 7 个官方模板（*.bin，几十字节）和一张 screenshot.png 占位图，
       直接拿第一个会得到 36 字节的二进制 → 模型报 400 unsupported image。
       所以按扩展名过滤，并且把模板占位图排除掉，页面图（resourceType=0）排最前。
    """
    exts = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    got = []
    try:
        for r in cli.get_resources(file_id):
            u = (r.get("ossImageUrl") or "").strip()
            if not u.startswith("http"):
                continue
            name = u.split("?")[0].rstrip("/").split("/")[-1].lower()
            if not name.endswith(exts):
                continue
            if name == "screenshot.png":          # 官方模板占位图，不是内容
                continue
            try:
                t = int(r.get("resourceType") or 0)
            except Exception:
                t = 0
            got.append((t, u))
    except Exception as e:
        log("取图片资源失败 %r" % (e,))
    got.sort(key=lambda x: x[0])                  # type=0（页面图）优先
    return [u for _, u in got[:limit]]


def do_vision(urls, question, max_images=3):
    """看图回答（拍照解题）。

    ⚠️ 模型就用 `deepseek-flash` —— 它是 V4.1 Flash，原生多模态；
       旧的 deepseek-v4-flash-vision-exp 已下线并路由到它。
       图片走 base64 data URL，最多带 3 张（超过 48MiB 请求体限制会失败）。
    """
    key = _load_ds_key()
    if not key:
        return "# 没配 DeepSeek 密钥\n\n看图解题要用它，回电脑上打开设置填一下。"
    parts = [{"type": "text",
              "text": ((question or "看看这张图").strip() +
                       "\n\n如果是题目，请给出完整解题过程，步骤写清楚；"
                       "如果是文字或表格，请提取并整理成可读的内容。")}]
    def _part(raw):
        if raw[:2] == b"\xff\xd8":
            mime = "image/jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif raw[:4] == b"RIFF":
            mime = "image/webp"
        else:
            mime = "image/jpeg"
        return {"type": "image_url",
                "image_url": {"url": "data:%s;base64,%s"
                              % (mime, base64.b64encode(raw).decode())}}

    got = 0
    sliced = 0
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for u in urls[:max_images]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with op.open(req, timeout=60) as r:
                raw = r.read()
            if len(raw) > 12 * 1024 * 1024:
                log("图片太大（%.1fMB），跳过" % (len(raw) / 1048576.0))
                continue
            parts.append(_part(raw))          # 整图：看整体布局
            got += 1
            for piece in _split_image(raw):   # 切片：看小字细节
                parts.append(_part(piece))
                sliced += 1
        except Exception as e:
            log("下载图片失败 %r（%s）" % (e, u[:60]))
    if not got:
        return "# 图片没下载下来\n\n可能是网络问题，或者图片地址失效了。"
    if sliced:
        parts[0]["text"] += ("\n\n说明：第一张是整图（用来看整体布局），"
                             "后面 %d 张是同一张图放大后的局部切片（用来看小字细节）。"
                             "请结合两者来读，题目一道都别漏。" % sliced)

    body = {"model": "deepseek-flash", "max_tokens": 1500,
            "messages": [{"role": "user", "content": parts}]}
    try:
        req = urllib.request.Request(
            DS_API, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        with op.open(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        txt = (msg.get("content") or "").strip()
        if not txt:
            return "# 模型没给出内容\n\n这张图可能读不出来，换张清楚点的再试。"
        u = data.get("usage") or {}
        log("看图回答：%d 张图，%s tokens" % (got, u.get("total_tokens")))
        return txt
    except Exception as e:
        log("看图失败 %r" % (e,))
        return "# 看图失败\n\n%s" % repr(e)


CHAT_HISTORY = os.path.join(_base_dir(), "chat_history.json")
CHAT_MAX_TURNS = 8          # 保留最近 8 轮（16 条消息）
CHAT_HISTORY_CHARS = 6000   # 历史总字数上限，超了从最老的开始丢


def load_history():
    """读**今天**的聊天历史。跨天（日期变了）自动清空。"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    try:
        with open(CHAT_HISTORY, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") != today:
            log("聊天历史跨天，已清空（昨天 %s）" % d.get("date"))
            return []
        msgs = d.get("messages")
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def save_history(msgs):
    """写回历史：只留最近 CHAT_MAX_TURNS 轮，且总字数不超过上限"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    keep = list(msgs)[-CHAT_MAX_TURNS * 2:]
    # 从最老的开始丢，直到总字数达标
    total = sum(len(str(m.get("content") or "")) for m in keep)
    while total > CHAT_HISTORY_CHARS and len(keep) > 2:
        drop = keep.pop(0)
        total -= len(str(drop.get("content") or ""))
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        with open(CHAT_HISTORY, "w", encoding="utf-8") as f:
            json.dump({"date": today, "messages": keep}, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log("保存聊天历史失败 %r" % (e,))


def ds_chat(question):
    """全功能模式下的聊天/问答。返回回答文本；失败返回 None。

    带**当天**的上下文记忆：历史存 chat_history.json，跨天自动清空。
    """
    key = _load_ds_key()
    if not key:
        return None
    lvl = thinking_value()
    hist = load_history()
    msgs = ([{"role": "system", "content": chat_system()}]
            + hist[-CHAT_MAX_TURNS * 2:]
            + [{"role": "user", "content": str(question)[:500]}])
    body = {
        "model": "deepseek-flash",
        "max_tokens": max_tokens_value(),   # 可在设置界面或用「字数 3000」调整
        "messages": msgs,
    }
    if lvl == "off":
        body["thinking"] = {"type": "disabled"}
        body["temperature"] = 0.7           # 只有非思考模式 temperature 才生效
    else:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = "max" if lvl == "max" else "high"
    try:
        req = urllib.request.Request(
            DS_API, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        txt = (msg.get("content") or "").strip() \
            or (msg.get("reasoning_content") or "").strip()
        if txt:
            # 只把「用户问题 + 最终回答」存进历史（思维链不回传，省 token）
            hist.append({"role": "user", "content": str(question)[:500]})
            hist.append({"role": "assistant", "content": txt})
            save_history(hist)
        return txt or None
    except Exception as e:
        log("DeepSeek 聊天失败 %r" % (e,))
        return None


def report_control_only(word):
    return "\n".join([
        "# 现在只是「仅控制」模式",
        "",
        "你写的是：「%s」" % word,
        "",
        "这个模式下我只管操作电脑，不聊天、不回答问题。",
        "",
        "能用的：",
        "  状态 / 日报 / 额度 / DeepSeek余额 / 截图 / 点 xxx / 重启 / 帮助",
        "  仪表盘（看当前设置）/ 频率 3（改检查间隔）",
        "",
        "想聊天或问问题，直接发一条「全功能」就能切过去，",
        "不用回电脑上操作。",
    ])



# ================================================================== 安全护栏
# 这个遥控器是**白名单执行器**：它只能做下面固定的几件事，
# 永远**不会执行你写的任何系统命令**（没有 eval / 没有 shell / 没有"运行任意程序"入口）。
# 即便如此，还是在识别之前先拦一道危险请求 —— 关键词和本地 AI 都可能认错，
# 绝不能因为"认错了"就去做危险的事。

DANGEROUS_RULES = [
    # (说明, 命中词)
    ("删除或格式化文件", ("删除", "删掉", "删了", "删文件", "删了东西", "清空", "清碟",
                  "格式化", "format", "rmdir", "erase", "恢复出厂", "重装系统", "抹掉")),
    ("关机或重启整台电脑", ("关机", "shutdown", "poweroff", "重启电脑", "重启计算机",
                   "reboot", "关掉电脑", "断电", "帮我重启")),
    ("改账号、密码或注册表", ("密码", "账号", "帐号", "登录", "注册表", "regedit",
                     "bios", "管理员权限", "提权")),
    ("安装、卸载或运行程序", ("装软件", "装个", "装一下", "安装", "卸载", "删软件",
                     "运行cmd", "运行exe", "运行脚本", "运行程序", "运行命令",
                     "执行命令", "执行cmd", "powershell", "打开程序", "启动程序",
                     "脚本", "bat文件", "exe文件", "跑个程序")),
    ("碰摄像头、麦克风或录音", ("摄像头", "麦克风", "录音", "录像", "拍照", "前置镜头")),
    ("动钱或下单", ("付款", "支付", "下单", "转账", "充值", "花呗", "信用卡", "付钱")),
    ("往外发东西（发消息/邮件/发帖）", ("发消息", "发邮件", "邮件", "发帖", "微博", "朋友圈",
                          "发给他", "告诉别人", "通知别人")),
    ("改开机启动项或系统服务", ("开机启动", "启动项", "自启动", "启动服务", "系统服务",
                       "计划任务")),
]

# 抓屏的时间窗（窗外不抓，避免在你睡觉/不在时把屏幕内容发上云端）
SHOT_WINDOW = (7, 23)      # 07:00 ~ 23:00


def _in_shot_window():
    h = datetime.datetime.now().hour
    a, b = SHOT_WINDOW
    return (a <= h < b) if a <= b else (h >= a or h < b)


def check_dangerous(text):
    """命中危险词就返回 (True, 原因)；否则 (False, None)"""
    t = str(text or "").lower()
    for reason, words in DANGEROUS_RULES:
        for w in words:
            if w.lower() in t:
                return True, reason
    return False, None


def refusal_text(what, reason):
    return "\n".join([
        "# 这条我没做",
        "",
        "你说的是：「%s」" % what,
        "",
        "**「%s」被列进了禁止清单，我不会执行。**" % reason,
        "",
        "这套遥控器只认这几件事：",
        "  状态 / 日报 / 额度 / 截图 / 点 xxx / 重启 / 帮助 / 校准 数字",
        "",
        "它**没有**执行任意命令的能力（没有终端、没有脚本、不会装删任何东西），",
        "所以危险的事它做不到。",
        "",
        "（如果你确实需要某个操作，下来单独跟我说，我人工帮你做。）",
    ])


# ------------------------------------------------------------------ 本地体检

class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def proc_count():
    """数一数 WorkBuddy / EasyClaw 各有几个进程（纯 ctypes，不依赖 powershell）

    ⚠️ EasyClaw 要分开看：`easyclawsrv.exe` 只是后台服务（壳），
    真正的引擎是 `easyclaw.exe`。只有壳在跑 = 干不了活。
    """
    out = {"workbuddy": 0, "claw_srv": 0, "claw_app": 0}
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                n = (pe.szExeFile or "").lower()
                if "workbuddy" in n:
                    out["workbuddy"] += 1
                elif "claw" in n:
                    if "srv" in n:
                        out["claw_srv"] += 1
                    else:
                        out["claw_app"] += 1
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
    except Exception as e:
        log("proc_count 失败 %r" % (e,))
    return out


def port_open(port, timeout=1.2):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


# ------------------------------------------------------------------ 数据库（复制后只读）

def _db_copy():
    """把 WorkBuddy 数据库复制到临时目录再读，绝不碰原库。失败返回 None"""
    try:
        src = os.path.join(os.environ.get("USERPROFILE", ""), ".workbuddy")
        dst = os.path.join(os.environ.get("TEMP", "."), "_ctrl_wbdb")
        os.makedirs(dst, exist_ok=True)
        for fn in ("workbuddy.db", "workbuddy.db-wal", "workbuddy.db-shm"):
            p = os.path.join(src, fn)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(dst, fn))
        return os.path.join(dst, "workbuddy.db")
    except Exception as e:
        log("复制数据库失败 %r" % (e,))
        return None


def last_automation_run_ms(db):
    """所有自动化里最近一次的运行时间（毫秒时间戳），查不到返回 None"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        cur = con.cursor()
        cur.execute("SELECT MAX(last_run_at) FROM automation_runtime_state")
        v = cur.fetchone()[0]
        con.close()
        return v
    except Exception as e:
        log("读 last_run_at 失败 %r" % (e,))
        return None


def today_usage(db):
    """今天所有后台自动化会话的 (轮数, 积分, token)"""
    lo = int(datetime.datetime.now().replace(hour=0, minute=0, second=0,
                                             microsecond=0).timestamp() * 1000)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        cur = con.cursor()
        cur.execute("""
            SELECT COALESCE(u.used,0), u.credit_json
            FROM sessions s LEFT JOIN session_usage u ON u.session_id = s.id
            WHERE s.is_background_automation = 1 AND s.created_at >= ?""", (lo,))
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        log("读用量失败 %r" % (e,))
        return (0, 0.0, 0)

    n = len(rows)
    tok = 0
    cred = 0.0
    for used, cj in rows:
        tok += used or 0
        if cj:
            try:
                cred += sum(v for v in json.loads(cj).values()
                            if isinstance(v, (int, float)))
            except Exception:
                pass
    return (n, cred, tok)


def usage_between(db, d1, d2):
    """[d1, d2) 之间的后台自动化消耗总积分（给日报/额度估算用）"""
    if not db:
        return 0.0
    lo = int(d1.timestamp() * 1000)
    hi = int(d2.timestamp() * 1000)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        cur = con.cursor()
        cur.execute("""SELECT u.credit_json FROM sessions s
                       LEFT JOIN session_usage u ON u.session_id = s.id
                       WHERE s.is_background_automation = 1
                         AND s.created_at >= ? AND s.created_at < ?""", (lo, hi))
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        log("usage_between 失败 %r" % (e,))
        return 0.0
    tot = 0.0
    for (cj,) in rows:
        if cj:
            try:
                tot += sum(v for v in json.loads(cj).values()
                           if isinstance(v, (int, float)))
            except Exception:
                pass
    return tot


def _ago(ms):
    if not ms:
        return "查不到"
    try:
        t = datetime.datetime.fromtimestamp(int(ms) / 1000)
        mins = (datetime.datetime.now() - t).total_seconds() / 60.0
        if mins < 1:
            return "刚刚"
        if mins < 60:
            return "%d 分钟前" % int(mins)
        return "%.1f 小时前（%s）" % (mins / 60.0, t.strftime("%m-%d %H:%M"))
    except Exception:
        return "查不到"


# ------------------------------------------------------------------ 报表内容

def _sys_usage(sample=0.5):
    """CPU / 内存占用（ctypes 调 Windows 自带 API，**零依赖**，不用装 psutil）。

    内存：GlobalMemoryStatusEx 一把拿。
    CPU ：GetSystemTimes 采两次样算差值（两次之间 sleep 0.5 秒）。
    返回 (cpu%, 内存占用%, 已用GB, 总GB)；读不到就返回 None。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt
        import time as _t

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        gb = 1024.0 ** 3

        class _FT(ctypes.Structure):
            _fields_ = [("low", wt.DWORD), ("high", wt.DWORD)]

        def _snap():
            i, k, u = _FT(), _FT(), _FT()
            ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(i), ctypes.byref(k), ctypes.byref(u))
            val = lambda f: (f.high << 32) | f.low          # noqa: E731
            return val(i), val(k), val(u)

        a = _snap()
        _t.sleep(sample)
        b = _snap()
        di, dk, du = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        busy = dk + du                                       # kernel 已含 idle
        cpu = (100.0 * (busy - di) / busy) if busy else 0.0
        return (cpu, m.dwMemoryLoad,
                (m.ullTotalPhys - m.ullAvailPhys) / gb, m.ullTotalPhys / gb)
    except Exception as e:
        log("系统占用读取失败 %r" % (e,))
        return None


# ------------------------------------------------------------------ 内存告警（主动）
MEM_ALERT_PCT = 95          # 内存占用超过这个百分比 → 主动发一条告警
MEM_ALERT_COOLDOWN = 1800   # 两条告警至少隔 30 分钟，防刷屏
MEM_TOP_N = 6               # 告警里列出占用最大的几个


def top_mem_procs(n=MEM_TOP_N):
    """占用内存最大的几个进程（同名合并）。返回 [(MB, 进程数, 名字), ...]。
    ctypes 调 Windows 自带 API，零依赖（不装 psutil）。"""
    try:
        import ctypes
        import ctypes.wintypes as wt

        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

        class _ENTRY(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        snap = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        e = _ENTRY()
        e.dwSize = ctypes.sizeof(e)
        agg = {}
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            h = k32.OpenProcess(0x1000, False, e.th32ProcessID)   # QUERY_LIMITED
            if h:
                pmc = _PMC()
                pmc.cb = ctypes.sizeof(pmc)
                if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    name = e.szExeFile.decode("gbk", "replace")
                    cur = agg.setdefault(name.lower(), [0.0, 0, name])
                    cur[0] += pmc.WorkingSetSize / 1048576.0
                    cur[1] += 1
                k32.CloseHandle(h)
            e.dwSize = ctypes.sizeof(e)
            ok = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        return sorted(agg.values(), key=lambda x: -x[0])[:n]
    except Exception as ex:
        log("进程内存读取失败 %r" % (ex,))
        return []


def mem_alert_text(usage, procs):
    """内存告警正文：占多少 + 后台占用最大的几个"""
    lines = ["# 内存告警",
             "",
             "时间：%s" % datetime.datetime.now().strftime("%m-%d %H:%M"),
             "**内存：%.0f%%**（%.1f / %.1f GB 已用）" % (usage[1], usage[2], usage[3]),
             "",
             "后台占用最大的几个任务："]
    for mb, cnt, name in procs:
        lines.append("  · %s%s —— %.0f MB" % (name, ("  ×%d" % cnt) if cnt > 1 else "", mb))
    if procs:
        lines += ["",
                  "最占的是 **%s**（%.0f MB）。" % (procs[0][2], procs[0][0]),
                  "想让它重启一下的话，在「控制」里发「重启」就行。"]
    return "\n".join(lines)


def mem_alert_if_needed(st, dry=False):
    """内存占用 ≥ MEM_ALERT_PCT 就**主动**发一条告警（带冷却，防刷屏）。
    每轮巡检（每 2 分钟）都会看一眼，但同一波超标 30 分钟内只发一条。"""
    try:
        u = _sys_usage()
        if not u or u[1] < MEM_ALERT_PCT:
            return False
        now_ts = time.time()
        if now_ts - float(st.get("memAlertAt") or 0) < MEM_ALERT_COOLDOWN:
            return False                       # 还在冷却期，别刷屏
        text = mem_alert_text(u, top_mem_procs())
        if dry:
            print("[dry-run] 内存 %.0f%% ≥ %d%%，会发一条告警" % (u[1], MEM_ALERT_PCT))
            return False
        send_back("内存告警",
                  "%s内存告警-%s" % (REPLY_PREFIX,
                                     datetime.datetime.now().strftime("%m%d-%H%M")),
                  text)
        st["memAlertAt"] = now_ts
        log("内存告警已发送（%.0f%% / %d 条进程）" % (u[1], len(top_mem_procs())))
        return True
    except Exception as e:
        log("内存告警出错（已忽略）%r" % (e,))
        return False


def report_state():
    p = proc_count()
    db = _db_copy()
    last_ms = last_automation_run_ms(db) if db else None
    rounds, cred, tok = today_usage(db) if db else (0, 0.0, 0)
    accel = next((pt for pt in ACCEL_PORTS if port_open(pt)), None)
    usage = _sys_usage()                  # CPU / 内存（采样 0.5 秒，零依赖）

    lines = []
    lines.append("# 状态报告")
    lines.append("")
    lines.append("时间：%s" % datetime.datetime.now().strftime("%m-%d %H:%M"))

    # 结论
    problems = []
    if p["workbuddy"] == 0:
        problems.append("WorkBuddy 没在跑")
    if last_ms:
        mins = (datetime.datetime.now()
                - datetime.datetime.fromtimestamp(last_ms / 1000)).total_seconds() / 60.0
        if mins > 40:
            problems.append("接力链已经 %d 分钟没动静了" % int(mins))
    else:
        problems.append("查不到接力链的运行记录")
    if not accel:
        problems.append("加速器没开")
    if usage and usage[1] >= 88:
        problems.append("内存快满了（%.0f%%）" % usage[1])

    lines.append("结论：%s" % ("⚠️ 有问题 —— " + "；".join(problems) if problems else "一切正常"))
    lines.append("")
    lines.append("【电脑这边】")
    lines.append("- WorkBuddy：%s" % ("运行中（%d 个进程）" % p["workbuddy"]
                                     if p["workbuddy"] else "没有在跑"))
    lines.append("- 接力链：最后活动 %s" % _ago(last_ms))
    lines.append("- 加速器：%s" % ("正常" if accel else "没检测到"))
    lines.append("- 今天已用：%d 轮 / %.1f 积分" % (rounds, cred))
    if usage:
        lines.append("- CPU：%.0f%%（%d 个逻辑核）" % (usage[0], os.cpu_count() or 0))
        lines.append("- 内存：%.0f%%（%.1f / %.1f GB 已用）" % (usage[1], usage[2], usage[3]))
    d = disk_line()
    if d:
        lines.append("- 磁盘：%s" % d)
    bl, spent, left = quota_left(db)
    lines.append("- 估算剩余：%s" % ("%.0f 分" % left if bl else "未校准（发「校准 5000」）"))
    lines.append("")
    lines.append("【其它】")
    if p["claw_app"]:
        claw = "运行中"
    elif p["claw_srv"]:
        claw = "只有后台服务在跑（引擎没启动，干不了活）"
    else:
        claw = "未启动"
    lines.append("- EasyClaw：%s" % claw)
    lines.append("")
    lines.append("（发「帮助」看能用哪些指令）")
    return "\n".join(lines)


def report_help():
    return "\n".join([
        "# 指令字典",
        "",
        "怎么用：在「控制」文件夹里新建一条笔记，把下面某个词写在【标题】上，"
        "等一会儿（多久看检查频率，发「仪表盘」能看到）。",
        "",
        "【看情况】",
        "· 状态  → 电脑体检：CPU / 内存占用、WorkBuddy、接力链、加速器、磁盘、今天用了多少",
        "           （内存超过 95% 它会**主动**发告警，还会列出最占内存的几个程序，不用你问）",
        "· 日报  → 今天和昨天各花了多少积分",
        "· 额度  → 估算还剩多少积分、还能撑几天（顺带显示 DeepSeek 账户余额）",
        "· DeepSeek余额 → 只看 API 账户里还有多少钱（真钱，不是积分）",
        "· 截图  → 拍一张电脑屏幕发给你（看看电脑到底在干嘛）",
        "",
        "【动一下】",
        "· 点 / 双击 / 右键 xxx  → 截屏 → AI 两段定位（整图 + 局部放大）→ 点 → 发回画面",
        "                目标随便描述：「点 任务栏的浏览器图标」「点 弹窗右上角的关闭」",
        "                点偏了就在回传的图上再来一次：「点 保存按钮 右边一点」",
        "· 重启  → **真的重启 WorkBuddy**：先把所有进程关掉，再重新启动",
        "         （有任务正在跑时会自动推迟，怕打断它）",
        "· 开启workbuddy  → 没在跑时把它启动起来（在跑就回「本来就在跑」）",
        "· 关闭workbuddy  → 把它所有进程关掉（有任务在跑会推迟）",
        "",
        "【看图 / 解题】",
        "· 把照片插进**这个文件夹里**的笔记（发在别的文件夹它看不到）",
        "  标题随便写「解这道题」就行，它会读出题面并给出步骤",
        "· 也能读截图里的文字、看图表（一次最多 3 张）",
        "",
        "【联网搜索】",
        "· 搜索 无锡今天天气  → 联网搜 5 条给你（每次约 5 厘钱）",
        "· 搜 xxx / 查一下 xxx 也能用",
        "",
        "【设置】",
        "· 校准 5000  → 告诉电脑你当前的真实余额（写一次，以后每天自动扣减）",
        "· 仪表盘  → 把设置页面的内容整理成图发给你（模式/频率/上次巡检）",
        "· 全功能  → 切到全功能模式（能聊天、能答题）",
        "· 仅控制  → 切回仅控制（只认指令，不聊天）",
        "· 频率 3  → 检查频率改成每 3 分钟（范围 1~60）",
        "· 字数 3000 → 回答长度上限（200~8000，越大越长也越费钱）",
        "· 思考 高 / 思考 关 / 思考 max → 思考强度（越强越慢也越费钱）",
        "· 开启搜索 / 关闭搜索 → 联网搜索的开关",
        "",
        "【说明】",
        "· 截图 / 点 只在 07:00~23:00 执行",
        "· 只认白名单动作，不会执行你写的任何命令",
        "· 随时发「帮助」再看这份表",
    ])


def report_dashboard():
    """仪表盘：把设置界面的关键内容整理成一张图发到平板"""
    cfg = load_config()
    st = load_state()
    n = max(1, int(cfg.get("interval_min") or 1))
    mode = ("全功能 —— 能聊天、能答题" if cfg.get("mode") == "full"
            else "仅控制 —— 只认指令，不聊天")
    lc = (st.get("lastCheck") or "").strip()
    line_lc, line_next = "还没跑过", "—"
    if lc:
        try:
            t = datetime.datetime.strptime(lc, "%Y-%m-%dT%H:%M:%S")
            gap = int((datetime.datetime.now() - t).total_seconds())
            line_lc = "%s（%d 分钟前）" % (t.strftime("%m-%d %H:%M:%S"), gap // 60)
            line_next = (t + datetime.timedelta(minutes=n)).strftime("%H:%M:%S")
            if gap > max(180, n * 180):
                line_lc += "  ⚠ 可能已停止"
        except Exception:
            line_lc = lc
    return "\n".join([
        "# 遥控器仪表盘",
        "",
        "【模式】" + mode,
        "【检查频率】每 %d 分钟" % n,
        "【回答上限】%d token" % max_tokens_value(),
        "【思考强度】" + THINK_LABELS.get(thinking_value(), "关"),
        "【上次巡检】" + line_lc,
        "【下次预计】" + line_next,
        "",
        "————————————",
        "改模式：发「全功能」或「仅控制」",
        "改频率：发「频率 3」（1~60 分钟）",
    ])


def report_unknown(word):
    return "\n".join([
        "# 没看懂",
        "",
        "你写的是：「%s」" % word,
        "",
        "现在能认的指令：",
        "  状态   日报   额度   截图   重启   帮助",
        "  校准 5000（把数字换成你的余额）",
        "",
        "写「帮助」可以看完整字典。",
    ])


# ------------------------------------------------------------------ 额度基线

# 2026-10-17：Pillow 已装进 .venv（13.0.0），不再依赖系统里的 Python314。
# 变量名保留只为少改代码，实际指向项目自己的解释器。
PY314 = PY


def baseline_path():
    return os.path.join(_base_dir(), "credit_baseline.json")


def load_baseline():
    try:
        with open(baseline_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "balance" in d:
            return d
    except Exception:
        pass
    return None


def save_baseline(balance, day=None):
    os.makedirs(_base_dir(), exist_ok=True)
    d = {"balance": float(balance),
         "day": day or datetime.datetime.now().strftime("%Y-%m-%d"),
         "setAt": datetime.datetime.now().isoformat(timespec="seconds")}
    with open(baseline_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    return d


def quota_left(db=None):
    """返回 (基线dict, 此后累计消耗, 估算剩余)；没校准时后两项为 None"""
    bl = load_baseline()
    if not bl:
        return (None, None, None)
    try:
        cal = datetime.datetime.strptime(bl["day"], "%Y-%m-%d")
    except Exception:
        return (None, None, None)
    spent = usage_between(db or _db_copy(), cal, datetime.datetime.now())
    return (bl, spent, float(bl["balance"]) - spent)


# ------------------------------------------------------------------ 各类报表

def report_daily():
    db = _db_copy()
    now = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yest = today - datetime.timedelta(days=1)
    nt, ct, _ = today_usage(db)
    cy = usage_between(db, yest, today)

    L = ["# 日报", "", "时间：%s" % now.strftime("%m-%d %H:%M"), ""]
    L.append("【今天 %s】" % today.strftime("%m-%d"))
    if nt:
        L.append("- 跑了 %d 轮，消耗 %.1f 积分（平均每轮 %.2f）" % (nt, ct, ct / nt))
    else:
        L.append("- 还没有运行记录")
    L.append("")
    L.append("【昨天 %s】" % yest.strftime("%m-%d"))
    L.append("- 消耗 %.1f 积分" % cy)
    L.append("")

    bl, spent, left = quota_left()
    L.append("【额度】")
    if bl:
        L.append("- 校准基点：%s 时为 %.0f 分" % (bl["day"], float(bl["balance"])))
        L.append("- 此后累计消耗：%.1f 分" % spent)
        L.append("- 估算剩余：%.0f 分" % left)
        if ct > 0:
            L.append("- 按今天速度，约还能撑 %.0f 天" % (left / ct))
    else:
        L.append("- 还没校准。在「控制」里新建一条笔记，标题写：**校准 5000**")
        L.append("  （数字换成你当前的真实积分余额，写一次就行）")
    return "\n".join(L)


def report_quota():
    bl, spent, left = quota_left()
    L = ["# 额度", "", "时间：%s" % datetime.datetime.now().strftime("%m-%d %H:%M"), ""]
    if not bl:
        L.append("**还没校准**，所以算不出剩余。")
        L.append("")
        L.append("在「控制」里新建一条笔记，标题写：**校准 5000**")
        L.append("（把 5000 换成你当前的真实余额，写一次即可，以后自动扣减）")
        L.append("")
        L.append("【DeepSeek 账户】")
        L.extend(ds_balance_lines())
        return "\n".join(L)
    L.append("- 校准时间：%s" % bl["day"])
    L.append("- 当时余额：%.0f 分" % float(bl["balance"]))
    L.append("- 此后累计消耗：%.1f 分" % spent)
    L.append("")
    L.append("**估算剩余：%.0f 分**" % left)
    db = _db_copy()
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cy = usage_between(db, today - datetime.timedelta(days=1), today)
    if cy > 0:
        L.append("按昨天 %.1f 分/天算，约还能撑 %.0f 天" % (cy, left / cy))
    L.append("")
    L.append("【DeepSeek 账户】")
    L.extend(ds_balance_lines())
    L.append("")
    L.append("（上面的「分」是 WorkBuddy 积分，这里是 API 账户的真钱）")
    return "\n".join(L)


def ds_balance():
    """查 DeepSeek 账户余额（真钱）。没 key / 网络不通返回 None。"""
    key = _load_ds_key()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Accept": "application/json",
                     "Authorization": "Bearer " + key})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连
        with op.open(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log("查 DeepSeek 余额失败 %r" % (e,))
        return None


def ds_balance_lines():
    """余额报告用的几行文本（给 quota / dsbal 共用）"""
    d = ds_balance()
    if d is None:
        return ["- **查不到**（key 没填、或者网络不通）"]
    infos = d.get("balance_infos") or []
    if not infos:
        return ["- **没有余额信息**"]
    out = []
    for b in infos:
        cur = b.get("currency") or "CNY"
        sym = "¥" if cur == "CNY" else "$"
        out.append("- 剩余 **%s%s**（充值 %s%s + 赠送 %s%s）"
                   % (sym, b.get("total_balance"),
                      sym, b.get("topped_up_balance"),
                      sym, b.get("granted_balance")))
    if d.get("is_available") is False:
        out.append("- ⚠️ 账户当前**不可用**（余额不足或被停用）")
    return out


def report_dsbal():
    L = ["# DeepSeek 账户",
         "",
         "时间：%s" % datetime.datetime.now().strftime("%m-%d %H:%M"),
         ""]
    L.extend(ds_balance_lines())
    L.append("")
    L.append("这是 API 账户里的**真钱**；「额度」那个报告里的是 WorkBuddy 积分，两码事。")
    return "\n".join(L)


def do_calibrate(word):
    n = pick_number(word)
    if n is None:
        return "\n".join(["# 校准失败", "",
                          "没看懂你要设多少。标题请写成：**校准 5000**",
                          "（数字换成你当前的真实积分余额）"])
    if n < 0 or n > 1000000:
        # 防手滑：写错一个数把基线搞坏
        return "\n".join(["# 校准失败", "",
                          "这个数不合理：%s" % n,
                          "",
                          "余额请填 **0 ~ 1000000** 之间的数字。",
                          "标题写成：**校准 5000** 这样。"])
    d = save_baseline(n)
    log("校准余额 = %.1f" % n)
    return "\n".join(["# 校准好了", "",
                      "- 基准日期：%s" % d["day"],
                      "- 当时余额：%.0f 分" % n,
                      "",
                      "以后每天会自动扣减实际消耗，发「额度」就能看剩余。"])


def _workbuddy_procs():
    """列出所有名字里带 workbuddy 的进程 [(pid, name)]"""
    res = []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                n = pe.szExeFile or ""
                if "workbuddy" in n.lower():
                    res.append((int(pe.th32ProcessID), n))
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
    except Exception as e:
        log("枚举 WorkBuddy 进程失败 %r" % (e,))
    return res


def do_restart():
    """真正把 WorkBuddy 重启一遍：先结束进程，再重新拉起。

    2026-10-18 改：以前只会在「没在跑」时拉起来，已经在跑就回「不用重启」，
    等于平板上的「重启」指令形同虚设。现在按用户要求做真重启。
    """
    lock = os.path.join(_base_dir(), "zy_wake.lock")
    if os.path.exists(lock):
        try:
            age = time.time() - os.path.getmtime(lock)
            if age < 300:
                return "\n".join(["# 稍等一下", "",
                                  "现在有一个任务正在跑（%.0f 秒前开始的），先不打断它。"
                                  % age,
                                  "过 5 分钟再发一次「重启」。"])
        except Exception:
            pass

    killed = []
    procs = _workbuddy_procs()
    if procs:
        for pid, nm in procs:
            try:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=30,
                                   creationflags=NO_WINDOW)
                if r.returncode == 0:
                    killed.append("%s(%d)" % (nm, pid))
            except Exception as e:
                log("结束 %s(%d) 失败 %r" % (nm, pid, e))
        log("重启：已结束 %d 个 WorkBuddy 进程 %s" % (len(killed), killed))
        time.sleep(6)                      # 等它退干净、让文件句柄释放
        left = proc_count()["workbuddy"]
        if left > 0:
            return "\n".join([
                "# 没退干净",
                "",
                "还有 %d 个 WorkBuddy 进程在（可能被保护或有权限限制）。" % left,
                "已结束的：%s" % ("、".join(killed) or "无"),
                "",
                "这种情况需要你回家手动关掉再开。",
            ])

    if not os.path.exists(WORKBUDDY_EXE):
        return "\n".join(["# 启动失败", "", "找不到程序文件：", WORKBUDDY_EXE])
    try:
        subprocess.Popen([WORKBUDDY_EXE], cwd=os.path.dirname(WORKBUDDY_EXE),
                         close_fds=True)
        log("重启：已重新启动 WorkBuddy")
    except Exception as e:
        log("启动 WorkBuddy 失败 %r" % (e,))
        return "\n".join(["# 启动失败", "", "出错了：%r" % (e,)])

    if killed:
        return "\n".join([
            "# 已重启 WorkBuddy",
            "",
            "结束了 %d 个进程：" % len(killed),
            "  · " + "\n  · ".join(killed),
            "",
            "已经重新启动，等 1~2 分钟发「状态」确认一下。",
        ])
    return "\n".join(["# 已经拉起来了", "",
                      "WorkBuddy 本来没在跑，已启动。等 1~2 分钟发「状态」确认。"])


def wb_kill_all():
    """结束所有 WorkBuddy 进程，返回成功结束的名单"""
    killed = []
    for pid, nm in _workbuddy_procs():
        try:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=30,
                               creationflags=NO_WINDOW)
            if r.returncode == 0:
                killed.append("%s(%d)" % (nm, pid))
        except Exception as e:
            log("结束 %s(%d) 失败 %r" % (nm, pid, e))
    if killed:
        log("已结束 %d 个 WorkBuddy 进程" % len(killed))
    return killed


def wb_start():
    """启动 WorkBuddy，返回 (成功, 说明)"""
    if not os.path.exists(WORKBUDDY_EXE):
        return False, "找不到程序文件：%s" % WORKBUDDY_EXE
    try:
        subprocess.Popen([WORKBUDDY_EXE], cwd=os.path.dirname(WORKBUDDY_EXE),
                         close_fds=True)
        return True, "已启动"
    except Exception as e:
        log("启动 WorkBuddy 失败 %r" % (e,))
        return False, repr(e)


def do_wb_start():
    """「开启workbuddy」：没在跑就启动，已经在跑就告诉他不用开"""
    procs = _workbuddy_procs()
    if procs:
        return "\n".join([
            "# WorkBuddy 本来就在跑",
            "",
            "当前有 %d 个进程，不用开。" % len(procs),
            "想让它重来一遍，发「重启」。",
        ])
    ok, why = wb_start()
    if not ok:
        return "\n".join(["# 启动失败", "", why])
    return "\n".join(["# 已开启 WorkBuddy", "",
                      "等 1~2 分钟发「状态」确认一下。"])


def do_wb_stop():
    """「关闭workbuddy」：结束所有 WorkBuddy 进程"""
    lock = os.path.join(_base_dir(), "zy_wake.lock")
    if os.path.exists(lock):
        try:
            age = time.time() - os.path.getmtime(lock)
            if age < 300:
                return "\n".join(["# 稍等一下", "",
                                  "现在有一个任务正在跑（%.0f 秒前开始的），"
                                  "先不关它。" % age,
                                  "过 5 分钟再发一次「关闭workbuddy」。"])
        except Exception:
            pass

    procs = _workbuddy_procs()
    if not procs:
        return "\n".join(["# WorkBuddy 本来就没开", "", "现在没有它的进程，不用关。"])
    killed = wb_kill_all()
    time.sleep(5)
    left = proc_count()["workbuddy"]
    if left > 0:
        return "\n".join([
            "# 没关干净",
            "",
            "还有 %d 个进程在（可能被保护或有权限限制）。" % left,
            "已结束的：%s" % ("、".join(killed) or "无"),
            "",
            "这种情况需要你回家手动关。",
        ])
    return "\n".join([
        "# 已关闭 WorkBuddy",
        "",
        "结束了 %d 个进程：" % len(killed),
        "  · " + "\n  · ".join(killed),
        "",
        "想再开回来，发「开启workbuddy」。",
    ])


def do_screenshot(cli, note):
    if not os.path.exists(PY314):
        return None, "找不到 Python314（截图要用它自带的 Pillow）"
    # ⚠️ 必须强制子进程走 UTF-8：它输出里带中文文件名，
    #    计划任务环境的管道默认是 cp936，按 UTF-8 解码会得到乱码路径 → 找不到文件。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(_child_cmd("shot_page", ["--raw", "--note", note]),
                           cwd=HERE, capture_output=True, timeout=240, env=env,
                           creationflags=NO_WINDOW)
    except Exception as e:
        return None, "抓屏失败：%r" % (e,)
    out = _decode_out(r.stdout)
    m1 = re.search(r"^PNG=(.+)$", out, re.M)
    m2 = re.search(r"^JPEG=(.+)$", out, re.M)
    if not (m1 and m2):
        return None, "抓屏没出图：%s" % (out[-300:] or (r.stderr or b"")[:300])
    png, jpg = m1.group(1).strip(), m2.group(1).strip()
    # 校验文件真的在（并发时可能被另一个实例回收掉）
    if not (os.path.exists(png) and os.path.exists(jpg)):
        return None, "图生成了但文件不见了（可能被并发实例回收）：%s" % png
    return (png, jpg), None


def _decode_out(raw):
    """子进程输出解码：依次试 utf-8 / mbcs / gbk，取第一个没有替换符的。

    中文 Windows 下控制台程序往管道写，默认是 cp936(GBK)；
    但脚本若自己 reconfigure 成 UTF-8 则又是 UTF-8。两种都得认。
    """
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            cand = (raw or b"").decode(enc)
        except Exception:
            continue
        if "\ufffd" not in cand:
            return cand
    return (raw or b"").decode("utf-8", "replace")


def do_click(target, button="left", double=False):
    """调 dsc_click.py：截屏 → DeepSeek 两阶段定位 → 点击。返回 (成功, 说明)。

    ⚠️ 真正的护栏在 dsc_click.py 里（时间窗 / 锁屏 / 前台危险窗口 / 只点鼠标），
    这里只负责把它叫起来并回收结果。
    """
    frozen = getattr(sys, "frozen", False)
    if not frozen:
        script = os.path.join(HERE, "dsc_click.py")
        if not os.path.exists(script):
            return False, "找不到 dsc_click.py：%s" % script
        if not os.path.exists(PY314):
            return False, "找不到 Python314（dsc_click 要用它）"
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    args = [target, "--click"]
    if button == "right":
        args += ["--button", "right"]
    if double:
        args += ["--double"]
    try:
        r = subprocess.run(_child_cmd("dsc_click", args),
                           cwd=HERE, capture_output=True, timeout=240, env=env,
                           creationflags=NO_WINDOW)
    except Exception as e:
        return False, "调用超时或失败：%r" % (e,)
    out = _decode_out(r.stdout).strip()
    err = _decode_out(r.stderr).strip()
    # 成功判据：输出里出现「点击 (」——左键/右键/双击都是这个格式
    ok = "点击 (" in out
    return ok, (out or err or "（没有任何输出）")


def disk_line():
    try:
        t, u, f = shutil.disk_usage("C:\\")
        return "C 盘剩余 %.1f GB / 共 %.1f GB" % (f / 2**30, t / 2**30)
    except Exception:
        return None


def report_nolink():
    return "\n".join([
        "# 这个不用你操心",
        "",
        "「补链 / 重启接力」这类**脚本做不了** —— 因为创建和修改定时任务是",
        "WorkBuddy 应用内部的接口，普通脚本碰不到。",
        "",
        "不过你也不需要它：",
        "· 看门狗**每小时会自动检查一次**，链子断了它自己补",
        "· 想知道现在有没有断 → 发「**状态**」，看「接力链最后活动」那一行",
        "· 如果链子真断了，状态报告会直接告诉你",
        "",
        "想知道有哪些指令，发「帮助」。",
    ])


# ------------------------------------------------------------------ 发回云端

def send_back(title, name, text, dry=False, parent=None):
    """调 send_text.py 渲染并上传（默认发到「控制」文件夹）。

    ⚠️ 一张页面图最多放得下 50 行出头，超了会被截断（帮助文档 72 行就踩过）。
       所以超过 52 行自动拆成多页发，标题带 (1/2) 这种页码。
    """
    lines = str(text).split("\n")
    if len(lines) > 52 and not dry:
        pages = [lines[i:i + 52] for i in range(0, len(lines), 52)]
        ok = True
        for idx, pg in enumerate(pages, 1):
            ok = send_back("%s（%d/%d）" % (title, idx, len(pages)),
                           "%s-P%d" % (name, idx),
                           "\n".join(pg), dry=dry, parent=parent) and ok
            time.sleep(1)
        return ok
    tmp = os.path.join(_base_dir(), "ctrl_reply.txt")
    os.makedirs(_base_dir(), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)

    cmd = _child_cmd("send_text", ["--text-file", tmp, "--title", title,
                                   "--name", name,
                                   "--parent", parent or CONTROL_DIR])
    if dry:
        print("[dry-run] 将要上传：title=%s  name=%s" % (title, name))
        print(text)
        return True

    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        env.pop(k, None)
    try:
        r = subprocess.run(cmd, cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=300,
                           creationflags=NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        ok = "已上传" in out
        log("上传 %s rc=%s ok=%s %s" % (name, r.returncode, ok, out.strip()[-200:]))
        if not ok:
            print(out[-800:])
            print(err[-800:])
        return ok
    except Exception as e:
        log("上传异常 %r" % (e,))
        return False


# ------------------------------------------------------------------ 状态文件

def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("processed"), dict):
            return d
    except Exception:
        pass
    return {"processed": {}, "lastCheck": None}


def save_state(st):
    try:
        items = list(st.get("processed", {}).items())
        if len(items) > MAX_HISTORY:
            st["processed"] = dict(items[-MAX_HISTORY:])
        os.makedirs(_base_dir(), exist_ok=True)
        tmp = state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_path())
    except Exception as e:
        log("保存状态失败 %r" % (e,))


# ------------------------------------------------------------------ 主流程

# ------------------------------------------------------------------ 保底程序体检
# 用户说的「保底程序」= 桌面那套开关：
#   本地AI 开关.bat / 启动本地AI.bat / 关闭本地AI.bat
# 2026-10-17：本地模型（llama-server + gguf）已按要求删除，这里只检查开关文件还在不在。
# 每轮巡检顺带跑一次，纯本地、零成本；只在**有问题时**写日志，正常不刷屏。

SWITCH_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "本地AI")
SWITCH_FILES = ("本地AI 开关.bat", "启动本地AI.bat", "关闭本地AI.bat")
GUARD_LOG = os.path.join(_base_dir(), "zy_local_ai_guard.log")


def guard_switch():
    """体检保底程序是否完好；有问题返回 False 并写日志"""
    missing = []
    for name in SWITCH_FILES:
        if not os.path.exists(os.path.join(SWITCH_DIR, name)):
            missing.append("开关文件 " + name)
    if not missing:
        return True
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        with open(GUARD_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] BAD 保底程序缺失：%s\n" % (
                datetime.datetime.now().strftime("%m-%d %H:%M:%S"), "、".join(missing)))
    except Exception:
        pass
    return False


def list_control_items(cli):
    """列「控制」文件夹里的子项（只读）"""
    import watch_channel as W
    return W.folder_items(cli, CONTROL_DIR)


def main():
    ap = argparse.ArgumentParser(description="云笔记「控制」遥控器")
    ap.add_argument("--dry", action="store_true", help="只打印，不上传")
    ap.add_argument("--force", action="store_true", help="忽略已处理记录，重跑最新一条")
    ap.add_argument("--daemon", action="store_true",
                    help="常驻后台：一直跑着，每隔 --interval 秒巡检一次（推荐）")
    ap.add_argument("--interval", type=float, default=90,
                    help="常驻模式下的巡检间隔秒数（默认 90）")
    ap.add_argument("--once", action="store_true", help="只跑一次就退出（兼容旧行为）")
    args = ap.parse_args()

    if args.daemon:
        return run_daemon(args)

    return run_once(args, force=args.force, dry=args.dry)


# ------------------------------------------------------------------ 常驻模式

HEARTBEAT = os.path.join(_base_dir(), "control_daemon.heartbeat")
LOCK = os.path.join(_base_dir(), "control_bot.lock")


def _pid_alive(pid):
    try:
        k = ctypes.WinDLL("kernel32")
        h = k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            k.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _acquire_lock():
    """同一时刻只允许一个实例处理指令。

    ⚠️ 2026-10-05：手动跑一次 + 5 分钟的自动任务同时触发，同一条指令被处理两遍。
    ⚠️ 2026-10-17：改成**原子创建**（O_CREAT|O_EXCL）。原来是「先 exists 再写」，
       两个实例同时启动时会双双通过检查，照样并发 —— 这就是截图抢文件的根因。
    """
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            pass
        # 锁已存在：持有者还活着就让路，死了就把锁抢过来
        try:
            with open(LOCK) as f:
                pid = int(f.read().strip())
            if pid and pid != os.getpid() and _pid_alive(pid):
                print("另一个实例正在处理（PID %d），本次不重复处理。" % pid)
                return False
        except Exception:
            pass
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True          # 锁坏了也别挡住主功能


def _release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def _another_daemon_alive(max_age=300):
    """看是否已经有常驻实例在跑（心跳够新就算）"""
    try:
        with open(HEARTBEAT) as f:
            last = float(f.read().strip())
        return (time.time() - last) < max_age
    except Exception:
        return False


def _heartbeat():
    try:
        os.makedirs(_base_dir(), exist_ok=True)
        with open(HEARTBEAT, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def run_daemon(args):
    """常驻后台：一直巡检，每隔 interval 秒一次。

    单实例保证：如果已经有另一个常驻实例在跑（心跳还新），本次直接退出。
    这样"开机自启 + 挂了自动拉起"用一个计划任务就能搞定 ——
    计划任务每 5 分钟触发一次，活着的那次秒退，死掉的那次就接手。
    """
    if _another_daemon_alive():
        print("已有常驻实例在跑，本次退出。")
        return 0

    log("常驻模式启动（每 %.0f 秒巡检一次）" % args.interval)
    print("遥控器已进入常驻模式，每 %.0f 秒巡检一次。Ctrl+C 退出。" % args.interval)

    class _A:
        dry = False
        force = False

    while True:
        try:
            _heartbeat()
            run_once(_A(), force=False, dry=False)
        except KeyboardInterrupt:
            print("\n已退出。")
            try:
                os.remove(HEARTBEAT)
            except Exception:
                pass
            return 0
        except Exception as e:
            log("常驻循环出错（已忽略，继续）: %r" % (e,))
        time.sleep(max(15.0, float(args.interval)))


def run_once(args, force=False, dry=False):
    """巡检一次：列「控制」文件夹 → 处理新指令 → 退出（常驻模式和单次调用共用）"""
    if not _acquire_lock():
        return 0
    try:
        return _run_once_inner(args, force=force, dry=dry)
    finally:
        _release_lock()


def _run_once_inner(args, force=False, dry=False):
    guard_switch()          # 顺带体检「保底程序」（桌面开关 + 依赖），缺失才写日志
    import zy_client
    try:
        cli = zy_client.build_client()
    except Exception as e:
        log("登录失败 %r" % (e,))
        print("[错误] 登录失败：%r" % (e,))
        return 1

    try:
        cdir = resolve_control_dir(cli)   # 先定位指令文件夹（不写死 ID）
        if not cdir:
            # 定位不到就**什么都不做**，绝不退化成"读整个根目录"；
            # 但要往根目录发一条告警，别让用户以为程序还活着
            print("[跳过] 没找到指令文件夹，本轮不做任何事")
            log("跳过本轮：没有可用的指令文件夹")
            _notify_dir_failure(
                cli, str(load_config().get("control_subdir") or CONTROL_SUBDIR_NAME))
            return 0
        items = list_control_items(cli)
    except Exception as e:
        log("列目录失败 %r" % (e,))
        print("[错误] 列目录失败：%r" % (e,))
        return 1

    cfg = load_config()
    mode = str(cfg.get("mode") or "control").lower()
    st = load_state()
    done = st.setdefault("processed", {})
    st["lastCheck"] = datetime.datetime.now().isoformat(timespec="seconds")
    mem_alert_if_needed(st, dry=args.dry)  # 内存 ≥95% 主动发一条告警（30 分钟冷却，防刷屏）

    todo = []
    for n in items:
        name = str(n.get("fileName") or "")
        fid = str(n.get("fileId") or "")
        upd = str(n.get("updateTime") or "")
        # 自己发的回执，跳过。除了本程序的【电脑】前缀，也认「AI回复」——
        # send_text 往根目录发时会自动加这个前缀（防套娃），不能把它当新指令
        if name.startswith(REPLY_PREFIX) or name.startswith("AI回复"):
            continue
        if not force and done.get(fid) == upd:
            continue                              # 处理过了
        todo.append((n, fid, upd))

    if force:
        todo = sorted(todo, key=lambda x: x[2])[-1:] if todo else []

    if not todo:
        print("没有新指令。")
        save_state(st)
        return 0

    now = datetime.datetime.now().strftime("%m%d-%H%M")
    for n, fid, upd in todo:
        name = str(n.get("fileName") or "")

        # ---------- 第 1 道：危险请求直接拒（在识别之前，绝不依赖认得准）----------
        bad, why = check_dangerous(name)
        if bad:
            print("  ⛔ 拒绝执行（%s）：%s" % (why, name))
            log("拒绝危险请求 %r  原因=%s" % (name, why))
            send_back("已拒绝", "%s已拒绝-%s" % (REPLY_PREFIX, now),
                      refusal_text(name, why), dry=args.dry)
            if not args.dry:
                done[fid] = upd
            continue

        cmd = match_cmd(name)
        src = "关键词"
        dtarget = None
        img_urls = []
        if cmd is None:
            # 认不出关键词时，先看这条笔记有没有图 —— 发了图就是想让我看图（拍照解题）
            img_urls = pick_image_urls(cli, fid)
            if img_urls:
                cmd, src = "vision", "图片"
            else:
                # 纯文字才让 DeepSeek 判意图（省一次调用）
                cmd, dtarget = ds_intent(name)
                src = "DeepSeek" if cmd else "都不认识"
        print("处理指令：%s -> %s（%s）" % (name, cmd or "不认识", src))

        # ---- 截图是"发图"不是"发字"，单独走一条路
        if cmd == "shot":
            # 隐私护栏：抓屏有时间窗，怕你人不在时把屏幕内容自动传上云
            if not _in_shot_window():
                a, b = SHOT_WINDOW
                print("  ⏰ 不在抓屏时间窗内（%02d:00~%02d:00），已拒绝" % (a, b))
                send_back("现在不抓屏", "%s现在不抓屏-%s" % (REPLY_PREFIX, now),
                          "\n".join([
                              "# 现在不抓屏",
                              "",
                              "抓屏只允许在 **%02d:00 ~ %02d:00** 之间做。" % (a, b),
                              "",
                              "原因：怕你人不在的时候，电脑屏幕上的东西",
                              "（密码、聊天记录、私人页面）被自动拍下来传到云笔记。",
                              "",
                              "现在是 %s，到时间窗里你再发一次「截图」就行。" %
                              datetime.datetime.now().strftime("%H:%M")]),
                          dry=dry)
                if not dry:
                    done[fid] = upd
                continue
            if dry:
                print("[dry-run] 会抓屏 → 排成页面图 → 上传到「控制」")
                continue
            paths, err = do_screenshot(cli, "来自「控制」文件夹的截图")
            if paths:
                png, jpg = paths
                try:
                    import zy_upload
                    zy_upload.upload_note_page(
                        cli, png, "%s截屏-%s" % (REPLY_PREFIX, now),
                        parent_id=CONTROL_DIR, progress=lambda *a: None,
                        body_image_path=jpg)
                    log("截屏已上传 %s" % png)
                    done[fid] = upd
                except Exception as e:
                    # ⚠️ 记完整堆栈：只记 repr(e) 时看不出是哪一行（2026-10-17 踩过）
                    tb = traceback.format_exc()
                    log("截屏上传失败 %r\n%s" % (e, tb))
                    print("截屏上传失败：%r" % (e,))
                    # ⚠️ 2026-10-17 修：这里原来只写日志、不回消息，
                    # 用户在平板上看到的就是「发了没反应」，完全不知道卡在哪。
                    if send_back("截图没发出去", "%s截图失败-%s" % (REPLY_PREFIX, now),
                                 "\n".join([
                                     "# 截图没发出去",
                                     "",
                                     "屏幕抓到了，但往云笔记上传时出错：",
                                     "",
                                     "```",
                                     str(e)[:300],
                                     "```",
                                     "",
                                     "过一会儿再发一次「截图」试试。",
                                     "（电脑本地日志里也记了一份）"]),
                                 dry=dry) and not dry:
                        done[fid] = upd
            else:
                # 抓屏失败也回一条说明，免得他不知道发生了什么
                log("截屏失败（抓屏阶段）：%s" % (err or "未知原因"))
                if send_back("截图失败", "%s截图失败-%s" % (REPLY_PREFIX, now),
                             "\n".join(["# 截图没成功", "", err or "未知原因"])):
                    done[fid] = upd
            continue

        # ---- 点击：AI 看屏幕找目标 → 单击 → 回传一张新截图
        if cmd in ("click", "dclick", "rclick"):
            if not _in_shot_window():
                a, b = SHOT_WINDOW
                send_back("现在不点", "%s现在不点-%s" % (REPLY_PREFIX, now),
                          "\n".join([
                              "# 现在不点",
                              "",
                              "远程点击只允许在 **%02d:00 ~ %02d:00** 之间做。" % (a, b),
                              "",
                              "原因和截图一样：怕你人不在的时候乱点。",
                              "现在是 %s。" % datetime.datetime.now().strftime("%H:%M")]),
                          dry=dry)
                if not dry:
                    done[fid] = upd
                continue

            tgt = click_target(name) or dtarget
            if not tgt:
                send_back("要点哪里", "%s要点哪里-%s" % (REPLY_PREFIX, now),
                          "\n".join([
                              "# 要点哪里",
                              "",
                              "你写的是：「%s」" % name,
                              "",
                              "标题里带上要点的东西就行，比如：",
                              "  · 点 保存按钮",
                              "  · 点 任务栏的浏览器图标",
                              "  · 双击 桌面上的文件夹",
                              "  · 右键 那个文件名",
                              "",
                              "电脑会自己截屏找位置（整图找一遍，再把那块放大看一遍），再点。"]),
                          dry=dry)
                if not dry:
                    done[fid] = upd
                continue

            if dry:
                print("[dry] 会点：%s" % tgt)
                continue

            ok, msg = do_click(tgt,
                               "right" if cmd == "rclick" else "left",
                               cmd == "dclick")
            log("点击 %r -> ok=%s %s" % (tgt, ok, msg[-200:]))
            if ok:
                paths, serr = do_screenshot(cli, "「%s」点完之后" % tgt)
                if paths:
                    try:
                        import zy_upload
                        zy_upload.upload_note_page(
                            cli, paths[0], "%s点完-%s" % (REPLY_PREFIX, now),
                            parent_id=CONTROL_DIR, progress=lambda *a: None,
                            body_image_path=paths[1])
                        done[fid] = upd
                        continue
                    except Exception as e:
                        log("点完的截图上传失败 %r" % (e,))
                send_back("点完了", "%s点完了-%s" % (REPLY_PREFIX, now),
                          "\n".join([
                              "# 点完了",
                              "",
                              "点了：**%s**" % tgt,
                              "",
                              "```", msg[-600:], "```",
                              "",
                              "（点完的截图没发上去，先用文字告诉你）"]),
                          dry=dry)
                if not dry:
                    done[fid] = upd
            else:
                send_back("没点成功", "%s没点成功-%s" % (REPLY_PREFIX, now),
                          "\n".join([
                              "# 没点成功",
                              "",
                              "目标：**%s**" % tgt,
                              "",
                              "```", msg[-700:], "```",
                              "",
                              "常见原因：屏幕上没找到那个东西、不在允许时间段、",
                              "或者前台是危险窗口（UAC / 注册表 / 磁盘管理会被拦）。"]),
                          dry=dry)
                if not dry:
                    done[fid] = upd
            continue

        if cmd == "state":
            text, title = report_state(), "状态报告"
        elif cmd == "help":
            text, title = report_help(), "指令字典"
        elif cmd == "daily":
            text, title = report_daily(), "日报"
        elif cmd == "quota":
            text, title = report_quota(), "额度"
        elif cmd == "dsbal":
            text, title = report_dsbal(), "DeepSeek账户"
        elif cmd == "calib":
            text, title = do_calibrate(name), "校准结果"
        elif cmd == "restart":
            text, title = do_restart(), "重启结果"
        elif cmd == "wb_start":
            text, title = do_wb_start(), "开启WorkBuddy"
        elif cmd == "wb_stop":
            text, title = do_wb_stop(), "关闭WorkBuddy"
        elif cmd == "vision":
            text, title = do_vision(img_urls, name), "看图回答"
        elif cmd == "dashboard":
            text, title = report_dashboard(), "仪表盘"
        elif cmd == "search_on":
            cfg["web_search"] = True
            save_config(cfg)
            text, title = ("# 联网搜索已开启\n\n发「搜索 无锡天气」试试。", "搜索已开启")
            log("联网搜索：已开启")
        elif cmd == "search_off":
            cfg["web_search"] = False
            save_config(cfg)
            text, title = ("# 联网搜索已关闭\n\n再发「搜索 xxx」会提示已关闭。\n\n"
                           "想开回来，发「开启搜索」。", "搜索已关闭")
            log("联网搜索：已关闭")
        elif cmd == "search":
            if not cfg.get("web_search", True):
                text, title = ("# 联网搜索现在是关着的\n\n发「开启搜索」就能打开。",
                               "搜索已关闭")
            else:
                q = pick_query(name)
                if not q:
                    text, title = ("# 没写搜索词\n\n写成「搜索 无锡天气」这样。", "搜索")
                else:
                    items, err = ds_search(q, 5)
                    if not items:
                        text, title = ("# 搜索没成功\n\n%s\n\n"
                                       "（可能是网络问题，或没配 DeepSeek key）" % err,
                                       "搜索失败")
                    else:
                        text, title = format_search(q, items), "搜索结果"
                        log("联网搜索：%s → %d 条" % (q, len(items)))
        elif cmd == "mode_full":
            cfg["mode"] = "full"
            save_config(cfg)
            text, title = ("# 已切到全功能\n\n现在能聊天、能答题了。\n\n"
                           "想改回去，发「仅控制」。", "已切到全功能")
            log("模式切换：full")
        elif cmd == "mode_control":
            cfg["mode"] = "control"
            save_config(cfg)
            text, title = ("# 已切到仅控制\n\n现在只认指令，不聊天、不答题。\n\n"
                           "想切回来，发「全功能」。", "已切到仅控制")
            log("模式切换：control")
        elif cmd == "set_think":
            lvl = _parse_think(name)
            cfg["thinking"] = lvl
            save_config(cfg)
            extra = ("关闭后最快最省，但复杂题、长推理容易出错。"
                     if lvl == "off" else
                     "开启后解题、推理会更准，但更慢也更费钱"
                     "（思考过程也按输出计费）。")
            text, title = ("# 思考强度：%s\n\n%s\n\n下一轮生效。"
                           % (THINK_LABELS[lvl], extra),
                           "思考%s" % ("关闭" if lvl == "off" else lvl))
            log("思考强度改为 %s" % lvl)
        elif cmd == "set_maxtok":
            v = max(200, min(8000, int(pick_number(name) or 2000)))
            cfg["max_tokens"] = v
            save_config(cfg)
            text, title = ("# 回答长度上限：%d\n\n"
                           "范围 200~8000。调大回答更完整、也更费钱；调小更省，"
                           "但知识点/解题这类可能被砍断。" % v,
                           "长度上限%d" % v)
            log("回答长度上限改为 %d" % v)
        elif cmd == "set_freq":
            num = pick_number(name)
            n2 = max(1, min(60, int(num or 1)))
            cfg["interval_min"] = n2
            save_config(cfg)
            text, title = ("# 检查频率已调整\n\n现在每 %d 分钟检查一次。\n\n"
                           "（范围 1~60 分钟。调小会更频繁地登录，太密可能触发风控）" % n2,
                           "频率改为%d分钟" % n2)
            log("检查频率改为 %d 分钟" % n2)
        elif cmd == "nolink":
            text, title = report_nolink(), "这个不用你操心"
        else:
            # 认不出来的话：全功能模式交给我聊天，仅控制模式直接拒绝
            if mode == "full":
                ans = ds_chat(name)
                if ans:
                    text, title = ans, "回答"
                    log("聊天回答：%s -> %s" % (name[:30], ans[:40]))
                else:
                    text, title = report_unknown(name), "没看懂"
            else:
                text, title = report_control_only(name), "只做控制"

        ok = send_back(title, "%s%s-%s" % (REPLY_PREFIX, title, now), text, dry=dry)
        # ⚠️ 只有"真上传成功"才记账；--dry 试跑不记账（否则会把指令吃掉），失败也不记账（下轮重试）
        if ok and not dry:
            done[fid] = upd

    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
