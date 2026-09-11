"""Glue between config, location, solar maths and the schedule."""
from __future__ import annotations

from datetime import date, datetime
from typing import Dict, NamedTuple, Optional, Tuple

from . import solar
from .config import DEFAULT_CONFIG, Config
from .schedule import Schedule, ScheduleError, SolarLookup


class Location(NamedTuple):
    latitude: float
    longitude: float
    source: str  # "config" or the time zone name it was guessed from

    @property
    def description(self) -> str:
        if self.source == "config":
            return f"{self.latitude:.2f}, {self.longitude:.2f}"
        return f"{self.source} (guessed from time zone)"


def resolve_location(cfg: Config) -> Optional[Location]:
    if cfg.has_location:
        return Location(cfg.latitude, cfg.longitude, "config")  # type: ignore[arg-type]
    guess = solar.location_from_timezone()
    if guess is None:
        return None
    lat, lon, name = guess
    return Location(lat, lon, name)


def make_solar_lookup(location: Optional[Location]) -> Optional[SolarLookup]:
    if location is None:
        return None
    cache: Dict[Tuple[date, str], Optional[datetime]] = {}

    def lookup(day: date, event: str) -> Optional[datetime]:
        key = (day, event)
        if key not in cache:
            if len(cache) > 500:
                cache.clear()
            cache[key] = solar.sun_event(day, location.latitude, location.longitude, event)
        return cache[key]

    return lookup


def build_schedule(cfg: Config, location: Optional[Location]) -> Schedule:
    """Schedule from the config; raises ScheduleError with a readable message."""
    return Schedule.from_config(cfg.keyframes, cfg.easing, make_solar_lookup(location))


def default_schedule(location: Optional[Location]) -> Schedule:
    return Schedule.from_config(DEFAULT_CONFIG["keyframes"], "smooth", make_solar_lookup(location))
