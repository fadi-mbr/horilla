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
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from biometric.models import BiometricDevices
from biometric.views import anviz_import_mutex

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
