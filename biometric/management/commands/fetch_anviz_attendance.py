"""Fetch Anviz CrossChex Cloud attendance for configured biometric devices.

Horilla's UI scheduler stores `is_scheduler=True` on the device, but the
actual APScheduler job is created in the web process that handled the click.
That job is not durable across container restarts/deploys and is not visible to
Celery beat. This command gives production a restart-safe entrypoint that can be
called from Coolify cron, host cron, or a scheduled container.
"""

import logging

from django.core.management.base import BaseCommand, CommandError

from biometric.models import BiometricDevices
from biometric.views import anviz_biometric_attendance_logs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fetch Anviz/CrossChex Cloud attendance logs for active biometric devices."

    def add_arguments(self, parser):
        parser.add_argument(
            "--device-id",
            dest="device_id",
            help="Fetch one biometric device UUID only.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Fetch all active Anviz devices, not only devices marked is_scheduler=True.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List matching devices without contacting CrossChex or writing attendance.",
        )

    def handle(self, *args, **options):
        qs = BiometricDevices.objects.filter(machine_type="anviz", is_active=True)

        if options["device_id"]:
            qs = qs.filter(id=options["device_id"])
        elif not options["all"]:
            qs = qs.filter(is_scheduler=True)

        devices = list(qs.order_by("name"))
        if not devices:
            raise CommandError(
                "No active Anviz biometric devices matched. "
                "Check device type, active flag, and scheduler flag."
            )

        if options["dry_run"]:
            for device in devices:
                self.stdout.write(
                    f"{device.id} {device.name} "
                    f"scheduled={device.is_scheduler} "
                    f"duration={device.scheduler_duration} "
                    f"last_fetch={device.last_fetch_date} {device.last_fetch_time}"
                )
            return

        total = 0
        failures = 0
        for device in devices:
            try:
                count = anviz_biometric_attendance_logs(device)
                if not isinstance(count, int):
                    failures += 1
                    self.stderr.write(f"{device.name}: fetch returned non-count result {count!r}")
                    continue
                total += count
                self.stdout.write(f"{device.name}: processed {count} record(s)")
            except Exception as exc:  # keep processing other devices
                failures += 1
                logger.exception("Anviz attendance fetch failed for device %s", device.id)
                self.stderr.write(f"{device.name}: failed: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Anviz attendance fetch complete: devices={len(devices)}, "
                f"processed={total}, failures={failures}"
            )
        )

        if failures:
            raise CommandError(f"{failures} Anviz device fetch(es) failed")
