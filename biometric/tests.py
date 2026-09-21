"""Tests for the Anviz import concurrency guard.

Root cause of the MBR attendance corruption: two Anviz schedulers ran at once
(one started at module import in `biometric.views`, once per gunicorn worker,
plus the durable one in `biometric.apps`). Overlapping fetches delivered the
same punch twice; the importer's check-then-act idempotency guard has no
database constraint behind it, so both runs imported it, and the replayed
clock-in nulled that day's clock-out in `clock_in_attendance_and_activity`.
"""

import multiprocessing
import re
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from biometric.models import BiometricDevices
from employee.models import Employee
from attendance.models import AttendanceActivity
from biometric.anviz import CrossChexCloudAPI
from biometric.views import (
    anviz_biometric_attendance_logs,
    anviz_import_mutex,
    employee_for_badge,
)

VIEWS_SOURCE = Path(__file__).resolve().parent / "views.py"


def _hold_mutex(device, started, release):
    """Acquire the mutex in a separate process and hold it."""
    with anviz_import_mutex(device) as acquired:
        started.set()
        release.wait(timeout=10)
        return acquired


class AnvizImportMutexTests(TestCase):
    def setUp(self):
        self.device = BiometricDevices.objects.create(
            name="Test Anviz",
            machine_type="anviz",
            is_active=True,
            is_scheduler=True,
            scheduler_duration="00:05",
        )

    def test_mutex_is_acquired_when_free(self):
        with anviz_import_mutex(self.device) as acquired:
            self.assertTrue(acquired)

    def test_mutex_is_released_after_use(self):
        with anviz_import_mutex(self.device) as first:
            self.assertTrue(first)
        with anviz_import_mutex(self.device) as second:
            self.assertTrue(second, "mutex must be reusable once released")

    def test_second_holder_is_refused_while_first_holds(self):
        """A concurrent importer must back off, not run in parallel."""
        ctx = multiprocessing.get_context("fork")
        started, release = ctx.Event(), ctx.Event()
        holder = ctx.Process(
            target=_hold_mutex, args=(self.device, started, release)
        )
        holder.start()
        try:
            self.assertTrue(started.wait(timeout=10), "holder never started")
            with anviz_import_mutex(self.device) as acquired:
                self.assertFalse(
                    acquired,
                    "a second importer acquired the lock while one was held",
                )
        finally:
            release.set()
            holder.join(timeout=10)

    def test_mutex_is_per_device(self):
        other = BiometricDevices.objects.create(
            name="Other Anviz",
            machine_type="anviz",
            is_active=True,
            is_scheduler=True,
            scheduler_duration="00:05",
        )
        with anviz_import_mutex(self.device) as first:
            with anviz_import_mutex(other) as second:
                self.assertTrue(first)
                self.assertTrue(second, "devices must not block each other")


class NoDuplicateAnvizSchedulerTests(SimpleTestCase):
    """The module-level auto-start must not schedule Anviz devices."""

    def test_module_level_block_does_not_start_anviz_scheduler(self):
        source = VIEWS_SOURCE.read_text()
        tail = source[source.index("\ntry:\n    devices = BiometricDevices"):]
        anviz_branch = re.search(
            r'if device\.machine_type == "anviz":(.*?)elif device\.machine_type == "zk":',
            tail,
            re.S,
        )
        self.assertIsNotNone(anviz_branch, "anviz branch not found in views.py tail")
        self.assertNotIn(
            "scheduler.start()",
            anviz_branch.group(1),
            "views.py must not start a second Anviz scheduler; apps.py owns it",
        )


