# 全功能模式 Agent 化 · 重构方案

> 状态：**设计稿，未实现**。审阅通过后再动代码。
> 基线：`app/control_bot.py`（2026-10-30 版，含识图关思考 + 思考四档 + 每轮花费显示）

---

## 一、三模式定义

| config 值 | 显示名 | 行为 | 关键词 | agent | 聊天 |
|---|---|---|---|---|---|
| `full` | **全功能** | 全部交给 agent 编排 | ❌ 关掉 | ✅ | ✅（0 步） |
| `control` | **仅控制** | 只执行关键词命中的动作 | ✅ | ❌ | ❌ |
| `chat` | **普通模式** | 原版行为：关键词动作 + 认不出就聊天 | ✅ | ❌ | ✅ |

**`chat` = 改造前的 `full`**。改造后 `full` 的语义变成「agent」，所以老用户升级时要迁移，见第八节。

### 模式切换指令
- `全功能` / `仅控制` / `普通模式`（也认「仅聊天」「只聊天」「普通」）
- `match_cmd` 增加：`"普通模式" in n or "仅聊天" in n or "只聊天" in n` → `mode_chat`

---

## 二、文件改动总览

| 文件 | 动作 | 说明 |
|---|---|---|
| `app/agent_tools.py` | **新增** | 12 个工具的注册表（schema + handler），handler 直接复用 `control_bot` 里的现有函数 |
| `app/agent_loop.py` | **新增** | agent 主循环、配额记账、工具调用分发、最终汇总 |
| `app/control_bot.py` | 修改 | 三模式判断、`full` 走 agent、help/dashboard 文案、`send_back` 支持附图 |
| `app/send_text.py` | 修改 | 新增 `--body-image` 参数（真实截图替换默认缩略图） |
| `app/setup_gui.py` | 修改 | 模式下拉从 2 项变 3 项 |
| `云笔记遥控器.spec` | 修改 | hiddenimports 加两个新模块 |
| `README.md` | 修改 | 三模式说明、agent 配额说明 |

---

## 三、工具表（12 个）

### 3.1 计入 3 次配额

| 工具 | 参数 | 复用函数 | 返回给模型的 brief |
|---|---|---|---|
| `get_state` | 无 | `report_state()` | 只给 CPU/内存/WorkBuddy 在跑没，≤80 字 |
| `get_daily` | 无 | `report_daily()` | 今天/昨天各花多少，≤60 字 |
| `get_quota` | 无 | `report_quota()` | 剩余积分 + 日均消耗，≤60 字 |
| `get_ds_balance` | 无 | `report_dsbal()` | DeepSeek 余额，≤40 字 |
| `get_dashboard` | 无 | `report_dashboard()` | 模式/频率/思考档位，≤80 字 |
| `get_help` | 无 | `report_help()` | 只给「有哪些指令」的清单，≤300 字 |
| `click` | `target`（必填） | `do_click()` | 点到没 + 屏幕坐标 |
| `double_click` | `target`（必填） | `do_click(double=True)` | 同上 |
| `right_click` | `target`（必填） | `do_click(button="right")` | 同上 |
| `look_at_image` | 无 | `do_vision()` | 看图结论摘要，≤400 字 |
| `web_search` | `query`（必填） | `ds_search()` | 前 3 条摘要，≤600 字 **（≤2 次）** |

### 3.2 不计入配额

| 工具 | 参数 | 说明 |
|---|---|---|
| `screenshot` | 无 | 截屏，**单独限 3 次**。不调模型不花钱，但慢（5~10 秒），不限会反复截 |

### 3.3 物理不注册（模型看不见 → 不可能调用）

- `restart`、`stopped_workbuddy`（**动进程**）
- `set_think`、`set_maxtok`、`set_freq`、`mode_full`、`mode_control`、`set_mode_chat`（**改配置**）
- `search_on`、`search_off`（**改配置**）
- `calibrate`（**改配置**，写余额基线）
- `nolink`（纯文字说明，无价值）

> **原则：不注册 > 提示词禁止。** 模型看不到的工具，不可能被诱导调用。

### 3.4 日限额（防成本失控）

