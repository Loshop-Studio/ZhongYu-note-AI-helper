"""agent 模式能用的工具注册表。

设计原则
--------
1. **只放「全功能(agent)」模式能用的工具**，共 12 个（11 个计配额 + 截图不计配额）。
2. **动进程的、改配置的一律不注册**：重启、关闭 WorkBuddy、思考档位、字数上限、
   检查频率、模式开关、搜索开关、校准余额 —— 模型在 tools 列表里看不到它们，
   就不存在"被诱导调用"的可能。这比在提示词里写「不要」可靠得多。
3. `handler` 内部才 `import control_bot`，避免模块级循环导入
   （control_bot → agent_loop → agent_tools → control_bot）。
4. `handler(args, ctx)` 统一返回 `(给模型看的文本, 截图路径或 None)`。
   - 文本是模型推理的依据，尽量保留原始数据（模型不会照抄，但它需要数字）
   - 截图路径由 agent_loop 收集，最终作为回复的 body_image 附上
5. 所有 handler 都必须自己兜异常 —— 一个工具炸掉不能把整轮 agent 带走。

ctx 里有什么
------------
    cli        云笔记客户端（截图/上传要用）
    question   用户这条笔记的原始标题（点击工具做"原话匹配"要用）
    img_urls   这条笔记附带的图片地址列表
    shot_paths 本轮已产生的截图文件路径（list，可追加）
"""

import os


# 危险目标词：出现在**点击目标**里就直接拒，不看模型怎么解释。
# ⚠️ 用白名单思路反着理解：这份表里的是"绝对不点"，
#    表外的靠"原话匹配"兜底，两层叠加。
CLICK_DANGER = ("删除", "移除", "卸载", "格式化", "清空", "清盘", "恢复出厂",
                "覆盖", "提交", "付款", "支付", "购买", "下单", "转账",
                "发送", "发布", "注销", "退出登录")


# ------------------------------------------------------------------ 点击相关

def _check_target(target, question):
    """原话匹配：目标必须是用户自己写的，不许模型编。

    双向判断，避免过度严格：
      a) 目标原样出现在原话里            → 通过（「点 保存按钮」+ target=保存按钮）
      b) 原话里「点 xxx」的 xxx 是目标的子串 → 通过（「点保存」+ target=保存按钮）
    这样模型把"保存"补全成"保存按钮"不会被误拒，而它自己瞎编的目标一定过不了。
    """
    import control_bot as cb

    nt, nq = cb.norm(target), cb.norm(question or "")
    if not nt:
        return False, "没给点击目标"
    if nt in nq:
        return True, ""
    want = cb.norm(cb.click_target(question or ""))
    if want and want in nt:
        return True, ""
    return False, ("目标「%s」和用户原话对不上（原话里要点的看起来是「%s」），"
                   "只能点用户明确写过的东西" % (target, want or "空"))


def _force_shot(ctx, note):
    """点击后的强制截图。

    ⚠️ 故意**不检查** 07:00~23:00 时间窗：点击本身已经过了那道检查，
       紧接着截图时若正好跨过 23:00，不该因为截图被拦而丢掉复核凭据。
    """
    import control_bot as cb

    cli = ctx.get("cli")
    if cli is None:
        return None
    try:
        paths, err = cb.do_screenshot(cli, str(note)[:30])
    except Exception as e:
        cb.log("点击后截图异常 %r" % (e,))
        return None
    if not paths:
        cb.log("点击后截图失败：%s" % (err,))
        return None
    png = paths[0] if isinstance(paths, (tuple, list)) else paths
    return png if png and os.path.exists(png) else None


