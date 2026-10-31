"""全功能（agent）模式的主循环。

一次「笔记标题」进来，交给模型自己决定调哪几个工具、调几次，
最后把工具结果组织成一条回答。

硬约束（全部是代码层，不靠提示词）
----------------------------------
    每轮工具调用      ≤ 3 次（截图不计入）
    其中 web_search   ≤ 2 次
    截图单独          ≤ 3 次
    总时长            ≤ 90 秒
    循环硬上限        8 轮（防模型来回试探）

刻意设计
--------
* **编排阶段强制关思考**：官方文档要求带 tools 时把上一轮 reasoning_content
  完整回传，漏了直接 400。关掉思考就绕开这个坑，而且更省钱。
* **配额用完就不给 tools**：模型只能基于已有信息作答，自然收尾，
  不用额外写"强制结束"的分支。
* **同一个工具连调两次不拦**：由提示词约束 + 3 次总配额兜底，
  代码层不额外加规则（否则模型正常的"先截图再点击"会被误伤）。
"""

import json
import time
import urllib.request

MAX_TOOL_CALLS = 3      # 每轮工具调用总配额（截图不计）
MAX_SEARCH = 2          # 联网搜索单独配额
MAX_SHOTS = 3           # 截图单独配额
AGENT_TIMEOUT = 90      # 秒
MAX_STEPS = 8           # 循环硬上限


AGENT_SYSTEM = """你是家里那台 Windows 电脑上的助手。用户在学校，用平板通过云笔记给你留言。
你可以调用工具操作电脑，也可以直接回答。回答会被渲染成一张图片发到他的平板上。

## 调用纪律

1. 这一轮最多调用 3 次工具（截图不计入）。次数用完就必须给结论，不许无限试探。
2. 联网搜索最多 2 次。两次还不够，就把已查到的整理出来，并说明没查全。
3. 能直接回答就直接答，不要为了显得努力去调工具——
   「什么是导数」「解释一下动量守恒」这类问题不需要查电脑状态。
4. 同一个工具不要连着调两次做同一件事。第一次没拿到想要的结果，
   换思路或者直接说明失败，不要重试第三次。
5. 工具返回「被拒绝」「不在时间窗」「次数已用完」时，不要换个说法再试一遍，
   直接把结果告诉用户。

## 关于点击（最容易出事，逐条遵守，不许跳）

1. 只有用户明确写了「点 / 点击 / 点开 / 双击 / 右键 xxx」时，才能调用
   click / double_click / right_click。用户没写"点"，绝不许点。
2. target 必须来自用户原话，原样照抄。不许替换、不许推断、
   不许顺便多点一个"看起来该点"的东西。
3. 目标描述模糊（「点那个东西」「点一下」）时，先 screenshot 看清楚再说。
   仍然确定不了，就直接问「你说的那个我没认出是哪个，能在标题里写具体点吗」，
   不要猜着点。
4. 一次只点一个。用户说点两个，就分两次说清楚，不要连着点。
5. 以下目标绝对不点，一律回「这个我不敢点，你回家自己弄」：
   删除、移除、卸载、格式化、清空、恢复出厂、覆盖、提交、付款、支付、
   购买、下单、转账、发送、发布、注销、退出登录。
   就算用户要点的东西紧挨着这些按钮，也宁可不点。
6. 点完必须说明：点了哪个、在哪个窗口点的、屏幕上现在看到什么。
7. 你点的每一次都会被人看到。点错了比不点更糟。

## 回答要求

- 直接给结论，别客套：不要用「好的」「当然可以」「我理解」这类开头。
- **工具返回的内容用户是看不到原文的**，你必须把关键数据写进回答里：
  数字、结论、时间、状态，一个都不能漏，也不许编。
- 长度按需要来：简单问题一两句说清；解题、分析这类该展开就写完整。
- 用短段落和分点，别写成一大坨；关键公式、结论单独占一行。
- 不要用 emoji。
- 遇到危险请求（删文件、关机、改密码、付款），直接说做不到，让他回家自己弄。
"""


