"""Тесты `mcp_server.cron` — чистая логика, без внешних зависимостей."""

from __future__ import annotations

import unittest
from datetime import datetime

from mcp_server.cron import ScheduleError, compute_next_run, describe_schedule, validate_schedule


class EveryScheduleTests(unittest.TestCase):
    def test_every_minutes(self):
        self.assertEqual(compute_next_run("every:5m", datetime(2026, 1, 1, 10, 0)), datetime(2026, 1, 1, 10, 5))

    def test_every_hours(self):
        self.assertEqual(compute_next_run("every:1h", datetime(2026, 1, 1, 10, 0)), datetime(2026, 1, 1, 11, 0))

    def test_every_seconds(self):
        self.assertEqual(compute_next_run("every:30s", datetime(2026, 1, 1, 10, 0, 0)), datetime(2026, 1, 1, 10, 0, 30))

    def test_zero_or_negative_rejected(self):
        with self.assertRaises(ScheduleError):
            validate_schedule("every:0m")


class DailyScheduleTests(unittest.TestCase):
    def test_time_later_today(self):
        self.assertEqual(compute_next_run("daily:14:30", datetime(2026, 1, 1, 9, 0)), datetime(2026, 1, 1, 14, 30))

    def test_time_already_passed_rolls_to_tomorrow(self):
        self.assertEqual(compute_next_run("daily:09:00", datetime(2026, 1, 1, 9, 0)), datetime(2026, 1, 2, 9, 0))

    def test_exact_boundary_rolls_to_tomorrow(self):
        # after == candidate -> должно продвинуться на следующий день (next
        # run строго ПОСЛЕ after, не равно ему).
        self.assertEqual(compute_next_run("daily:09:00", datetime(2026, 1, 1, 9, 0, 0)), datetime(2026, 1, 2, 9, 0))


class CronScheduleTests(unittest.TestCase):
    def test_every_15_minutes(self):
        self.assertEqual(compute_next_run("*/15 * * * *", datetime(2026, 1, 1, 10, 7)), datetime(2026, 1, 1, 10, 15))

    def test_weekday_only(self):
        # 2026-01-01 is Thursday; 0 9 * * 1-5 (Mon-Fri at 9:00) after 10:00 -> next day 9:00 (Fri)
        self.assertEqual(compute_next_run("0 9 * * 1-5", datetime(2026, 1, 1, 10, 0)), datetime(2026, 1, 2, 9, 0))

    def test_sunday_0_and_7_are_equivalent(self):
        # 2026-01-04 is Sunday.
        r0 = compute_next_run("0 0 * * 0", datetime(2026, 1, 1, 0, 0))
        r7 = compute_next_run("0 0 * * 7", datetime(2026, 1, 1, 0, 0))
        self.assertEqual(r0, r7)
        self.assertEqual(r0, datetime(2026, 1, 4, 0, 0))

    def test_comma_list(self):
        self.assertEqual(compute_next_run("0 9,18 * * *", datetime(2026, 1, 1, 10, 0)), datetime(2026, 1, 1, 18, 0))

    def test_invalid_field_count(self):
        with self.assertRaises(ScheduleError):
            validate_schedule("* * * *")

    def test_invalid_range_out_of_bounds(self):
        with self.assertRaises(ScheduleError):
            validate_schedule("0 25 * * *")

    def test_impossible_date_raises(self):
        # 30 февраля не существует ни в одном году.
        with self.assertRaises(ScheduleError):
            compute_next_run("0 0 30 2 *", datetime(2026, 1, 1, 0, 0))

    def test_describe_schedule(self):
        self.assertIn("cron", describe_schedule("*/15 * * * *"))
        self.assertIn("каждые", describe_schedule("every:5m"))
        self.assertIn("ежедневно", describe_schedule("daily:09:00"))


class InvalidScheduleStringTests(unittest.TestCase):
    def test_empty(self):
        with self.assertRaises(ScheduleError):
            validate_schedule("")

    def test_garbage(self):
        with self.assertRaises(ScheduleError):
            validate_schedule("not a schedule at all")


if __name__ == "__main__":
    unittest.main()
