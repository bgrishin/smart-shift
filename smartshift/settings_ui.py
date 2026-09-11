"""Native settings window (AppKit via PyObjC): edit keyframes and options without touching JSON."""
from __future__ import annotations

import copy
import logging
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

import objc
from AppKit import (
    NSApp,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSIndexSet,
    NSMakeRect,
    NSPopUpButton,
    NSScrollView,
    NSTableColumn,
    NSTableView,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSObject

from . import APP_NAME, solar
from .config import DEFAULT_CONFIG, Config, ConfigError, make_raw_config, parse_config
from .engine import build_schedule, resolve_location
from .schedule import MINUTES_PER_DAY, Schedule, ScheduleError, TimeExpr, minute_to_datetime

log = logging.getLogger("smartshift.settings")

WIN_W, WIN_H = 640, 604
MARGIN = 20
HELP_TEXT = (
    "Time: HH:MM, or dawn / sunrise / noon / sunset / dusk with an offset like sunset-1:30 or dusk+20m. "
    "Rows are in day order; after the last one the schedule wraps to the first. "
    "Warmth: 0 = coolest, 100 = warmest."
)


def _rect(x: float, top: float, w: float, h: float):
    """Frame from a top-left description (AppKit's origin is bottom-left)."""
    return NSMakeRect(x, WIN_H - top - h, w, h)


def _label(text: str, x: float, top: float, w: float, h: float = 18, bold: bool = False, small: bool = False):
    field = NSTextField.labelWithString_(text)
    field.setFrame_(_rect(x, top, w, h))
    if bold:
        field.setFont_(NSFont.boldSystemFontOfSize_(13))
    elif small:
        field.setFont_(NSFont.systemFontOfSize_(11))
    return field


def _text_field(x: float, top: float, w: float, h: float = 22, delegate=None):
    field = NSTextField.alloc().initWithFrame_(_rect(x, top, w, h))
    if delegate is not None:
        field.setDelegate_(delegate)
    return field


class CurveView(NSView):
    """Draws today's warmth curve with the keyframes as dots and a marker for 'now'."""

    def initWithFrame_(self, frame):
        self = objc.super(CurveView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._schedule = None
        self._day = date.today()
        return self

    def setSchedule_(self, schedule) -> None:
        self._schedule = schedule
        self._day = date.today()
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect_):
        bounds = self.bounds()
        NSColor.controlBackgroundColor().setFill()
        NSBezierPath.fillRect_(bounds)
        pad_l, pad_r, pad_t, pad_b = 36, 12, 8, 18
        x0, y0 = pad_l, pad_b
        w = bounds.size.width - pad_l - pad_r
        h = bounds.size.height - pad_t - pad_b
        small = {NSFontAttributeName: NSFont.systemFontOfSize_(9), NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}

        NSColor.separatorColor().setStroke()
        for hour in range(0, 25, 3):
            x = x0 + w * hour / 24
            NSBezierPath.strokeLineFromPoint_toPoint_((x, y0), (x, y0 + h))
            if hour < 24:
                NSAttributedString.alloc().initWithString_attributes_(f"{hour:02d}", small).drawAtPoint_((x - 5, 3))
        for pct in (0, 50, 100):
            y = y0 + h * pct / 100
            NSBezierPath.strokeLineFromPoint_toPoint_((x0, y), (x0 + w, y))
            NSAttributedString.alloc().initWithString_attributes_(f"{pct}%", small).drawAtPoint_((4, y - 5))

        if self._schedule is None:
            attrs = {NSFontAttributeName: NSFont.systemFontOfSize_(12), NSForegroundColorAttributeName: NSColor.secondaryLabelColor()}
            NSAttributedString.alloc().initWithString_attributes_("Fix the schedule to see today's curve", attrs).drawAtPoint_(
                (x0 + w / 2 - 110, y0 + h / 2 - 7)
            )
            return

        base = datetime.combine(self._day, datetime.min.time())
        points = []
        for minute in range(0, MINUTES_PER_DAY + 1, 5):
            strength = self._schedule.strength_at(base + timedelta(minutes=minute))
            points.append((x0 + w * minute / MINUTES_PER_DAY, y0 + h * strength))

        area = NSBezierPath.bezierPath()
        area.moveToPoint_((x0, y0))
        for p in points:
            area.lineToPoint_(p)
        area.lineToPoint_((x0 + w, y0))
        area.closePath()
        NSColor.systemOrangeColor().colorWithAlphaComponent_(0.18).setFill()
        area.fill()

        line = NSBezierPath.bezierPath()
        line.moveToPoint_(points[0])
        for p in points[1:]:
            line.lineToPoint_(p)
        line.setLineWidth_(2.0)
        NSColor.systemOrangeColor().setStroke()
        line.stroke()

        NSColor.systemOrangeColor().setFill()
        for frame in self._schedule.resolve(self._day):
            x = x0 + w * (frame.minute % MINUTES_PER_DAY) / MINUTES_PER_DAY
            y = y0 + h * frame.strength
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x - 3.5, y - 3.5, 7, 7)).fill()

        now = datetime.now()
        x = x0 + w * (now.hour * 60 + now.minute) / MINUTES_PER_DAY
        NSColor.controlAccentColor().setStroke()
        marker = NSBezierPath.bezierPath()
        marker.moveToPoint_((x, y0))
        marker.lineToPoint_((x, y0 + h))
        marker.setLineWidth_(1.5)
        marker.stroke()


