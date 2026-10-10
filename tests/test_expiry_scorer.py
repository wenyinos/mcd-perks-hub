#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门权益中枢 · 窗口引擎单元测试

覆盖：实测券形态、跨日区间、星期约束、边界时刻、异常输入。
运行：python3 -m pytest tests/ -v   或   python3 tests/test_expiry_scorer.py
"""

import os
import sys
import unittest
from datetime import date, datetime, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from expiry_scorer import (  # noqa: E402
    WindowState,
    parse_coupon_validity,
    judge_state,
    score_priority,
    summarize_window,
    analyze,
)


class TestParse(unittest.TestCase):
    """解析层：真实券形态。"""

    def test_real_breakfast_coupon(self):
        """实测形态：单日 + 限定时段 + 星期。"""
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        self.assertTrue(w.parsed_ok)
        self.assertEqual(w.days, [date(2026, 10, 11)])
        self.assertEqual(w.daily_start, time(5, 0))
        self.assertEqual(w.daily_end, time(10, 29))
        self.assertIn(6, w.weekdays)  # 周日 = 6
        self.assertFalse(w.all_day)

    def test_cross_day_range(self):
        """跨日区间：展开为逐日。"""
        w = parse_coupon_validity("2026-10-08 00:00-2026-10-21 23:59")
        self.assertEqual(len(w.days), 14)
        self.assertEqual(w.days[0], date(2026, 10, 8))
        self.assertEqual(w.days[-1], date(2026, 10, 21))
        self.assertTrue(w.all_day)

    def test_weekend_only(self):
        """仅星期约束，无具体日期。"""
        w = parse_coupon_validity("限周六日可用")
        self.assertEqual(w.weekdays, [5, 6])

    def test_empty_validity(self):
        """空值：标记未解析，不臆造。"""
        w = parse_coupon_validity("")
        self.assertFalse(w.parsed_ok)
        self.assertTrue(w.all_day)

    def test_garbage_input(self):
        """异常串：不应抛异常。"""
        w = parse_coupon_validity("有效期以券面为准")
        self.assertFalse(w.parsed_ok)


class TestJudge(unittest.TestCase):
    """判定层：三态与边界。"""

    def setUp(self):
        self.now = datetime(2026, 10, 10, 16, 5)  # 周六 16:05

    def test_active_within_window(self):
        """窗口进行中。"""
        w = parse_coupon_validity("2026-10-10 05:00-2026-10-10 10:29 周六 05:00-10:29")
        self.assertEqual(judge_state(w, self.now), WindowState.MISSED)  # 16:05 已过

        now2 = datetime(2026, 10, 10, 8, 0)
        self.assertEqual(judge_state(w, now2), WindowState.ACTIVE)

    def test_boundary_start(self):
        """边界：恰好开启时刻。"""
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        self.assertEqual(judge_state(w, datetime(2026, 10, 11, 5, 0)), WindowState.ACTIVE)

    def test_boundary_end(self):
        """边界：恰好结束时刻仍算进行中。"""
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        self.assertEqual(judge_state(w, datetime(2026, 10, 11, 10, 29)), WindowState.ACTIVE)

    def test_one_minute_after(self):
        """边界：结束后一分钟应关闭。"""
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        self.assertEqual(judge_state(w, datetime(2026, 10, 11, 10, 30)), WindowState.MISSED)

    def test_pending(self):
        """未来窗口。"""
        w = parse_coupon_validity("2026-10-12 05:00-2026-10-12 10:29 周一 05:00-10:29")
        self.assertEqual(judge_state(w, self.now), WindowState.PENDING)

    def test_missed(self):
        """已关闭。"""
        w = parse_coupon_validity("2026-10-09 05:00-2026-10-09 10:29 周五 05:00-10:29")
        self.assertEqual(judge_state(w, self.now), WindowState.MISSED)


class TestPriority(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 10, 10, 16, 5)

    def test_p0_imminent(self):
        """24 小时内开启 -> P0。"""
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        self.assertEqual(score_priority(w, self.now), "P0")

    def test_p1_within_3days(self):
        """3 日内 -> P1。"""
        w = parse_coupon_validity("2026-10-13 05:00-2026-10-13 10:29 周二 05:00-10:29")
        self.assertEqual(score_priority(w, self.now), "P1")

    def test_p2_within_7days(self):
        """7 日内 -> P2。"""
        w = parse_coupon_validity("2026-10-16 05:00-2026-10-16 10:29 周五 05:00-10:29")
        self.assertEqual(score_priority(w, self.now), "P2")

    def test_p3_missed(self):
        """已关闭 -> P3（沉底）。"""
        w = parse_coupon_validity("2026-10-01 05:00-2026-10-01 10:29 周四 05:00-10:29")
        self.assertEqual(score_priority(w, self.now), "P3")


class TestSummary(unittest.TestCase):

    def setUp(self):
        self.now = datetime(2026, 10, 10, 16, 5)

    def test_pending_summary(self):
        w = parse_coupon_validity("2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29")
        s = summarize_window(w, self.now)
        self.assertIn("⏰", s)
        self.assertIn("10-11", s)
        self.assertIn("周日", s)

    def test_unparsed_summary(self):
        w = parse_coupon_validity("")
        self.assertIn("未识别", summarize_window(w, self.now))


class TestAnalyze(unittest.TestCase):

    def test_sorting(self):
        """结果应按 P0..P3 排序。"""
        now = datetime(2026, 10, 10, 16, 5)
        items = [
            ("远期券", "2026-10-20 05:00-2026-10-20 10:29 周二 05:00-10:29"),
            ("明日券", "2026-10-11 05:00-2026-10-11 10:29 周日 05:00-10:29"),
            ("已过期券", "2026-10-01 05:00-2026-10-01 10:29 周四 05:00-10:29"),
        ]
        rows = analyze(items, now)
        prios = [r["priority"] for r in rows]
        self.assertEqual(prios, sorted(prios, key=lambda p: int(p[1])))
        self.assertEqual(rows[0]["name"], "明日券")
        self.assertEqual(rows[-1]["priority"], "P3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
