#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门权益中枢 · 券结构化解析器
==============================

实测确认（2026-10-10）：`query-my-coupons` 的 `structuredContent.data.coupons[]`
携带**完整的结构化字段**，远优于其 content[0].text 中的 Markdown 摘要。
本模块负责把结构化券对象转换为窗口引擎可用的输入。

关键字段（实测）
---------------
tradeStartDateTime : "2026-10-11 05:00:00"   交易开始（精确到秒）
tradeEndDateTime   : "2026-10-11 10:29:59"   交易结束
tradeTimePeriods   : [{startTime:"05:00:00", endTime:"10:29:59"}]  每日时段
weekdays           : [1]                     星期约束（1=周日，与 Python weekday 不同！）
isAllTradeTime     : 0                       是否全天可用
currentDayAvailableCount : 0                 今日剩余可用次数
instructions.labels: [{text:"未到使用时间"}]   服务端可用状态
discountInfo       : {discountValue:"29.9", discountTypeText:"¥"}  用券价值

重要：服务端 weekdays 采用「周日=1 ... 周六=7」编号，
      与 Python 的 `date.weekday()`（周一=0 ... 周日=6）不同，转换见 `_w2py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Optional

__all__ = ["CouponView", "parse_coupon_obj", "label_state"]


def _w2py(mcd_weekday: int) -> Optional[int]:
    """麦当劳星期编号（周日=1..周六=7）→ Python weekday（周一=0..周日=6）。"""
    try:
        n = int(mcd_weekday)
    except (TypeError, ValueError):
        return None
    if not 1 <= n <= 7:
        return None
    return 6 if n == 1 else n - 2


def _parse_dt(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip()[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _parse_t(s: str) -> Optional[time]:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(s).strip(), fmt).time()
        except (ValueError, TypeError):
            continue
    return None


@dataclass
class CouponView:
    """一张券的结构化视图。"""

    title: str = ""
    code: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    daily_start: Optional[time] = None
    daily_end: Optional[time] = None
    weekdays_py: list[int] = field(default_factory=list)
    all_day: bool = False
    today_available: int = 0        # currentDayAvailableCount
    total_available: int = 0        # totalAvailableCount
    enabled: bool = False           # enable
    server_labels: list[str] = field(default_factory=list)
    discount_value: str = ""
    discount_prefix: str = ""
    tags: list[str] = field(default_factory=list)
    order_types: list[int] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    # ---- 派生 ----

    @property
    def weekday_label(self) -> str:
        CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return "、".join(CN[i] for i in sorted(self.weekdays_py)) if self.weekdays_py else ""

    @property
    def label(self) -> str:
        """服务端状态标签（优先），如「未到使用时间」「可用」。"""
        return "、".join(self.server_labels)

    @property
    def value_text(self) -> str:
        if not self.discount_value:
            return ""
        desc = self.raw.get("discountInfo", {}).get("discountDesc", "")
        return f"{self.discount_prefix}{self.discount_value}" + (f"（{desc}）" if desc else "")


def parse_coupon_obj(obj: dict) -> CouponView:
    """解析单张结构化券对象。"""
    v = CouponView(raw=obj)
    if not isinstance(obj, dict):
        return v

    v.title = str(obj.get("title", "")).strip()
    v.code = str(obj.get("code", ""))

    v.start = _parse_dt(obj.get("tradeStartDateTime") or obj.get("tradeStartDate") or "")
    v.end = _parse_dt(obj.get("tradeEndDateTime") or obj.get("tradeEndDate") or "")

    # 每日时段（可能有多个，取首个；多时段场景在窗口引擎中可扩展）
    periods = obj.get("tradeTimePeriods") or []
    if isinstance(periods, list) and periods:
        p0 = periods[0] if isinstance(periods[0], dict) else {}
        v.daily_start = _parse_t(p0.get("startTime", ""))
        v.daily_end = _parse_t(p0.get("endTime", ""))

    # 星期约束
    wds = obj.get("weekdays") or []
    if isinstance(wds, list):
        for n in wds:
            py = _w2py(n)
            if py is not None and py not in v.weekdays_py:
                v.weekdays_py.append(py)
        v.weekdays_py.sort()

    # 全天判定
    v.all_day = bool(obj.get("isAllTradeTime")) or (
        v.daily_start == time(0, 0) and v.daily_end is not None
        and v.daily_end >= time(23, 59)
    )

    v.today_available = int(obj.get("currentDayAvailableCount") or 0)
    v.total_available = int(obj.get("totalAvailableCount") or 0)
    v.enabled = bool(obj.get("enable"))

    labels = (obj.get("instructions") or {}).get("labels") or []
    if isinstance(labels, list):
        v.server_labels = [str(x.get("text", "")) for x in labels
                           if isinstance(x, dict) and x.get("text")]

    di = obj.get("discountInfo") or {}
    if isinstance(di, dict):
        v.discount_value = str(di.get("discountValue", "") or "")
        v.discount_prefix = str(di.get("discountTypeText", "") or "")

    tags = obj.get("tags") or []
    if isinstance(tags, list):
        v.tags = [str(t.get("label", "")) for t in tags
                  if isinstance(t, dict) and t.get("label")]

    ot = obj.get("orderTypes") or []
    if isinstance(ot, list):
        v.order_types = [int(x) for x in ot if isinstance(x, (int, float, str)) and str(x).isdigit()]

    return v


def label_state(v: CouponView) -> str:
    """服务端标签 → 归一化状态文案。"""
    joined = v.label
    if "未到使用时间" in joined:
        return "未到"
    if "已过期" in joined or "已失效" in joined:
        return "已失效"
    if not v.enabled and v.today_available == 0 and v.total_available == 0:
        return "不可用"
    return "可用"


if __name__ == "__main__":
    demo = {
        "title": "巧克力味厚松饼猪柳蛋套餐",
        "tradeStartDateTime": "2026-10-11 05:00:00",
        "tradeEndDateTime": "2026-10-11 10:29:59",
        "tradeTimePeriods": [{"startTime": "05:00:00", "endTime": "10:29:59"}],
        "weekdays": [1],
        "isAllTradeTime": 0,
        "currentDayAvailableCount": 0,
        "totalAvailableCount": 1,
        "enable": 0,
        "instructions": {"labels": [{"text": "未到使用时间"}, {"text": "到店"}]},
        "discountInfo": {"discountValue": "29.9", "discountTypeText": "¥",
                         "discountDesc": "用券价格"},
        "tags": [{"label": "到店专用"}],
        "orderTypes": [1],
    }
    v = parse_coupon_obj(demo)
    print("券名:", v.title)
    print("窗口:", v.start, "~", v.end)
    print("每日时段:", v.daily_start, "-", v.daily_end)
    print("允许星期:", v.weekday_label, "(Python weekday:", v.weekdays_py, ")")
    print("全天:", v.all_day, "| 今日可用次数:", v.today_available)
    print("服务端标签:", v.label, "→ 归一化:", label_state(v))
    print("价值:", v.value_text, "| 标签:", v.tags)
