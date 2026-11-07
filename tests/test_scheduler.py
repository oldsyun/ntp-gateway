import unittest
from datetime import datetime

from port_worker import PortWorker


class SchedulerTests(unittest.TestCase):
    def test_next_due_is_integer_second_boundary(self):
        worker = object.__new__(PortWorker)
        due = worker._compute_next_due_epoch({"interval_sec": 60}, after_epoch=120.0)
        self.assertEqual(due, 180.0)
        self.assertEqual(due, int(due))

    def test_interval_boundary(self):
        worker = object.__new__(PortWorker)
        due = worker._compute_next_due_epoch({"interval_sec": 7}, after_epoch=100.0)
        self.assertEqual(due % 7, 0)
        self.assertEqual(due, int(due))

    def test_daily_schedule_uses_local_whole_second(self):
        worker = object.__new__(PortWorker)
        base = datetime(2026, 11, 7, 10, 0, 0)
        due = worker._compute_next_due_epoch(
            {
                "schedule_mode": "daily",
                "daily_time": "12:30:15",
                "interval_sec": 60,
            },
            after_epoch=base.timestamp(),
        )
        self.assertEqual(
            datetime.fromtimestamp(due), datetime(2026, 11, 7, 12, 30, 15)
        )
        self.assertEqual(due, int(due))

    def test_daily_schedule_rolls_to_next_day(self):
        worker = object.__new__(PortWorker)
        base = datetime(2026, 11, 7, 12, 30, 15)
        due = worker._compute_next_due_epoch(
            {
                "schedule_mode": "daily",
                "daily_time": "12:30:15",
                "interval_sec": 60,
            },
            after_epoch=base.timestamp(),
        )
        self.assertEqual(
            datetime.fromtimestamp(due), datetime(2026, 11, 8, 12, 30, 15)
        )


if __name__ == "__main__":
    unittest.main()
