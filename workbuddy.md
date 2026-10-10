# workbuddy.md —— WorkBuddy 开发对话上下文

> 本文件记录使用 **腾讯 WorkBuddy** 开发「麦门权益中枢 mcd-perks-hub」的对话上下文，
> 用于核验本参赛项目符合 WorkBuddy 专项奖励的联动条件。
>
> - 开发工具：腾讯 WorkBuddy（Agent 模式）
> - 开发时间：2026-10-10
> - 协作内容：MCP 工具实测摸底 → 核心算法设计 → 代码实现与单测 → 文档与参赛物料
> - 项目地址：https://github.com/wenyinos/mcd-perks-hub

---

## 会话一 · 立项与 MCP 能力摸底

**我：**

我申请到了麦当劳的 MCP Token，想在 WorkBuddy 里做一个 Skill：帮用户盘点麦当劳账户里快过期的权益。
先帮我把这个 MCP Server 的工具能力摸清楚。

**WorkBuddy：**

先握手确认连通性，再拉全量工具列表。

```bash
curl -s https://mcp.mcd.cn \
  -H "Authorization: Bearer $MCD_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"workbuddy","version":"1.0"}}}'
# → 握手成功
```

`tools/list` 结果：**线上实测 35 个工具**，按域分组后可归纳为四块：

| 域 | 代表工具 |
|---|---|
| 优惠权益 | `query-my-coupons`、`available-coupons`、`auto-bind-coupons`、`query-store-coupons`、`campaign-calendar` |
| 积分资产 | `query-my-account`、`mall-points-products`、`query-lottery-info`、`draw-lottery`、`query-my-prizes` |
| 点餐交易 | `query-nearby-stores`、`query-meals`、`calculate-price`、`create-order` |
| 主题活动 | `query-party-city/store/store-date/store-session`、`party-order-create` |

**有两个计划外发现：**

1. 存在 3 个官方 README **未文档化**的工具：`query-survey-coupon`（CSAT 答卷奖券）、`query-promotions`（团餐促销规则）、`query-party-store-date` / `session`。
2. **命名修正**：README 写 `query-partystore-date`，线上实际是 `query-party-store-date`（**多一个连字符**）。调用必须用线上实名。

---

## 会话二 · 真机实测与踩坑

**我：**

拿真实 Token 把核心接口都跑一遍，把返回结构摸清楚。重点是券和积分。

**WorkBuddy：**

跑完 7 个核心接口，踩到 5 个坑，全部是"文档不会告诉你的"：

**坑 1 · 数值字段全是字符串**

```json
{"availablePoint": "20", "accumulativePoint": "472.1", "discountValue": "29.9"}
```

不是数字，是 `"20"`、`"472.1"`。**排序/比较前必须显式转数值**，否则 `"9" > "10"` 成立。

**坑 2 · 字段拼写错误，但必须照抄**

接口字段是 `currentMouthExpirePoint` / `nextMouthExpirePoint` —— 用的是 `Mouth` 而不是 `Month`。
我一开始想"帮它纠正"，结果取不到值。**结论：照抄，不做纠正。**

**坑 3 · `weekdays` 的编号与 Python 相反**

```
weekdays: [1]   # 麦当劳：1 = 周日
```

Python 的 `datetime.weekday()` 里 `0 = 周一`。直接套用会把"周日"算成"周一"，
整个窗口判定全错。必须写一层显式转换。

**坑 4 · 麦麦省用 `couponStatus` 而不是 `status`**

`available-coupons` 返回的字段名与其它接口不一致，判定"可领取"要用 `couponStatus == "CAN_GET"`；
已领取的是 `RECEIVE_OTHER_CHANNEL`，不能重复推荐给用户。

**坑 5（最重要）· 券的有效期带「每日限定时段」，不是单纯日期**

`query-my-coupons` 返回的真实券：

```
## 巧克力味厚松饼猪柳蛋套餐
- 优惠: ¥29.9 (用券价格)
- 有效期: 2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29
- 标签: 到店专用
```

结构化字段：

```json
{
  "tradeStartDateTime": "2026-10-11 05:00:00",
  "tradeEndDateTime":   "2026-10-11 10:29:59",
  "tradeTimePeriods":   [{"startTime":"05:00:00","endTime":"10:29:59"}],
  "weekdays":           [1],
  "isAllTradeTime":     0
}
```

**含义：只在 10-11（周日）05:00-10:29 之间可用。**

---

## 会话三 · 核心洞察与算法设计

**我：**

我账户里 11 张券，全是这种早餐券。如果只做"哪天过期"的提醒，根本没用——因为**我每天起床的时候，当天的窗口早就过了**。
这个才是真痛点。帮我把它抽象成模型。

**WorkBuddy：**

这就构成了项目的核心壁垒：**「权益可用窗口」模型**。

传统做法只看截止日，漏掉了时段维度：

| 券类型 | 真实形态 | 传统提醒 | 本 Skill |
|---|---|---|---|
| 早餐券 | `05:00-10:29 周日` | "10-11 过期" | "仅今天 05:00-10:29 可用；现在 16:05，今天已错过" |
| 周末券 | 限周六日 | "某日过期" | "本周末可用，还剩两个窗口" |
| 全天券 | 至 23:59 | "今天过期" | "今天随时可用，还剩 7 小时 54 分" |

把有效期解析为 **`日期 × 每日时段 × 星期约束`** 的三维窗口，输出**三态判定**：

1. **窗口进行中** → P0，标注剩余倒计时
2. **窗口今日未到 / 已过** → 标注"今天 HH:MM 开启"或"今日已错过，下次窗口 X 日"
3. **窗口彻底关闭** → 移入"已错失"区，用于复盘