class SettingsWindowController(NSObject):
    """Owns the settings window. ``on_save`` receives the validated raw config dict."""

    def initWithConfig_onSave_(self, config: Config, on_save: Callable[[dict], None]):
        self = objc.super(SettingsWindowController, self).init()
        if self is None:
            return None
        self._on_save = on_save
        self.on_dismiss: Optional[Callable[[], None]] = None  # standalone mode quits when the window goes away
        self._path = config.path
        self._draft: Dict = {}
        self._schedule: Optional[Schedule] = None
        self._today_column: List[str] = []
        self._tz_guess = solar.location_from_timezone()
        self._loading = False
        self._build_window()
        self._load(config)
        return self

    # -- window ----------------------------------------------------------------

    @objc.python_method
    def _build_window(self) -> None:
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable
        self._window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, NSBackingStoreBuffered, False
        )
        self._window.setTitle_(f"{APP_NAME} Settings")
        self._window.setReleasedWhenClosed_(False)
        self._window.setDelegate_(self)
        self._window.center()
        content = self._window.contentView()
        add = content.addSubview_
        inner = WIN_W - 2 * MARGIN

        add(_label("Schedule", MARGIN, 16, 200, bold=True))

        scroll = NSScrollView.alloc().initWithFrame_(_rect(MARGIN, 40, inner, 172))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)  # bezel
        self._table = NSTableView.alloc().initWithFrame_(scroll.contentView().bounds())
        for ident, title, width, editable in (
            ("at", "Time", 170, True),
            ("today", "Today", 90, False),
            ("strength", "Warmth %", 80, True),
            ("label", "Label", 200, True),
        ):
            column = NSTableColumn.alloc().initWithIdentifier_(ident)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            column.setEditable_(editable)
            self._table.addTableColumn_(column)
        self._table.setDataSource_(self)
        self._table.setDelegate_(self)
        self._table.setUsesAlternatingRowBackgroundColors_(True)
        self._table.setAllowsMultipleSelection_(False)
        self._table.setRowHeight_(22)
        scroll.setDocumentView_(self._table)
        add(scroll)

        x = MARGIN
        for title, action in (("+", "addKeyframe:"), ("−", "removeKeyframe:"), ("↑", "moveKeyframeUp:"), ("↓", "moveKeyframeDown:")):
            button = NSButton.buttonWithTitle_target_action_(title, self, action)
            button.setFrame_(_rect(x, 218, 34, 24))
            add(button)
            x += 38
        help_field = NSTextField.wrappingLabelWithString_(HELP_TEXT)
        help_field.setFrame_(_rect(x + 8, 216, inner - (x + 8 - MARGIN), 46))
        help_field.setFont_(NSFont.systemFontOfSize_(11))
        help_field.setTextColor_(NSColor.secondaryLabelColor())
        add(help_field)

        self._error = _label("", MARGIN, 266, inner, 18, small=True)
        self._error.setTextColor_(NSColor.systemRedColor())
        add(self._error)

        self._curve = CurveView.alloc().initWithFrame_(_rect(MARGIN, 288, inner, 112))
        add(self._curve)

        top = 414
        add(_label("Transition:", MARGIN, top + 3, 80))
        self._easing = NSPopUpButton.alloc().initWithFrame_pullsDown_(_rect(MARGIN + 84, top, 120, 26), False)
        self._easing.addItemsWithTitles_(["Smooth", "Linear"])
        self._easing.setTarget_(self)
        self._easing.setAction_("easingChanged:")
        add(self._easing)
        add(_label("Fade each step over", MARGIN + 224, top + 3, 140))
        self._fade = _text_field(MARGIN + 366, top + 1, 50, delegate=self)
        add(self._fade)
        add(_label("s, recompute every", MARGIN + 420, top + 3, 124))
        self._interval = _text_field(MARGIN + 546, top + 1, 50, delegate=self)
        add(self._interval)
        add(_label("s", MARGIN + 600, top + 3, 12))

        top = 450
        add(_label("Location:", MARGIN, top + 3, 80))
        self._location_mode = NSPopUpButton.alloc().initWithFrame_pullsDown_(_rect(MARGIN + 84, top, 270, 26), False)
        guess = f"From time zone ({self._tz_guess[2]})" if self._tz_guess else "Unknown (fixed sunset times)"
        self._location_mode.addItemsWithTitles_([guess, "Coordinates"])
        self._location_mode.setTarget_(self)
        self._location_mode.setAction_("locationModeChanged:")
        add(self._location_mode)
        add(_label("Lat", MARGIN + 366, top + 3, 30))
        self._lat = _text_field(MARGIN + 396, top + 1, 80, delegate=self)
        add(self._lat)
        add(_label("Lon", MARGIN + 486, top + 3, 30))
        self._lon = _text_field(MARGIN + 520, top + 1, 80, delegate=self)
        add(self._lon)
        self._sun = _label("", MARGIN + 84, top + 30, inner - 84, 16, small=True)
        self._sun.setTextColor_(NSColor.secondaryLabelColor())
        add(self._sun)

        top = 506
        add(_label("Menu bar:", MARGIN, top + 3, 80))
        self._icon = _text_field(MARGIN + 84, top + 1, 50, delegate=self)
        add(self._icon)
        self._show_percent = NSButton.checkboxWithTitle_target_action_("Show percentage", self, "showPercentChanged:")
        self._show_percent.setFrame_(_rect(MARGIN + 146, top + 2, 150, 22))
        add(self._show_percent)
        self._pause_off = NSButton.checkboxWithTitle_target_action_("Pause turns Night Shift off", self, "pauseOffChanged:")
        self._pause_off.setFrame_(_rect(MARGIN + 316, top + 2, 220, 22))
        add(self._pause_off)

        top = 556
        reset = NSButton.buttonWithTitle_target_action_("Reset to Defaults", self, "resetDefaults:")
        reset.setFrame_(_rect(MARGIN, top, 140, 30))
        add(reset)
        cancel = NSButton.buttonWithTitle_target_action_("Cancel", self, "cancel:")
        cancel.setFrame_(_rect(WIN_W - MARGIN - 190, top, 90, 30))
        cancel.setKeyEquivalent_("\x1b")
        add(cancel)
        self._save = NSButton.buttonWithTitle_target_action_("Save", self, "save:")
        self._save.setFrame_(_rect(WIN_W - MARGIN - 92, top, 92, 30))
        self._save.setKeyEquivalent_("\r")
        add(self._save)

    # -- state -----------------------------------------------------------------

    @objc.python_method
    def _load(self, config: Config) -> None:
        self._path = config.path
        self._draft = {
            "keyframes": [
                {"at": str(kf.get("at", "")), "strength": kf.get("strength", 50), "label": str(kf.get("label", "") or "")}
                if isinstance(kf, dict) else {"at": str(kf), "strength": 50, "label": ""}
                for kf in config.keyframes
            ],
            "easing": config.easing,
            "latitude": config.latitude,
            "longitude": config.longitude,
            "fade_seconds": config.fade_seconds,
            "update_interval_seconds": config.update_interval_seconds,
            "pause_turns_off": config.pause_turns_off,
            "icon": config.menubar_icon,
            "show_percent": config.show_percent,
        }
        self._loading = True
        try:
            self._easing.selectItemAtIndex_(0 if config.easing == "smooth" else 1)
            self._fade.setStringValue_(_fmt_number(config.fade_seconds))
            self._interval.setStringValue_(_fmt_number(config.update_interval_seconds))
            coords = config.has_location
            self._location_mode.selectItemAtIndex_(1 if coords else 0)
            self._lat.setStringValue_(_fmt_number(config.latitude) if coords else "")
            self._lon.setStringValue_(_fmt_number(config.longitude) if coords else "")
            self._lat.setEnabled_(coords)
            self._lon.setEnabled_(coords)
            self._icon.setStringValue_(config.menubar_icon)
            self._show_percent.setState_(1 if config.show_percent else 0)
            self._pause_off.setState_(1 if config.pause_turns_off else 0)
        finally:
            self._loading = False
        self.refresh()

    @objc.python_method
    def raw(self) -> dict:
        d = self._draft
        return make_raw_config(
            keyframes=d["keyframes"],
            easing=d["easing"],
            latitude=d["latitude"],
            longitude=d["longitude"],
            fade_seconds=d["fade_seconds"],
            update_interval_seconds=d["update_interval_seconds"],
            pause_turns_off=d["pause_turns_off"],
            icon=d["icon"],
            show_percent=d["show_percent"],
        )

    @objc.python_method
    def validate(self) -> Optional[str]:
        """Re-derive the schedule from the draft. Returns an error message or None."""
        self._schedule = None
        try:
            cfg = parse_config(self.raw(), self._path)
            location = resolve_location(cfg)
            self._schedule = build_schedule(cfg, location)
            self._location = location
            return None
        except (ConfigError, ScheduleError) as e:
            self._location = None
            return str(e)

    @objc.python_method
    def refresh(self) -> None:
        error = self.validate()
        today = date.today()
        frames = self._draft["keyframes"]
        if self._schedule is not None:
            self._today_column = [
                f"{minute_to_datetime(today, f.minute):%H:%M}" + (" (+1)" if f.minute >= MINUTES_PER_DAY else "")
                for f in self._schedule.resolve(today)
            ]
        else:
            self._today_column = [_probe_time(kf["at"]) for kf in frames]
        self._error.setStringValue_(error or "")
        self._save.setEnabled_(error is None)
        self._curve.setSchedule_(self._schedule)
        self._sun.setStringValue_(self._sun_text(today))
        self._table.reloadData()

    @objc.python_method
    def _sun_text(self, today: date) -> str:
        if self._schedule is None or self._schedule.solar_lookup is None:
            return "No location: sunrise/sunset keyframes use fixed times (06:30 / 19:30)."
        parts = []
        for name in ("sunrise", "sunset", "dusk"):
            moment = self._schedule.solar_lookup(today, name)
            parts.append(f"{name} {moment:%H:%M}" if moment else f"{name} —")
        return "Today: " + " · ".join(parts)

    def showWithConfig_(self, config: Config) -> None:
        self._load(config)
        NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    # -- table data source / delegate -----------------------------------------

    def numberOfRowsInTableView_(self, _table):
        return len(self._draft.get("keyframes", []))

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        kf = self._draft["keyframes"][row]
        ident = column.identifier()
        if ident == "at":
            return kf["at"]
        if ident == "strength":
            return _fmt_number(kf["strength"])
        if ident == "label":
            return kf.get("label", "")
        return self._today_column[row] if row < len(self._today_column) else ""

    def tableView_setObjectValue_forTableColumn_row_(self, _table, value, column, row):
        kf = self._draft["keyframes"][row]
        text = ("" if value is None else str(value)).strip()
        ident = column.identifier()
        if ident == "at":
            kf["at"] = text
        elif ident == "strength":
            kf["strength"] = _parse_number(text.rstrip("%"))
        elif ident == "label":
            kf["label"] = text
        self.refresh()

    def tableView_shouldEditTableColumn_row_(self, _table, column, _row):
        return column.identifier() != "today"

    # -- actions ---------------------------------------------------------------

    @objc.python_method
    def _commit_pending_edit(self) -> None:
        """Finish any cell edit in progress so it lands on the row it was started on."""
        self._window.makeFirstResponder_(None)

    def addKeyframe_(self, _sender):
        self._commit_pending_edit()
        frames = self._draft["keyframes"]
        selected = self._table.selectedRow()
        index = len(frames) if selected < 0 else selected + 1
        previous = frames[index - 1] if index > 0 and frames else None
        frames.insert(index, {"at": _suggest_time(previous), "strength": previous["strength"] if previous else 50, "label": ""})
        self.refresh()
        self._table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(index), False)
        self._table.editColumn_row_withEvent_select_(0, index, None, True)

    def removeKeyframe_(self, _sender):
        self._commit_pending_edit()
        selected = self._table.selectedRow()
        if selected < 0:
            return
        del self._draft["keyframes"][selected]
        self.refresh()

    def moveKeyframeUp_(self, _sender):
        self._move(-1)

    def moveKeyframeDown_(self, _sender):
        self._move(1)

    @objc.python_method
    def _move(self, delta: int) -> None:
        self._commit_pending_edit()
        frames = self._draft["keyframes"]
        i = self._table.selectedRow()
        j = i + delta
        if i < 0 or not 0 <= j < len(frames):
            return
        frames[i], frames[j] = frames[j], frames[i]
        self.refresh()
        self._table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(j), False)

    def easingChanged_(self, sender):
        self._draft["easing"] = "smooth" if sender.indexOfSelectedItem() == 0 else "linear"
        self.refresh()

    def locationModeChanged_(self, sender):
        coords = sender.indexOfSelectedItem() == 1
        if coords:
            if self._draft["latitude"] is None and self._tz_guess:
                self._draft["latitude"], self._draft["longitude"] = self._tz_guess[0], self._tz_guess[1]
            self._lat.setStringValue_(_fmt_number(self._draft["latitude"]))
            self._lon.setStringValue_(_fmt_number(self._draft["longitude"]))
        else:
            self._draft["latitude"] = self._draft["longitude"] = None
            self._lat.setStringValue_("")
            self._lon.setStringValue_("")
        self._lat.setEnabled_(coords)
        self._lon.setEnabled_(coords)
        self.refresh()

    def showPercentChanged_(self, sender):
        self._draft["show_percent"] = bool(sender.state())
        self.refresh()

    def pauseOffChanged_(self, sender):
        self._draft["pause_turns_off"] = bool(sender.state())
        self.refresh()

    def controlTextDidChange_(self, notification):
        if self._loading:
            return
        field = notification.object()
        text = field.stringValue().strip()
        if field is self._fade:
            self._draft["fade_seconds"] = _parse_number(text)
        elif field is self._interval:
            self._draft["update_interval_seconds"] = _parse_number(text)
        elif field is self._lat:
            self._draft["latitude"] = _parse_number(text) if text else None
        elif field is self._lon:
            self._draft["longitude"] = _parse_number(text) if text else None
        elif field is self._icon:
            self._draft["icon"] = text or DEFAULT_CONFIG["menubar"]["icon"]
        else:
            return
        self.refresh()

    def resetDefaults_(self, _sender):
        self._load(parse_config(copy.deepcopy(DEFAULT_CONFIG), self._path))

    @objc.python_method
    def _dismiss(self) -> None:
        self._window.orderOut_(None)
        if self.on_dismiss is not None:
            self.on_dismiss()

    def windowWillClose_(self, _notification):
        if self.on_dismiss is not None:
            self.on_dismiss()

    def cancel_(self, _sender):
        self._dismiss()

    def save_(self, _sender):
        self._commit_pending_edit()
        error = self.validate()
        if error:
            self.refresh()
            return
        raw = self.raw()
        try:
            self._on_save(raw)
        except Exception as e:  # noqa: BLE001
            log.exception("saving settings failed")
            self._error.setStringValue_(f"Could not save: {e}")
            return
        self._dismiss()


