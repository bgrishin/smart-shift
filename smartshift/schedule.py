"""Keyframe schedule: a list of (time, warmth) points interpolated over a day.

A keyframe time is either a clock time ("21:30") or a solar event ("dawn",
"sunrise", "noon", "sunset", "dusk") with an optional offset ("sunset-1:30",
"dusk+20m", "sunrise + 2h").  Between keyframes the warmth is eased smoothly;
after the last keyframe it wraps around to the first one on the next day.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, List, NamedTuple, Optional, Sequence

from . import solar

MINUTES_PER_DAY = 24 * 60
SOLAR_EVENTS = solar.EVENTS
EASINGS = ("smooth", "linear")

# Used when there is no location, or the sun never rises/sets that day.
FALLBACK_EVENT_MINUTES = {
    "dawn": 6 * 60,
    "sunrise": 6 * 60 + 30,
    "noon": 12 * 60,
    "sunset": 19 * 60 + 30,
    "dusk": 20 * 60,
}

# (day, event name) -> local datetime, or None when the event does not occur.
SolarLookup = Callable[[date, str], Optional[datetime]]


class ScheduleError(ValueError):
    pass


_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_EVENT_RE = re.compile(r"^([a-z]+)\s*(?:([+-])\s*(\S.*))?$")
_UNIT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)?$")


def _parse_duration_minutes(text: str) -> int:
    text = text.strip().lower()
    m = _CLOCK_RE.match(text)
    if m:
        return int(m[1]) * 60 + int(m[2])
    m = _UNIT_RE.match(text)
    if m:
        value = float(m[1])
        unit = m[2] or "m"
        return int(round(value * 60 if unit.startswith("h") else value))
    raise ScheduleError(f"can't understand offset {text!r}; use e.g. '1:30', '90m' or '2h'")


@dataclass(frozen=True)
class TimeExpr:
    base: str  # "clock" or a solar event name
    minutes: int  # minutes after midnight (clock) or offset from the event
    text: str  # what the user wrote

    @classmethod
    def parse(cls, text: object) -> "TimeExpr":
        if not isinstance(text, str):
            raise ScheduleError(f"time must be a string like '21:30' or 'sunset-1:30', got {text!r}")
        raw = text.strip().lower().replace("−", "-")  # unicode minus
        m = _CLOCK_RE.match(raw)
        if m:
            hour, minute = int(m[1]), int(m[2])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ScheduleError(f"invalid clock time {text!r}")
            return cls("clock", hour * 60 + minute, text)
        m = _EVENT_RE.match(raw)
        if m and m[1] in SOLAR_EVENTS:
            offset = 0
            if m[2]:
                offset = _parse_duration_minutes(m[3]) * (-1 if m[2] == "-" else 1)
            if abs(offset) >= MINUTES_PER_DAY:
                raise ScheduleError(f"offset in {text!r} must be less than 24 hours")
            return cls(m[1], offset, text)
        raise ScheduleError(
            f"can't understand time {text!r}; use 'HH:MM' or one of {', '.join(SOLAR_EVENTS)} "
            "with an optional offset like 'sunset-1:30' or 'dusk+20m'"
        )

    @property
    def is_solar(self) -> bool:
        return self.base != "clock"


@dataclass(frozen=True)
class Keyframe:
    at: TimeExpr
    strength: float  # 0.0 .. 1.0
    label: str = ""

    @classmethod
    def from_dict(cls, item: object, index: int) -> "Keyframe":
        where = f"keyframe #{index + 1}"
        if not isinstance(item, dict):
            raise ScheduleError(f'{where} must be an object like {{"at": "21:00", "strength": 75}}')
        if "at" not in item or "strength" not in item:
            raise ScheduleError(f'{where} needs both "at" and "strength"')
        at = TimeExpr.parse(item["at"])
        strength = item["strength"]
        if isinstance(strength, bool) or not isinstance(strength, (int, float)):
            raise ScheduleError(f"{where}: strength must be a number 0-100, got {strength!r}")
        if not 0 <= strength <= 100:
            raise ScheduleError(f"{where}: strength must be between 0 and 100, got {strength!r}")
        label = item.get("label", "")
        return cls(at, float(strength) / 100.0, str(label) if label else "")

    @property
    def name(self) -> str:
        return self.label or self.at.text


class ResolvedKeyframe(NamedTuple):
    minute: int  # minutes since local midnight of the base day; may exceed 1440 (= next day)
    strength: float
    keyframe: Keyframe
    solar_fallback: bool  # True if a solar event could not be computed and a fixed time was used


class Segment(NamedTuple):
    day: date
    start: ResolvedKeyframe
    end: ResolvedKeyframe
    end_minute: int  # unrolled minute of ``end`` (the first keyframe wraps to +1 day)
    progress: float  # 0..1 eased position inside the segment
    strength: float  # interpolated warmth 0..1

    @property
    def end_time(self) -> datetime:
        return minute_to_datetime(self.day, self.end_minute)

    @property
    def direction(self) -> str:
        delta = self.end.strength - self.start.strength
        if abs(delta) < 1e-9:
            return "holding"
        return "warming" if delta > 0 else "cooling"


def minute_to_datetime(day: date, minute: float) -> datetime:
    """Naive local datetime for an (unrolled) minute of ``day``."""
    return datetime.combine(day, time(0)) + timedelta(minutes=minute)


class Schedule:
    def __init__(
        self,
        keyframes: Sequence[Keyframe],
        easing: str = "smooth",
        solar_lookup: Optional[SolarLookup] = None,
    ) -> None:
        if len(keyframes) < 2:
            raise ScheduleError("the schedule needs at least two keyframes")
        if easing not in EASINGS:
            raise ScheduleError(f"easing must be one of {EASINGS}, got {easing!r}")
        self.keyframes: List[Keyframe] = list(keyframes)
        self.easing = easing
        self.solar_lookup = solar_lookup

    @classmethod
    def from_config(
        cls, items: object, easing: str = "smooth", solar_lookup: Optional[SolarLookup] = None
    ) -> "Schedule":
        if not isinstance(items, list):
            raise ScheduleError('"keyframes" must be a list')
        return cls([Keyframe.from_dict(item, i) for i, item in enumerate(items)], easing, solar_lookup)

    @property
    def uses_solar_events(self) -> bool:
        return any(kf.at.is_solar for kf in self.keyframes)

    # -- resolution --------------------------------------------------------

    def _minute_of(self, day: date, expr: TimeExpr) -> tuple[int, bool]:
        if not expr.is_solar:
            return expr.minutes, False
        moment = self.solar_lookup(day, expr.base) if self.solar_lookup else None
        if moment is None:
            return FALLBACK_EVENT_MINUTES[expr.base] + expr.minutes, True
        return moment.hour * 60 + moment.minute + expr.minutes, False

    def resolve(self, day: date) -> List[ResolvedKeyframe]:
        """Keyframes with concrete minutes for ``day``, in list order.

        Times are "unrolled": each keyframe is placed at or after the previous
        one, so "00:00" following "dusk" means midnight at the *end* of the
        day (minute 1440).  The whole list always fits inside one 24 h cycle
        starting at the first keyframe.
        """
        resolved: List[ResolvedKeyframe] = []
        first = prev = 0
        for index, kf in enumerate(self.keyframes):
            minute, fallback = self._minute_of(day, kf.at)
            if index == 0:
                minute %= MINUTES_PER_DAY
                first = minute
            else:
                if minute < prev:
                    minute += MINUTES_PER_DAY  # e.g. "00:00" after "dusk" means the *next* midnight
                minute = min(max(minute, prev), first + MINUTES_PER_DAY - 1)
            resolved.append(ResolvedKeyframe(minute, kf.strength, kf, fallback))
            prev = minute
        return resolved

    # -- evaluation --------------------------------------------------------

    def _ease(self, p: float) -> float:
        p = min(1.0, max(0.0, p))
        if self.easing == "smooth":
            return p * p * (3.0 - 2.0 * p)  # smoothstep: gentle start and end
        return p

    def segment_at(self, when: datetime) -> Segment:
        day = when.date()
        frames = self.resolve(day)
        t = when.hour * 60 + when.minute + when.second / 60.0
        if t < frames[0].minute:
            # Early morning belongs to the cycle that started yesterday.
            yesterday = day - timedelta(days=1)
            yesterday_frames = self.resolve(yesterday)
            if t < yesterday_frames[0].minute:
                day, frames, t = yesterday, yesterday_frames, t + MINUTES_PER_DAY
        first = frames[0].minute

        count = len(frames)
        for i, start in enumerate(frames):
            last = i == count - 1
            end = frames[0] if last else frames[i + 1]
            end_minute = first + MINUTES_PER_DAY if last else end.minute
            if start.minute <= t < end_minute or (last and t >= start.minute):
                span = end_minute - start.minute
                raw = 0.0 if span <= 0 else (t - start.minute) / span
                p = self._ease(raw)
                strength = start.strength + (end.strength - start.strength) * p
                return Segment(day, start, end, end_minute, p, strength)

        start = frames[0]  # unreachable in practice; be safe
        return Segment(day, start, start, start.minute, 0.0, start.strength)

    def strength_at(self, when: datetime) -> float:
        return self.segment_at(when).strength