**关键工程决策：时间计算全部下沉到本地 Python 脚本，绝不交给 LLM 心算。**

原因很直接：让我推算"从 16:05 到 10:29 还剩多少分钟""跨日窗口怎么算"——极易出错，
而且错得毫无规律。脚本的结论是确定性的，还能写单测。

```python
def parse_window(validity_str: str) -> Window:
    """从 '2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29' 提取窗口"""
    # 1. 提取日期段（可能跨多日）
    # 2. 提取每日时段 05:00-10:29
    # 3. 提取星期约束（注意 周日=1 的转换）
    return Window(dates=[...], daily_start="05:00", daily_end="10:29", weekdays=[...])

def judge_state(w: Window, now: datetime) -> str:
    """三态：ACTIVE / PENDING / MISSED"""

def score_priority(w: Window, now: datetime) -> str:
    """剩余可用时长 → P0~P3"""
```

---

## 会话四 · 实现与单测

**我：**

按这个模型实现。要求：零第三方依赖，能直接跑；边界情况要有单测覆盖。

**WorkBuddy：**

产出三个脚本：

| 文件 | 职责 |
|---|---|
| `scripts/expiry_scorer.py` | 窗口引擎（核心算法）：解析、三态判定、P0~P3 打分 |
| `scripts/coupons_parser.py` | 券结构化解析（含 `weekdays` 周日=1 → Python weekday 转换） |
| `scripts/perks_scan.py` | 扫描入口：采集 → 计算 → 生成清单 |

单测覆盖 18 项，重点打边界：

```
test_p0_imminent (24 小时内开启 -> P0) ... ok
test_p1_within_3days (3 日内 -> P1) ... ok
test_p2_within_7days (7 日内 -> P2) ... ok
test_p3_missed (已关闭 -> P3 沉底) ... ok
test_pending_summary ... ok
test_unparsed_summary ... ok

Ran 18 tests in 0.001s
OK
```

**其中 `test_unparsed_summary` 对应的是一条硬约束**：有效期字符串解析失败时，
**原样展示并标注"未识别"，绝不臆造时段**。

---

## 会话五 · 真实账户验收

**我：**

跑一遍完整流程，看真实输出。

**WorkBuddy：**

```bash
python3 scripts/perks_scan.py
```

真实结果（本人账户，未造数据）：

```
🍔 麦门权益中枢 · 2026-10-10 16:15:21
==============================================
资产：券 11 张（进行中 0 / 未到 11 / 已关闭 0）｜积分可用 20｜本月将过期 0

【🔥 P0 · 立即行动】
- 巧克力味厚松饼猪柳蛋套餐
    ⏰ 10-11（周日）05:00-10:29 开启，还有 12 小时 44 分

【⏰ P1 · 三日内】
- 火腿扒堡早餐两件套
    ⏰ 10-12（周一）05:00-10:29 开启，还有 1 天 12 小时
...

【📊 账户总览】
- 可用 20 ｜累计 472.1 ｜冻结 0 ｜已过期 424.1
- 本月将过期 0 ｜下月将过期 0
```

**一句话读懂**：11 张券全是"每日 05:00-10:29"限时早餐券。
普通提醒只会说"10-11 到期"；本工具告诉你**"还有 12 小时 44 分，今晚设个 9:30 的闹钟"**。

积分维度同样有发现：可用 20、累计 472.1、**已过期 424.1** —— 说明"积分烂在账户里"是真实存在且量级巨大的损失。

---

## 会话六 · 参赛物料与发布

**我：**

整理成参赛项目：补齐必备文件、做安全检查、推到 GitHub 并发报名 Issue。
注意内部资料（方案文档、快速上手）不要上传。

**WorkBuddy：**

- `.gitignore` 增加 `方案文档.md`、`快速上手.md`
- 推送前扫描 `Bearer`，确认仓库内只有 `${MCD_MCP_TOKEN}` 占位符，无真实 Token
- 核对 `CONTEST_DECLARATION.md` 与官方原版**逐字节一致**（1522 字节）
- 提交 14 文件 / 2396 行 → 创建公开仓库 → 发布报名 Issue

**发布时发现官方报名校验的两条硬性格式陷阱**（写在活动仓库 README 里，不在指导文档中）：

1. 「简介标题」与「简介正文」之间**不能有换行符**（`项目简介：xxx` 必须同一行）
2. 「项目地址」**不能用 Markdown 链接格式**，必须纯文本 URL

我手上的示例文案正好是多行简介，照抄会导致**报名失败**，已手工合并为单行后提交。

---

## 附：WorkBuddy 协作要点小结

| 环节 | WorkBuddy 的作用 |
|---|---|
| MCP 能力摸底 | 握手验证 + 全量工具拉取，发现 3 个未文档化工具与 1 处命名差异 |
| 真机实测 | 跑通 7 个核心接口，定位 5 个字段/结构级坑（字符串数值、`Mouth` 拼写、`weekdays` 错位等） |
| 痛点抽象 | 从实测数据中提炼「权益可用窗口」模型，形成项目差异化壁垒 |
| 架构决策 | 将时间计算从 LLM 下沉到确定性脚本，规避模型心算误差 |
| 代码与测试 | 实现 3 个脚本 + 18 项单测，全部通过 |
| 文档与发布 | 生成 README / MCP_INTEGRATION / SKILL.md，完成脱敏检查与仓库发布 |

**说明**：本文件为开发过程的对话上下文整理导出，所有数据均来自真实 MCP 接口返回与本地实测，
未做虚构。项目源代码由参赛者与 WorkBuddy 协作产出。
