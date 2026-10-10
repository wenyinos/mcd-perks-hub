# MCP 集成说明（MCP_INTEGRATION.md）

本文档说明「麦门权益中枢」实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。
所有字段与行为均基于 **2026-10-10 真机实测**（Token 真实调用线上 `https://mcp.mcd.cn`）。

---

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp`（麦当劳中国 MCP Server） |
| 版本 | `serverInfo.version = 1.0.0`，协议 `2025-03-26` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer ${MCD_MCP_TOKEN}` |
| 限流 | 600 次/分钟（超限 429） |

接入配置见 `mcp-config.example.json`（仅环境变量占位符，无真实凭证）。

---

## 2. 实际使用的 Tool（共 12 个）

### 2.1 核心链路（每次盘点必调）

| # | Tool | 调用目的 | 实测关键返回字段 |
|---|---|---|---|
| 1 | `now-time-info` | 锚定时间基准，所有窗口判定的起点 | `data.datetime`（`yyyy-MM-dd HH:mm:ss`）、`dayOfWeek` |
| 2 | `query-my-coupons` | 获取持有券（结构化） | `data.coupons[]`：`tradeStartDateTime`、`tradeEndDateTime`、`tradeTimePeriods[]`、`weekdays[]`、`isAllTradeTime`、`currentDayAvailableCount`、`enable`、`instructions.labels[]`、`discountInfo`、`tags[]` |
| 3 | `available-coupons` | 麦麦省可领券 | `data[].couponName`、`couponStatus`（`CAN_GET` / `RECEIVE_OTHER_CHANNEL`）、`label` |
| 4 | `query-my-account` | 积分账户 | `availablePoint`、`accumulativePoint`、`frozenPoint`、`expiredPoint`、`currentMouthExpirePoint`、`nextMouthExpirePoint` |
| 5 | `campaign-calendar` | 当月活动日历（无入参返回全量） | 按「往期回顾 / 今日 / 未来」分组的活动标题 + 内容 + 图片 |
| 6 | `query-my-prizes` | 已中奖品盘点 | `data.prizes[]`：`name`、`recordTime`、`statusText`（可用/已使用/已过期）、`timeRemindText` |

### 2.2 增值链路

| # | Tool | 调用目的 | 实测要点 |
|---|---|---|---|
| 7 | `query-lottery-info` | 抽奖活动状态与资源判定 | `activityStatusText`、`drawPoint`、`availableTimes`、**`drawDecision.resourceEligible` + `.reason`** |
| 8 | `mall-points-products` | 积分商城兑换建议 | `spuName`/`spuId`/`point`/`price`/`selling`/`upTime`/`downTime`/`status`（1仓库 2上架 3售罄 4下架 5预热） |

### 2.3 按需链路（落地验证，P0/P1 触发）

| # | Tool | 调用目的 |
|---|---|---|
| 9 | `query-nearby-stores` | 定位默认门店 |
| 10 | `query-store-coupons` | 验证券在该门店真实可用 |
| 11 | `calculate-price` | 用券 / 不用券双轨算价，量化「省多少」 |

### 2.4 写操作（用户确认后执行）

| # | Tool | 说明 |
|---|---|---|
| 12 | `auto-bind-coupons` | 一键领取麦麦省全部可领券（无需指定 couponId） |

> 本项目**不自动执行**任何写操作。`auto-bind-coupons` 需用户明确同意后才调用；
> `draw-lottery`、`create-order`、`mall-create-order` 同理（默认不纳入自动流程）。

---

## 3. 调用流程

```
┌─ 阶段 0 · 时间锚定 ────────────────────────────────┐
│  now-time-info → now = data.datetime               │
└────────────────────────────────────────────────────┘
                      ↓
┌─ 阶段 1 · 并行资产采集 ────────────────────────────┐
│  query-my-coupons  available-coupons               │
│  query-my-account  campaign-calendar               │
│  query-lottery-info  query-my-prizes               │
└────────────────────────────────────────────────────┘
                      ↓
┌─ 阶段 2 · 窗口计算（本地，零配额） ────────────────┐
│  coupons_parser.parse_coupon_obj()                 │
│    ├─ weekdays [1..7] → Python weekday（周日=1→6） │
│    ├─ tradeTimePeriods → 每日时段                  │
│    └─ isAllTradeTime → 全天标志                    │
│  expiry_scorer.judge_state()   → ACTIVE/PENDING/MISSED │
│  expiry_scorer.score_priority() → P0/P1/P2/P3      │
│  expiry_scorer.summarize_window() → 人类可读描述   │
└────────────────────────────────────────────────────┘
                      ↓
┌─ 阶段 3 · 落地验证（按需，仅 P0/P1） ──────────────┐
│  query-nearby-stores → query-store-coupons         │
│  → calculate-price（用券/不用券对比）              │
└────────────────────────────────────────────────────┘
                      ↓
┌─ 阶段 4 · 行动（逐条用户确认） ────────────────────┐
│  auto-bind-coupons（领券）                         │
└────────────────────────────────────────────────────┘
```

单次完整盘点调用 **6~7 次**（含验证约 10~11 次），远低于 600 次/分钟限流。

---

## 4. 实测踩坑与适配（本项目工程价值所在）

