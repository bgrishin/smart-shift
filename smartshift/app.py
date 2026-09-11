"""The menu bar app (rumps + Cocoa notifications)."""
from __future__ import annotations

import fcntl
import logging
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta
from typing import Dict, Optional

import objc
import rumps
from AppKit import NSWorkspace
from Foundation import NSDate, NSNotificationCenter, NSObject, NSRunLoop

from . import APP_NAME, __version__, loginitem
from .config import CONFIG_DIR, Config, ConfigError, load_config, save_config
from .engine import Location, build_schedule, default_schedule, resolve_location
from .nightshift import MODE_OFF, NightShift, NightShiftUnavailable, Snapshot
from .schedule import Schedule, ScheduleError, Segment, minute_to_datetime
from .settings_ui import SettingsWindowController

log = logging.getLogger("smartshift")

PAUSE_CHOICES = [("Pause for 15 minutes", 15), ("Pause for 1 hour", 60), ("Pause for 3 hours", 180), ("Pause until I resume", None)]
STRENGTH_EPSILON = 0.0005  # ignore differences below 0.05%
INFINITE = datetime.max


class _NotificationObserver(NSObject):
    """Tiny NSObject so Cocoa can deliver notifications (sleep/wake, clock changes) to Python."""

    def initWithCallback_(self, callback):
        self = objc.super(_NotificationObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def fire_(self, _notification):
        self._callback()


class SmartShiftApp(rumps.App):
    def __init__(self, config_file: Optional[str] = None) -> None:
        super().__init__(APP_NAME, title="☾", quit_button=None)
        self._config_override = config_file
        self.config: Config = load_config(config_file)
        self.night = NightShift()
        self.original: Snapshot = self.night.snapshot()
        log.info("Night Shift before start: %s", self.original)

        self.schedule: Optional[Schedule] = None
        self.location: Optional[Location] = None
        self.paused_until: Optional[datetime] = None
        self.current_target: Optional[float] = None
        self._today_built_for: Optional[date] = None
        self._last_error: Optional[str] = None
        self._shut_down = False
        self._observers = []
        self._settings: Optional[SettingsWindowController] = None

        migrated = loginitem.migrate_legacy_agent()
        if migrated:
            log.info("login item: %s", migrated)
        self._build_menu()
        self._apply_config(self.config)

        self._timer = rumps.Timer(self._tick, self.config.update_interval_seconds)
        self._timer.start()
        # A cheap 1 s timer gives Python a chance to run SIGINT/SIGTERM handlers promptly.
        self._signal_pump = rumps.Timer(lambda _: None, 1)
        self._signal_pump.start()
        self._observe_system_events()
        self._tick()

    # -- menu ----------------------------------------------------------------

    def _build_menu(self) -> None:
        self.item_status = rumps.MenuItem("Starting…")
        self.item_next = rumps.MenuItem("Next: …")
        self.item_sun = rumps.MenuItem("Sun: …")
        self.item_today = rumps.MenuItem("Today's Keyframes")
        self.item_pause = rumps.MenuItem("Pause")
        for title, minutes in PAUSE_CHOICES:
            self.item_pause.add(rumps.MenuItem(title, callback=self._make_pause_callback(minutes)))
        self.item_resume = rumps.MenuItem("Resume", callback=self._on_resume)
        self.item_resume.hidden = True
        self.item_settings = rumps.MenuItem("Settings…", callback=self._on_settings, key=",")
        self.item_edit = rumps.MenuItem("Edit Config File…", callback=self._on_edit)
        self.item_reload = rumps.MenuItem("Reload Config", callback=self._on_reload)
        self.item_login = rumps.MenuItem("Start at Login", callback=self._on_toggle_login)
        self.item_login.state = 1 if loginitem.is_enabled() else 0
        self.item_about = rumps.MenuItem(f"About {APP_NAME}", callback=self._on_about)
        self.item_quit = rumps.MenuItem(f"Quit {APP_NAME}", callback=self._on_quit, key="q")
        self.menu = [
            self.item_status,
            self.item_next,
            self.item_sun,
            self.item_today,
            None,
            self.item_pause,
            self.item_resume,
            None,
            self.item_settings,
            self.item_edit,
            self.item_reload,
            self.item_login,
            None,
            self.item_about,
            self.item_quit,
        ]

    def _rebuild_today(self, day: date) -> None:
        if self.schedule is None:
            return
        if len(self.item_today):  # rumps only creates the submenu on first add()
            self.item_today.clear()
        seen: Dict[str, int] = {}
        for frame in self.schedule.resolve(day):
            when = minute_to_datetime(day, frame.minute)
            suffix = " (+1)" if frame.minute >= 1440 else ""
            note = " · fixed time" if frame.solar_fallback else ""
            title = f"{when:%H:%M}{suffix}  {frame.strength * 100:.0f}%   {frame.keyframe.name}{note}"
            if frame.keyframe.label and frame.keyframe.at.is_solar:
                title += f" ({frame.keyframe.at.text})"
            # rumps keys menu items by title, so make sure they are unique.
            count = seen.get(title, 0)
            seen[title] = count + 1
            self.item_today.add(rumps.MenuItem(title + " " * count))
        self._today_built_for = day

    def _refresh_menu(self, now: datetime, seg: Optional[Segment]) -> None:
        icon = self.config.menubar_icon
        if self.paused_until is not None:
            self.title = f"{icon} ⏸"
            until = "" if self.paused_until == INFINITE else f" until {self.paused_until:%H:%M}"
            off = " · Night Shift off" if self.config.pause_turns_off else ""
            self.item_status.title = f"Paused{until}{off}"
            self.item_next.title = "Choose Resume to continue the schedule"
        elif seg is None:
            self.title = f"{icon} !"
            self.item_status.title = "Schedule problem - see Reload Config"
            self.item_next.title = self._last_error or ""
        else:
            pct = round(seg.strength * 100)
            self.title = f"{icon} {pct}%" if self.config.show_percent else icon
            end_pct = round(seg.end.strength * 100)
            if seg.direction == "holding":
                self.item_status.title = f"Night Shift {pct}% · holding until {seg.end_time:%H:%M}"
            else:
                self.item_status.title = f"Night Shift {pct}% · {seg.direction} to {end_pct}% by {seg.end_time:%H:%M}"
            self.item_next.title = f"Next: {seg.end.keyframe.name} at {seg.end_time:%H:%M} → {end_pct}%"

        if self._today_built_for != now.date():
            self._rebuild_today(now.date())
        self.item_sun.title = self._sun_line(now.date())

    def _sun_line(self, day: date) -> str:
        if self.schedule is None or self.schedule.solar_lookup is None:
            if self.schedule is not None and self.schedule.uses_solar_events:
                return "Sun: location unknown, using fixed times (set it in config)"
            return "Sun: not used by this schedule"
        parts = []
        for name in ("sunrise", "sunset", "dusk"):
            moment = self.schedule.solar_lookup(day, name)
            parts.append(f"{name} {moment:%H:%M}" if moment else f"{name} —")
        where = self.location.description if self.location else ""
        return "Sun: " + " · ".join(parts) + (f" · {where}" if where else "")

    # -- config --------------------------------------------------------------

    def _apply_config(self, cfg: Config) -> None:
        self.config = cfg
        self.location = resolve_location(cfg)
        try:
            self.schedule = build_schedule(cfg, self.location)
            self._last_error = None
        except ScheduleError as e:
            self._report_error(f"{cfg.path.name}: {e}")
            if self.schedule is None:
                log.warning("using the built-in default schedule until the config is fixed")
                self.schedule = default_schedule(self.location)
        self._today_built_for = None
        timer = getattr(self, "_timer", None)
        if timer is not None and timer.interval != cfg.update_interval_seconds:
            timer.stop()
            timer.interval = cfg.update_interval_seconds
            timer.start()
        log.info(
            "config loaded: %s (%d keyframes, easing=%s, location=%s)",
            cfg.path, len(cfg.keyframes), cfg.easing, self.location.description if self.location else "unknown",
        )

    def _maybe_reload_config(self, force: bool = False) -> None:
        try:
            mtime: Optional[float] = self.config.path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self.config.mtime:
            return
        try:
            cfg = load_config(self._config_override)
        except ConfigError as e:
            self._report_error(str(e))
            self.config.mtime = mtime  # don't nag every tick about the same broken save
            return
        self._apply_config(cfg)

    def _report_error(self, message: str) -> None:
        log.error(message)
        if message != self._last_error:
            self._last_error = message
            rumps.alert(title=f"{APP_NAME}: config problem", message=message)

    # -- control loop --------------------------------------------------------

    def _tick(self, _sender=None) -> None:
        if self._shut_down:
            return
        try:
            self._maybe_reload_config()
            now = datetime.now()
            if self.paused_until is not None and now >= self.paused_until:
                self._resume()
            if self.paused_until is not None or self.schedule is None:
                self._refresh_menu(now, None)
                return
            seg = self.schedule.segment_at(now)
            self._apply_strength(seg.strength)
            self._refresh_menu(now, seg)
        except Exception:  # keep the timer alive no matter what
            log.exception("tick failed")

    def _apply_strength(self, target: float) -> None:
        status = self.night.status()
        if status.mode != MODE_OFF:
            log.info("Night Shift schedule mode was '%s'; switching it off so macOS doesn't fight the app", status.mode)
            self.night.set_mode(MODE_OFF)
            # Dropping the schedule can switch Night Shift off; turn it straight back on.
            self.night.set_enabled(True)
        elif not status.enabled:
            log.info("Night Shift was off; turning it on")
            self.night.set_enabled(True)
        current = self.night.strength()
        if abs(current - target) >= STRENGTH_EPSILON:
            log.debug("strength %.4f -> %.4f (fade %ss)", current, target, self.config.fade_seconds)
            self.night.set_strength(target, fade_seconds=self.config.fade_seconds)
        self.current_target = target

    def _observe_system_events(self) -> None:
        observer = _NotificationObserver.alloc().initWithCallback_(self._on_system_event)
        self._observers.append(observer)
        workspace_center = NSWorkspace.sharedWorkspace().notificationCenter()
        for name in ("NSWorkspaceDidWakeNotification", "NSWorkspaceScreensDidWakeNotification"):
            workspace_center.addObserver_selector_name_object_(observer, "fire:", name, None)
        default_center = NSNotificationCenter.defaultCenter()
        for name in ("NSSystemClockDidChangeNotification", "NSSystemTimeZoneDidChangeNotification"):
            default_center.addObserver_selector_name_object_(observer, "fire:", name, None)

    def _on_system_event(self) -> None:
        log.debug("system event (wake / clock change): re-evaluating")
        self._apply_config(self.config)  # drops cached sun times in case the time zone changed
        self._tick()

    # -- pause / resume ------------------------------------------------------

    def _make_pause_callback(self, minutes: Optional[int]):
        def callback(_sender):
            self._pause(minutes)

        return callback

    def _pause(self, minutes: Optional[int]) -> None:
        self.paused_until = INFINITE if minutes is None else datetime.now() + timedelta(minutes=minutes)
        log.info("paused until %s", "resumed manually" if minutes is None else self.paused_until)
        if self.config.pause_turns_off:
            self.night.set_enabled(False)
        self.item_resume.hidden = False
        self._tick()

    def _resume(self) -> None:
        self.paused_until = None
        self.item_resume.hidden = True
        log.info("resumed")

    def _on_resume(self, _sender) -> None:
        self._resume()
        self._tick()

    # -- menu actions --------------------------------------------------------

    def _on_settings(self, _sender) -> None:
        if self._settings is None:
            self._settings = SettingsWindowController.alloc().initWithConfig_onSave_(self.config, self._save_settings)
        self._settings.showWithConfig_(self.config)

    def _save_settings(self, raw: dict) -> None:
        save_config(self.config.path, raw)
        log.info("settings saved to %s", self.config.path)
        self._last_error = None
        self._maybe_reload_config(force=True)
        self._tick()

    def _on_edit(self, _sender) -> None:
        subprocess.Popen(["open", "-t", str(self.config.path)])

    def _on_reload(self, _sender) -> None:
        self._last_error = None
        self._maybe_reload_config(force=True)
        self._tick()

    def _on_toggle_login(self, sender) -> None:
        try:
            message = loginitem.uninstall() if loginitem.is_enabled() else loginitem.install()
            log.info("login item: %s", message)
        except Exception as e:  # noqa: BLE001
            log.exception("login item change failed")
            rumps.alert(title=f"{APP_NAME}", message=f"Could not change the login item:\n{e}")
        sender.state = 1 if loginitem.is_enabled() else 0

    def _on_about(self, _sender) -> None:
        rumps.alert(
            title=f"{APP_NAME} {__version__}",
            message=(
                "Keeps Night Shift on and eases its warmth through the day.\n\n"
                f"Config: {self.config.path}\n"
                f"Running from: {loginitem.bundle_path() or loginitem.PROJECT_ROOT}\n"
                f"Start at login: {loginitem.describe()}\n"
                f"Log: {loginitem.LOG_PATH}\n"
                "Engine: Apple's own Night Shift (CoreBrightness), no gamma tables.\n\n"
                "Quitting restores the Night Shift settings you had before."
            ),
        )

    def _on_quit(self, _sender) -> None:
        self.shutdown()
        rumps.quit_application()

    def shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        try:
            self._timer.stop()
            self._signal_pump.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.night.restore(self.original)
            # The CoreBrightness calls are delivered over XPC asynchronously; give the
            # run loop a moment so they reach the daemon before the process exits.
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))
            log.info("restored original Night Shift settings")
        except Exception:  # noqa: BLE001
            log.exception("could not restore original Night Shift settings")


def _acquire_single_instance_lock():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(CONFIG_DIR / "smartshift.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run(config_file: Optional[str] = None) -> int:
    lock = _acquire_single_instance_lock()
    if lock is None:
        print(f"{APP_NAME} is already running (look for {'☾'} in the menu bar).", file=sys.stderr)
        return 0
    try:
        app = SmartShiftApp(config_file)
    except NightShiftUnavailable as e:
        rumps.alert(title=APP_NAME, message=str(e))
        return 1
    except ConfigError as e:
        rumps.alert(title=f"{APP_NAME}: config problem", message=f"{e}\n\nFix the file and start again.")
        return 1

    def _handle_signal(signum, _frame):
        log.info("signal %s: shutting down", signum)
        app.shutdown()
        rumps.quit_application()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    app.run()
    return 0