class EmployeeForBadgeTests(TestCase):
    """CrossChex sends '2'; Horilla stores '002'. Resolve that, but safely."""

    def _employee(self, badge, first, active=True):
        return Employee.objects.create(
            employee_first_name=first,
            employee_last_name="Test",
            email=f"{first.lower().replace(' ', '.')}@example.invalid",
            badge_id=badge,
            is_active=active,
        )

    def test_exact_badge_match(self):
        emp = self._employee("103", "Naif")
        self.assertEqual(employee_for_badge("103"), emp)

    def test_zero_padded_badge_matches(self):
        """The bug: six managers' punches were dropped by this mismatch."""
        emp = self._employee("002", "Micheal")
        self.assertEqual(employee_for_badge("2"), emp)

    def test_ignored_badges_never_match(self):
        """'1' is Admin MBR on the device; '001' is a different person."""
        self._employee("001", "Basel")
        self.assertIsNone(employee_for_badge("1"))
        self.assertIsNone(employee_for_badge("101"))

    def test_inactive_employee_never_matches(self):
        """A resigned employee whose badge still works accrues no attendance."""
        self._employee("102", "Abraham", active=False)
        self.assertIsNone(employee_for_badge("102"))

    def test_ambiguous_normalisation_is_refused(self):
        """Two candidates must not be guessed between."""
        self._employee("007", "Ata")
        self._employee("7", "Other")
        self.assertIsNone(employee_for_badge("07"))

    def test_unknown_badge_returns_none(self):
        self.assertIsNone(employee_for_badge("999"))

    def test_blank_badge_returns_none(self):
        self.assertIsNone(employee_for_badge(None))
        self.assertIsNone(employee_for_badge("  "))


class FetchWindowAdvanceTests(TestCase):
    """The fetch window must advance when punches actually land.

    `clock_in`/`clock_out` render a template against a fake request object and
    raise AFTER writing. Treating that exception as a failed import pinned
    `last_fetch` permanently, so the importer re-fetched an ever-growing window
    and the freshness monitor read as stale forever.
    """

    def setUp(self):
        self.device = BiometricDevices.objects.create(
            name="Test Anviz",
            machine_type="anviz",
            is_active=True,
            is_scheduler=True,
            scheduler_duration="00:05",
            last_fetch_date=date(2026, 9, 21),
            last_fetch_time=time(8, 0),
        )
        self.employee = Employee.objects.create(
            employee_first_name="Punch",
            employee_last_name="Tester",
            email="punch.tester@example.invalid",
            badge_id="500",
        )

    def _records(self, punched_at):
        return {
            "list": [
                {
                    "checktime": punched_at.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "checktype": 0,
                    "employee": {"workno": "500"},
                }
            ]
        }

    def test_window_advances_when_the_punch_landed_despite_an_exception(self):
        punched = datetime(2026, 9, 21, 9, 0, 0)

        def write_then_raise(request):
            AttendanceActivity.objects.create(
                employee_id=self.employee,
                attendance_date=request.date,
                clock_in_date=request.date,
                clock_in=request.time,
            )
            raise AttributeError("'dict' object has no attribute 'session_key'")

        with patch.object(
            CrossChexCloudAPI, "get_attendance_records",
            return_value=self._records(punched),
        ), patch("biometric.views.clock_in", side_effect=write_then_raise):
            anviz_biometric_attendance_logs(self.device)

        self.device.refresh_from_db()
        stored = datetime.combine(
            self.device.last_fetch_date, self.device.last_fetch_time
        )
        self.assertGreater(
            stored,
            punched,
            "the window must advance past a punch that was stored; holding it "
            "at the punch re-fetches an ever-growing window forever",
        )

    def test_window_is_held_back_when_the_punch_did_not_land(self):
        punched = datetime(2026, 9, 21, 9, 0, 0)

        with patch.object(
            CrossChexCloudAPI, "get_attendance_records",
            return_value=self._records(punched),
        ), patch("biometric.views.clock_in", side_effect=RuntimeError("boom")):
            anviz_biometric_attendance_logs(self.device)

        self.device.refresh_from_db()
        stored = datetime.combine(
            self.device.last_fetch_date, self.device.last_fetch_time
        )
        self.assertLessEqual(
            stored, punched, "a genuinely failed punch must be re-fetched"
        )
