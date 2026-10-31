#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一段文字排版成一页云笔记图，上传成一本新笔记 —— 也就是「把文字发到平板」。

原则：**一个字都不改**，只负责排版。
------------------------------------------------------------------
· 正文原样保留（标点、引号、空格都不动）。
· 只有 `**……**` 这种 markdown 粗体标记会被**当成格式**处理：文字照留，
  星号本身不画出来，改成用粗体字重渲染（它本来就是格式标记，不是正文）。
· 每个自然段之间加一点行距，读起来不挤。
· 字号自适应：从 52 号往下试，找到第一个能把整篇塞进 1920×2184 一页的字号，
  所以长一点的作文也能整篇送达，不会被截断成「……」。

它自己会挑解释器
----------------
· Pillow 只装在 `C:\\Program Files\\Python314`，pycryptodome 只装在 `.venv`。
· 所以本文件是「一份脚本两种身份」：
    默认身份（.venv 下跑）→ 渲染交给 3.14 子进程 → 自己负责上传
    `--render-only`（3.14 下跑）→ 只渲染，不联网

用法
----
    .venv\\Scripts\\python.exe -B send_text.py --text-file 正文.txt \\
        --title "慎言，而非噤声" --parent 80934bc7ad4f44e69cf4c43d426cf07b

    type 正文.txt | .venv\\Scripts\\python.exe -B send_text.py --title "标题"

· 正文首行若写成 `# 标题`，会被拿去做页首标题。
· 渲染图先落在工作区 `_out/`，**上传成功后自动进回收站**（沿用 zy_upload 的规矩）。
· `--dry-run` 只渲染、不上传，并把图丢到桌面先看一眼。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 2026-10-17：Pillow 已装进 .venv，直接用自己的解释器，不再依赖系统 Python314
PY_WITH_PIL = sys.executable

# 云笔记「猫cat」文件夹（顶层，type=0）
# 2026-10-17：不再写死文件夹 ID（开源要让别人也能用）。
# 默认发到根目录；control_bot 调用时会用 --parent 显式传入指令文件夹。
ROOT_FOLDER = "0"

OUT_DIR = os.path.join(os.path.dirname(HERE), "_out")

# ------------------------------------------------------------------ 版式常量
# 页面 1920×2184 与 reply_render 保持一致（云笔记手写画布的实测上限）

PAGE_W, PAGE_H = 1920, 2184
MARGIN_X = 130
BODY_TOP = 240              # 正文起始 y
BOTTOM_LIMIT = PAGE_H - 150  # 正文最低可到哪
LINE_RATIO = 1.72           # 行距倍数
PARA_GAP = 0.30             # 段间距（相对行高）
TITLE_BAR_Y = 88
TITLE_SIZE = 78
BG = (255, 255, 255)
INK = (38, 42, 48)
ACCENT = (26, 148, 160)
RULE = (222, 228, 234)

FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
# 缺字回退用的字体：微软雅黑没给下面这批符号定义字形，Segoe UI Symbol 全都有
FONT_SYM = "C:/Windows/Fonts/seguisym.ttf"

# 微软雅黑（msyh / msyhbd）**实测**缺失的数学符号，共 31 个。
# 来源：2026-10-25 逐字核对（见桌面《备忘报错日志.txt》第 9 条）。
#   上标 ⁰ ⁶ ⁹ / 下标 ₀₋₉ / ⁺ ⁻ / ∛ ∜ / ∉ ⊂ ⊆ ⊈ ∅ / ∀ ∃ / ⇒ ⇔ / ✓ ✗ ✔ ✘ / ≅
#
# ⚠️ 为什么硬编码而不是运行时检测字形：试过「拿私用区 U+E000 当缺字基准比对字模」，
#    在微软雅黑上不成立（雅黑对 U+E000 的处理与缺字方块不同），结果把所有字都判成
#    "有字形"，回退完全不生效，画出来全是方块。这份清单是逐字目测核对的，最可靠。
FALLBACK_CHARS = frozenset(
    "\u2070\u2076\u2079"                                     # 上标 0 6 9
    "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089"   # 下标 0-9
    "\u207a\u207b"                                            # ⁺ ⁻
    "\u221b\u221c"                                            # ∛ ∜
    "\u2209\u2282\u2286\u2288\u2205"                          # ∉ ⊂ ⊆ ⊈ ∅
    "\u2200\u2203"                                            # ∀ ∃
    "\u21d2\u21d4"                                            # ⇒ ⇔
    "\u2713\u2717\u2714\u2718"                                # ✓ ✗ ✔ ✘
    "\u2245"                                                  # ≅
)