# -- helpers -------------------------------------------------------------------

def _fmt_number(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def _parse_number(text: str):
    """Float if the text is a number, else the text itself so validation can complain."""
    try:
        return float(text)
    except ValueError:
        return text


def _probe_time(text: str) -> str:
    try:
        TimeExpr.parse(text)
        return ""
    except ScheduleError:
        return "✗"


def _suggest_time(previous: Optional[dict]) -> str:
    """A sensible time for a new row: an hour after the previous clock time."""
    if previous:
        try:
            expr = TimeExpr.parse(previous["at"])
            if not expr.is_solar:
                minute = (expr.minutes + 60) % MINUTES_PER_DAY
                return f"{minute // 60:02d}:{minute % 60:02d}"
            return f"{expr.base}+1h"
        except ScheduleError:
            pass
    return "21:00"


def run_standalone(config_file=None) -> int:
    """Open just the settings window (``python -m smartshift --settings``).

    A running SmartShift notices the saved file and reloads it by itself.
    """
    from AppKit import NSApplication

    from .config import load_config, save_config

    cfg = load_config(config_file)
    app = NSApplication.sharedApplication()

    def on_save(raw: dict) -> None:
        save_config(cfg.path, raw)
        print(f"Saved {cfg.path}")

    controller = SettingsWindowController.alloc().initWithConfig_onSave_(cfg, on_save)
    controller.on_dismiss = lambda: app.terminate_(None)
    controller.showWithConfig_(cfg)
    app.run()
    return 0
