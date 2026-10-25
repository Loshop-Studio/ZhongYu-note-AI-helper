#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拍一张「电脑桌面截屏」，排版成云笔记页面图（1920×2184），交给 zy_upload_note 传回云笔记。

为什么这么做
------------
用户在平板那边只能看云笔记，看不到家里电脑的屏幕。云笔记的「一页」本质就是一张
图片（见 zy_upload.py 的说明），所以「发个电脑截屏过来」= 把截屏排成页面图再上传。

排版沿用 reply_render 的版式常量（白底、页边距、微软雅黑、右下角落款），
只是把正文换成截屏图片：等比缩放到页宽内，居中偏上，下方留一行说明。

只读 + 本地写两张图（PNG 总览图 + JPEG 页内图，对齐 zy_reply 的做法），不碰云端；
图片写到工作区上层的 `_out/`（zy_upload 上传成功后会自动把它移进回收站）。

用法：
    python _shot_page.py [--out 路径] [--note "底部说明"]
"""

import argparse
import datetime
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# ⚠️ 强制 stdout 用 UTF-8。
# 输出里的文件名含中文（"电脑截屏-…"），而管道默认用系统代码页（cp936/GBK），
# 调用方若按 UTF-8 解码就会得到乱码路径 → open 时 FileNotFoundError。
# 2026-10-17 踩过：交互式终端下正常，只有计划任务环境才暴露。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PAGE_W, PAGE_H = 1920, 2184
MARGIN_X = 100
BG = (255, 255, 255)
INK = (38, 42, 48)
INK_SOFT = (120, 128, 138)
ACCENT = (26, 148, 160)
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_out")


def _font(path, size):
    try:
        return ImageFont.truetype(path, size, index=0)
    except Exception:
        return ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", size)


def grab_screen():
    """抓主屏（多屏时抓所有屏拼成的整张）。"""
    from PIL import ImageGrab
    try:
        im = ImageGrab.grab(all_screens=True)
    except TypeError:
        im = ImageGrab.grab()
    return im.convert("RGB")


def make_page(shot, note="", stamp=None):
    now = stamp or datetime.datetime.now()
    page = Image.new("RGB", (PAGE_W, PAGE_H), BG)
    d = ImageDraw.Draw(page)

    f_title = _font(FONT_BOLD, 72)
    f_meta = _font(FONT_REG, 38)
    f_note = _font(FONT_REG, 40)

    # 页首标题
    title = "电脑截屏"
    d.text((MARGIN_X, 110), title, font=f_title, fill=INK)
    # 标题下的一条短横线
    d.line([(MARGIN_X, 220), (MARGIN_X + 150, 220)], fill=ACCENT, width=6)
    d.text((MARGIN_X, 250),
           now.strftime("%Y-%m-%d %H:%M:%S") + "  家里这台电脑的桌面",
           font=f_meta, fill=INK_SOFT)

    # 截屏：等比缩放到页宽内
    max_w = PAGE_W - 2 * MARGIN_X
    max_h = 1450
    sw, sh = shot.size
    k = min(max_w / sw, max_h / sh, 1.0)
    if k < 1.0:
        shot = shot.resize((max(1, int(sw * k)), max(1, int(sh * k))),
                           Image.LANCZOS)
    sw, sh = shot.size
    x = (PAGE_W - sw) // 2
    y = 360
    d.rectangle([x - 3, y - 3, x + sw + 2, y + sh + 2], outline=(210, 216, 222),
                width=3)
    page.paste(shot, (x, y))

    # 图下方说明
    if note:
        d.multiline_text((MARGIN_X, y + sh + 60), note, font=f_note,
                         fill=INK, spacing=28)
    # 右下角落款
    f_foot = _font(FONT_REG, 34)
    foot = "来自你电脑里的云笔记小助手"
    w = f_foot.getlength(foot)
    d.text((PAGE_W - MARGIN_X - w, PAGE_H - 110), foot, font=f_foot,
           fill=INK_SOFT)
    return page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--note", default="")
    ap.add_argument("--raw", action="store_true",
                    help="原图直出：不做白底排版，保持屏幕原始尺寸")
    a = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%m%d-%H%M%S") + "-%d" % os.getpid()

    if a.raw:
        # 原图直出：整张屏幕截图就是页面，不缩、不加标题、不加落款
        shot = grab_screen()
        out = a.out or os.path.join(OUT_DIR, "电脑截屏-%s.png" % stamp)
        shot.save(out, "PNG")
        jpg = os.path.splitext(out)[0] + ".jpg"
        shot.convert("RGB").save(jpg, "JPEG", quality=92, optimize=True)
        print("屏幕尺寸 %dx%d -> 原图直出 %s (%d bytes)"
              % (shot.size[0], shot.size[1], out, os.path.getsize(out)))
        print("PNG=%s" % out)
        print("JPEG=%s" % jpg)
        return 0

    shot = grab_screen()
    page = make_page(shot, a.note)
    out = a.out or os.path.join(OUT_DIR, "电脑截屏-%s.png" % stamp)
    page.save(out, "PNG")
    # 与 zy_reply 保持一致：PNG 当「页面总览图」，JPEG 当「页内图片」素材。
    jpg = os.path.splitext(out)[0] + ".jpg"
    page.convert("RGB").save(jpg, "JPEG", quality=88, optimize=True)
    print("屏幕尺寸 %dx%d -> 总览图 %s (%d bytes)\n                页内图 %s (%d bytes)"
          % (shot.size[0], shot.size[1], out, os.path.getsize(out),
             jpg, os.path.getsize(jpg)))
    print("PNG=%s" % out)
    print("JPEG=%s" % jpg)


if __name__ == "__main__":
    sys.exit(main())
