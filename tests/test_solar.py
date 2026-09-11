import os
import tempfile
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from smartshift import solar

WARSAW = (52.23, 21.01, ZoneInfo("Europe/Warsaw"))
HONOLULU = (21.31, -157.86, ZoneInfo("Pacific/Honolulu"))
TROMSO = (69.65, 18.96, ZoneInfo("Europe/Oslo"))


def minutes(moment):
    return moment.hour * 60 + moment.minute


class SunEventTests(unittest.TestCase):
    def assert_close(self, moment, hour, minute, tolerance=8):
        self.assertIsNotNone(moment)
        self.assertLessEqual(abs(minutes(moment) - (hour * 60 + minute)), tolerance, f"{moment} vs {hour:02d}:{minute:02d}")

    def test_warsaw_summer_solstice(self):
        lat, lon, tz = WARSAW
        day = date(2026, 6, 21)
        self.assert_close(solar.sun_event(day, lat, lon, "sunrise", tz), 4, 14)
        self.assert_close(solar.sun_event(day, lat, lon, "sunset", tz), 21, 1)
        dusk = solar.sun_event(day, lat, lon, "dusk", tz)
        self.assertGreater(minutes(dusk), 21 * 60 + 30)
        self.assertEqual(dusk.date(), day)

    def test_warsaw_winter_solstice(self):
        lat, lon, tz = WARSAW
        day = date(2026, 12, 21)
        self.assert_close(solar.sun_event(day, lat, lon, "sunrise", tz), 7, 43)
        self.assert_close(solar.sun_event(day, lat, lon, "sunset", tz), 15, 25)
        self.assert_close(solar.sun_event(day, lat, lon, "noon", tz), 11, 34, tolerance=15)

    def test_honolulu_dates_snap_to_local_day(self):
        lat, lon, tz = HONOLULU
        day = date(2026, 6, 21)
        sunrise = solar.sun_event(day, lat, lon, "sunrise", tz)
        sunset = solar.sun_event(day, lat, lon, "sunset", tz)
        self.assert_close(sunrise, 5, 50, tolerance=10)
        self.assert_close(sunset, 19, 16, tolerance=10)
        self.assertEqual(sunrise.date(), day)
        self.assertEqual(sunset.date(), day)

    def test_polar_day_and_night(self):
        lat, lon, tz = TROMSO
        self.assertIsNone(solar.sun_event(date(2026, 6, 21), lat, lon, "sunset", tz))
        self.assertIsNone(solar.sun_event(date(2026, 12, 21), lat, lon, "sunrise", tz))
        self.assertIsNotNone(solar.sun_event(date(2026, 6, 21), lat, lon, "noon", tz))

    def test_event_order(self):
        lat, lon, tz = WARSAW
        times = solar.sun_times(date(2026, 3, 20), lat, lon, tz)
        order = [times[e] for e in ("dawn", "sunrise", "noon", "sunset", "dusk")]
        self.assertEqual(order, sorted(order))

    def test_unknown_event(self):
        with self.assertRaises(ValueError):
            solar.sun_event(date(2026, 1, 1), 0, 0, "teatime")


class LocationTests(unittest.TestCase):
    def test_parse_iso6709(self):
        lat, lon = solar.parse_iso6709("+5215+02100")
        self.assertAlmostEqual(lat, 52.25)
        self.assertAlmostEqual(lon, 21.0)
        lat, lon = solar.parse_iso6709("+404251-0740023")
        self.assertAlmostEqual(lat, 40.714, places=3)
        self.assertAlmostEqual(lon, -74.006, places=3)
        with self.assertRaises(ValueError):
            solar.parse_iso6709("52.25,21")

    def test_location_from_zone_tab(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tab", delete=False) as fh:
            fh.write("# comment\nPL\t+5215+02100\tEurope/Warsaw\nUS\t+404251-0740023\tAmerica/New_York\tEastern\n")
            path = fh.name
        try:
            self.assertEqual(solar.location_from_timezone("Europe/Warsaw", path), (52.25, 21.0, "Europe/Warsaw"))
            self.assertIsNone(solar.location_from_timezone("Mars/Olympus", path))
        finally:
            os.unlink(path)

    def test_system_timezone_name_from_env(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        try:
            self.assertEqual(solar.system_timezone_name(), "America/New_York")
        finally:
            if old is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = old


if __name__ == "__main__":
    unittest.main()
