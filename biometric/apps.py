"""
Django application configuration for the biometric app.
"""

import fcntl
import logging
import sys

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)
_ANVIZ_SCHEDULER = None


class BiometricConfig(AppConfig):
    """
    This class defines the configuration for the biometric Django app. It sets the
    default auto field to use a BigAutoField for model primary keys.

    Attributes:
        default_auto_field (str): The default auto field to use for model primary keys.
        name (str): The name of the Django app, which is 'biometric'.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "biometric"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        settings.APPS.append("biometric")
        urlpatterns.append(
            path("biometric/", include("biometric.urls")),
        )

        from biometric import sidebar

        super().ready()
        self._start_anviz_schedulers()

    def _start_anviz_schedulers(self):
        """Restart persisted Anviz schedules when the web process boots.

        The built-in schedule button starts APScheduler only in memory. Without
        this, devices with `is_scheduler=True` stop syncing after every
        container restart/redeploy until someone manually schedules them again.
        """
        global _ANVIZ_SCHEDULER

        # Only the web server should run the lightweight APScheduler jobs.
        # Management commands/migrations/imports should not contact CrossChex.
        argv = " ".join(sys.argv).lower()
        if "gunicorn" not in argv and "runserver" not in argv:
            return
        if _ANVIZ_SCHEDULER is not None:
            return

        # Gunicorn runs several workers and each imports this app; only the
        # worker that wins this container-local flock runs the scheduler,
        # otherwise every worker would fetch (and trip CrossChex's 15s limit).
        try:
            self._anviz_lock_fh = open("/tmp/horilla-anviz-scheduler.lock", "w")
            fcntl.flock(self._anviz_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return

        try:
            from datetime import datetime

            from apscheduler.schedulers.background import BackgroundScheduler

            from biometric.models import BiometricDevices
            from biometric.views import (
                anviz_biometric_attendance_scheduler,
                str_time_seconds,
            )

            devices = BiometricDevices.objects.filter(
                machine_type="anviz",
                is_active=True,
                is_scheduler=True,
            )
            if not devices.exists():
                return

            scheduler = BackgroundScheduler()
            scheduled = 0
            for device in devices:
                interval = str_time_seconds(device.scheduler_duration)
                if interval <= 0:
                    logger.warning(
                        "Skipping Anviz scheduler for %s: invalid interval %s",
                        device.id,
                        device.scheduler_duration,
                    )
                    continue
                scheduler.add_job(
                    lambda device_id=device.id: anviz_biometric_attendance_scheduler(
                        device_id
                    ),
                    "interval",
                    seconds=interval,
                    next_run_time=datetime.now(),
                    id=f"anviz-attendance-{device.id}",
                    replace_existing=True,
                )
                scheduled += 1

            if scheduled:
                scheduler.start()
                _ANVIZ_SCHEDULER = scheduler
                logger.info("Started %s persisted Anviz attendance scheduler(s)", scheduled)
        except Exception:
            logger.exception("Failed to start persisted Anviz attendance scheduler(s)")
