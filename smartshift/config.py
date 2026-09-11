"""User configuration: ~/.config/smart-shift/config.json (created on first run)."""
from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_DIR = Path.home() / ".config" / "smart-shift"
CONFIG_ENV = "SMARTSHIFT_CONFIG"

DEFAULT_CONFIG: Dict[str, Any] = {
    "_help": (
        "Edit and save - SmartShift reloads this file automatically. "
        "'at' is a clock time like '21:30' or a solar event (dawn, sunrise, noon, sunset, dusk) "
        "with an optional offset like 'sunset-1:30' or 'dusk+20m'. "
        "'strength' is Night Shift warmth in percent: 0 = coolest, 100 = warmest. "
        "Between keyframes the warmth eases smoothly; after the last one it wraps to the first."
    ),
    "keyframes": [
        {"at": "10:00", "strength": 50, "label": "day"},
        {"at": "sunset-1:30", "strength": 50, "label": "start warming"},
        {"at": "dusk", "strength": 75, "label": "dark outside"},
        {"at": "00:00", "strength": 85, "label": "night"},
        {"at": "06:00", "strength": 85, "label": "start cooling"},
    ],
    "easing": "smooth",
    "location": {
        "_help": "Leave null to guess from your time zone (no network, no permissions). Set for exact sunset times.",
        "latitude": None,
        "longitude": None,
    },
    "fade_seconds": 10,
    "update_interval_seconds": 30,
    "pause_turns_off": True,
    "menubar": {"icon": "☾", "show_percent": True},
}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    path: Path
    mtime: Optional[float]
    keyframes: List[Any]
    easing: str
    latitude: Optional[float]
    longitude: Optional[float]
    fade_seconds: float
    update_interval_seconds: float
    pause_turns_off: bool
    menubar_icon: str
    show_percent: bool

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


def config_path(override: Optional[os.PathLike | str] = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return CONFIG_DIR / "config.json"


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _number(raw: Dict[str, Any], key: str, default: float, minimum: float, maximum: float, where: str) -> float:
    value = raw.get(key, default)
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f'{where}: "{key}" must be a number')
    if not minimum <= value <= maximum:
        raise ConfigError(f'{where}: "{key}" must be between {minimum} and {maximum}')
    return float(value)


def load_config(override: Optional[os.PathLike | str] = None) -> Config:
    """Load (creating a default file if needed) and validate the config."""
    path = config_path(override)
    if not path.exists():
        write_default_config(path)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}") from e
    except OSError as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        mtime: Optional[float] = path.stat().st_mtime
    except OSError:
        mtime = None
    return parse_config(raw, path, mtime)


def parse_config(raw: Any, path: Path, mtime: Optional[float] = None) -> Config:
    """Validate an in-memory config (as loaded from JSON) into a Config.

    Keyframes are kept raw here; ``Schedule.from_config`` validates them.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a JSON object")

    where = path.name
    defaults = copy.deepcopy(DEFAULT_CONFIG)

    easing = raw.get("easing", defaults["easing"])
    if easing not in ("smooth", "linear"):
        raise ConfigError(f'{where}: "easing" must be "smooth" or "linear"')

    location = raw.get("location") or {}
    if not isinstance(location, dict):
        raise ConfigError(f'{where}: "location" must be an object with latitude and longitude')
    lat = location.get("latitude")
    lon = location.get("longitude")
    if (lat is None) != (lon is None):
        raise ConfigError(f"{where}: set both latitude and longitude, or neither")
    if lat is not None:
        lat = _number(location, "latitude", 0, -90, 90, where)
        lon = _number(location, "longitude", 0, -180, 180, where)

    menubar = raw.get("menubar") or {}
    if not isinstance(menubar, dict):
        raise ConfigError(f'{where}: "menubar" must be an object')
    icon = str(menubar.get("icon", defaults["menubar"]["icon"]) or defaults["menubar"]["icon"])

    return Config(
        path=path,
        mtime=mtime,
        keyframes=raw.get("keyframes", defaults["keyframes"]),
        easing=easing,
        latitude=lat,
        longitude=lon,
        fade_seconds=_number(raw, "fade_seconds", defaults["fade_seconds"], 0, 600, where),
        update_interval_seconds=_number(raw, "update_interval_seconds", defaults["update_interval_seconds"], 5, 3600, where),
        pause_turns_off=bool(raw.get("pause_turns_off", defaults["pause_turns_off"])),
        menubar_icon=icon,
        show_percent=bool(menubar.get("show_percent", defaults["menubar"]["show_percent"])),
    )


def make_raw_config(
    *,
    keyframes: List[Any],
    easing: Any,
    latitude: Any,
    longitude: Any,
    fade_seconds: Any,
    update_interval_seconds: Any,
    pause_turns_off: Any,
    icon: Any,
    show_percent: Any,
) -> Dict[str, Any]:
    """Assemble the JSON-shaped dict that config.json holds (with its help texts)."""
    frames = []
    for kf in keyframes:
        if isinstance(kf, dict):
            frame: Dict[str, Any] = {"at": kf.get("at"), "strength": kf.get("strength")}
            if kf.get("label"):
                frame["label"] = kf["label"]
            frames.append(frame)
        else:
            frames.append(kf)
    return {
        "_help": DEFAULT_CONFIG["_help"],
        "keyframes": frames,
        "easing": easing,
        "location": {"_help": DEFAULT_CONFIG["location"]["_help"], "latitude": latitude, "longitude": longitude},
        "fade_seconds": fade_seconds,
        "update_interval_seconds": update_interval_seconds,
        "pause_turns_off": pause_turns_off,
        "menubar": {"icon": icon, "show_percent": show_percent},
    }


def save_config(path: Path, raw: Dict[str, Any]) -> None:
    """Write ``raw`` as pretty JSON, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
