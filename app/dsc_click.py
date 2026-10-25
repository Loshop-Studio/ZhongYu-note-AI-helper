#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsc_click.py —— 截屏交给 DeepSeek 识别目标位置，然后模拟单击。

设计要点
--------
1. **key 不进代码**：从 %LOCALAPPDATA%\\ZhongYuToolBox\\deepseek_key.txt 读，
   一行一个 key 就行。代码里永远不出现明文。
2. **图片先缩放再上传**：屏幕 1366×768 的 PNG 有一两百 KB，缩放后既省流量也省 token；
   模型返回的是缩放图上的坐标，这里按比例换回真实屏幕像素。
3. **DPI 感知**：截图 / 坐标 / 鼠标三者必须在同一坐标系，否则点偏。
4. **默认不点**：不加 --click 就只识别、只打印坐标（dry 模式）。
5. **护栏**：时间窗 + 前台窗口黑名单 + 锁屏检测，任何一个不满足都不点。

用法
----
    python dsc_click.py "保存按钮"              # 只识别，打印坐标
    python dsc_click.py "保存按钮" --click      # 识别并真的点
    python dsc_click.py --self-check            # 只测 API 连通性
"""

import argparse
import base64
import ctypes
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ⚠️ 强制 stdout 用 UTF-8：输出里有中文结论，调用方按 UTF-8 解码，
# 若这里走系统代码页（cp936）就会变乱码，导致「已点击」判断永远失败（2026-10-17 踩过）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from ctypes import wintypes

API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-flash"          # V4.1 Flash，原生多模态
MAX_SIDE = 1280                   # 上传前缩放到的长边
SHOT_WINDOW = (7, 23)             # 只在这个时间段允许操作

# 前台窗口命中这些词就拒绝点击（危险操作/提权界面）
BLOCK_WORDS = (
    "用户账户控制", "user account control", "注册表", "regedit", "磁盘管理",
    "格式化", "格式化磁盘", "删除", "卸载", "安装程序", "setup", "任务管理器",
    "task manager", "设备管理器", "控制面板", "系统配置", "msconfig",
    "恢复", "重置此电脑", "bitlocker", "密码", "凭据",
)


def base_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                        "ZhongYuToolBox")


def log(msg):
    try:
        p = os.path.join(base_dir(), "dsc_click.log")
        os.makedirs(base_dir(), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_key():
    p = os.path.join(base_dir(), "deepseek_key.txt")
    if not os.path.exists(p):
        return None, p
    try:
        with open(p, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.upper() in ("REPLACE_ME", "PASTE_HERE") or len(s) < 20:
                    continue                 # 还没填
                return s, p
    except Exception:
        pass
    return None, p


# ------------------------------------------------------------------ DPI / 屏幕

def make_dpi_aware():
    u = ctypes.windll.user32
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            u.SetProcessDPIAware()


def virtual_screen():
    """多屏拼接时的虚拟桌面原点与尺寸（主屏左上角不一定是 0,0）"""
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),      # XVIRTUALSCREEN, YVIRTUALSCREEN
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))      # CXVIRTUALSCREEN, CYVIRTUALSCREEN


def grab():
    from PIL import ImageGrab
    try:
        im = ImageGrab.grab(all_screens=True)
    except TypeError:
        im = ImageGrab.grab()
    return im.convert("RGB")


# ------------------------------------------------------------------ 环境检查

def in_window():
    h = datetime.datetime.now().hour
    a, b = SHOT_WINDOW
    return (a <= h < b) if a <= b else (h >= a or h < b)


def locked():
    """当前输入桌面不是默认桌面 → 锁屏或 UAC 安全桌面"""
    u = ctypes.windll.user32
    h = u.OpenInputDesktop(0, False, 0x0100)      # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    u.CloseDesktop(h)
    return False


def foreground_title():
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    if not h:
        return ""
    n = u.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    u.GetWindowTextW(h, buf, n + 2)
    return buf.value


def blocked_by_window(title):
    t = (title or "").lower()
    for w in BLOCK_WORDS:
        if w.lower() in t:
            return w
    return None


# ------------------------------------------------------------------ DeepSeek

def _pick_json(txt):
    """从模型输出里挑出真正的结果 JSON。

    低档思考时，正文里可能先出现一段思考文字、再给答案，
    所以逐个候选解析，取**最后一个**带 found 键的对象。
    """
    s = (txt or "").strip()
    try:
        d = json.loads(s)
        if isinstance(d, dict) and "found" in d:
            return d
    except Exception:
        pass
    best = None
    for m in re.finditer(r"\{[^{}]*\}", s):
        try:
            d = json.loads(m.group(0))
        except Exception:
            continue
        if isinstance(d, dict) and "found" in d:
            best = d
    return best


def call_vision(key, img_b64, mime, w, h, target):
    import urllib.request
    prompt = (
        "这是一张 Windows 桌面截图，图片尺寸 %d x %d 像素。\n"
        "坐标系：左上角为原点 (0,0)，x 向右增大，y **向下**增大。\n"
        "\n"
        "为了减少定位误差，请按两步走：\n"
        "第一步，在脑中把整张图按 4x4 均分成 16 格（列 A~D 从左到右，行 1~4 从上到下），"
        "先确定目标落在哪一格；\n"
        "第二步，再给出这个目标在**整张图**中的精确矩形边界。\n"
        "\n"
        "用户想点击的是：%s\n"
        "\n"
        "只输出一行 JSON，reason 不超过 12 个字：\n"
        '{"found":true,"grid":"B3","box":[左,上,右,下],"label":"控件名"}\n'
        "\n"
        "要求：\n"
        "- box 必须是**整张图**的像素坐标，不是格子内的相对坐标\n"
        "- 左<右、上<下，四个整数，紧贴控件可见边缘\n"
        "- 控件很小也照实给（任务栏图标可能只有十几像素），不要为了好看而放大\n"
        "- 找不到就输出 "
        '{"found":false,"reason":"为什么找不到"}，同样不超过 12 个字'
        % (w, h, target))
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 1200,
        # ⚠️ 必须关思考：定位要的是**直接吐一行 JSON**。
        #    之前写 reasoning_effort:"low"，而 DeepSeek 会把 low 映射成 high，
        #    结果模型把整段推理写进 reasoning_content、content 为空，
        #    解析不到 JSON → 直接报「模型没返回可用 JSON」。（2026-10-25 实测踩到）
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, img_b64)}},
        ]}],
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 直连，避开沙箱代理
    t0 = time.time()
    try:
        with op.open(req, timeout=120) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError("HTTP %s：%s" % (e.code, detail[:400]))
    cost = time.time() - t0
    data = json.loads(raw)
    msg = data["choices"][0]["message"]
    txt = (msg.get("content") or "").strip()
    # 万一思考模式没关掉，正文会空、内容全在 reasoning_content 里
    if not txt:
        txt = (msg.get("reasoning_content") or "").strip()
    usage = data.get("usage") or {}
    if not txt:
        raise RuntimeError("模型返回空内容。原始响应：%s"
                           % json.dumps(data, ensure_ascii=False)[:700])
    obj = _pick_json(txt)
    if obj is None:
        # 兜底：JSON 被 max_tokens 截断时，直接从原文里抓 box 数组
        m = re.search(r'"box"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)'
                      r'(?:\s*,\s*(-?\d+(?:\.\d+)?)){3}\s*\]', txt)
        if m:
            nums = re.findall(r"-?\d+(?:\.\d+)?", m.group(0))
            if len(nums) == 4:
                obj = {"found": True,
                       "box": [float(v) for v in nums],
                       "label": "",
                       "reason": "（JSON 被截断，已从原文提取 box）"}
    if obj is None:
        raise RuntimeError("模型没返回可用 JSON：%s" % txt[:300])
    return obj, usage, cost

# ------------------------------------------------------------------ 点击

def _crop_zoom_from(shot, box, scale, pad=180, zoom=2.5):
    """从**原始截图**里裁出 box 附近并放大，返回 (b64, w, h, x1, y1, zoom)。

    box 是「上传小图」的坐标，scale = 小图宽 / 原图宽。
    第二阶段从原图裁，细节比小图多得多，这是提高点击精度的关键。
    """
    from PIL import Image                      # 模块级没导入，这里显式引入
    W, H = shot.size
    x1 = max(0, int(box[0] / scale) - pad)
    y1 = max(0, int(box[1] / scale) - pad)
    x2 = min(W, int(box[2] / scale) + pad)
    y2 = min(H, int(box[3] / scale) + pad)
    if x2 - x1 < 60 or y2 - y1 < 60:          # 目标太小，扩一点保证上下文
        x1 = max(0, x1 - 40)
        y1 = max(0, y1 - 40)
        x2 = min(W, x2 + 40)
        y2 = min(H, y2 + 40)
    crop = shot.crop((x1, y1, x2, y2))
    if zoom and zoom != 1:
        crop = crop.resize((int(crop.width * zoom), int(crop.height * zoom)),
                           Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, "PNG")
    return (base64.b64encode(buf.getvalue()).decode(), crop.width, crop.height,
            x1, y1, zoom)


def locate_precise(key, shot, small_b64, mime, mw, mh, scale, target):
    """两阶段定位：整图粗定位 → 局部放大精定位。

    以前点不准的根本原因：模型看整屏时图会被压到约 800px，
    任务栏图标只有十几像素，估算误差必然几十上百像素。
    第二阶段把第一阶段的区域从**原图**裁出来放大 2.5 倍再问一次，坐标能收紧很多。

    返回 (obj, usage合计, 耗时合计)。obj 里 refined=True 时 box 是**原图**坐标。
    """
    obj, us, cost = call_vision(key, small_b64, mime, mw, mh, target)
    tin = (us or {}).get("prompt_tokens") or 0
    tout = (us or {}).get("completion_tokens") or 0
    usage = {"prompt_tokens": tin, "completion_tokens": tout}

    box = obj.get("box")
    if not obj.get("found") or not (isinstance(box, (list, tuple)) and len(box) == 4):
        return obj, usage, cost

    try:
        cb64, cw, ch, x1, y1, zoom = _crop_zoom_from(
            shot, [float(v) for v in box], scale)
    except Exception as e:
        log("裁剪放大失败 %r" % (e,))
        return obj, usage, cost

    try:
        obj2, us2, cost2 = call_vision(
            key, cb64, "image/png", cw, ch,
            target + "\n（这是上一步定位区域的放大图，坐标按这张图算）")
    except Exception as e:
        log("二次定位失败 %r" % (e,))
        return obj, usage, cost

    usage["prompt_tokens"] += (us2 or {}).get("prompt_tokens") or 0
    usage["completion_tokens"] += (us2 or {}).get("completion_tokens") or 0
    cost += cost2

    b2 = obj2.get("box")
    if not (obj2.get("found") and isinstance(b2, (list, tuple)) and len(b2) == 4):
        log("二次定位没找到，沿用第一次结果")
        return obj, usage, cost

    b2 = [float(v) for v in b2]
    obj["box"] = [x1 + b2[0] / zoom, y1 + b2[1] / zoom,
                  x1 + b2[2] / zoom, y1 + b2[3] / zoom]
    obj["label"] = obj2.get("label") or obj.get("label")
    obj["grid"] = obj2.get("grid") or obj.get("grid")
    obj["refined"] = True
    return obj, usage, cost


def click_at(x, y, button="left", double=False):
    """在 (x, y) 点击。button: left/right；double: 是否双击"""
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.35)
    if button == "right":
        down, up, times = 0x0008, 0x0010, 1      # RIGHTDOWN / RIGHTUP
    else:
        down, up, times = 0x0002, 0x0004, (2 if double else 1)
    for i in range(times):
        u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.05)
        u.mouse_event(up, 0, 0, 0, 0)
        if i + 1 < times:
            time.sleep(0.08)                     # 双击间隔要小于系统的 500ms


# ------------------------------------------------------------------ 主流程

def main():
    ap = argparse.ArgumentParser(description="截屏 + DeepSeek 识别 + 模拟点击")
    ap.add_argument("target", nargs="?", help="要点的东西，例如「保存按钮」")
    ap.add_argument("--click", action="store_true", help="真的点击（默认只识别）")
    ap.add_argument("--self-check", action="store_true", help="只测 API 连通性")
    ap.add_argument("--save", metavar="PATH", help="把这次截图存下来看看")
    ap.add_argument("--button", choices=("left", "right"), default="left",
                    help="用哪个键点（默认 left）")
    ap.add_argument("--double", action="store_true", help="双击（仅左键）")
    a = ap.parse_args()

    make_dpi_aware()
    key, keyp = load_key()
    if not key:
        print("没找到 API key。请把 key 写进这个文件（一行，只写 key 本体）：")
        print("  " + keyp)
        return 2

    if a.self_check:
        # 用一张真实的小图测，不能传空图（会 400）
        try:
            from PIL import Image
            im = Image.new("RGB", (64, 64), (200, 30, 30))
            b = io.BytesIO()
            im.save(b, "PNG")
            r, u, c = call_vision(key, base64.b64encode(b.getvalue()).decode(),
                                  "image/png", 64, 64, "这张图是什么颜色")
            print("连通 OK（%.1fs）  模型回答：%s"
                  % (c, json.dumps(r, ensure_ascii=False)))
            print("tokens 输入 %s / 输出 %s"
                  % ((u or {}).get("prompt_tokens"), (u or {}).get("completion_tokens")))
        except Exception as e:
            print("调用失败：%r" % (repr(e)[:300],))
            return 1
        return 0

    if not a.target:
        print("请给出要点击的目标，例如：python dsc_click.py \"保存按钮\"")
        return 2

    if not in_window():
        print("不在允许操作的时间段（%02d:00~%02d:00），已拒绝。" % SHOT_WINDOW)
        return 3
    if locked():
        print("屏幕锁着（或安全桌面），点了也没用，已拒绝。")
        return 3

    title = foreground_title()
    bad = blocked_by_window(title)
    if bad:
        print("前台窗口是「%s」，命中危险词「%s」，拒绝操作。" % (title[:40], bad))
        log("拒绝：前台窗口含危险词 %s" % bad)
        return 3

    vx, vy, vw, vh = virtual_screen()
    shot = grab()
    sw, sh = shot.size
    print("屏幕 %dx%d（虚拟桌面原点 %d,%d）" % (sw, sh, vx, vy))

    scale = min(1.0, MAX_SIDE / float(max(sw, sh)))
    small = shot if scale >= 1.0 else shot.resize(
        (max(1, int(sw * scale)), max(1, int(sh * scale))), 1)
    mw, mh = small.size
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=88, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    print("上传尺寸 %dx%d（%.1f KB）· 缩放比 %.3f"
          % (mw, mh, len(buf.getvalue()) / 1024.0, scale))

    if a.save:
        shot.save(a.save)
        print("原始截图已存：%s" % a.save)

    try:
        res, usage, cost = locate_precise(key, shot, b64, "image/jpeg",
                                          mw, mh, scale, a.target)
    except Exception as e:
        print("识别失败：%r" % (repr(e)[:400],))
        log("识别失败 %r" % (repr(e)[:300],))
        return 1

    print("模型回答（%.1fs，输入 %s / 输出 %s tokens）：%s"
          % (cost, usage.get("prompt_tokens"), usage.get("completion_tokens"),
             json.dumps(res, ensure_ascii=False)))

    if not res.get("found"):
        print("图上没找到「%s」：%s" % (a.target, res.get("reason", "")))
        return 4

    # 优先用边界框取中心（比模型直接报单点准得多），没有就回退到 x/y
    box = res.get("box")
    mx = my = None
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            bx = [float(v) for v in box]
            mx = (bx[0] + bx[2]) / 2.0
            my = (bx[1] + bx[3]) / 2.0
            print("边界框 %s → 中心 (%.0f, %.0f)" % (box, mx, my))
        except Exception:
            mx = my = None
    if mx is None:
        if res.get("x") is None or res.get("y") is None:
            print("模型既没给 box 也没给 x/y，无法定位。")
            return 4
        mx, my = float(res["x"]), float(res["y"])
    # 缩放图坐标 → 原图坐标 → 虚拟桌面坐标
    # 二次定位（refined）返回的 box 已经是**原图**坐标，不能再除 scale
    if res.get("refined"):
        rx, ry = mx, my
    else:
        rx, ry = mx / scale, my / scale
    ax = int(round(vx + rx))
    ay = int(round(vy + ry))
    print("目标「%s」→ 图内 (%.0f, %.0f) → 屏幕 (%d, %d)"
          % (res.get("label") or a.target, rx, ry, ax, ay))

    if not a.click:
        print("\n[dry] 没有真的点。确认坐标准了就加 --click。")
        log("识别 %r -> (%d,%d) dry" % (a.target, ax, ay))
        return 0

    click_at(ax, ay, a.button, a.double)
    log("点击 %r -> (%d,%d) %s%s 前台=%r"
        % (a.target, ax, ay, "右键" if a.button == "right" else "左键",
           "双击" if (a.double and a.button != "right") else "",
           title[:60]))
    print("已%s点击 (%d, %d)。"
          % ("右键" if a.button == "right" else ("双击" if a.double else ""), ax, ay))
    return 0


if __name__ == "__main__":
    sys.exit(main())
