"""Bridge to Apple's private CoreBrightness framework.

``CBBlueLightClient`` is the very object System Settings uses for Night Shift,
so every change made here goes through Apple's own colour pipeline - the same
warmth curve you get from the Night Shift slider, no gamma-table hacks.

The framework is private/undocumented.  That is fine for a personal or
open-source tool, but it rules out the Mac App Store.
"""
from __future__ import annotations

from typing import NamedTuple, Tuple

import objc

FRAMEWORK_PATH = "/System/Library/PrivateFrameworks/CoreBrightness.framework"

# Values for NightShiftStatus.mode
MODE_OFF = 0  # no schedule; Night Shift is just a manual toggle
MODE_SUNSET_TO_SUNRISE = 1
MODE_CUSTOM = 2  # custom from/to schedule

MODE_NAMES = {MODE_OFF: "off", MODE_SUNSET_TO_SUNRISE: "sunset to sunrise", MODE_CUSTOM: "custom"}

HourMinute = Tuple[int, int]


class NightShiftStatus(NamedTuple):
    active: bool  # Night Shift is currently tinting the screen
    enabled: bool  # the user-facing on/off toggle
    sun_schedule_permitted: bool
    mode: int  # one of the MODE_* constants
    schedule: Tuple[HourMinute, HourMinute]  # (from, to) of the custom schedule
    disable_flags: int
    available: bool


class Snapshot(NamedTuple):
    """Everything needed to put Night Shift back the way we found it."""

    status: NightShiftStatus
    strength: float


class NightShiftUnavailable(RuntimeError):
    pass


_client_class = None


def _load_client_class():
    global _client_class
    if _client_class is not None:
        return _client_class
    objc.loadBundle("CoreBrightness", {}, bundle_path=FRAMEWORK_PATH)
    cls = objc.lookUpClass("CBBlueLightClient")
    # These selectors take pointer arguments. PyObjC can only marshal them once
    # it knows which direction the data flows.
    objc.registerMetaDataForSelector(
        b"CBBlueLightClient", b"getStrength:", {"arguments": {2: {"type_modifier": objc._C_OUT}}}
    )
    objc.registerMetaDataForSelector(
        b"CBBlueLightClient", b"getBlueLightStatus:", {"arguments": {2: {"type_modifier": objc._C_OUT}}}
    )
    objc.registerMetaDataForSelector(
        b"CBBlueLightClient", b"setSchedule:", {"arguments": {2: {"type_modifier": objc._C_IN}}}
    )
    _client_class = cls
    return cls


class NightShift:
    """Thin, typed wrapper around one CBBlueLightClient instance."""

    def __init__(self) -> None:
        cls = _load_client_class()
        if not cls.supportsBlueLightReduction():
            raise NightShiftUnavailable("This Mac (or its display) does not support Night Shift.")
        self._client = cls.alloc().init()

    # -- reading ---------------------------------------------------------

    def strength(self) -> float:
        """Current warmth, 0.0 (coolest / off) .. 1.0 (warmest)."""
        ok, value = self._client.getStrength_(None)
        if not ok:
            raise RuntimeError("CBBlueLightClient getStrength: failed")
        return float(value)

    def status(self) -> NightShiftStatus:
        ok, raw = self._client.getBlueLightStatus_(None)
        if not ok:
            raise RuntimeError("CBBlueLightClient getBlueLightStatus: failed")
        active, enabled, sun_ok, mode, (start, end), flags, available = raw
        return NightShiftStatus(
            active=bool(active),
            enabled=bool(enabled),
            sun_schedule_permitted=bool(sun_ok),
            mode=int(mode),
            schedule=((int(start[0]), int(start[1])), (int(end[0]), int(end[1]))),
            disable_flags=int(flags),
            available=bool(available),
        )

    def snapshot(self) -> Snapshot:
        return Snapshot(status=self.status(), strength=self.strength())

    # -- writing ---------------------------------------------------------

    def set_strength(self, value: float, fade_seconds: float = 0.0, commit: bool = True) -> bool:
        """Set warmth 0.0..1.0, optionally fading over ``fade_seconds``."""
        value = min(1.0, max(0.0, float(value)))
        if fade_seconds > 0:
            return bool(self._client.setStrength_withPeriod_commit_(value, float(fade_seconds), commit))
        return bool(self._client.setStrength_commit_(value, commit))

    def set_enabled(self, enabled: bool) -> bool:
        return bool(self._client.setEnabled_(bool(enabled)))

    def set_mode(self, mode: int) -> bool:
        return bool(self._client.setMode_(int(mode)))

    def set_schedule(self, start: HourMinute, end: HourMinute) -> bool:
        return bool(self._client.setSchedule_(((int(start[0]), int(start[1])), (int(end[0]), int(end[1])))))

    def restore(self, snap: Snapshot) -> None:
        """Put back mode, schedule, on/off state and strength from ``snap``."""
        start, end = snap.status.schedule
        self.set_schedule(start, end)
        self.set_mode(snap.status.mode)
        self.set_strength(snap.strength)
        self.set_enabled(snap.status.enabled)
