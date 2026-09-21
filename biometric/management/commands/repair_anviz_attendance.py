"""Reconcile Horilla attendance against CrossChex Cloud's own punch record.

Two Anviz schedulers ran concurrently between 2026-08-22 and the fix that
retires the module-level one. Overlapping fetches imported the same punch
twice: each replay added an AttendanceActivity and, via the `else` branch of
`clock_in_attendance_and_activity`, nulled that day's `attendance_clock_out`.

Horilla's own activity rows are downstream of that corruption, so they cannot
be the source of truth for a repair. The device's raw punch log is. This
command re-reads punches straight from CrossChex for a date window, rebuilds
what each employee-day should look like, and reports (or, with --apply,
writes) the difference.

Read-only by default. It never advances the device's fetch window, so it is
safe to run alongside the live scheduler. Take a database dump before --apply.

    python manage.py repair_anviz_attendance --since 2026-08-24
    python manage.py repair_anviz_attendance --since 2026-08-24 --apply
"""

import logging
from collections import defaultdict
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone as django_timezone

from attendance.methods.utils import format_time, overtime_calculation
from attendance.models import Attendance, AttendanceActivity
from attendance.views.views import attendance_validate
from biometric.anviz import CrossChexCloudAPI
from biometric.models import BiometricDevices
from employee.models import Employee

logger = logging.getLogger(__name__)

IN_CODES = {0, 128}

# Two taps this close together are one person tapping twice, not a break.
# Badge 117 double-punches most mornings (e.g. 07:40:03 and 07:40:07).
DEDUP_SECONDS = 90