# ------------------------------------------------------------------ 文本处理

def split_para(line):
    """把一行按 `**` 切成 [(片段, 是否粗体), ...]。不删一个字，只认格式标记。"""
    parts = line.split("**")
    return [(parts[i], i % 2 == 1) for i in range(len(parts)) if parts[i]]


def parse_text(raw):
    """切成 (标题, 段落行列表)。只认首行 `# xxx` 是标题，其余原样。"""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title, body = None, []
    for ln in lines:
        if title is None and ln.lstrip().startswith("#") and ln.strip():
            title = ln.lstrip().lstrip("#").strip()
            continue
        body.append(ln)          # 原样保留，不做 strip
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return title, body


# ------------------------------------------------------------------ 渲染（3.14 侧）

def render_only():
    from PIL import Image, ImageDraw, ImageFont

    payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    title = payload.get("title") or "笔记"
    src_lines = payload.get("lines") or []
    png, jpg = payload["png"], payload["jpg"]

    paras = []                    # [(runs, is_blank)]
    for ln in src_lines:
        if not ln.strip():
            paras.append((None, True))
            continue
        runs = split_para(ln)
        paras.append((runs, False) if runs else (None, True))

    _fcache = {}

    def font(bold, size):
        k = (bool(bold), size)
        if k not in _fcache:
            _fcache[k] = ImageFont.truetype(FONT_BOLD if bold else FONT_REG,
                                            size, index=0)
        return _fcache[k]

    # ---- 缺字回退 ----------------------------------------------------
    # 微软雅黑缺一批数学符号（清单见文件顶部 FALLBACK_CHARS，共 31 个）。
    # 缺字时 Pillow 会画一个空心方块，看着就像"乱码"（数学答案里尤其明显）。
    # 做法：逐字画，命中清单的字符换 Segoe UI Symbol 画那一个。
    _fb_cache = {}

    def fallback_font(size):
        if size not in _fb_cache:
            try:
                _fb_cache[size] = (ImageFont.truetype(FONT_SYM, size)
                                   if os.path.exists(FONT_SYM) else None)
            except Exception:
                _fb_cache[size] = None
        return _fb_cache[size]

    def draw_mixed(d, x, y, text, bold, size, fill):
        """逐字画，命中缺字清单的字符换备用字体。返回画完后的 x。"""
        f = font(bold, size)
        fb = fallback_font(size)
        cx = x
        for ch in text:
            if fb is not None and ch in FALLBACK_CHARS:
                d.text((cx, y), ch, font=fb, fill=fill)
                cx += fb.getlength(ch)
            else:
                d.text((cx, y), ch, font=f, fill=fill)
                cx += f.getlength(ch)
        return cx

    max_w = PAGE_W - MARGIN_X * 2

    def wrap(runs, size):
        """按像素宽度折行，返回 [[(片段, 粗体), ...], ...]。"""
        lines, cur, cur_w = [], [], 0.0
        for text, bold in runs:
            f = font(bold, size)
            buf = ""
            for ch in text:
                w = f.getlength(ch)
                if cur_w + w > max_w and (cur or buf):
                    if buf:
                        cur.append((buf, bold))
                        buf = ""
                    lines.append(cur)
                    cur, cur_w = [], 0.0
                buf += ch
                cur_w += w
            if buf:
                cur.append((buf, bold))
        if cur:
            lines.append(cur)
        return lines

    # --- 字号自适应
    room = BOTTOM_LIMIT - BODY_TOP
    chosen, total = 18, 0
    for size in range(52, 17, -1):
        line_h = int(size * LINE_RATIO)
        h, n = 0, 0
        for runs, blank in paras:
            if blank:
                h += int(line_h * 0.5)
                continue
            n += 1
            h += len(wrap(runs, size)) * line_h
            h += int(line_h * PARA_GAP)
        if h <= room:
            chosen, total = size, h
            break

    # --- 画
    img = Image.new("RGB", (PAGE_W, PAGE_H), BG)
    d = ImageDraw.Draw(img)

    f_title = font(True, TITLE_SIZE)
    d.rounded_rectangle([MARGIN_X, TITLE_BAR_Y, MARGIN_X + 14, TITLE_BAR_Y + 70],
                        radius=7, fill=ACCENT)
    draw_mixed(d, MARGIN_X + 44, TITLE_BAR_Y - 8, title, True, TITLE_SIZE, INK)
    d.line([MARGIN_X, 196, PAGE_W - MARGIN_X, 196], fill=RULE, width=3)

    y = BODY_TOP
    line_h = int(chosen * LINE_RATIO)
    for i, (runs, blank) in enumerate(paras):
        if blank:
            y += int(line_h * 0.5)
            continue
        for line in wrap(runs, chosen):
            x = MARGIN_X
            for txt, is_bold in line:
                # 逐字画，缺字自动换备用字体（数学符号不再变方块）
                x = draw_mixed(d, x, y, txt, is_bold, chosen, INK)
            y += line_h
        if i != len(paras) - 1:
            y += int(line_h * PARA_GAP)

    for path in (png, jpg):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if path.lower().endswith((".jpg", ".jpeg")):
            img.save(path, "JPEG", quality=92, optimize=True)
        else:
            img.save(path, "PNG")

    print(json.dumps({"size": chosen, "height": total, "room": room,
                      "paragraphs": sum(1 for r, b in paras if not b),
                      "png": png, "jpg": jpg}, ensure_ascii=False))