| 项 | 值 | 说明 |
|---|---|---|
| agent 任务次数 | **100 次/天** | 约等于每天最多 ¥1~2 的额外开销 |
| 计数存储 | `control_state.json` 的 `agentDaily` 字段 | `{"date": "2026-10-30", "count": 17}`，跨天自动清零 |
| 超限行为 | **降级不瘫痪** | 不跑 agent，回执「今天 agent 次数用完了（100/100），我先按普通模式处理」，然后**自动退回 `match_cmd` 关键词路径** |
| 手动重置 | 发「重置 agent 次数」 | 想提前解封时用 |

> 降级设计的意义：限额到了不是"什么都不做"，而是**退化成普通模式继续干活**，只是不理解复合指令了。

---

## 四、agent loop 伪代码

```python
MAX_TOOL_CALLS = 3       # 总配额
MAX_SEARCH     = 2       # 搜索单独配额
MAX_SHOTS      = 3       # 截图单独配额
AGENT_TIMEOUT  = 90      # 总时长（秒），超了强制收尾

def run_agent(question, img_urls=None):
    msgs = [{"role": "system", "content": AGENT_SYSTEM},
            {"role": "user",   "content": build_user_content(question, img_urls)}]
    used = {}          # {"click": 1, "web_search": 2, ...}
    total = 0
    shots = 0
    t0 = time.time()
    shot_paths = []    # 点击后的强制截图，最终合并进回复

    while True:
        # ── 超时保护
        if time.time() - t0 > AGENT_TIMEOUT:
            return summarize(msgs) + "\n\n（超时中断，已完成的部分在上面）"

        body = {
            "model": "deepseek-flash",
            "messages": msgs,
            "tools": build_tools_schema(),          # 12 个，永远全给
            "thinking": {"type": "disabled"},       # 编排阶段强制关思考，省钱且避开
                                                    # reasoning_content 回传的 400 坑
            "max_tokens": 1200,
        }
        resp = post_json(DS_API, body, timeout=60)
        msg  = resp["choices"][0]["message"]
        calls = msg.get("tool_calls") or []

        if not calls:                               # 不调工具了 → 这就是最终回答
            answer = msg.get("content") or ""
            return with_cost(answer, resp.get("usage")), shot_paths

        for tc in calls:
            name = tc["function"]["name"]
            args = json.loads(tc["function"].get("arguments") or "{}")

            # ── 配额检查（硬拦，不靠提示词）
            if name == "screenshot":
                if shots >= MAX_SHOTS:
                    result = "截图次数已用完（本轮上限 3 张）"
                    msgs.append(tool_msg(tc, result)); continue
                shots += 1
            else:
                if total >= MAX_TOOL_CALLS:
                    result = "工具调用次数已用完（本轮上限 3 次），请直接用已有信息回答"
                    msgs.append(tool_msg(tc, result)); continue
                if name == "web_search" and used.get("web_search", 0) >= MAX_SEARCH:
                    result = "搜索次数已用完（本轮上限 2 次），请用已查到的内容回答"
                    msgs.append(tool_msg(tc, result)); continue
                total += 1
                used[name] = used.get(name, 0) + 1

            # ── 执行
            handler = TOOLS[name]["handler"]
            try:
                brief, shot = handler(args, question)     # question = 用户原话，给原话匹配用
            except Exception as e:
                brief, shot = "工具执行失败：%r" % (e,), None
            if shot:
                shot_paths.append(shot)
            msgs.append(tool_msg(tc, brief))
```

### 关键设计点

1. **编排阶段强制关思考**：带 `tools` 时必须回传 `reasoning_content`，否则 400；关掉思考就绕开这个坑，还省一半钱。
2. **最终回答不额外请求**：模型不再调工具的那一轮 `content` 就是答案，不重复付费。
3. **`question` 一直传下去**：`click` 工具要用它做「原话匹配」。
4. **每次工具调用都写日志**：`step/tool/args/耗时/结果摘要`，出问题能复盘。

---

## 五、点击的四层保护 + 强制截图