class Command(BaseCommand):
    help = "Reconcile attendance against CrossChex Cloud punch records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--since", required=True, help="First attendance date to check (YYYY-MM-DD)."
        )
        parser.add_argument(
            "--until", help="Last attendance date to check (YYYY-MM-DD). Default: today."
        )
        parser.add_argument("--device-id", help="Restrict to one Anviz device UUID.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the corrections. Without this the command only reports.",
        )

    # ------------------------------------------------------------------ fetch

    def _punches(self, device, since, until):
        """Every punch CrossChex holds for the window: {badge: {date: [(dt, code)]}}."""
        api = CrossChexCloudAPI(
            api_url=device.api_url,
            api_key=device.api_key,
            api_secret=device.api_secret,
            anviz_request_id=device.anviz_request_id,
        )
        begin = datetime.combine(since, time.min)
        end = datetime.combine(until, time.max)
        self.stdout.write(f"Fetching CrossChex punches {begin} .. {end} (UTC) ...")
        records = api.get_attendance_records(begin_time=begin, end_time=end)

        local_tz = django_timezone.get_current_timezone()
        by_employee_day = defaultdict(lambda: defaultdict(list))
        for record in records["list"]:
            punched = datetime.strptime(
                record["checktime"], "%Y-%m-%dT%H:%M:%S%z"
            ).astimezone(local_tz)
            badge = record["employee"]["workno"]
            by_employee_day[badge][punched.date()].append((punched, record["checktype"]))
        for badge in by_employee_day:
            for day in by_employee_day[badge]:
                by_employee_day[badge][day].sort(key=lambda item: item[0])
        return by_employee_day

    # ----------------------------------------------------------------- derive

    def _pairs(self, punches):
        """Pair a day's punches into (in, out) spans by alternation.

        The device's own check-type is not trustworthy: badge 103's 19:59
        departure on 2026-08-24 is recorded as an IN (code 0), and several
        staff double-tap. So ignore `checktype` entirely — collapse taps that
        are seconds apart, then alternate from the first punch of the day,
        which is always an arrival. An odd count leaves the last IN unclosed,
        which is reported rather than guessed at; this data feeds payroll.
        """
        collapsed, anomalies = [], []
        for stamp, code in punches:
            if collapsed and (stamp - collapsed[-1]).total_seconds() <= DEDUP_SECONDS:
                anomalies.append(
                    f"double tap {collapsed[-1]:%H:%M:%S} / {stamp:%H:%M:%S}, collapsed"
                )
                continue
            collapsed.append(stamp)

        disagree = sum(
            1
            for idx, stamp in enumerate(collapsed)
            for orig, code in punches
            if orig == stamp and ((idx % 2 == 0) != (code in IN_CODES))
        )
        if disagree:
            anomalies.append(
                f"{disagree} punch(es) whose device check-type contradicts "
                f"alternation; alternation used"
            )

        # A day's first punch is an arrival and its last is a departure. Pure
        # alternation breaks that when someone misses a mid-day punch: the
        # parity flips and the real evening departure reads as an unclosed
        # arrival, which would erase a check-out Horilla already has right.
        # So pair by alternation, then force the day to close on its last
        # punch when the count is odd.
        pairs = []
        for idx in range(0, len(collapsed), 2):
            punch_in = collapsed[idx]
            punch_out = collapsed[idx + 1] if idx + 1 < len(collapsed) else None
            pairs.append((punch_in, punch_out))

        if len(collapsed) == 1:
            anomalies.append(
                f"single punch at {collapsed[0]:%H:%M}; no departure on the device"
            )
        elif pairs and pairs[-1][1] is None:
            # Odd count: a punch was missed earlier in the day. Close the last
            # span on the final punch and flag the span for human review
            # rather than dropping the departure.
            dangling = pairs[-1][0]
            pairs[-1] = (dangling, collapsed[-1])
            if dangling == collapsed[-1]:
                pairs.pop()
                pairs[-1] = (pairs[-1][0], collapsed[-1]) if pairs else pairs
            anomalies.append(
                f"odd punch count ({len(collapsed)}); a mid-day punch is missing, "
                f"day closed on the last punch {collapsed[-1]:%H:%M} — worked hours "
                f"need a human check"
            )

        return pairs, anomalies

    # ------------------------------------------------------------- comparison

    def _observed(self, employee, day):
        activities = list(
            AttendanceActivity.objects.filter(
                employee_id=employee, attendance_date=day
            ).order_by("id")
        )
        row = (
            Attendance.objects.filter(employee_id=employee, attendance_date=day)
            .order_by("-id")
            .first()
        )
        return activities, row

    def _diverges(self, pairs, activities, row):
        """What is wrong with this employee-day, as a list of short strings."""
        problems = []
        if row is None:
            return ["no attendance day row"]

        want_out = pairs[-1][1] if pairs else None
        have_out = row.attendance_clock_out
        if _hm(want_out) != _hm(have_out):
            problems.append(f"day clock_out {_hm(have_out)} != device {_hm(want_out)}")

        want_in = pairs[0][0] if pairs else None
        if _hm(want_in) != _hm(row.attendance_clock_in):
            problems.append(
                f"day clock_in {_hm(row.attendance_clock_in)} != device {_hm(want_in)}"
            )

        if len(activities) != len(pairs):
            problems.append(f"{len(activities)} activities, device shows {len(pairs)}")

        return problems

    # ------------------------------------------------------------------ write

    def _rebuild(self, employee, day, pairs, activities, row):
        """Make the day's activities and day row match the device exactly."""
        shift_day = activities[0].shift_day if activities else None
        for activity in activities:
            activity.delete()

        duration = 0
        for punch_in, punch_out in pairs:
            AttendanceActivity.objects.create(
                employee_id=employee,
                attendance_date=day,
                shift_day=shift_day,
                clock_in_date=punch_in.date(),
                clock_in=punch_in.time(),
                in_datetime=punch_in,
                clock_out_date=punch_out.date() if punch_out else None,
                clock_out=punch_out.time() if punch_out else None,
                out_datetime=punch_out,
            )
            if punch_out:
                duration += int((punch_out - punch_in).total_seconds())

        first_in = pairs[0][0] if pairs else None
        last_out = pairs[-1][1] if pairs else None
        row.attendance_clock_in = first_in.time() if first_in else None
        row.attendance_clock_in_date = first_in.date() if first_in else None
        row.attendance_clock_out = last_out.time() if last_out else None
        row.attendance_clock_out_date = last_out.date() if last_out else None
        row.attendance_worked_hour = format_time(duration)
        row.attendance_overtime = overtime_calculation(row)
        row.attendance_validated = attendance_validate(row)
        row.save()

    # ------------------------------------------------------------------- main

    def handle(self, *args, **options):
        since = datetime.strptime(options["since"], "%Y-%m-%d").date()
        until = (
            datetime.strptime(options["until"], "%Y-%m-%d").date()
            if options.get("until")
            else django_timezone.localdate()
        )
        if until < since:
            raise CommandError("--until is before --since")
        apply = options["apply"]

        devices = BiometricDevices.objects.filter(machine_type="anviz", is_active=True)
        if options.get("device_id"):
            devices = devices.filter(id=options["device_id"])
        if not devices.exists():
            raise CommandError("No active Anviz device configured.")

        self.stdout.write(
            self.style.WARNING(
                f"=== {'APPLY' if apply else 'DRY RUN'} === {since} .. {until}"
            )
        )

        findings, repaired, checked = [], 0, 0
        for device in devices:
            punches = self._punches(device, since, until)
            for badge, days in sorted(punches.items()):
                employee = Employee.objects.filter(badge_id=badge).first()
                if employee is None:
                    findings.append(f"  UNKNOWN BADGE  {badge} ({len(days)} days skipped)")
                    continue
                for day in sorted(days):
                    if not since <= day <= until:
                        continue
                    checked += 1
                    pairs, anomalies = self._pairs(days[day])
                    activities, row = self._observed(employee, day)
                    problems = self._diverges(pairs, activities, row)

                    for note in anomalies:
                        findings.append(f"  DEVICE ODD  {badge} {day}: {note}")

                    if not problems:
                        continue
                    if row is None:
                        findings.append(
                            f"  NO DAY ROW  {badge} {day}: needs a clock-in replay, "
                            f"not repaired"
                        )
                        continue

                    if apply:
                        with transaction.atomic():
                            self._rebuild(employee, day, pairs, activities, row)
                        repaired += 1
                    findings.append(
                        f"  {'FIXED' if apply else 'DIFF '}  {badge} {day}: "
                        + "; ".join(problems)
                    )

        self.stdout.write("")
        for line in findings:
            self.stdout.write(line)
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{checked} employee-days checked, "
                f"{len(findings)} findings, {repaired} repaired."
            )
        )
        if findings and not apply:
            self.stdout.write("Re-run with --apply to write these corrections.")


def _hm(value):
    """Render a datetime or time as HH:MM, ignoring seconds."""
    if value is None:
        return "--:--"
    return value.strftime("%H:%M")
