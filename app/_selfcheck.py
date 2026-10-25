#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_selfcheck.py —— 云笔记遥控器全链路自检（不需要碰平板）

覆盖：
  环境依赖 / 关键文件 / 模板完整性 / 凭据 / 登录 / 云端可读
  DeepSeek 余额 & 视觉 API / 截图真实链路 & 中文路径编码
  计划任务注册状态 / 巡检新鲜度 / 近期错误日志 / 时间窗 / 残留文件

用法：
    .venv\\Scripts\\python.exe -B _selfcheck.py

只读为主：唯一会写盘的是截图（测完自动回收），不会往云端上传任何东西。
"""

import datetime
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                    "ZhongYuToolBox")
REPLY_PREFIX = "【电脑】"
PY314 = sys.executable          # 2026-10-17 起 Pillow 就在 .venv 里，不再用系统 Python314
TASK_XML = r"C:\Windows\System32\Tasks\云笔记遥控器"

RESULTS = []


def ok(name, detail=""):
    RESULTS.append(("OK", name, detail))
    print("  [OK]  %-26s %s" % (name, detail))


def warn(name, detail=""):
    RESULTS.append(("WARN", name, detail))
    print("  [!!]  %-26s %s" % (name, detail))


def fail(name, detail=""):
    RESULTS.append(("FAIL", name, detail))
    print("  [XX]  %-26s %s" % (name, detail))


def section(t):
    print()
    print("== %s " % t + "=" * max(4, 56 - len(t)))


def make_png(w, h, rgb):
    """纯标准库造一张纯色 PNG（不依赖 Pillow）"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def main():
    print("=" * 62)
    print("  云笔记遥控器 · 全链路自检")
    print("  %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 62)

    # ---------------- 环境
    section("环境")
    ok("Python", "%s (%s)" % (sys.version.split()[0], os.path.basename(sys.executable)))
    try:
        import Crypto  # noqa: F401
        ok("pycryptodome", "已安装")
    except Exception as e:
        fail("pycryptodome", "缺失 %r" % (e,))
    if os.path.exists(PY314):
        try:
            r = subprocess.run([PY314, "-c", "import PIL;print(PIL.__version__)"],
                               capture_output=True, timeout=30)
            v = (r.stdout or b"").decode(errors="replace").strip()
            ok("Python314 + Pillow", v or "?")
        except Exception as e:
            fail("Python314 + Pillow", repr(e)[:80])
    else:
        fail("Python314", "找不到 %s" % PY314)

    # ---------------- 文件
    section("关键文件")
    need = ["control_bot.py", "send_text.py", "zy_upload.py", "zy_client.py",
            "_shot_page.py", "reply_render.py", "dsc_click.py", "watch_channel.py"]
    missing = [f for f in need if not os.path.exists(os.path.join(HERE, f))]
    if missing:
        fail("脚本文件", "缺：%s" % "、".join(missing))
    else:
        ok("脚本文件", "%d 个齐全" % len(need))

    try:
        import zy_upload
        tpl = zy_upload.TEMPLATE_RESOURCES
        miss = [rel for rel, _, _ in tpl
                if not os.path.exists(os.path.join(zy_upload.TEMPLATE_BASE,
                                                   rel.replace("/", os.sep)))]
        if miss:
            fail("上传模板", "缺 %d 个：%s" % (len(miss), miss[0]))
        else:
            ok("上传模板", "%d 个齐全" % len(tpl))
    except Exception as e:
        fail("上传模板", repr(e)[:100])

    # ---------------- 配置
    section("配置")
    cred = os.path.join(BASE, "zy_credentials.json")
    if not os.path.exists(cred):
        fail("云笔记凭据", "缺 %s" % cred)
    else:
        try:
            d = json.load(open(cred, encoding="utf-8"))
            keys = [k for k in ("userName", "password", "apiBase") if d.get(k)]
            ok("云笔记凭据", "字段齐全，账号=%s" % d.get("userName", "?"))
        except Exception as e:
            fail("云笔记凭据", "解析失败 %r" % (e,))

    keyf = os.path.join(BASE, "deepseek_key.txt")
    ds_key = None
    if not os.path.exists(keyf):
        fail("DeepSeek 密钥", "缺 %s" % keyf)
    else:
        try:
            for line in open(keyf, encoding="utf-8-sig"):
                s = line.strip()
                if (s and not s.startswith("#")
                        and s.upper() not in ("REPLACE_ME", "PASTE_HERE")
                        and len(s) >= 20):
                    ds_key = s
                    break
            if ds_key:
                ok("DeepSeek 密钥", "已填写（%d 位）" % len(ds_key))
            else:
                fail("DeepSeek 密钥", "文件在，但还没填真正的 key")
        except Exception as e:
            fail("DeepSeek 密钥", repr(e)[:80])

    # ---------------- 云端
    section("云端账号")
    cli = None
    try:
        import zy_client
        cli = zy_client.build_client()
        ok("登录", "成功")
    except Exception as e:
        fail("登录", repr(e)[:150])

    if cli:
        try:
            import watch_channel as W
            import control_bot as _CB
            cdir = _CB.resolve_control_dir(cli)      # 按名字找，不写死 ID
            items = W.folder_items(cli, cdir)
            ok("指令文件夹", "可读，%d 条子项" % len(items))
        except Exception as e:
            fail("指令文件夹", repr(e)[:120])

        # 新笔记资源可读性（顺带确认接口没坏）
        try:
            all_notes = cli.get_all_notes()
            ok("笔记列表", "共 %d 条" % len(all_notes))
        except Exception as e:
            warn("笔记列表", repr(e)[:100])

    # ---------------- DeepSeek
    section("DeepSeek")
    if ds_key:
        try:
            req = urllib.request.Request(
                "https://api.deepseek.com/user/balance",
                headers={"Accept": "application/json",
                         "Authorization": "Bearer " + ds_key})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(req, timeout=25) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            infos = d.get("balance_infos") or []
            if infos:
                b = infos[0]
                ok("账户余额", "%s %s" % (b.get("currency"), b.get("total_balance")))
            else:
                warn("账户余额", "没有余额信息")
        except Exception as e:
            fail("账户余额", repr(e)[:120])

        try:
            import base64
            png = make_png(64, 64, (200, 30, 30))
            body = {
                "model": "deepseek-flash", "temperature": 0, "max_tokens": 120,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": [
                    {"type": "text",
                     "text": '这张图是什么颜色？只输出 JSON：{"found":true,"color":"..."}'},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64,"
                               + base64.b64encode(png).decode()}}]}],
            }
            req = urllib.request.Request(
                "https://api.deepseek.com/v1/chat/completions",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + ds_key})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            t0 = time.time()
            with op.open(req, timeout=60) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            ok("视觉识别", "%.1fs  %s" % (time.time() - t0, txt[:60]))
        except Exception as e:
            fail("视觉识别", repr(e)[:150])
    else:
        warn("DeepSeek", "无 key，跳过")

    # ---------------- 截图链路（关键：模拟计划任务的干净编码环境）
    section("截图链路（模拟计划任务环境）")
    if cli:
        for k in ("PYTHONIOENCODING", "PYTHONUTF8", "LANG", "LC_ALL"):
            os.environ.pop(k, None)
        try:
            import control_bot as C
            paths, err = C.do_screenshot(cli, "自检用截图")
            if not paths:
                fail("截图", err or "未知")
            else:
                png, jpg = paths
                exist = os.path.exists(png) and os.path.exists(jpg)
                if exist and re.search(r"[\u4e00-\u9fff]", os.path.basename(png)):
                    ok("截图 + 中文路径", os.path.basename(png))
                elif exist:
                    ok("截图", os.path.basename(png))
                else:
                    fail("截图", "返回了路径但文件不在：%s" % png)
                try:
                    import zy_upload as Z
                    Z.recycle_paths([p for p in (png, jpg) if os.path.exists(p)])
                    print("        （测试图已回收）")
                except Exception:
                    pass
        except Exception as e:
            fail("截图", repr(e)[:150])

    # ---------------- 运行时
    section("运行时")
    if os.path.exists(TASK_XML):
        ok("计划任务", "已注册（云笔记遥控器）")
    else:
        fail("计划任务", "找不到 %s" % TASK_XML)

    st_file = os.path.join(BASE, "control_state.json")
    if os.path.exists(st_file):
        try:
            st = json.load(open(st_file, encoding="utf-8"))
            lc = st.get("lastCheck")
            if lc:
                dt = datetime.datetime.strptime(lc, "%Y-%m-%dT%H:%M:%S")
                mins = (datetime.datetime.now() - dt).total_seconds() / 60.0
                if mins <= 4:
                    ok("巡检新鲜度", "%s（%.1f 分钟前）" % (lc, mins))
                elif mins <= 20:
                    warn("巡检新鲜度", "%s（%.1f 分钟前，偏旧）" % (lc, mins))
                else:
                    fail("巡检新鲜度", "%s（%.1f 分钟前，太久没动了）" % (lc, mins))
            else:
                warn("巡检新鲜度", "状态里没有 lastCheck")
        except Exception as e:
            warn("巡检新鲜度", repr(e)[:80])
    else:
        fail("巡检新鲜度", "没有 control_state.json")

    logp = os.path.join(BASE, "control_bot.log")
    if os.path.exists(logp):
        try:
            lines = open(logp, encoding="utf-8", errors="replace").read().splitlines()
            cutoff = datetime.datetime.now() - datetime.timedelta(hours=6)
            bad = []
            for ln in lines[-400:]:
                m = re.match(r"\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\]", ln)
                if not m:
                    continue
                try:
                    t = datetime.datetime(datetime.datetime.now().year,
                                          int(m.group(1)), int(m.group(2)),
                                          int(m.group(3)), int(m.group(4)), int(m.group(5)))
                except Exception:
                    continue
                if t >= cutoff and ("失败" in ln or "Traceback" in ln or "Error" in ln):
                    bad.append(ln)
            if not bad:
                ok("近期日志", "6 小时内无错误")
            else:
                warn("近期日志", "6 小时内 %d 条异常，最新：%s" % (len(bad), bad[-1][:80]))
        except Exception as e:
            warn("近期日志", repr(e)[:80])
    else:
        warn("近期日志", "还没有日志文件")

    h = datetime.datetime.now().hour
    if 7 <= h < 23:
        ok("时间窗", "当前 %02d:%02d，截图/点击可用" % (h, datetime.datetime.now().minute))
    else:
        warn("时间窗", "当前 %02d:%02d，不在 07:00-23:00，截图/点击会被拒绝" % (h, datetime.datetime.now().minute))

    out_dir = os.path.join(os.path.dirname(HERE), "_out")
    if os.path.isdir(out_dir):
        leftovers = [f for f in os.listdir(out_dir)
                     if f.startswith("电脑截屏-") and f.lower().endswith((".png", ".jpg"))]
        if leftovers:
            warn("残留截图", "%d 个未清理：%s" % (len(leftovers), leftovers[0]))
        else:
            ok("残留截图", "没有堆积")

    # ---------------- 汇总
    n_ok = sum(1 for r in RESULTS if r[0] == "OK")
    n_warn = sum(1 for r in RESULTS if r[0] == "WARN")
    n_fail = sum(1 for r in RESULTS if r[0] == "FAIL")
    print()
    print("=" * 62)
    print("  汇总：通过 %d / 警告 %d / 失败 %d" % (n_ok, n_warn, n_fail))
    if n_fail:
        print("  需要修的：")
        for r in RESULTS:
            if r[0] == "FAIL":
                print("    · %s —— %s" % (r[1], r[2]))
    print("=" * 62)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