def _post(key, body, timeout=60):
    """直连 DeepSeek（不走系统代理，理由见 control_bot 里的同类代码）"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _add_usage(acc, usage):
    """把一次请求的 usage 累加进总数（字段不全时按 0 处理）"""
    if not isinstance(usage, dict):
        return
    for k in ("prompt_tokens", "completion_tokens",
              "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        try:
            acc[k] = acc.get(k, 0) + int(usage.get(k) or 0)
        except Exception:
            pass


def _finish(answer, usage, cb):
    """收尾：没有正文时给兜底话术，末尾统一补花费"""
    txt = (answer or "").strip()
    if not txt:
        txt = ("# 没得出结果\n\n"
               "我试着办了，但没能给出结论。你换个说法再发一次，"
               "或者把要求写具体一点。")
    return cb.with_cost(txt, usage)


def run_agent(question, img_urls=None, cli=None):
    """跑一轮 agent。返回 (回答文本, 截图路径列表)。

    任何异常都不会往外抛 —— agent 失败也让用户看到理由，而不是"发了没反应"。
    """
    import control_bot as cb
    import agent_tools as at

    question = str(question or "").strip()
    ctx = {"cli": cli, "question": question,
           "img_urls": list(img_urls or []), "shot_paths": []}

    key = cb._load_ds_key()
    if not key:
        return ("# 没配 DeepSeek 密钥\n\n"
                "全功能模式要用它来调度工具，回电脑上打开设置填一下。"), []

    # 有图的话不把图塞进上下文（太贵），只告诉模型"你可以看图"
    user_text = question
    if ctx["img_urls"]:
        user_text += ("\n\n（这条笔记里附了 %d 张图片，需要看图请调用 look_at_image）"
                      % len(ctx["img_urls"]))

    msgs = [{"role": "system", "content": AGENT_SYSTEM},
            {"role": "user", "content": user_text}]

    used = {}
    total = 0
    shots = 0
    step = 0
    t0 = time.time()
    usage_sum = {"prompt_tokens": 0, "completion_tokens": 0,
                 "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
    schemas = at.build_schemas()

    cb.log("[agent] 开始：%r（图 %d 张）" % (question[:40], len(ctx["img_urls"])))

    while True:
        step += 1
        if step > MAX_STEPS:
            cb.log("[agent] 超过循环上限 %d 轮，强制收尾" % MAX_STEPS)
            return _finish("", usage_sum, cb), ctx["shot_paths"]

        if time.time() - t0 > AGENT_TIMEOUT:
            used_s = time.time() - t0
            cb.log("[agent] 超时（%.0fs），中断" % used_s)
            txt = ("# 这轮没跑完\n\n"
                   "我办了 %.0f 秒还没结束，先停下来了（一共调了 %d 次工具）。\n\n"
                   "要是上面已经有你要的结果，直接用就行；"
                   "没办完的部分，把要求写细一点再发一次。" % (used_s, total))
            return cb.with_cost(txt, usage_sum), ctx["shot_paths"]

        body = {
            "model": "deepseek-flash",
            "messages": msgs,
            "thinking": {"type": "disabled"},     # 编排不需要深度推理，省一半钱
            "max_tokens": 1500,
        }
        # 配额用完 → 不再给工具，逼它直接作答
        if total < MAX_TOOL_CALLS:
            body["tools"] = schemas

        try:
            data = _post(key, body)
        except Exception as e:
            cb.log("[agent] 请求失败 %r" % (e,))
            return ("# agent 请求失败\n\n%r\n\n"
                    "（可能是网络问题，过一会儿再发一次）" % (e,)), ctx["shot_paths"]

        _add_usage(usage_sum, data.get("usage"))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls = msg.get("tool_calls") or []

        # 不再调工具 → 这轮 content 就是最终回答
        if not calls:
            answer = (msg.get("content") or "").strip() \
                or (msg.get("reasoning_content") or "").strip()
            cb.log("[agent] 完成：%d 轮、%d 次工具、%d 张截图、%s token"
                   % (step, total, shots, usage_sum.get("total_tokens")
                      or (usage_sum["prompt_tokens"] + usage_sum["completion_tokens"])))
            return _finish(answer, usage_sum, cb), ctx["shot_paths"]

        # 把 assistant 的这次决定塞回上下文（tool_calls 必须原样带回）
        asst = {"role": "assistant", "content": msg.get("content") or "",
                "tool_calls": calls}
        if msg.get("reasoning_content"):
            asst["reasoning_content"] = msg["reasoning_content"]
        msgs.append(asst)

        for tc in calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}

            # ---- 未知工具（理论上不会发生，防御性处理）
            if name not in at.TOOLS:
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": "没有这个工具：%s" % name})
                continue

            # ---- 配额检查（硬拦）
            if at.is_quota(name):
                if total >= MAX_TOOL_CALLS:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "本轮工具调用次数已用完（上限 %d 次），"
                                            "请直接用已有信息回答。" % MAX_TOOL_CALLS})
                    continue
                if name == "web_search" and used.get("web_search", 0) >= MAX_SEARCH:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "搜索次数已用完（每轮上限 %d 次），"
                                            "请用已经查到的内容回答。" % MAX_SEARCH})
                    continue
                total += 1
                used[name] = used.get(name, 0) + 1
            else:
                if shots >= MAX_SHOTS:
                    msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                                 "content": "截图次数已用完（每轮上限 %d 张）。"
                                            % MAX_SHOTS})
                    continue
                shots += 1

            # ---- 执行
            argtxt = json.dumps(args, ensure_ascii=False)
            cb.log("[agent] 第%d轮 调 %s %s" % (step, name, argtxt[:180]))
            t1 = time.time()
            try:
                brief, shot = at.TOOLS[name]["handler"](args, ctx)
            except Exception as e:
                brief, shot = "工具执行失败：%r" % (e,), None
                cb.log("[agent] %s 异常 %r" % (name, e))
            cb.log("[agent] %s 用时 %.1fs 结果 %s"
                   % (name, time.time() - t1, str(brief)[:180]))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": str(brief)})