def _do_click_guarded(args, ctx, button="left", double=False):
    """点击统一入口：原话匹配 → 危险词 → 执行 → 强制截图。"""
    import control_bot as cb

    args = args or {}
    target = str(args.get("target") or "").strip()

    ok, why = _check_target(target, ctx.get("question") or "")
    if not ok:
        return why, None

    hit = [w for w in CLICK_DANGER if w in target]
    if hit:
        return ("「%s」里含危险动作（%s），这个不点。"
                "直接告诉用户：这个我不敢点，让他回家自己弄。"
                % (target, "、".join(hit))), None

    try:
        done, msg = cb.do_click(target, button=button, double=double)
    except Exception as e:
        cb.log("点击异常 %r" % (e,))
        done, msg = False, repr(e)

    shot = _force_shot(ctx, "点了「%s」之后" % target)
    if shot:
        ctx.setdefault("shot_paths", []).append(shot)

    head = "点击%s：%s" % ("成功" if done else "失败", target)
    tail = "（已自动截图）" if shot else "（截图没抓下来，如实告诉用户）"
    return "%s\n%s\n%s" % (head, str(msg)[-300:], tail), shot


def _t_click(args, ctx):
    return _do_click_guarded(args, ctx, button="left", double=False)


def _t_dclick(args, ctx):
    return _do_click_guarded(args, ctx, button="left", double=True)


def _t_rclick(args, ctx):
    return _do_click_guarded(args, ctx, button="right", double=False)


# ------------------------------------------------------------------ 只读查询

def _t_state(args, ctx):
    import control_bot as cb
    return cb.report_state(), None


def _t_daily(args, ctx):
    import control_bot as cb
    return cb.report_daily(), None


def _t_quota(args, ctx):
    import control_bot as cb
    return cb.report_quota(), None


def _t_dsbal(args, ctx):
    import control_bot as cb
    return cb.report_dsbal(), None


def _t_dashboard(args, ctx):
    import control_bot as cb
    return cb.report_dashboard(), None


def _t_help(args, ctx):
    import control_bot as cb
    return cb.report_help(), None


# ------------------------------------------------------------------ 截图 / 看图

def _t_screenshot(args, ctx):
    """截屏。不调模型所以不花钱，但慢（5~10 秒），单独限次数。"""
    import control_bot as cb

    cli = ctx.get("cli")
    if cli is None:
        return "拿不到云笔记连接，截不了图", None
    try:
        paths, err = cb.do_screenshot(cli, (ctx.get("question") or "截个图")[:20])
    except Exception as e:
        cb.log("截图异常 %r" % (e,))
        return "截图异常：%r" % (e,), None
    if not paths:
        return "截图失败：%s" % (err,), None
    png = paths[0] if isinstance(paths, (tuple, list)) else paths
    if png and os.path.exists(png):
        ctx.setdefault("shot_paths", []).append(png)
        return "已截图，图片会附在回复里给用户看", png
    return "截图生成了但文件不见了", None


def _t_vision(args, ctx):
    import control_bot as cb

    urls = ctx.get("img_urls") or []
    if not urls:
        return "这条笔记里没有图片，没法看。告诉用户把照片插进笔记再发一次", None
    q = str((args or {}).get("question") or ctx.get("question") or "看看这张图").strip()
    try:
        return cb.do_vision(urls, q), None
    except Exception as e:
        cb.log("看图异常 %r" % (e,))
        return "看图失败：%r" % (e,), None


# ------------------------------------------------------------------ 联网搜索

def _t_search(args, ctx):
    import control_bot as cb

    q = str((args or {}).get("query") or "").strip()
    if not q:
        return "没给搜索词，没法搜", None
    try:
        items, err = cb.ds_search(q, 5)
    except Exception as e:
        cb.log("搜索异常 %r" % (e,))
        return "搜索异常：%r" % (e,), None
    if not items:
        return "搜索没成功：%s" % (err or "没有结果"), None

    lines = ["搜索「%s」的结果：" % q]
    for i, it in enumerate(items[:5], 1):
        title = str(it.get("title") or "").strip()
        site = str(it.get("site_name") or "").strip()
        date = str(it.get("date") or "").strip()
        summary = str(it.get("summary") or it.get("snippet") or "").strip()
        head = "%d. %s" % (i, title)
        meta = "｜".join([x for x in (site, date) if x])
        if meta:
            head += "（%s）" % meta
        lines.append(head)
        if summary:
            lines.append("   " + summary[:300])
    return "\n".join(lines), None


# ------------------------------------------------------------------ 注册表

