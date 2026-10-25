#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蹲守云笔记频道，等新内容 —— 供 WorkBuddy 定时任务在"任务执行期间"反复调用。

为什么要它
----------
WorkBuddy 的定时自动化**最小粒度是每小时**，但用户人不在电脑旁（在学校用平板），
没法手动喊 AI。所以要让"检测"这件事在**一次任务执行期间持续进行**：

    AI 调本脚本（蹲 N 分钟）──► 有新内容？──► 返回内容给 AI ──► AI 生成回复并上传
              ▲                      │
              └──────── 没有 ────────┘（超时退出，AI 再调一次）

这样把"每小时一次"变成"任务期间每 2 分钟一次"，聊天延迟从 60 分钟降到 ~2 分钟。

⚠️ 请求预算：默认每 2 分钟查一次（±20% 随机抖动）。一次任务蹲 9 分钟 ≈ 4~5 个请求。
   整天跑也就是几百个请求，与正常用 App 翻笔记的量级相当。
   **不要**把 --interval 调到 30 秒以下，那会明显偏离正常用户行为。
   （2026-10-02：同学实测 2 分钟间隔不会被风控，故从 4 分钟调回 2 分钟。）

⚠️ 抖动是防"行为指纹"的：固定间隔 + 固定节奏是机器人特征，
   间隔在基准上浮动才像真人随手刷。--interval 是"基准值"，实际每轮 ±20%。

退出码
    0 = 发现了新内容（详情打到 stdout）
    2 = 超时，期间没有任何变化（stdout 为 NO_MESSAGE）
    1 = 出错
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zy_client  # noqa: E402

# 2026-10-17：不再写死具体文件夹 ID。默认根目录 "0"，
# 需要盯某个频道时用 --folder 或 config.json 指定。
DEFAULT_FOLDER = "0"
STATE_SUFFIX = "zy_watch_state.json"

# ⚠️ 必须忽略 AI 自己发的回复，否则会死循环：
# AI 回复 → 上传 → 文件夹内容变了 → 又被判定为"新内容" → 再回复 → …
IGNORE_PREFIX = "AI回复-"


# ------------------------------------------------------------------ 状态存取


def state_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ZhongYuToolBox", STATE_SUFFIX)


def load_state():
    p = state_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(st):
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ------------------------------------------------------------------ 频道快照


def folder_items(cli, folder_id):
    """取某个文件夹的直接子项（1 次请求），剔除「AI 自己的回复」和「子文件夹」。

    ⚠️ 2026-10-05 修：`GetByParentId` 会把**子文件夹**（type=0）也混在 noteList 里返回。
    用户在猫cat下建了「控制」子文件夹后，它的 updateTime 比所有笔记都新 →
    被当成"最新消息"误回复了一条。所以这里必须把 type=0 的文件夹过滤掉。
    """
    q = cli.enc("parentid=%s&isNoteNode=true" % folder_id)
    text = cli._authed_get(
        cli.api_base + "/CloudNotes/api/Notes/GetByParentId?" + q, timeout=60)
    inner = json.loads(cli.dec(json.loads(text)["data"]))
    items = inner.get("noteList", []) or []
    out = []
    for n in items:
        if n.get("type") == 0:                      # 子文件夹，不是消息
            continue
        if str(n.get("fileName") or "").startswith(IGNORE_PREFIX):
            continue
        out.append(n)
    return out


def fingerprint(items):
    """频道指纹：对每个子项的「名字 + 更新时间 + fileId」排序后取 sha1。"""
    rows = []
    for n in items:
        rows.append("%s|%s|%s" % (n.get("fileId"), n.get("fileName"),
                                  n.get("updateTime") or n.get("createTime")))
    rows.sort()
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()


def brief(items, limit=10):
    items = sorted(items, key=lambda x: str(x.get("updateTime") or ""), reverse=True)
    return [{"fileId": n.get("fileId"), "fileName": n.get("fileName"),
             "type": n.get("type"), "updateTime": n.get("updateTime")}
            for n in items[:limit]]


# ------------------------------------------------------------------ 主循环


def main():
    ap = argparse.ArgumentParser(description="蹲守云笔记频道等新内容")
    ap.add_argument("--folder", default=DEFAULT_FOLDER, help="频道文件夹 fileId")
    ap.add_argument("--interval", type=int, default=120,
                    help="检查间隔基准（秒），默认 120；实际每轮 ±20%% 抖动。别低于 60")
    ap.add_argument("--timeout", type=int, default=540,
                    help="本次蹲守总时长（秒），默认 540（单条命令上限 600s，留余量）")
    ap.add_argument("--reset", action="store_true", help="忽略已存的状态，重新建基线")
    ap.add_argument("--allow-fast", action="store_true",
                    help="允许 15 秒以下的间隔。仅用于**手动值守/演示**——"
                         "无人值守的自动任务不要用，会对服务器造成不必要的压力")
    args = ap.parse_args()

    # 无人值守自动任务：默认 120 秒（±20% 抖动），别低于 60。
    # 演示/手动值守时可以快一些，但要显式加 --allow-fast。
    floor = 1 if args.allow_fast else 15
    if args.interval < floor:
        print("[错误] --interval 不要小于 %d 秒（会对服务器造成不必要的压力）；"
              "演示场景确需更小请加 --allow-fast" % floor)
        return 1

    cli = zy_client.build_client()
    st = load_state()
    key = args.folder
    prev = None if args.reset else (st.get(key) or {}).get("fingerprint")

    deadline = time.time() + args.timeout
    rounds = 0

    while True:
        rounds += 1
        try:
            items = folder_items(cli, args.folder)
        except Exception as e:
            print("[错误] 查询频道失败：%s" % e)
            return 1

        fp = fingerprint(items)
        now = datetime.datetime.now().strftime("%H:%M:%S")

        if prev is None:
            # 第一次跑：只记基线，不当成"新内容"
            st[key] = {"fingerprint": fp, "baseline": True,
                       "updatedAt": datetime.datetime.now().isoformat(timespec="seconds")}
            save_state(st)
            prev = fp
            print("[%s] 已建立频道基线（%d 项），继续蹲守…" % (now, len(items)))
        elif fp != prev:
            st[key] = {"fingerprint": fp,
                       "baseline": False,
                       "updatedAt": datetime.datetime.now().isoformat(timespec="seconds")}
            save_state(st)
            out = {
                "event": "folder_changed",
                "folderId": args.folder,
                "rounds": rounds,
                "folderItemCount": len(items),
                "items": brief(items),
                "hint": ("频道里出现了新内容。请逐个确认哪些是新来的（对比 fileId 与之前不同），"
                         "用 zy_read_note_text 读打字内容；若抽出 0 段文本说明是手写，"
                         "改用 zy_download_note 下载后识图。"),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        # 随机抖动：实际间隔 = 基准 ±20%（例：240s → 192~288s），
        # 每轮都不一样，避免固定节奏的行为指纹。
        nap = max(1, int(args.interval * random.uniform(0.8, 1.2)))

        if time.time() + nap >= deadline:
            print("NO_MESSAGE")
            return 2

        time.sleep(nap)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except zy_client.ZhongYuError as e:
        print("[出错] " + str(e))
        sys.exit(1)
