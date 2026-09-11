"""Sunrise, sunset and civil twilight, plus a permission-free location guess.

The maths is the classic "Almanac for Computers" (US Naval Observatory, 1990)
algorithm.  It is accurate to a minute or two, which is plenty for deciding
when it is getting dark outside.
"""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Dict, Optional, Tuple

ZENITH_OFFICIAL = 90.833  # sunrise / sunset (solar disc + atmospheric refraction)
ZENITH_CIVIL = 96.0  # civil dawn / dusk - roughly "it got dark outside"

EVENTS = ("dawn", "sunrise", "noon", "sunset", "dusk")

ZONE_TAB = "/usr/share/zoneinfo/zone.tab"


def _sun_event_ut(day: date, lat: float, lon: float, zenith: float, rising: bool) -> Optional[float]:
    """Event time as UTC hours (0..24) on ``day``.

    Returns None when the sun never crosses ``zenith`` that day (polar day or
    polar night).
    """
    day_of_year = day.timetuple().tm_yday
    lng_hour = lon / 15.0
    t = day_of_year + (((6 - lng_hour) if rising else (18 - lng_hour)) / 24.0)

    mean_anomaly = 0.9856 * t - 3.289
    true_lon = (
        mean_anomaly
        + 1.916 * math.sin(math.radians(mean_anomaly))
        + 0.020 * math.sin(math.radians(2 * mean_anomaly))
        + 282.634
    ) % 360.0

    right_asc = math.degrees(math.atan(0.91764 * math.tan(math.radians(true_lon)))) % 360.0
    right_asc += (math.floor(true_lon / 90) * 90) - (math.floor(right_asc / 90) * 90)  # same quadrant
    right_asc /= 15.0

    sin_dec = 0.39782 * math.sin(math.radians(true_lon))
    cos_dec = math.cos(math.asin(sin_dec))

    cos_h = (math.cos(math.radians(zenith)) - sin_dec * math.sin(math.radians(lat))) / (
        cos_dec * math.cos(math.radians(lat))
    )
    if cos_h > 1 or cos_h < -1:
        return None

    hour_angle = (360 - math.degrees(math.acos(cos_h))) if rising else math.degrees(math.acos(cos_h))
    local_mean = hour_angle / 15.0 + right_asc - 0.06571 * t - 6.622
    return (local_mean - lng_hour) % 24.0


def _ut_to_local(day: date, ut_hours: float, tz: Optional[tzinfo]) -> datetime:
    """Convert UTC hours on ``day`` to an aware local datetime on that same
    calendar day (the UTC and local dates can differ near the date line)."""
    moment = datetime.combine(day, time(0), tzinfo=timezone.utc) + timedelta(hours=ut_hours)
    local = moment.astimezone(tz)  # tz=None -> the system's local time zone
    if local.date() < day:
        local += timedelta(days=1)
    elif local.date() > day:
        local -= timedelta(days=1)
    return local


def sun_event(day: date, lat: float, lon: float, event: str, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """Local, timezone-aware time of ``event`` ("dawn", "sunrise", "noon",
    "sunset", "dusk") on ``day``; None if it does not happen that day."""
    if event == "noon":
        rise = _sun_event_ut(day, lat, lon, ZENITH_OFFICIAL, True)
        sett = _sun_event_ut(day, lat, lon, ZENITH_OFFICIAL, False)
        if rise is not None and sett is not None:
            if sett < rise:
                sett += 24
            ut = ((rise + sett) / 2) % 24
        else:
            ut = (12 - lon / 15.0) % 24
        return _ut_to_local(day, ut, tz)

    try:
        zenith, rising = {
            "dawn": (ZENITH_CIVIL, True),
            "sunrise": (ZENITH_OFFICIAL, True),
            "sunset": (ZENITH_OFFICIAL, False),
            "dusk": (ZENITH_CIVIL, False),
        }[event]
    except KeyError:
        raise ValueError(f"unknown solar event {event!r}; expected one of {EVENTS}") from None
    ut = _sun_event_ut(day, lat, lon, zenith, rising)
    return None if ut is None else _ut_to_local(day, ut, tz)


def sun_times(day: date, lat: float, lon: float, tz: Optional[tzinfo] = None) -> Dict[str, Optional[datetime]]:
    return {event: sun_event(day, lat, lon, event, tz) for event in EVENTS}


# -- location guess from the system time zone ------------------------------

_ISO6709 = re.compile(r"^([+-])(\d{2})(\d{2})(\d{2})?([+-])(\d{3})(\d{2})(\d{2})?$")


def parse_iso6709(text: str) -> Tuple[float, float]:
    """Parse the +DDMM+DDDMM / +DDMMSS+DDDMMSS coordinates used by zone.tab."""
    m = _ISO6709.match(text.strip())
    if not m:
        raise ValueError(f"not an ISO 6709 coordinate: {text!r}")
    lat = int(m[2]) + int(m[3]) / 60 + (int(m[4]) if m[4] else 0) / 3600
    lon = int(m[6]) + int(m[7]) / 60 + (int(m[8]) if m[8] else 0) / 3600
    return (-lat if m[1] == "-" else lat, -lon if m[5] == "-" else lon)


def system_timezone_name() -> Optional[str]:
    """e.g. "Europe/Warsaw", read from the /etc/localtime symlink."""
    tz_env = os.environ.get("TZ")
    if tz_env and "/" in tz_env:
        return tz_env
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    idx = target.find(marker)
    return target[idx + len(marker):] if idx >= 0 else None


def location_from_timezone(name: Optional[str] = None, zone_tab: str = ZONE_TAB) -> Optional[Tuple[float, float, str]]:
    """Approximate (lat, lon, zone_name) for the system time zone.

    The tz database ships coordinates for each zone's principal city, so this
    needs no network and no location permission.  Good to within roughly half
    an hour of sunset for most people; set an exact location in the config if
    you want better.
    """
    name = name or system_timezone_name()
    if not name:
        return None
    try:
        with open(zone_tab, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[2] == name:
                    lat, lon = parse_iso6709(parts[1])
                    return lat, lon, name
    except (OSError, ValueError):
        return None
    return None
