"""Bring MBR's Horilla roster in line with the CrossChex device enrolment.

Four people were enrolled on the FaceDeep 3 and punching daily but carried no
shift in Horilla, so `clock_in` discarded every punch — roughly 98 employee-days
each. Their job titles come from the device's own records (the attendance
payload carries `job_title`), and the shifts below come from their observed
punch patterns, confirmed by the owner on 2026-09-21.

Creates only what is missing and never edits an employee who already has a
shift, so it is safe to re-run. Read-only unless --apply.

    python manage.py seed_mbr_roster
    python manage.py seed_mbr_roster --apply
"""

from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from base.models import (
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    JobPosition,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation

WORKING_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]

# name -> (start, end, minimum_working_hour)
SHIFTS = {
    "8:30 AM - 5:30 PM": (time(8, 30), time(17, 30), "09:00"),
    "7:00 AM - 9:00 PM": (time(7, 0), time(21, 0), "14:00"),
}

# badge -> department, job position, work type, shift
ASSIGNMENTS = {
    "113": {
        "department": "Accounts",
        "position": "Accountant",
        "work_type": "Finance & Accounting",
        "shift": "8:30 AM - 5:30 PM",
        "note": "Accountant; observed 08:21-17:36",
    },
    "114": {
        "department": "Workshop",
        "position": "Senior Mechanic",
        "work_type": "Mechanical Repair",
        "shift": "8:30 AM - 7:30 PM",
        "note": "Senior Mechanic; observed 08:23-19:48, same as the other mechanics",
    },
    "116": {
        "department": "Workshop",
        "position": "Tire & Wheel Alignment Technician",
        "work_type": "Mechanical Repair",
        "shift": "8:30 AM - 7:30 PM",
        "note": "Tire & Wheel Alignment Technician; observed 08:26-19:43",
    },
    "117": {
        "department": "Security",
        "position": "Security",
        "work_type": "Security",
        "shift": "7:00 AM - 9:00 PM",
        "note": "Security; observed 07:25-21:15. minimum_working_hour 14:00 means "
        "no daily overtime accrues — change it if this is a 12h shift plus overtime",
    },
}


class Command(BaseCommand):
    help = "Create MBR's missing shifts, positions and work info (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Write. Otherwise report only."
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        self.stdout.write(
            self.style.WARNING(f"=== {'APPLY' if apply else 'DRY RUN'} ===")
        )
        actions = []

        with transaction.atomic():
            for name, (start, end, minimum) in SHIFTS.items():
                actions += self._ensure_shift(name, start, end, minimum, apply)
            for badge, spec in sorted(ASSIGNMENTS.items()):
                actions += self._ensure_assignment(badge, spec, apply)
            if not apply:
                transaction.set_rollback(True)

        for line in actions:
            self.stdout.write(line)
        self.stdout.write("")
        changes = [a for a in actions if not a.startswith("  ok ")]
        self.stdout.write(
            self.style.SUCCESS(f"{len(changes)} change(s) {'applied' if apply else 'pending'}.")
        )
        if changes and not apply:
            self.stdout.write("Re-run with --apply to write them.")

    # ------------------------------------------------------------------ parts

    def _ensure_shift(self, name, start, end, minimum, apply):
        out = []
        shift = EmployeeShift.objects.filter(employee_shift=name).first()
        if shift is None:
            out.append(f"  CREATE shift  {name}  ({start:%H:%M}-{end:%H:%M}, min {minimum})")
            if apply:
                shift = EmployeeShift.objects.create(
                    employee_shift=name, weekly_full_time="40:00", full_time="200:00"
                )
        else:
            out.append(f"  ok    shift  {name} exists")

        if shift is None:
            return out

        for day_name in WORKING_DAYS:
            day = EmployeeShiftDay.objects.filter(day=day_name).first()
            if day is None:
                out.append(f"  WARN  no shift day '{day_name}'")
                continue
            existing = EmployeeShiftSchedule.objects.filter(
                shift_id=shift, day=day
            ).first()
            if existing is None:
                out.append(f"  CREATE schedule {name} {day_name}")
                if apply:
                    EmployeeShiftSchedule.objects.create(
                        shift_id=shift,
                        day=day,
                        start_time=start,
                        end_time=end,
                        minimum_working_hour=minimum,
                        is_night_shift=False,
                    )
        return out

    def _ensure_assignment(self, badge, spec, apply):
        out = []
        employee = Employee.objects.filter(badge_id=badge, is_active=True).first()
        if employee is None:
            out.append(f"  SKIP  badge {badge}: no active employee")
            return out

        department = Department.objects.filter(
            department__iexact=spec["department"].strip()
        ).first()
        if department is None:
            department = Department.objects.filter(
                department__istartswith=spec["department"].strip()
            ).first()
        if department is None:
            out.append(f"  CREATE department {spec['department']}")
            if apply:
                department = Department.objects.create(department=spec["department"])

        position = JobPosition.objects.filter(
            job_position__iexact=spec["position"]
        ).first()
        if position is None:
            out.append(f"  CREATE position   {spec['position']}")
            if apply and department is not None:
                position = JobPosition.objects.create(
                    job_position=spec["position"], department_id=department
                )

        work_type = WorkType.objects.filter(work_type__iexact=spec["work_type"]).first()
        if work_type is None:
            out.append(f"  CREATE work type  {spec['work_type']}")
            if apply:
                work_type = WorkType.objects.create(work_type=spec["work_type"])

        shift = EmployeeShift.objects.filter(employee_shift=spec["shift"]).first()

        info = EmployeeWorkInformation.objects.filter(employee_id=employee).first()
        if info is None:
            out.append(f"  CREATE work info  {badge} {employee}")
            if apply:
                info = EmployeeWorkInformation.objects.create(employee_id=employee)

        if info is None:
            return out

        if info.shift_id is not None:
            out.append(f"  ok    {badge} {employee}: already has shift {info.shift_id}")
            return out

        out.append(
            f"  ASSIGN {badge} {employee}: shift={spec['shift']} "
            f"position={spec['position']} dept={spec['department']} "
            f"work_type={spec['work_type']}"
        )
        out.append(f"         reason: {spec['note']}")
        if apply:
            info.shift_id = shift
            info.job_position_id = position
            info.department_id = department
            info.work_type_id = work_type
            info.save()
        return out