# quota=True 表示计入「每轮 3 次」的总配额；False 表示独立计数
TOOLS = {
    "get_state": {
        "desc": "查看这台电脑现在怎么样：CPU 占用、内存、磁盘、WorkBuddy 是否在跑、"
                "值守接力链最后活动时间。用户问「电脑怎么样」「还好吗」时用它。",
        "params": {},
        "required": [],
        "handler": _t_state,
        "quota": True,
    },
    "get_daily": {
        "desc": "看今天和昨天各花了多少积分、消耗趋势。用户问「花了多少」「开销」时用它。",
        "params": {},
        "required": [],
        "handler": _t_daily,
        "quota": True,
    },
    "get_quota": {
        "desc": "看还剩多少积分、按当前速度还能撑多久。用户问「还剩多少」「够不够用」时用它。",
        "params": {},
        "required": [],
        "handler": _t_quota,
        "quota": True,
    },
    "get_ds_balance": {
        "desc": "查 DeepSeek API 账户余额（那个是美元计价的 API，和积分是两回事）。",
        "params": {},
        "required": [],
        "handler": _t_dsbal,
        "quota": True,
    },
    "get_dashboard": {
        "desc": "看当前遥控器的设置：运行模式、检查频率、思考强度、回答上限、上次巡检时间。",
        "params": {},
        "required": [],
        "handler": _t_dashboard,
        "quota": True,
    },
    "get_help": {
        "desc": "看这台电脑支持哪些指令的完整说明。用户问「你能干什么」「有哪些指令」时用它。",
        "params": {},
        "required": [],
        "handler": _t_help,
        "quota": True,
    },
    "screenshot": {
        "desc": "给电脑屏幕拍一张照片发给用户。只在 07:00~23:00 有效。"
                "目标看不清、需要确认屏幕上有什么的时候可以用；不要为了保险反复截。",
        "params": {
            "reason": {"type": "string", "description": "为什么要截图（一句话）"},
        },
        "required": [],
        "handler": _t_screenshot,
        "quota": False,          # 不计入总配额，单独限次数
    },
    "click": {
        "desc": "在屏幕上点一下某个东西。**只有用户在指令里明确写了「点/点击/点开 xxx」才能用**，"
                "target 必须照抄用户写的目标，不许自己编。点完会自动截图。",
        "params": {
            "target": {"type": "string",
                       "description": "要点的东西，照抄用户原话，例如「保存按钮」"},
        },
        "required": ["target"],
        "handler": _t_click,
        "quota": True,
    },
    "double_click": {
        "desc": "双击屏幕上的某个东西。规则同 click，用户明确说「双击 xxx」才能用。",
        "params": {
            "target": {"type": "string", "description": "要双击的东西，照抄用户原话"},
        },
        "required": ["target"],
        "handler": _t_dclick,
        "quota": True,
    },
    "right_click": {
        "desc": "在屏幕上的某个东西上点右键。规则同 click，用户明确说「右键 xxx」才能用。",
        "params": {
            "target": {"type": "string", "description": "要右键的东西，照抄用户原话"},
        },
        "required": ["target"],
        "handler": _t_rclick,
        "quota": True,
    },
    "look_at_image": {
        "desc": "读取这条笔记里附带的照片/截图，做题目识别、文字提取、图表解读。"
                "只有在笔记里确实有图片时才有效。",
        "params": {
            "question": {"type": "string",
                         "description": "要问图片的问题；不传则按用户原话处理"},
        },
        "required": [],
        "handler": _t_vision,
        "quota": True,
    },
    "web_search": {
        "desc": "联网搜索实时信息（天气、新闻、价格等）。**每轮最多 2 次**，用完就用已有信息回答。",
        "params": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
        "handler": _t_search,
        "quota": True,
    },
}


def build_schemas():
    """生成给 DeepSeek 的 tools 数组（OpenAI function calling 格式）"""
    out = []
    for name, t in TOOLS.items():
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t["desc"],
                "parameters": {
                    "type": "object",
                    "properties": t.get("params") or {},
                    "required": t.get("required") or [],
                },
            },
        })
    return out


def tool_names():
    return list(TOOLS.keys())


def is_quota(name):
    """这个工具是否计入「每轮 3 次」总配额"""
    return bool((TOOLS.get(name) or {}).get("quota", True))
