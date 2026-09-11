"""``python -m smartshift --show``: print today's plan without starting the GUI."""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import ConfigError, load_config
from .engine import build_schedule, resolve_location
from .nightshift import MODE_NAMES, NightShift, NightShiftUnavailable
from .schedule import ScheduleError, minute_to_datetime


def _hm(moment) -> str:
    return "--:--" if moment is None else moment.strftime("%H:%M")


def show(config_file=None) -> int:
    try:
        cfg = load_config(config_file)
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1
    print(f"Config:   {cfg.path}")

    location = resolve_location(cfg)
    if location is None:
        print("Location: unknown - solar keyframes use fixed fallback times")
    else:
        print(f"Location: {location.latitude:.3f}, {location.longitude:.3f}  [{location.description}]")

    try:
        schedule = build_schedule(cfg, location)
    except ScheduleError as e:
        print(f"Schedule error: {e}")
        return 1

    now = datetime.now()
    today = now.date()
    if schedule.solar_lookup is not None:
        events = {name: schedule.solar_lookup(today, name) for name in ("dawn", "sunrise", "sunset", "dusk")}
        print("Sun:      " + "  ".join(f"{name} {_hm(t)}" for name, t in events.items()))

    print(f"\nKeyframes for {today} ({cfg.easing} easing):")
    for frame in schedule.resolve(today):
        when = minute_to_datetime(today, frame.minute)
        day_tag = " (+1 day)" if frame.minute >= 1440 else ""
        note = "   [no sun event; fixed fallback time]" if frame.solar_fallback else ""
        print(f"  {when:%H:%M}{day_tag:9}  {frame.strength * 100:5.1f}%   {frame.keyframe.name} ({frame.keyframe.at.text}){note}")

    seg = schedule.segment_at(now)
    print(f"\nNow {now:%H:%M}: target {seg.strength * 100:.1f}%  ({seg.direction} -> {seg.end.strength * 100:.0f}% at {seg.end_time:%H:%M})")

    print("\nCurve (every 2 hours):")
    start = datetime.combine(today, datetime.min.time())
    row = []
    for hour in range(0, 24, 2):
        moment = start + timedelta(hours=hour)
        row.append(f"{moment:%H:%M} {schedule.strength_at(moment) * 100:3.0f}%")
    print("  " + "   ".join(row[:6]))
    print("  " + "   ".join(row[6:]))

    try:
        night = NightShift()
        status = night.status()
        print(
            f"\nNight Shift now: {night.strength() * 100:.1f}%  "
            f"{'on' if status.enabled else 'off'}, schedule mode: {MODE_NAMES.get(status.mode, status.mode)}"
        )
    except NightShiftUnavailable as e:
        print(f"\nNight Shift: {e}")
    return 0