```python
CLICK_DANGER = ("删除", "移除", "卸载", "格式化", "清空", "清盘", "恢复出厂",
                "覆盖", "提交", "付款", "支付", "购买", "下单", "转账",
                "发送", "发布", "注销", "退出登录")

def tool_click(args, question):
    target = str(args.get("target") or "").strip()
    if not target:
        return "没给点击目标", None

    # 第 2 层：原话匹配 —— 目标必须是用户自己写的，不许模型编
    if norm(target) not in norm(question):
        return ("目标「%s」不在用户原话里，不能点。只能点用户明确写过的东西"
                % target), None

    # 第 3 层：危险目标 —— 一律拒
    hit = [w for w in CLICK_DANGER if w in target]
    if hit:
        return "「%s」含危险动作（%s），这个我不点，让用户回家自己弄" % (
            target, "、".join(hit)), None

    # 执行
    ok, msg = do_click(target)

    # 第 4 层：强制截图（免时间窗，失败也截）
    shot = force_screenshot_no_window()      # 新增：跳过 SHOT_WINDOW 检查
    tail = "（已截图）" if shot else "（截图没抓下来）"
    return "点击%s：%s %s" % ("成功" if ok else "失败", msg[-200:], tail), shot
```

### 强制截图的实现要点

| 要点 | 做法 |
|---|---|
| 不占配额 | 截图是 `click` 的**内部动作**，不是独立工具调用 |
| 模型跳不掉 | 不做成「先 click 再 screenshot」两步，模型会忘 |
| 失败也截 | 没点到时更要截——让你看到屏幕上到底什么样 |
| 免时间窗 | 点击本身已在 07:00–23:00 内，截图紧跟其后，不能因为跨了 23:00 就被拦 |
| 多张合并 | 3 次点击 = 3 张图，纵向拼接成一张再作为 `body_image` 上传 |

---

## 六、系统提示词（AGENT_SYSTEM）

```
你是家里那台 Windows 电脑上的助手。用户在学校，用平板通过云笔记给你留言。
你可以调用工具操作电脑，也可以直接回答。

## 调用纪律

1. 这一轮最多调用 3 次工具（截图不计入）。用完就必须给结论，不许无限试探。
2. 联网搜索最多 2 次。两次还不够，就把已查到的整理出来并说明没查全。
3. 能直接回答就直接答，不要为了显得努力去调工具——
   「什么是导数」不需要查电脑状态。
4. 同一个工具不要连续调两次做同一件事。第一次没拿到想要的结果，
   换思路或者直接说明失败，不要重试第三次。
5. 工具返回「被拒绝」「不在时间窗」「次数已用完」时，不要换个说法再试，
   直接告诉用户结果。

## 关于点击（最容易出事，逐条遵守，不许跳）

1. 只有用户明确写了「点 / 点击 / 点开 / 双击 / 右键 xxx」时，才能调用
   click / double_click / right_click。用户没写"点"，绝不许点。
2. target 必须来自用户原话，原样照抄。不许替换、不许推断、
   不许顺便多点一个"看起来该点"的东西。
3. 目标模糊（「点那个东西」「点一下」）时，先 screenshot 看清楚再说。
   仍然确定不了，就直接问「你说的那个我没认出是哪个，能写具体点吗」，不要猜着点。
4. 一次只点一个。用户说点两个，就分两次说清楚，不要连着点。
5. 以下目标绝对不点，一律回「这个我不敢点，你回家自己弄」：
   删除、移除、卸载、格式化、清空、恢复出厂、覆盖、提交、付款、支付、
   购买、下单、转账、发送、发布、注销、退出登录。
   就算用户要点的东西紧挨着这些按钮，也宁可不点。
6. 点完必须说明：点了哪个、在哪个窗口点的、屏幕上现在看到什么。
7. 你点的每一次都会被人看到。点错了比不点更糟。

## 回答风格

- 直接给结论，别客套：不要「好的」「当然可以」「我理解」这类开头。
- 长度按需要来；关键公式、结论单独占一行。
- 用短段落和分点，别写成一大坨。
- 不要用 emoji。
- 遇到危险请求（删文件、关机、改密码、付款），直接说做不到，让他回家自己弄。
```

> `prompt_full`（用户在设置里自定义的聊天提示词）依旧生效，追加在「回答风格」之后。

---

## 七、`control_bot.py` 改造点（带行号）

| 位置 | 现状 | 改法 |
|---|---|---|
| L146 `CMDS` | 14 组关键词 | 不动（`control` / `chat` 模式还要用） |
| L171 `match_cmd` | 只认几种 | 加 `mode_chat` 识别（`仅聊天` / `只聊天`） |
| L1438 `report_help` | 两模式说明 | 改三模式 + agent 配额说明 + 点击要原话 |
| L1497 `report_dashboard` | 两模式 | 改三模式；`full` 时显示「agent 模式·工具配额 3」 |
| L1949 `do_click` | 写死 `--click` | 不动（agent 直接复用） |
| L2008 `send_back` | 只发文字 | 加 `image=None` 参数 → 传给子进程 `--body-image` |
| L2286 `mode = ...` | `full` / `control` | 加 `chat`；`full` 走 agent 分支 |
| L2547 `mode_full` | 设 `full` | 语义改为「切到 agent」，文案要写清会花钱 |
| L2553 `mode_control` | 设 `control` | 不变 |
| 新增 | — | `mode_chat` 分支 |
| 主循环 L2259 附近 | `cmd = match_cmd(name)` | `full` 模式下**跳过** `match_cmd`，直接 `run_agent()`；`control` / `chat` 保持原样 |

