import copy
import json
import tempfile
import unittest
from pathlib import Path

from smartshift.config import DEFAULT_CONFIG, ConfigError, load_config, make_raw_config, parse_config, save_config
from smartshift.schedule import Schedule, ScheduleError

PATH = Path("/tmp/config.json")


class ParseConfigTests(unittest.TestCase):
    def test_defaults_parse(self):
        cfg = parse_config(copy.deepcopy(DEFAULT_CONFIG), PATH)
        self.assertEqual(cfg.easing, "smooth")
        self.assertFalse(cfg.has_location)
        self.assertEqual(len(cfg.keyframes), 5)
        self.assertEqual(cfg.fade_seconds, 10)
        Schedule.from_config(cfg.keyframes, cfg.easing)  # keyframes are valid

    def test_missing_keys_fall_back_to_defaults(self):
        cfg = parse_config({"keyframes": [{"at": "08:00", "strength": 40}, {"at": "20:00", "strength": 80}]}, PATH)
        self.assertEqual(cfg.update_interval_seconds, 30)
        self.assertTrue(cfg.show_percent)
        self.assertEqual(cfg.menubar_icon, "☾")

    def test_validation_errors(self):
        bad = [
            ("top level", [1, 2]),
            ("easing", {"easing": "bouncy"}),
            ("fade_seconds", {"fade_seconds": "ten"}),
            ("update_interval_seconds", {"update_interval_seconds": 1}),
            ("latitude", {"location": {"latitude": 95, "longitude": 0}}),
            ("both", {"location": {"latitude": 10}}),
            ("menubar", {"menubar": "x"}),
        ]
        for name, raw in bad:
            with self.assertRaises(ConfigError, msg=name):
                parse_config(raw, PATH)

    def test_roundtrip_through_make_raw_config(self):
        raw = make_raw_config(
            keyframes=[{"at": "09:00", "strength": 45, "label": "day"}, {"at": "dusk", "strength": 80, "label": ""}],
            easing="linear",
            latitude=52.25,
            longitude=21.0,
            fade_seconds=5,
            update_interval_seconds=15,
            pause_turns_off=False,
            icon="☀",
            show_percent=False,
        )
        self.assertEqual(raw["keyframes"][1], {"at": "dusk", "strength": 80})  # empty label dropped
        self.assertIn("_help", raw)
        cfg = parse_config(raw, PATH)
        self.assertEqual(cfg.easing, "linear")
        self.assertEqual((cfg.latitude, cfg.longitude), (52.25, 21.0))
        self.assertEqual(cfg.fade_seconds, 5)
        self.assertFalse(cfg.pause_turns_off)
        self.assertEqual(cfg.menubar_icon, "☀")
        self.assertFalse(cfg.show_percent)

    def test_invalid_keyframes_are_reported_by_schedule(self):
        raw = make_raw_config(
            keyframes=[{"at": "09:00", "strength": "lots"}, {"at": "dusk", "strength": 80}],
            easing="smooth", latitude=None, longitude=None, fade_seconds=10,
            update_interval_seconds=30, pause_turns_off=True, icon="☾", show_percent=True,
        )
        cfg = parse_config(raw, PATH)
        with self.assertRaises(ScheduleError) as ctx:
            Schedule.from_config(cfg.keyframes, cfg.easing)
        self.assertIn("keyframe #1", str(ctx.exception))


class SaveLoadTests(unittest.TestCase):
    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.json"
            raw = make_raw_config(
                keyframes=[{"at": "10:00", "strength": 50}, {"at": "sunset-1:30", "strength": 50}, {"at": "dusk", "strength": 75}],
                easing="smooth", latitude=None, longitude=None, fade_seconds=10,
                update_interval_seconds=30, pause_turns_off=True, icon="☾", show_percent=True,
            )
            save_config(path, raw)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), raw)
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            cfg = load_config(path)
            self.assertEqual(cfg.path, path)
            self.assertIsNotNone(cfg.mtime)
            self.assertEqual([kf["at"] for kf in cfg.keyframes], ["10:00", "sunset-1:30", "dusk"])

    def test_load_creates_default_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = load_config(path)
            self.assertTrue(path.exists())
            self.assertEqual(len(cfg.keyframes), 5)


if __name__ == "__main__":
    unittest.main()
