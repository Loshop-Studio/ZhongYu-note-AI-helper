#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一段文本渲染成【云笔记页面图】。

为什么不是生成 PDF
------------------
云笔记里存的**不是 PDF，而是每页一张图片**（见 `src/api/pdfNote.ts`：
上传时也是先把 PDF 逐页光栅化成 webp 再传）。所以"回复"的最终形态就是一张图，
中间那层 PDF 纯属多余 —— 省掉它，链路更短、故障点更少。

页面比例：1920 × 2184（80:91 ≈ 0.879）—— 用户指定
--------------------------------------------------
来源是云笔记手写画布的实测上限（笔记「最大比例测试」）= 平板一屏 1920×1092 的两倍高。
不属于任何标准纸张（A4 0.707 / Letter 0.773 / 正方形 1.000），介于 Letter 与正方形之间。

依赖：Pillow（本机 Python 3.14 已自带，无需安装）。

用法：
    python reply_render.py                 # 渲染示例问候，输出到桌面
    python reply_render.py out.png "标题" "第一行" "第二行" ...
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ------------------------------------------------------------------ 版式常量

PAGE_W, PAGE_H = 1920, 2184

MARGIN_X = 150          # 左右页边距
MARGIN_TOP = 210        # 正文起始高度
BG = (255, 255, 255)
INK = (38, 42, 48)          # 正文色（近黑）
INK_SOFT = (120, 128, 138)  # 次要文字
ACCENT = (26, 148, 160)     # 点缀色（青蓝）
RULE = (222, 228, 234)      # 分隔线

FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"

SIZE_TITLE = 84
SIZE_BODY = 56
SIZE_META = 40
LINE_RATIO = 1.78       # 行距倍数


def _font(path, size):
    try:
        return ImageFont.truetype(path, size, index=0)
    except Exception:
        return ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", size)


def _wrap(text, font, max_w):
    """按像素宽度折行（中文逐字，英文按词）。"""
    lines, cur = [], ""
    for ch in text:
        trial = cur + ch
        if font.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def render_reply(title, paragraphs, out_path, footer="来自你电脑里的云笔记小助手"):
    """
    渲染一页回复。

    title       —— 页首标题（一行）
    paragraphs  —— 正文段落列表；列表项为 "" 表示空行
    out_path    —— 输出的 PNG 路径
    footer      —— 右下角落款，传 None 则不画
    """
    img = Image.new("RGB", (PAGE_W, PAGE_H), BG)
    d = ImageDraw.Draw(img)

    f_title = _font(FONT_BOLD, SIZE_TITLE)
    f_body = _font(FONT_REG, SIZE_BODY)
    f_meta = _font(FONT_REG, SIZE_META)

    body_max_w = PAGE_W - MARGIN_X * 2

    # --- 标题左侧装饰竖条
    y = MARGIN_TOP - 130
    d.rounded_rectangle([MARGIN_X, y, MARGIN_X + 14, y + 74], radius=7, fill=ACCENT)

    # --- 标题
    d.text((MARGIN_X + 44, y - 8), title, font=f_title, fill=INK)
    y += 74 + 34

    # --- 标题下分隔线
    d.line([MARGIN_X, y, PAGE_W - MARGIN_X, y], fill=RULE, width=3)
    y += 52

    # --- 正文
    line_h = int(SIZE_BODY * LINE_RATIO)
    for para in paragraphs:
        if para == "":
            y += int(line_h * 0.55)
            continue
        for ln in _wrap(para, f_body, body_max_w):
            if y + line_h > PAGE_H - 190:
                d.text((MARGIN_X, y), "……", font=f_body, fill=INK_SOFT)
                y += line_h
                break
            d.text((MARGIN_X, y), ln, font=f_body, fill=INK)
            y += line_h

    # --- 落款
    if footer:
        fy = PAGE_H - 150
        d.line([MARGIN_X, fy - 30, PAGE_W - MARGIN_X, fy - 30], fill=RULE, width=2)
        w = f_meta.getlength(footer)
        d.text((PAGE_W - MARGIN_X - w, fy), footer, font=f_meta, fill=INK_SOFT)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # 按扩展名决定格式：.jpg/.jpeg → JPEG（云笔记「页内图片」实测就是 JPEG 内容），
    # 其余 → PNG（「页面总览图」screenshot.png 实测是 1920×2184 的 PNG）。
    if out_path.lower().endswith((".jpg", ".jpeg")):
        img.save(out_path, "JPEG", quality=92, optimize=True)
    else:
        img.save(out_path, "PNG")
    return out_path


# ------------------------------------------------------------------ 示例内容

SAMPLE_TITLE = "问声好呀"
SAMPLE_BODY = [
    "看到了你在这页上写下的这句：",
    "",
    "        「发一段问候吧」",
    "",
    "那我就认认真真地回一句 ——",
    "",
    "今天过得怎么样呀？",
    "作业写完没，还是打算先歇一会儿？",
    "最近降温了，出门记得加件外套。",
    "",
    "以后想说什么，",
    "就写在这一页上吧。",
    "我会时不时过来看看的，喵～",
]


def _default_out():
    home = os.path.expanduser("~")
    for c in ("Desktop", "桌面"):
        p = os.path.join(home, c)
        if os.path.isdir(p):
            return os.path.join(p, "AI回复.png")
    return os.path.join(home, "AI回复.png")


def main():
    # --json 模式：从 stdin 读 {"title":..,"paragraphs":[..],"out":..}
    # 供 MCP 服务调用（MCP 跑在没装 Pillow 的 .venv 里，需要借这个解释器渲染）。
    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        import json
        # 必须走 buffer 并显式按 UTF-8 解码：父进程用 .encode("utf-8") 写进管道，
        # 而 sys.stdin.read() 会按「本进程的 locale 编码」解码（中文 Windows 上是 cp936），
        # 于是路径里的中文被解成乱码 → 落盘出乱码文件名 → 父进程再按正常路径
        # open() 就报 FileNotFoundError。buffer+utf-8 与父进程严格对称，不受 env 影响。
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        p = render_reply(payload["title"], payload.get("paragraphs") or [],
                         payload["out"])
        img = Image.open(p)
        print("OK %s %dx%d" % (p, img.width, img.height))
        return

    # 参数：out [title [body...]]
    # 不传参数 → 用内置示例，输出到桌面。
    if len(sys.argv) > 1:
        out = sys.argv[1]
        title = sys.argv[2] if len(sys.argv) > 2 else SAMPLE_TITLE
        body = sys.argv[3:] or SAMPLE_BODY
    else:
        out = _default_out()
        title = SAMPLE_TITLE
        body = SAMPLE_BODY

    p = render_reply(title, body, out)
    img = Image.open(p)
    print("已生成: %s" % p)
    print("尺寸: %d x %d   宽/高 = %.4f （目标 80:91 ≈ 0.8791）"
          % (img.width, img.height, img.width / img.height))


if __name__ == "__main__":
    main()
