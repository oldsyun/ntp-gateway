"""Whole-second scheduling helpers for RS485 synchronisation."""

from __future__ import annotations

import math
from datetime import datetime, timedelta


class ScheduleError(ValueError):
    pass


def parse_daily_time(value: object) -> tuple[int, int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3:
        raise ScheduleError("daily_time must use HH:MM or HH:MM:SS")
    try:
        hour, minute, second = (int(part) for part in parts)
    except ValueError as exc:
        raise ScheduleError("daily_time contains a non-numeric field") from exc
    if not 0 <= hour <= 23:
        raise ScheduleError("daily_time hour must be 0..23")
    if not 0 <= minute <= 59:
        raise ScheduleError("daily_time minute must be 0..59")
    if not 0 <= second <= 59:
        raise ScheduleError("daily_time second must be 0..59")
    return hour, minute, second


def normalize_daily_time(value: object) -> str:
    hour, minute, second = parse_daily_time(value)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def compute_next_due_epoch(
    *,
    schedule_mode: str,
    interval_sec: int,
    daily_time: str,
    after_epoch: float,
) -> float:
    base = float(after_epoch)
    mode = str(schedule_mode or "interval").lower()
    if mode == "interval":
        interval = max(1, int(interval_sec))
        return float((math.floor((base + 0.02) / interval) + 1) * interval)
    if mode != "daily":
        raise ScheduleError("schedule_mode must be interval or daily")

    hour, minute, second = parse_daily_time(daily_time)
    base_local = datetime.fromtimestamp(base)
    candidate = base_local.replace(
        hour=hour, minute=minute, second=second, microsecond=0
    )
    if candidate.timestamp() <= base + 0.02:
        candidate += timedelta(days=1)
    return float(int(candidate.timestamp()))
