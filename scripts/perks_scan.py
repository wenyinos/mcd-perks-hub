#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门权益中枢 · MCP 数据采集与清单生成
======================================

从麦当劳 MCP 拉取权益数据，交由窗口引擎分析，生成《今日权益行动清单》。

用法
----
    export MCD_MCP_TOKEN="你的Token"
    python3 scripts/perks_scan.py                 # 完整清单
    python3 scripts/perks_scan.py --json          # 仅输出结构化 JSON
    python3 scripts/perks_scan.py --no-verify     # 跳过门店校验（省配额）

设计原则
--------
1. Token 只从环境变量读取，绝不写入任何文件。
2. 数值字段（积分/价格）统一做字符串 → 数值转换（实测返回均为 string）。
3. 字段名严格照抄服务端（含 currentMouthExpirePoint 的 Mouth 拼写）。
4. 只读接口默认调用；写操作（领券/抽奖/下单）一律不自动执行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from urllib import request, error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from expiry_scorer import (  # noqa: E402
    analyze, WindowState, CouponWindow, judge_state, score_priority,
    summarize_window,
)

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL = "2025-03-26"


# --------------------------------------------------------------------------
# MCP 传输层
# --------------------------------------------------------------------------

class MCPClient:
    """极简 Streamable HTTP 客户端。"""

    def __init__(self, token: str, timeout: int = 25):
        if not token:
            raise ValueError("缺少 Token：请设置环境变量 MCD_MCP_TOKEN")
        self.token = token
        self.timeout = timeout
        self._id = 0

    def _call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id,
                   "method": method, "params": params or {}}
        req = request.Request(
            MCP_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as e:
            code = e.code
            if code == 401:
                raise RuntimeError("401 Token 无效或已过期，请回 open.mcd.cn 重新激活") from e
            if code == 429:
                raise RuntimeError("429 触发限流（600 次/分钟），请降低频率") from e
            raise
        return json.loads(body)

    def tool(self, name: str, arguments: dict | None = None) -> dict:
        """调用工具，返回 structuredContent（缺失时回退解析文本）。"""
        r = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        result = r.get("result", {})
        if "structuredContent" in result:
            return result["structuredContent"]
        # 回退：从 content[0].text 中提取
        text = ""
        for c in result.get("content", []):
            if c.get("type") == "text":
                text = c.get("text", "")
                break
        return {"_text": text}


# --------------------------------------------------------------------------
# 数据采集
# --------------------------------------------------------------------------

def _num(v) -> float:
    """实测数值字段均为字符串，统一转换。"""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def collect(client: MCPClient, verify: bool = True) -> dict:
    """并行度受限于同步实现，此处按依赖顺序采集。"""
    data: dict = {}

    # 阶段 0 · 时间锚定
    data["time"] = client.tool("now-time-info")

    # 阶段 1 · 资产采集
    for key, tool in (
        ("coupons", "query-my-coupons"),
        ("available", "available-coupons"),
        ("account", "query-my-account"),
        ("campaigns", "campaign-calendar"),
        ("lottery", "query-lottery-info"),
        ("prizes", "query-my-prizes"),
    ):
        try:
            data[key] = client.tool(tool)
        except Exception as e:  # noqa: BLE001
            data[key] = {"_error": str(e)}

    if verify:
        try:
            data["mall"] = client.tool("mall-points-products")
        except Exception as e:  # noqa: BLE001
            data["mall"] = {"_error": str(e)}

    return data


# --------------------------------------------------------------------------
# 清单生成
# --------------------------------------------------------------------------

def build_report(data: dict, now: datetime) -> dict:
    cfg = data.get("account", {}).get("data", {}) if isinstance(data.get("account"), dict) else {}
    rows = analyze_coupons(data.get("coupons", {}), now)

    groups: dict[str, list] = {"P0": [], "P1": [], "P2": [], "P3": []}
    for r in rows:
        groups.setdefault(r["priority"], []).append(r)

    acct = {
        "availablePoint": _num(cfg.get("availablePoint")),
        "accumulativePoint": _num(cfg.get("accumulativePoint")),
        "frozenPoint": _num(cfg.get("frozenPoint")),
        "expiredPoint": _num(cfg.get("expiredPoint")),
        "currentMouthExpirePoint": _num(cfg.get("currentMouthExpirePoint")),
        "nextMouthExpirePoint": _num(cfg.get("nextMouthExpirePoint")),
    }

    avail = data.get("available", {})
    claimable = count_claimable(avail)
    claimable_list = extract_claimable(avail)

    lottery = data.get("lottery", {}).get("data", {}) if isinstance(data.get("lottery"), dict) else {}
    decision = lottery.get("drawDecision", {}) or {}
    prizes = data.get("prizes", {}).get("data", {}).get("prizes", []) if isinstance(data.get("prizes"), dict) else []
    usable_prizes = [p for p in prizes if p.get("statusText") == "可用"]

    sc = {
        "ACTIVE": sum(1 for r in rows if r["state"] == WindowState.ACTIVE),
        "PENDING": sum(1 for r in rows if r["state"] == WindowState.PENDING),
        "MISSED": sum(1 for r in rows if r["state"] == WindowState.MISSED),
    }
    total_value = sum(r["value_num"] for r in rows if r["state"] != WindowState.MISSED)

    return {
        "generated_at": now.isoformat(sep=" "),
        "coupon_total": len(rows),
        "groups": groups,
        "account": acct,
        "claimable_count": claimable,
        "claimable_list": claimable_list,
        "total_value": round(total_value, 2),
        "lottery": {
            "name": lottery.get("activityName"),
            "status": lottery.get("activityStatusText"),
            "draw_point": lottery.get("drawPoint"),
            "resource_eligible": decision.get("resourceEligible"),
            "reason": decision.get("reason"),
        },
        "usable_prizes": usable_prizes,
        "state_counts": sc,
    }


def count_claimable(avail: Any) -> int:
    """统计麦麦省中「可领取」状态的券数量。

    实测（2026-10-10）返回结构：
        data: [{couponName, couponImage, couponStatus: "CAN_GET"|"RECEIVE_OTHER_CHANNEL", label: "可领取"|"已领取"}]
    注意字段是 couponStatus / label，**不是** status。
    """
    if not isinstance(avail, dict):
        return 0
    items = avail.get("data")
    if isinstance(items, list):
        return sum(1 for x in items
                   if isinstance(x, dict)
                   and (x.get("couponStatus") == "CAN_GET" or x.get("label") == "可领取"))
    # 文本兜底
    text = avail.get("_text", "") or ""
    return text.count("状态：可领取")


def extract_claimable(avail: Any) -> list[dict]:
    """提取可领取券清单（用于「待领取的羊毛」区块）。"""
    out: list[dict] = []
    if not isinstance(avail, dict):
        return out
    items = avail.get("data")
    if isinstance(items, list):
        for x in items:
            if not isinstance(x, dict):
                continue
            if x.get("couponStatus") == "CAN_GET" or x.get("label") == "可领取":
                out.append({
                    "name": str(x.get("couponName", "")).strip(),
                    "status": str(x.get("couponStatus", "")),
                    "label": str(x.get("label", "")),
                })
    return out


def analyze_coupons(coupons_raw: Any, now: datetime) -> list[dict]:
    """解析券并交给窗口引擎分析。优先结构化字段，降级 Markdown 文本。"""
    from coupons_parser import parse_coupon_obj  # noqa: E402

    objs: list[dict] = []
    if isinstance(coupons_raw, dict):
        data = coupons_raw.get("data")
        if isinstance(data, dict) and isinstance(data.get("coupons"), list):
            objs = [x for x in data["coupons"] if isinstance(x, dict)]

    results: list[dict] = []

    if objs:
        for o in objs:
            v = parse_coupon_obj(o)
            w = CouponWindow(
                raw=v.raw.get("datetimeText", ""),
                days=[v.start.date()] if v.start else [],
                daily_start=v.daily_start,
                daily_end=v.daily_end,
                weekdays=list(v.weekdays_py),
                all_day=v.all_day,
                # 跨日券：补齐日期区间
            )
            if v.start and v.end and v.end.date() > v.start.date():
                span = (v.end.date() - v.start.date()).days
                if span <= 366:
                    from datetime import timedelta
                    w.days = [v.start.date() + timedelta(days=i) for i in range(span + 1)]

            state = judge_state(w, now)
            nxt = w.next_occurrence(now)
            results.append({
                "name": v.title,
                "raw_validity": v.raw.get("datetimeText", ""),
                "state": state,
                "state_label": WindowState.LABEL[state],
                "priority": score_priority(w, now),
                "summary": summarize_window(w, now),
                "next_start": nxt[0].isoformat(sep=" ") if nxt else None,
                "next_end": nxt[1].isoformat(sep=" ") if nxt else None,
                "last_deadline": (w.last_deadline().isoformat(sep=" ")
                                  if w.last_deadline() else None),
                "all_day": w.all_day,
                "value_text": v.value_text,
                "value_num": _num(v.discount_value),
                "server_label": v.label,
                "weekday": v.weekday_label,
                "tags": v.tags,
                "today_available": v.today_available,
            })
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        results.sort(key=lambda x: (
            order.get(x["priority"], 9),
            x["next_start"] or "9999",
            x["name"],
        ))
        return results

    # 降级：Markdown 文本解析
    items = extract_coupon_items(coupons_raw)
    return analyze(items, now)


def extract_coupon_items(coupons_raw: dict) -> list[tuple[str, str]]:
    """从 query-my-coupons 的 Markdown 文本中提取 (券名, 有效期)。降级通路。"""
    import re
    text = coupons_raw.get("_text", "") or ""
    items: list[tuple[str, str]] = []
    blocks = re.split(r"(?m)^##\s+", text)
    for b in blocks[1:]:
        lines = [ln.strip() for ln in b.strip().splitlines()]
        name = lines[0].strip() if lines else ""
        validity = ""
        for ln in lines:
            if "有效期" in ln:
                validity = re.sub(r"^[-\s*]*\*{0,2}有效期\*{0,2}\s*[:：]\s*", "", ln).strip()
                break
        if name:
            items.append((name, validity))
    return items


def render(report: dict) -> str:
    """渲染《今日权益行动清单》。"""
    L: list[str] = []
    a = report["account"]
    L.append(f"🍔 麦门权益中枢 · {report['generated_at']}")
    L.append("=" * 46)

    sc = report["state_counts"]
    L.append(f"资产：券 {report['coupon_total']} 张"
             f"（进行中 {sc['ACTIVE']} / 未到 {sc['PENDING']} / 已关闭 {sc['MISSED']}）"
             f"｜积分可用 {a['availablePoint']:g}"
             f"｜本月将过期 {a['currentMouthExpirePoint']:g}")
    L.append("")

    titles = {
        "P0": "【🔥 P0 · 立即行动】",
        "P1": "【⏰ P1 · 三日内】",
        "P2": "【📅 P2 · 七日内】",
        "P3": "【📦 P3 · 其他 / 已错过】",
    }
    for p in ("P0", "P1", "P2", "P3"):
        rows = report["groups"].get(p) or []
        if not rows:
            continue
        L.append(titles[p])
        for r in rows:
            L.append(f"- {r['name']}")
            L.append(f"    {r['summary']}")
        L.append("")

    L.append("【💰 待领取】")
    if report.get("claimable_list"):
        for item in report["claimable_list"]:
            L.append(f"- {item['name']}（{item['label']}）")
        L.append("  → 一键补领需你确认后执行 auto-bind-coupons")
    else:
        L.append(f"- 麦麦省暂无可领取的券（共 {report['claimable_count']} 张）")
    L.append("")

    lot = report["lottery"]
    L.append("【🎰 积分与抽奖】")
    if lot.get("status"):
        L.append(f"- 活动：{lot.get('name')}（{lot.get('status')}）｜单次消耗 {lot.get('draw_point')} 分")
        L.append(f"- 资源判定（服务端）：available={lot.get('resource_eligible')}"
                 + (f"，原因：{lot.get('reason')}" if lot.get("reason") else ""))
    else:
        L.append("- 暂无进行中的抽奖活动")
    if report["usable_prizes"]:
        names = "、".join(p.get("name", "") for p in report["usable_prizes"])
        L.append(f"- 可用奖品 {len(report['usable_prizes'])} 份：{names}")
    L.append("")

    L.append("【📊 账户总览】")
    L.append(f"- 可用 {a['availablePoint']:g} ｜累计 {a['accumulativePoint']:g} "
             f"｜冻结 {a['frozenPoint']:g} ｜已过期 {a['expiredPoint']:g}")
    L.append(f"- 本月将过期 {a['currentMouthExpirePoint']:g} ｜下月将过期 {a['nextMouthExpirePoint']:g}")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="麦门权益中枢 · 权益扫描")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-verify", action="store_true", help="跳过商城校验，节省配额")
    ap.add_argument("--out", help="结果写入文件")
    args = ap.parse_args()

    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if not token:
        print("错误：未设置环境变量 MCD_MCP_TOKEN", file=sys.stderr)
        return 2

    client = MCPClient(token)
    raw = collect(client, verify=not args.no_verify)

    t = raw.get("time", {}).get("data", {}) or {}
    now = datetime.now()
    if t.get("datetime"):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                now = datetime.strptime(str(t["datetime"])[:19], fmt)
                break
            except ValueError:
                continue

    report = build_report(raw, now)
    text = json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[已写入 {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
