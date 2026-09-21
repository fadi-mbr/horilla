"""Tests for attendance clock-out day targeting.

Regression cover for the MBR Anviz data-integrity bug: upstream's
`clock_out_attendance_and_activity` closes the employee's oldest open activity
regardless of date and stamps the result onto their newest day row, so a single
missed check-out offsets every later punch by one day, permanently.
"""

from datetime import date, datetime, time, timedelta

from django.test import TestCase

from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import clock_out_attendance_and_activity
from employee.models import Employee

DAY_3 = date(2026, 9, 14)  # the day whose check-out went missing
DAY_2 = date(2026, 9, 15)
DAY_1 = date(2026, 9, 16)  # "today" in most tests


class ClockOutDayTargetingTests(TestCase):
    """`clock_out_attendance_and_activity` must act on the punch's own day."""

    def setUp(self):
        self.employee = Employee.objects.create(
            employee_first_name="Test",
            employee_last_name="Technician",
            email="test.technician@example.invalid",
            badge_id="999",
        )

    # ---------------------------------------------------------------- helpers

    def _activity(self, day, clock_in="08:00", clock_out=None):
        return AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=day,
            clock_in_date=day,
            clock_in=time.fromisoformat(clock_in),
            in_datetime=datetime.combine(day, time.fromisoformat(clock_in)),
            clock_out=time.fromisoformat(clock_out) if clock_out else None,
            clock_out_date=day if clock_out else None,
            out_datetime=(
                datetime.combine(day, time.fromisoformat(clock_out))
                if clock_out
                else None
            ),
        )

    def _attendance(self, day, clock_in="08:00"):
        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=day,
            attendance_clock_in_date=day,
            attendance_clock_in=time.fromisoformat(clock_in),
            minimum_hour="11:00",
        )

    def _clock_out(self, day, at="19:00"):
        return clock_out_attendance_and_activity(
            employee=self.employee,
            date_today=day,
            now=at,
            out_datetime=datetime.combine(day, time.fromisoformat(at)),
        )

    # ------------------------------------------------------------------ tests

    def test_closes_todays_activity_not_the_stale_one(self):
        """A stale open activity must not swallow today's check-out."""
        stale = self._activity(DAY_3)  # never clocked out
        self._attendance(DAY_3)
        today = self._activity(DAY_1)
        self._attendance(DAY_1)

        self._clock_out(DAY_1)

        stale.refresh_from_db()
        today.refresh_from_db()
        self.assertIsNone(stale.clock_out, "stale activity must stay open")
        self.assertEqual(today.clock_out, time(19, 0))

    def test_updates_the_matching_day_row_not_the_newest(self):
        """The day row updated must be the activity's own, not the latest."""
        self._activity(DAY_3)
        day_3_row = self._attendance(DAY_3)
        self._activity(DAY_1)
        day_1_row = self._attendance(DAY_1)

        returned = self._clock_out(DAY_1)

        day_3_row.refresh_from_db()
        day_1_row.refresh_from_db()
        self.assertEqual(returned.pk, day_1_row.pk)
        self.assertEqual(day_1_row.attendance_clock_out, time(19, 0))
        self.assertIsNone(
            day_3_row.attendance_clock_out,
            "the older day row must not receive today's punch",
        )

    def test_night_shift_closes_previous_day_activity(self):
        """An after-midnight punch closes the shift that opened yesterday."""
        night = self._activity(DAY_2, clock_in="22:00")
        self._attendance(DAY_2, clock_in="22:00")

        self._clock_out(DAY_1, at="06:00")

        night.refresh_from_db()
        self.assertEqual(night.clock_out, time(6, 0))
        self.assertEqual(night.clock_out_date, DAY_1)

    def test_no_match_leaves_stale_activity_open(self):
        """With nothing from today or yesterday, close nothing."""
        stale = self._activity(DAY_3)
        self._attendance(DAY_3)

        returned = self._clock_out(DAY_1)

        stale.refresh_from_db()
        self.assertIsNone(returned)
        self.assertIsNone(stale.clock_out)

    def test_worked_hour_reflects_the_activitys_own_day(self):
        """Duration is computed from the day being closed, not the newest."""
        self._activity(DAY_1, clock_in="08:00")
        row = self._attendance(DAY_1, clock_in="08:00")

        self._clock_out(DAY_1, at="19:00")

        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "11:00")

    def test_second_activity_same_day_is_preferred(self):
        """Split shifts: the latest open activity of that day wins."""
        first = self._activity(DAY_1, clock_in="08:00", clock_out="12:00")
        second = self._activity(DAY_1, clock_in="14:00")
        self._attendance(DAY_1, clock_in="08:00")

        self._clock_out(DAY_1, at="19:00")

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.clock_out, time(12, 0), "closed activity untouched")
        self.assertEqual(second.clock_out, time(19, 0))
