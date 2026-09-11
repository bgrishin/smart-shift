import unittest
from datetime import date, datetime

from smartshift.schedule import (
    FALLBACK_EVENT_MINUTES,
    Keyframe,
    Schedule,
    ScheduleError,
    TimeExpr,
)

DEFAULT_KEYFRAMES = [
    {"at": "10:00", "strength": 50, "label": "day"},
    {"at": "sunset-1:30", "strength": 50},
    {"at": "dusk", "strength": 75},
    {"at": "00:00", "strength": 85},
    {"at": "06:00", "strength": 85},
]

DAY = date(2026, 9, 10)


def fixed_sun(sunset_hm=(20, 0), dusk_hm=(20, 40)):
    """Solar lookup stub: same sunset/dusk every day."""
    table = {"sunset": sunset_hm, "dusk": dusk_hm, "sunrise": (6, 0), "dawn": (5, 30), "noon": (13, 0)}

    def lookup(day, event):
        h, m = table[event]
        return datetime(day.year, day.month, day.day, h, m)

    return lookup


def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute)


class TimeExprTests(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(TimeExpr.parse("7:05"), TimeExpr("clock", 425, "7:05"))
        self.assertEqual(TimeExpr.parse("00:00").minutes, 0)
        self.assertEqual(TimeExpr.parse("23:59").minutes, 1439)

    def test_solar_with_offsets(self):
        self.assertEqual(TimeExpr.parse("sunset-1:30"), TimeExpr("sunset", -90, "sunset-1:30"))
        self.assertEqual(TimeExpr.parse("dusk+20m").minutes, 20)
        self.assertEqual(TimeExpr.parse("Sunrise + 2h").minutes, 120)
        self.assertEqual(TimeExpr.parse("dawn - 45").minutes, -45)
        self.assertEqual(TimeExpr.parse("sunset−1:00").minutes, -60)  # unicode minus
        self.assertEqual(TimeExpr.parse("noon").minutes, 0)

    def test_errors(self):
        for bad in ("24:00", "12:60", "midnight", "sunset-", "sunset+abc", 1200, None, "sunset+30h"):
            with self.assertRaises(ScheduleError, msg=repr(bad)):
                TimeExpr.parse(bad)


class KeyframeTests(unittest.TestCase):
    def test_from_dict(self):
        kf = Keyframe.from_dict({"at": "21:00", "strength": 75, "label": "evening"}, 0)
        self.assertAlmostEqual(kf.strength, 0.75)
        self.assertEqual(kf.name, "evening")
        self.assertEqual(Keyframe.from_dict({"at": "dusk", "strength": 0}, 1).name, "dusk")

    def test_validation(self):
        for bad in ({"at": "21:00"}, {"strength": 5}, {"at": "21:00", "strength": 101}, {"at": "21:00", "strength": "75"}, {"at": "21:00", "strength": True}, "21:00"):
            with self.assertRaises(ScheduleError, msg=repr(bad)):
                Keyframe.from_dict(bad, 0)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.schedule = Schedule.from_config(DEFAULT_KEYFRAMES, "smooth", fixed_sun())

    def test_requires_two_keyframes(self):
        with self.assertRaises(ScheduleError):
            Schedule.from_config([{"at": "10:00", "strength": 50}])
        with self.assertRaises(ScheduleError):
            Schedule.from_config(DEFAULT_KEYFRAMES, "bouncy")
        with self.assertRaises(ScheduleError):
            Schedule.from_config({"at": "10:00"})

    def test_resolution_unrolls_past_midnight(self):
        minutes = [f.minute for f in self.schedule.resolve(DAY)]
        # 10:00, 18:30, 20:40, 00:00 (+1 day), 06:00 (+1 day)
        self.assertEqual(minutes, [600, 1110, 1240, 1440, 1800])

    def test_default_curve(self):
        s = self.schedule.strength_at
        self.assertAlmostEqual(s(at(12)), 0.50)
        self.assertAlmostEqual(s(at(18, 30)), 0.50)
        self.assertAlmostEqual(s(at(19, 35)), 0.625)  # midpoint of 18:30 -> 20:40
        self.assertAlmostEqual(s(at(20, 40)), 0.75)
        self.assertAlmostEqual(s(at(22, 20)), 0.80)  # midpoint of 20:40 -> 00:00
        self.assertAlmostEqual(s(at(0, 0)), 0.85)
        self.assertAlmostEqual(s(at(3)), 0.85)
        self.assertAlmostEqual(s(at(6)), 0.85)
        self.assertAlmostEqual(s(at(8)), 0.675)  # midpoint of 06:00 -> 10:00
        self.assertAlmostEqual(s(at(10)), 0.50)

    def test_smooth_easing_is_gentle_near_keyframes(self):
        smooth = self.schedule.strength_at(at(18, 45))
        linear = Schedule.from_config(DEFAULT_KEYFRAMES, "linear", fixed_sun()).strength_at(at(18, 45))
        self.assertLess(smooth, linear)
        self.assertGreater(smooth, 0.50)

    def test_monotonic_within_segments(self):
        prev = None
        for minute in range(18 * 60 + 30, 24 * 60, 5):
            value = self.schedule.strength_at(at(minute // 60, minute % 60))
            if prev is not None:
                self.assertGreaterEqual(value, prev - 1e-9)
            prev = value

    def test_rotated_keyframe_order_gives_same_curve(self):
        rotated = DEFAULT_KEYFRAMES[3:] + DEFAULT_KEYFRAMES[:3]
        other = Schedule.from_config(rotated, "smooth", fixed_sun())
        for hour in range(24):
            self.assertAlmostEqual(other.strength_at(at(hour)), self.schedule.strength_at(at(hour)), places=9)

    def test_segment_description(self):
        seg = self.schedule.segment_at(at(19, 0))
        self.assertEqual(seg.direction, "warming")
        self.assertEqual(seg.end.keyframe.name, "dusk")
        self.assertEqual(seg.end_time, at(20, 40))
        seg = self.schedule.segment_at(at(2, 0))
        self.assertEqual(seg.direction, "holding")
        self.assertEqual(seg.end_time, at(6, 0))
        seg = self.schedule.segment_at(at(9, 0))
        self.assertEqual(seg.direction, "cooling")
        self.assertEqual(seg.end_time, at(10, 0))

    def test_fallback_when_no_location(self):
        schedule = Schedule.from_config(DEFAULT_KEYFRAMES, "smooth", None)
        frames = schedule.resolve(DAY)
        self.assertTrue(frames[1].solar_fallback)
        self.assertEqual(frames[1].minute, FALLBACK_EVENT_MINUTES["sunset"] - 90)
        self.assertEqual(frames[2].minute, FALLBACK_EVENT_MINUTES["dusk"])
        self.assertFalse(frames[0].solar_fallback)

    def test_fallback_when_sun_never_sets(self):
        schedule = Schedule.from_config(DEFAULT_KEYFRAMES, "smooth", lambda day, event: None)
        self.assertTrue(all(f.solar_fallback for f in schedule.resolve(DAY) if f.keyframe.at.is_solar))
        self.assertAlmostEqual(schedule.strength_at(at(12)), 0.50)

    def test_dusk_after_midnight_does_not_break(self):
        # High latitude summer: civil dusk lands at 00:10 the next morning.
        schedule = Schedule.from_config(DEFAULT_KEYFRAMES, "smooth", fixed_sun(sunset_hm=(22, 30), dusk_hm=(0, 10)))
        minutes = [f.minute for f in schedule.resolve(DAY)]
        self.assertEqual(minutes, [600, 1260, 1450, 1450, 1800])
        self.assertTrue(0.50 <= schedule.strength_at(at(23, 50)) <= 0.75)
        self.assertAlmostEqual(schedule.strength_at(at(0, 30)), 0.85)
        self.assertAlmostEqual(schedule.strength_at(at(12)), 0.50)

    def test_two_point_schedule(self):
        schedule = Schedule.from_config([{"at": "08:00", "strength": 40}, {"at": "20:00", "strength": 80}], "linear")
        self.assertAlmostEqual(schedule.strength_at(at(14)), 0.60)
        self.assertAlmostEqual(schedule.strength_at(at(2)), 0.60)
        self.assertAlmostEqual(schedule.strength_at(at(8)), 0.40)


if __name__ == "__main__":
    unittest.main()
