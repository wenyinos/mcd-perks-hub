#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门权益中枢 · 权益可用窗口引擎
================================

麦门权益中枢的核心算法模块。负责把麦当劳 MCP 返回的「原始权益数据」
解析为结构化的「可用窗口」，并输出确定性的优先级判定。

设计动机
--------
麦当劳的优惠券有效期并非单纯的「某日截止」，而是普遍携带「每日限定时段」
约束。例如实测返回：

    有效期: 2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29

含义是：仅在 2026-10-11（周日）的 05:00 至 10:29 之间可用。
此类计算（跨日、跨时段、星期约束、剩余分钟数）交给大语言模型极易出错，
因此本模块以确定性代码承担全部时间计算职责。

三态模型
--------
- ACTIVE  窗口进行中：当前时刻落于可用时段内
- PENDING 窗口未到  ：窗口尚未开始（今日稍后 / 未来某日）
- MISSED  窗口已关闭：所有窗口均已过去

优先级
------
P0 立即行动 / P1 三日内 / P2 七日内 / P3 长期关注

作者：麦门权益中枢项目组
许可：MIT
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional

__all__ = [
    "CouponWindow",
    "WindowState",
    "parse_coupon_validity",
    "judge_state",
    "score_priority",
    "summarize_window",
    "humanize_delta",
]

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class WindowState:
    """窗口三态。"""

    ACTIVE = "ACTIVE"
    PENDING = "PENDING"
    MISSED = "MISSED"

    LABEL = {
        ACTIVE: "窗口进行中",
        PENDING: "窗口未到",
        MISSED: "窗口已关闭",
    }


@dataclass
class CouponWindow:
    """一张券（或任意权益）的可用窗口。"""

    raw: str = ""                                # 原始有效期字符串
    days: list[date] = field(default_factory=list)   # 可用日期（已展开）
    daily_start: Optional[time] = None           # 每日时段起
    daily_end: Optional[time] = None             # 每日时段止
    weekdays: list[int] = field(default_factory=list)  # 允许的星期（0=周一）
    all_day: bool = False                        # 是否全天可用
    parsed_ok: bool = True                       # 是否成功解析

    # ---- 派生信息 ----
    def occurrences(self) -> list[tuple[datetime, datetime]]:
        """展开为若干 (开始, 结束) 的绝对时间区间。"""
        out: list[tuple[datetime, datetime]] = []
        for d in self.days:
            if self.weekdays and d.weekday() not in self.weekdays:
                continue
            if self.all_day or self.daily_start is None or self.daily_end is None:
                out.append((datetime.combine(d, time(0, 0)),
                            datetime.combine(d, time(23, 59, 59))))
            else:
                out.append((datetime.combine(d, self.daily_start),
                            datetime.combine(d, self.daily_end)))
        return out

    def last_deadline(self) -> Optional[datetime]:
        occ = self.occurrences()
        return max((e for _, e in occ), default=None)

    def next_occurrence(self, now: datetime) -> Optional[tuple[datetime, datetime]]:
        """返回 now 之后最近的一个可用区间；进行中的窗口优先。"""
        for s, e in self.occurrences():
            if s <= now <= e:
                return (s, e)          # 正在进行
        future = [(s, e) for s, e in self.occurrences() if s > now]
        return min(future, key=lambda x: x[0]) if future else None


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")
# 星期匹配：兼容「周六日」「周一至周五」「周中」等口语化写法
_WEEKDAY_RE = re.compile(r"周([一二三四五六日天])")
_WEEKDAY_RANGE_RE = re.compile(r"周([一二三四五六日天])\s*(?:至|-|~)\s*周?([一二三四五六日天])")

_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _to_time(h: str, m: str) -> Optional[time]:
    hh, mm = int(h), int(m)
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return time(hh, mm)
    return None