### 主循环改造形状

```python
mode = str(cfg.get("mode") or "control").lower()

if mode == "full":                      # 全功能 = agent，不走关键词
    text, shots = run_agent(name, img_urls)
    title = "agent"
else:                                   # control / chat 走原来的路
    cmd = match_cmd(name)
    ...原有逻辑不动...
```

### `send_back` 附图

```python
def send_back(title, name, text, dry=False, parent=None, image=None):
    ...
    extra = ["--body-image", image] if image else []
    cmd = _child_cmd("send_text", ["--text-file", tmp, "--title", title,
                                   "--name", name,
                                   "--parent", parent or CONTROL_DIR] + extra)
```

`send_text.py` 对应加：
```python
ap.add_argument("--body-image", help="页内真实图片（截图），不给则用文字缩略图")
...
body_image_path=args.body_image or jpg
```

---

## 八、兼容与迁移（重要）

升级后 `full` 的语义从「关键词+聊天」变成了「agent」。**如果不处理，用户升级完会发现每条指令都开始花钱。**

**建议：升级时自动迁移 `full` → `chat`**，保持原有行为不变。

```python
def load_config():
    cfg = json.load(...)
    if cfg.get("_schema", 0) < 2:
        if str(cfg.get("mode") or "").lower() == "full":
            cfg["mode"] = "chat"        # 老 full = 新 chat（原版行为）
        cfg["_schema"] = 2
        save_config(cfg)
    return cfg
```

用户要启用 agent，必须**手动发「全功能」**。这样新功能不会突然上线咬人。

---

## 九、分阶段实施

| 阶段 | 内容 | 风险 | 可验证点 |
|---|---|---|---|
| **1** | `agent_tools.py` 工具表 + `--body-image` 打通 | 低（不改行为） | 手工调每个 handler，输出和现有回复一致 |
| **2** | `agent_loop.py` + `full` 模式接入 | 中 | 用 `--once --force` 单条测试，看日志 step 记录 |
| **3** | 三模式切换 + 迁移 + 文案 | 低 | 三种模式各发一条指令验证 |
| **4** | 点击四层保护 + 强制截图 | **高** | 故意发「点 删除」验证被拦；发「点 保存」验证有截图 |
| **5** | 打包 + 全链路回归 | 中 | 真机发 5 条不同类型的指令 |

每阶段结束都要：语法检查 → 单元验证 → 汇报 → 等下一个指令。

---

## 十、风险与待确认

| 风险 | 缓解 |
|---|---|
| agent 选错工具 | 只给 12 个工具，schema 描述写细；日志可复盘 |
| 成本失控 | 3 次配额 + 2 次搜索 + 日上限（建议 200 次/天，超了退回关键词） |
| 上传失败导致重跑 agent | **执行结果先落盘再上传回复**；回复失败只重传，不重跑 agent |
| 截图慢拖长巡检 | agent 期间不巡检（已确认）；总超时 90 秒 |
| 提示词被绕过 | 原话匹配 + 危险词是**代码层**，不是提示词 |
| 新模块打包漏掉 | `spec` 的 hiddenimports 同步加，打完包先在别的目录试跑 |

### 已确认的决定（2026-10-30 23:20）

1. **第三模式命名为「普通模式」**（config 值 `chat`），同时兼容「仅聊天」「只聊天」「普通」作为别名
2. **日限额 100 次/天**，超限降级回关键词路径继续干活，不瘫痪
3. **迁移策略**：升级后老配置的 `full` 自动迁移为 `chat`，保持原有行为不变；要启用 agent 必须手动发一次「全功能」
4. **点击不做圈图确认**，靠「提示词 + 原话匹配 + 危险词 + 强制截图」四层
5. **点击后强制截图**，作为 `body_image` 附在同一条回复里