| # | 实测发现 | 适配方案 |
|---|---|---|
| 1 | **数值字段均为字符串**（`"20"`、`"472.1"`、`"29.9"`） | `_num()` 统一转 float 后再比较 / 排序 |
| 2 | **字段拼写为 `Mouth`**（`currentMouthExpirePoint` 等） | 严格照抄，不做"拼写纠正" |
| 3 | **券有效期含「每日时段」**（如 `05:00-10:29`），非单一截止日 | 引入「权益可用窗口」模型，见下节 |
| 4 | **`weekdays` 编号为 周日=1…周六=7**，与 Python `weekday()`（周一=0…周日=6）不同 | `_w2py()` 显式转换，并单测覆盖 |
| 5 | `query-my-coupons` 同时返回结构化 `structuredContent.data.coupons[]` 与 Markdown 文本 | **优先结构化字段**，文本仅作降级兜底 |
| 6 | `available-coupons` 的字段是 `couponStatus` / `label`，**不是** `status` | 按 `couponStatus == "CAN_GET"` 判定可领取 |
| 7 | 工具名与官方 README 不一致：README 写 `query-partystore-date`，线上为 `query-party-store-date` | 以线上实测名为准 |
| 8 | 线上含 README 未文档化的工具（如 `query-survey-coupon`、`query-promotions`） | 通过 `tools/list` 动态获取，不硬编码数量 |
| 9 | `mall-points-products` 响应中明确要求按指定表格格式与字段顺序展示，「禁止模型丢弃」 | 输出模板遵循官方字段顺序 |
| 10 | 抽奖提供 `drawDecision`（服务端已判定资源是否足够 + 原因） | 直接引用，不自行计算期望值 |
| 11 | 服务端已给出券状态标签（`instructions.labels`，如「未到使用时间」） | 作为本地判定的**交叉校验**，两者不一致时以本地窗口计算为准并提示 |

---

## 5. 业务价值

### 5.1 解决了什么问题

麦当劳用户的权益资产（券、积分、活动、抽奖）**分散在 4 个互不相通的功能入口**，
且券的有效期规则复杂（限日期 + 限时段 + 限星期 + 限渠道）。用户常见的损失：

- 券在「有效期内」但错过了当日时段（如早餐券只有 05:00-10:29）
- 积分临期未用，到期清零
- 麦麦省可领券忘记领取
- 抽奖次数 / 积分闲置

传统工具只做「哪天过期」提醒，**漏掉了时段维度**，导致"提醒了仍然用不上"。

### 5.2 本项目的解法

**「权益可用窗口」模型** —— 把有效期解析为 `日期 × 每日时段 × 星期约束` 的三维窗口，
输出三态（进行中 / 未到 / 已错过）与精确倒计时：

| 传统输出 | 本 Skill 输出 |
|---|---|
| 「某券 10-11 到期」 | 「⏰ 10-11（周日）05:00-10:29 开启，还有 12 小时 51 分——今晚记得设闹钟」 |
| 「某券 10-10 到期」 | 「❌ 今天 05:00-10:29 窗口已过，下次窗口 10-17」 |

### 5.3 量化收益（实测账户）

以实测账户为例，单次扫描识别出：

- **11 张限期早餐券**，覆盖 10-11 ~ 10-21，未过期总面值 **¥168.9**
- **3 张可领取券**（免费脆薯饼 / 麦旋风买一送一 / 9.9 元冰美式）待领取
- **4 份可用抽奖奖品**（立秋 / 大暑 / 小暑 / 夏至）待使用
- 积分：可用 20、累计 472.1、已过期 424.1（过期损失可视化）

若用户仅凭记忆管理，上述权益极易在时段错位中贬值。

### 5.4 与单纯「省钱助手」的差异

| 维度 | 省钱 / 凑单类 | 麦门权益中枢 |
|---|---|---|
| 关注对象 | 单次下单价格 | **账户存量权益** |
| 时间维度 | 几乎不考虑 | **窗口三态 + 精确倒计时** |
| 工具组合 | 菜单 + 算价 | **券 + 积分 + 活动 + 抽奖 4 域交叉** |
| 用户频次 | 点餐时 | **每日可查** |
| 合规风险 | 易触及代下单 | 纯查询 + 决策辅助 |

---

## 6. 合规说明

- 本项目仅通过**官方授权 Token** 访问麦当劳 MCP，未做任何逆向、破解或绕过限流。
- 所有写操作（领券 / 抽奖 / 下单）均需用户**显式确认**，不进行自动化批量操作。
- 仓库内不含任何真实 Token、密钥或个人信息；配置示例仅使用 `${MCD_MCP_TOKEN}` 占位符。
- 项目输出仅供参考，餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准。
- 本项目为参赛作品，非麦当劳官方产品。

---

## 7. 复现方式

```bash
# 1. 申请 Token：https://open.mcd.cn/mcp （手机号验证 → 控制台 → 激活）
# 2. 设置环境变量（不要写入任何文件）
export MCD_MCP_TOKEN="你的Token"

# 3. 安装依赖：无第三方依赖，Python 3.9+
# 4. 运行扫描
python3 scripts/perks_scan.py             # 《今日权益行动清单》
python3 scripts/perks_scan.py --json      # 结构化输出
python3 scripts/perks_scan.py --no-verify # 跳过商城校验

# 5. 运行单测
python3 tests/test_expiry_scorer.py
```

---

*本文件为麦当劳程序员创意开发大赛参赛材料。*