def parse_coupon_validity(validity: str, _today: Optional[date] = None) -> CouponWindow:
    """解析麦当劳券有效期字符串。

    支持实测中出现的多种形态：
      A. "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29"   （单日 + 限定时段）
      B. "2026-10-08 00:00-2026-10-21 23:59"                     （跨日区间）
      C. "2026-10-11 23:59"                                      （仅截止时刻）
      D. "限周六日可用"                                            （仅星期约束）
      E. ""                                                      （无有效期信息）
    """
    w = CouponWindow(raw=validity or "")
    if not validity or not validity.strip():
        w.all_day = True
        w.parsed_ok = False
        return w

    # 星期约束
    # 先处理「周一至周五」这类区间写法，避免被逐字匹配覆盖
    ranges = list(_WEEKDAY_RANGE_RE.finditer(validity))
    if ranges:
        for m in ranges:
            a, b = _WEEKDAY_INDEX[m.group(1)], _WEEKDAY_INDEX[m.group(2)]
            span = range(a, b + 1) if b >= a else list(range(a, 7)) + list(range(0, b + 1))
            for i in span:
                if i not in w.weekdays:
                    w.weekdays.append(i)
        # 移除区间文本后再做单词匹配，防止重复
        residual = _WEEKDAY_RANGE_RE.sub(" ", validity)
    else:
        residual = validity

    for m in _WEEKDAY_RE.finditer(residual):
        idx = _WEEKDAY_INDEX[m.group(1)]
        if idx not in w.weekdays:
            w.weekdays.append(idx)
        # 处理「周六日」这类连写：捕获到「六」后紧跟「日/天」时补入周日
        tail = residual[m.end():m.end() + 1]
        if m.group(1) == "六" and tail in ("日", "天"):
            if 6 not in w.weekdays:
                w.weekdays.append(6)
    w.weekdays.sort()

    # 所有时间点
    times = [_to_time(h, mi) for h, mi in _TIME_RE.findall(validity)]
    times = [t for t in times if t is not None]

    # 所有日期
    dates: list[date] = []
    for y, mo, d in _DATE_RE.findall(validity):
        try:
            dates.append(date(int(y), int(mo), int(d)))
        except ValueError:
            continue

    if not dates:
        # 无具体日期：若有时段则视为「每日可用该时段」
        if times:
            w.daily_start, w.daily_end = times[0], times[-1]
        else:
            w.all_day = True
            w.parsed_ok = False
        w.days = []
        return w

    # 时段：优先取「第二组」时间（实测中星期后重复的 05:00-10:29 是最准的每日时段）
    if len(times) >= 4 and _WEEKDAY_RE.search(validity):
        # 形如 05:00 - 10:29 ... 周X 05:00-10:29
        w.daily_start, w.daily_end = times[-2], times[-1]
    elif len(times) >= 2:
        w.daily_start, w.daily_end = times[0], times[-1]

    # 日期展开
    if len(dates) >= 2 and not _WEEKDAY_RE.search(validity):
        # 跨日区间（形态 B）
        start, end = dates[0], dates[-1]
        if end >= start:
            span = (end - start).days
            if span <= 366:
                w.days = [start + timedelta(days=i) for i in range(span + 1)]
            else:
                w.days = [start, end]
        else:
            w.days = [start]
    else:
        w.days = sorted(set(dates))

    # 全天判定：无时段信息，或时段覆盖整日
    if w.daily_start is None or w.daily_end is None:
        w.all_day = True
    elif w.daily_start == time(0, 0) and w.daily_end >= time(23, 59):
        w.all_day = True

    return w


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def judge_state(w: CouponWindow, now: datetime) -> str:
    """三态判定。"""
    occ = w.occurrences()
    if not occ:
        # 无日期信息：若为全天券则视为长期有效
        return WindowState.ACTIVE if w.all_day else WindowState.MISSED
    if any(s <= now <= e for s, e in occ):
        return WindowState.ACTIVE
    if any(s > now for s, _ in occ):
        return WindowState.PENDING
    return WindowState.MISSED


def score_priority(w: CouponWindow, now: datetime) -> str:
    """按「距离错失的紧迫度」给出 P0~P3。"""
    state = judge_state(w, now)
    if state == WindowState.MISSED:
        return "P3"

    nxt = w.next_occurrence(now)
    if nxt is None:
        return "P3"
    start, end = nxt

    if state == WindowState.ACTIVE:
        remain = (end - now).total_seconds() / 60.0
        return "P0" if remain <= 24 * 60 else "P1"

    # PENDING：距离窗口开启的时长
    lead = (start - now).total_seconds() / 3600.0
    if lead <= 24:
        return "P0"
    if lead <= 72:
        return "P1"
    if lead <= 168:
        return "P2"
    return "P3"


# --------------------------------------------------------------------------
# 人类可读输出
# --------------------------------------------------------------------------

def humanize_delta(seconds: float) -> str:
    """把秒数转为「X 小时 Y 分」式可读文本。"""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分"


def summarize_window(w: CouponWindow, now: datetime) -> str:
    """生成一句话窗口描述，供清单直接引用。"""
    state = judge_state(w, now)
    if not w.parsed_ok:
        return f"有效期未识别：{w.raw or '未提供'}"

    nxt = w.next_occurrence(now)
    if nxt is None:
        return "所有可用窗口均已关闭"

    start, end = nxt
    wd = WEEKDAY_CN[start.weekday()]

    if w.all_day:
        span = "全天可用"
    else:
        span = f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}"

    if state == WindowState.ACTIVE:
        return (f"✅ 现在可用（{span}，剩余 "
                f"{humanize_delta((end - now).total_seconds())}）")
    if state == WindowState.PENDING:
        return (f"⏰ {start.strftime('%m-%d')}（{wd}）{span} 开启，"
                f"还有 {humanize_delta((start - now).total_seconds())}")
    return "❌ 已错过"


def analyze(items: Iterable[tuple[str, str]], now: datetime) -> list[dict]:
    """批量分析：入参为 (名称, 有效期字符串) 序列，返回按优先级排序的结果。"""
    out: list[dict] = []
    for name, validity in items:
        w = parse_coupon_validity(validity)
        state = judge_state(w, now)
        nxt = w.next_occurrence(now)
        out.append({
            "name": name,
            "raw_validity": validity,
            "state": state,
            "state_label": WindowState.LABEL[state],
            "priority": score_priority(w, now),
            "summary": summarize_window(w, now),
            "next_start": nxt[0].isoformat(sep=" ") if nxt else None,
            "next_end": nxt[1].isoformat(sep=" ") if nxt else None,
            "last_deadline": (w.last_deadline().isoformat(sep=" ")
                              if w.last_deadline() else None),
            "all_day": w.all_day,
        })
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    out.sort(key=lambda x: (order.get(x["priority"], 9), x["name"]))
    return out


if __name__ == "__main__":
    # 自检示例
    now = datetime(2026, 10, 10, 16, 5)
    demo = [
        ("巧克力味厚松饼猪柳蛋套餐", "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29"),
        ("火腿扒堡早餐两件套", "2026-10-12 05:00-2026-10-12 10:29 周一 05:00-10:29"),
        ("超值早餐券", "2026-10-08 00:00-2026-10-21 23:59"),
    ]
    for row in analyze(demo, now):
        print(f"[{row['priority']}] {row['name']} -> {row['summary']}")
