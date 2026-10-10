# 架构与数据流

本文档说明「麦门权益中枢」的模块划分、数据流与关键设计决策。

---

## 1. 分层结构

```
┌───────────────────────────────────────────────────────┐
│  表现层  SKILL.md 《今日权益行动清单》输出模板          │
│          （P0~P3 分组 + 窗口描述 + 账户总览）           │
└───────────────────────────────────────────────────────┘
                          ▲
┌───────────────────────────────────────────────────────┐
│  编排层  scripts/perks_scan.py                        │
│          · MCPClient —— Streamable HTTP 传输          │
│          · collect()  —— 阶段 0/1 数据采集            │
│          · build_report() —— 汇总与统计               │
│          · render()   —— 清单渲染                     │
└───────────────────────────────────────────────────────┘
                          ▲
┌───────────────────────────────────────────────────────┐
│  领域层  scripts/expiry_scorer.py   窗口引擎【核心】   │
│          scripts/coupons_parser.py  券结构化解析      │
│          · parse → judge → score → summarize          │
└───────────────────────────────────────────────────────┘
                          ▲
┌───────────────────────────────────────────────────────┐
│  数据层  麦当劳 MCP Server（https://mcp.mcd.cn）       │
│          35 个工具，本项目使用其中 12 个               │
└───────────────────────────────────────────────────────┘
```

---

## 2. 数据流

```
now-time-info ──┐
                │
query-my-coupons ──► structuredContent.data.coupons[]
                │            │
                │            ▼
                │    coupons_parser.parse_coupon_obj()
                │            ├─ tradeStartDateTime/EndDateTime → start/end
                │            ├─ tradeTimePeriods[0]           → daily_start/end
                │            ├─ weekdays[1..7] ──_w2py()──►   Python weekday
                │            ├─ isAllTradeTime                → all_day
                │            ├─ currentDayAvailableCount      → 今日可用次数
                │            └─ instructions.labels[]         → 服务端状态
                │            │
                │            ▼
                │    CouponWindow（窗口对象）
                │            │
                │            ▼
                │    expiry_scorer
                │      ├─ judge_state()       → ACTIVE / PENDING / MISSED
                │      ├─ score_priority()    → P0 / P1 / P2 / P3
                │      ├─ next_occurrence()   → 下一个可用区间
                │      └─ summarize_window()  → 人类可读描述
                │            │
                ▼            ▼
available-coupons ──► 可领取券清单（couponStatus == CAN_GET）
query-my-account ──► 积分四象限（字符串 → float）
campaign-calendar ──► 当月活动
query-lottery-info ─► drawDecision（服务端资源判定）
query-my-prizes ────► 可用奖品
                │
                ▼
          build_report() → 结构化报告
                │
                ▼
          render() → 《今日权益行动清单》
```

---

## 3. 关键设计决策

### 3.1 为什么时间计算要下沉到脚本？

**问题**：LLM 处理日期/时段计算不可靠。例如"现在是 16:05，券的窗口是 05:00-10:29，
下次可用是明天 05:00，距今还有多久"——这类跨日推算极易出错，且不可复现。

**决策**：所有时间逻辑集中在 `expiry_scorer.py`，LLM 只负责调用与呈现。

**收益**：
- 结果确定性、可复现、可单测
- 边界条件（恰好开启 / 恰好结束 / 跨日 / 星期约束）得到系统性覆盖
- LLM 的上下文不必承载日期算术，降低出错面

### 3.2 为什么优先用结构化字段而非 Markdown 文本？

实测发现 `query-my-coupons` 同时返回：
- `structuredContent.data.coupons[]`：完整结构化对象（40+ 字段）
- `content[0].text`：人类可读 Markdown 摘要

**决策**：结构化优先，Markdown 仅作降级兜底。

**理由**：
- 结构化字段含 `tradeTimePeriods`、`weekdays`、`currentDayAvailableCount` 等
  文本中没有的精确信息
- 无需正则解析，鲁棒性更高
- 文本格式可能随服务端调整而变化

### 3.3 为什么 `weekdays` 需要显式转换？

麦当劳 MCP 的 `weekdays` 采用 **周日=1 … 周六=7** 编号，
而 Python `datetime.weekday()` 是 **周一=0 … 周日=6**。两者错位一天。

**决策**：在 `coupons_parser._w2py()` 做显式转换，并用单测锁定。

**风险若忽略**：所有涉及星期约束的券会被判定到错误的日期，
这是"静默错误"——结果看起来合理但实际上是错的。

### 3.4 为什么写操作要人工确认？

`auto-bind-coupons` 会自动领取全部可领券，`draw-lottery` 会消耗积分。
若自动执行，可能违背用户意愿（如不想现在消耗积分抽奖）。

**决策**：写操作零自动化，逐条确认，一次一件。

---

## 4. 状态机

```
                 ┌──────────────┐
   now < start   │   PENDING    │  score_priority:
   ──────────────►│   窗口未到    │    lead ≤24h  → P0
                 └──────┬───────┘    lead ≤72h  → P1
                        │            lead ≤168h → P2
             now ≥ start│            else       → P3
                        ▼
                 ┌──────────────┐
                 │    ACTIVE    │  score_priority:
                 │  窗口进行中   │    remain ≤24h → P0
                 └──────┬───────┘    else         → P1
                        │
              now > end │（且无后续窗口）
                        ▼
                 ┌──────────────┐
                 │    MISSED    │  score_priority: P3（沉底）
                 │  窗口已关闭   │
                 └──────────────┘
```

> 注：若券有多个窗口（如跨日区间的每日时段），`next_occurrence()` 会返回
> 下一个尚未错过的窗口，因此状态可能在 PENDING ↔ ACTIVE 间循环，直到最后一个窗口关闭。

---

## 5. 扩展点

| 扩展方向 | 切入点 |
|---|---|
| 多时段券（`tradeTimePeriods` 有多项） | `CouponWindow.occurrences()` 支持多时段展开 |
| 叠加活动优惠计算 | 阶段 3 串联 `calculate-price` + `query-promotions` |
| 门店级可用性校验 | 阶段 3 串联 `query-nearby-stores` + `query-store-coupons` |
| 主题活动预约 | 接入 `query-party-city` → `store` → `store-date` → `store-session` |
| 消费历史分析 | 接入 `order-list` / `mall-order-list` |

---

## 6. 测试策略

`tests/test_expiry_scorer.py` 共 18 项测试，覆盖：

- **解析层**：真实券形态、跨日区间、仅星期约束、空值、异常串
- **判定层**：窗口进行中、恰好开启、恰好结束、结束后一分钟、未来、已关闭
- **优先级**：P0/P1/P2/P3 四档边界
- **输出层**：描述文案格式、未识别兜底
- **集成层**：批量分析排序

```bash
python3 tests/test_expiry_scorer.py
# Ran 18 tests in 0.001s — OK
```
