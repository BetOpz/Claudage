"""
optimization.py - Rank the 168 (day × hour) time slots by historical burn rate.

Best times  = lowest average tokens/minute consumed
Worst times = highest average tokens/minute consumed
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class TimeSlot:
    day_of_week: int   # 0 = Monday … 6 = Sunday  (matches datetime.weekday())
    hour_of_day: int   # 0-23
    avg_burn_rate: float
    sample_count: int

    @property
    def label(self) -> str:
        day = DAYS[self.day_of_week]
        end_h = (self.hour_of_day + 1) % 24
        return f"{day} {self.hour_of_day:02d}:00-{end_h:02d}:00"

    @property
    def burn_display(self) -> str:
        rate = int(self.avg_burn_rate)
        return f"{rate:,}/m"


def get_best_worst_times(
    hourly_stats: List[dict],
    top_n: int = 5,
) -> Tuple[List[TimeSlot], List[TimeSlot]]:
    """
    Return the top_n best (lowest burn) and top_n worst (highest burn) slots.

    hourly_stats is the list of dicts returned by UsageDatabase.get_hourly_stats().
    Returns (best, worst) where both lists are ordered best-first / worst-first.
    """
    if not hourly_stats:
        return [], []

    slots = [
        TimeSlot(
            day_of_week=int(row["day_of_week"]),
            hour_of_day=int(row["hour_of_day"]),
            avg_burn_rate=float(row["avg_burn_rate"]),
            sample_count=int(row["sample_count"]),
        )
        for row in hourly_stats
    ]

    slots.sort(key=lambda s: s.avg_burn_rate)
    best  = slots[:top_n]
    worst = list(reversed(slots[-top_n:]))
    return best, worst


def get_current_slot_rank(
    hourly_stats: List[dict],
    day_of_week: int,
    hour_of_day: int,
) -> Tuple[Optional[int], int]:
    """
    Return (rank, total) for the given time slot, where rank=1 is the best.

    Returns (None, total) if the slot has no recorded data yet.
    """
    if not hourly_stats:
        return None, 0

    sorted_slots = sorted(hourly_stats, key=lambda r: r["avg_burn_rate"])
    total = len(sorted_slots)
    for rank, row in enumerate(sorted_slots, 1):
        if row["day_of_week"] == day_of_week and row["hour_of_day"] == hour_of_day:
            return rank, total
    return None, total