# ------------------------------------------------------------------ 上传（.venv 侧）

def do_render(title, lines, png, jpg):
    payload = json.dumps({"title": title, "lines": lines, "png": png, "jpg": jpg},
                         ensure_ascii=False)
    # ⚠️ 打包成 exe 后，sys.executable 就是 exe 本身，不能再当解释器去跑 `-B xxx.py`。
    #    必须走 main.py 的 --child 分发，否则会弹
    #    「unrecognized arguments: -B ...」（2026-10-18 踩到）。
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--child", "send_text_render"]
    else:
        if not os.path.exists(PY_WITH_PIL):
            raise RuntimeError("找不到带 Pillow 的解释器：" + PY_WITH_PIL)
        cmd = [PY_WITH_PIL, "-B", os.path.abspath(__file__), "--render-only"]
    r = subprocess.run(cmd,
                       input=payload.encode("utf-8"),
                       capture_output=True, timeout=300,
                       # 不弹控制台：这个孙进程若开窗，会闪一下甚至被同轮截图拍进去
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError("渲染失败：\n" + r.stderr.decode("utf-8", "replace")[-800:])
    return json.loads(r.stdout.decode("utf-8", "replace"))


def send_block(text, title=None, name=None, parent=ROOT_FOLDER, keep=False):
    """把一段文字渲染成图片笔记并上传。供 GUI 等模块**直接调用**（不起命令行）。

    返回 upload_note_page 的结果 dict；异常直接抛给调用方处理。
    """
    t, lines = parse_text(text)
    t = (title or t or "电脑发来的笔记").strip()
    stamp = datetime.datetime.now().strftime("%m%d-%H%M%S")
    os.makedirs(OUT_DIR, exist_ok=True)
    base = os.path.join(OUT_DIR, "发送-" + stamp)
    png, jpg = base + ".png", base + ".jpg"
    do_render(t, lines, png, jpg)

    import zy_client
    import zy_upload
    cli = zy_client.build_client()
    return zy_upload.upload_note_page(
        cli, png, name or t, parent_id=parent,
        progress=lambda *a: None, body_image_path=jpg,
        keep_source=keep)


def main():
    ap = argparse.ArgumentParser(description="把一段文字排版后发成云笔记（发到平板）")
    ap.add_argument("--render-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--text-file", help="正文文件（UTF-8）；不传则读 stdin")
    ap.add_argument("--title", help="标题（也可在正文首行写 `# 标题`）")
    ap.add_argument("--parent", default=ROOT_FOLDER,
                    help="父文件夹 fileId，默认根目录 0")
    ap.add_argument("--name", help="笔记名（默认=标题）")
    ap.add_argument("--dry-run", action="store_true", help="只渲染，不上传（图丢桌面）")
    ap.add_argument("--out", help="配合 --dry-run：渲染到指定路径（不含扩展名）")
    ap.add_argument("--keep", action="store_true", help="上传后不回收本地渲染图")
    ap.add_argument("--body-image", metavar="PATH",
                    help="额外的页内真实图片（比如点击后的截图）；"
                         "不传就用文字页面缩略图")
    args = ap.parse_args()

    if args.render_only:
        render_only()
        return

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8") as f:
            raw = f.read()
    else:
        raw = sys.stdin.buffer.read().decode("utf-8")

    if not raw.strip():
        print("[出错] 正文是空的")
        sys.exit(1)

    title, lines = parse_text(raw)
    title = (args.title or title or "电脑发来的笔记").strip()

    stamp = datetime.datetime.now().strftime("%m%d-%H%M%S")
    if args.dry_run:
        home = os.path.expanduser("~")
        desk = os.path.join(home, "Desktop")
        if not os.path.isdir(desk):
            desk = os.path.join(home, "桌面")
        base = args.out or os.path.join(desk, "预览-" + stamp)
    else:
        os.makedirs(OUT_DIR, exist_ok=True)
        base = os.path.join(OUT_DIR, "发送-" + stamp)
    png, jpg = base + ".png", base + ".jpg"

    info = do_render(title, lines, png, jpg)
    print("渲染完成：字号 %d，正文高 %d / 可用 %d，共 %d 段"
          % (info["size"], info["height"], info["room"], info["paragraphs"]))

    if args.dry_run:
        print("（dry-run，没有上传）\n  %s\n  %s" % (png, jpg))
        return

    import zy_client
    import zy_upload

    name = args.name or title
    # ⚠️ 保险栓：往**根目录**发的回复，笔记名必须带 `AI回复-` 前缀。
    # 根目录最可能被别的扫描器（如接力链）看到；不带前缀它就成了"最新一条"，
    # 下轮又被判成新消息 → 回复自己的回复 → 无限套娃（2026-10-05 实际踩到过）。
    # 例外：已经带【电脑】前缀的（本程序自己的回执/告警）不用再套一层，
    #       因为 control_bot 扫描时同样会跳过它。
    if args.parent == ROOT_FOLDER and not name.startswith(("AI回复", "【电脑】")):
        name = "AI回复-%s-%s" % (name, datetime.datetime.now().strftime("%m%d-%H%M"))
        print("[保险栓] 笔记名不以 AI回复 开头，已自动改为：%s" % name)
    cli = zy_client.build_client()
    # 页内图：优先用调用方给的真实截图（点击后的复核图），没有就退回文字缩略图
    body_img = args.body_image or jpg
    if args.body_image and not os.path.exists(args.body_image):
        print("[警告] --body-image 指的文件不存在，改用缩略图：%s" % args.body_image)
        body_img = jpg
    res = zy_upload.upload_note_page(
        cli, png, name, parent_id=args.parent,
        progress=lambda *a: None, body_image_path=body_img,
        keep_source=args.keep)
    print("已上传：%s\n%s" % (name, json.dumps(res, ensure_ascii=False, indent=2)))
    if res.get("recycledLocal"):
        print("本地渲染图已移入回收站。")


if __name__ == "__main__":
    main()
