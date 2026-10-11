"""
Employee Attendance & Analytics API

Single-file implementation compliant with openapi.yaml, DATA_MODEL.md, and PROBLEM_STATEMENT.docx.
Starts with: uvicorn app.main:app --port 8000
"""

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Startup & Index Creation (Idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    """Create all required indexes at startup idempotently."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1)])

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)])
    db.attendance_logs.create_index([("date", 1)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def truncate_to_seconds(epoch_ms: int) -> int:
    """R1: Instants are truncated to whole seconds."""
    return (epoch_ms // 1000) * 1000


def ms_to_utc_dt(epoch_ms: int) -> datetime:
    """Convert epoch milliseconds to timezone-aware UTC datetime."""
    truncated = truncate_to_seconds(epoch_ms)
    return datetime.fromtimestamp(truncated / 1000, tz=UTC)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds integer (or None)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def round_half_up(value: float | Decimal, decimals: int) -> float:
    """R8: Rounding half-up (not banker's rounding)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Overnight shifts: shift_end <= shift_start (R1)."""
    return shift_end <= shift_start


def get_attendance_date(punch_in_ms: int, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST.
    Overnight shifts: a punch-in whose IST time is strictly earlier than
    shift_end belongs to the shift that started the previous day.
    """
    punch_dt_ist = ms_to_utc_dt(punch_in_ms).astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))
        shift_end_t = time(end_h, end_m, 0)
        if punch_dt_ist.time() < shift_end_t:
            return (punch_dt_ist.date() - timedelta(days=1)).isoformat()
    return punch_dt_ist.date().isoformat()


def compute_late_minutes(punch_in_ms: int, shift_start: str, attendance_date: str) -> int:
    """
    R2: Grace period 10 minutes (600s).
    Strictly more than 10:00 after shift_start -> floor minutes since shift_start.
    Otherwise 0.
    """
    punch_dt_ist = ms_to_utc_dt(punch_in_ms).astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    start_dt_ist = datetime(y, m, d, sh, sm, 0, tzinfo=IST)
    diff_sec = (punch_dt_ist - start_dt_ist).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_ms: int, punch_out_ms: int) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    sec = (truncate_to_seconds(punch_out_ms) - truncate_to_seconds(punch_in_ms)) / 1000
    hours = Decimal(sec) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_ms: int, shift_start: str, shift_end: str, attendance_date: str
) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on next calendar day.
    """
    punch_dt_ist = ms_to_utc_dt(punch_out_ms).astimezone(IST)
    eh, em = map(int, shift_end.split(":"))
    y, m, d = map(int, attendance_date.split("-"))
    base_date = dt_date(y, m, d)
    if is_overnight_shift(shift_start, shift_end):
        end_date = base_date + timedelta(days=1)
    else:
        end_date = base_date

    end_dt_ist = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)
    diff_sec = (punch_dt_ist - end_dt_ist).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: true when rounded work_hours < 4.50."""
    if work_hours is not None and work_hours < 4.50:
        return True
    return False


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB attendance log document for API response matching AttendanceRecord."""
    history_out = []
    for h in doc.get("history", []):
        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_ms = dt_to_epoch_ms(at_val)
        else:
            at_ms = at_val

        changes_out = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                from_v = dt_to_epoch_ms(from_v) if isinstance(from_v, datetime) else from_v
                to_v = dt_to_epoch_ms(to_v) if isinstance(to_v, datetime) else to_v
            changes_out[k] = {"from": from_v, "to": to_v}

        history_out.append({
            "at": at_ms,
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format MongoDB employee document for API response matching Employee."""
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #
StatusType = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatusType = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(description="YYYY-MM-DD")

    @model_validator(mode="after")
    def validate_shift_and_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            dt_date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: PresenceStatusType = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[StatusType] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB is unreachable",
        )


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=status.HTTP_201_CREATED, tags=["employees"])
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(tz=UTC)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{body.emp_code}' already exists",
        )

    return format_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None, description="Exact match"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    punch_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_ms = truncate_to_seconds(punch_ms)
    punch_dt = ms_to_utc_dt(punch_ms)

    att_date = get_attendance_date(punch_ms, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_ms, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already punched in for date '{att_date}'",
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found",
        )

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    out_ms = body.punched_at if body.punched_at is not None else now_ms
    out_ms = truncate_to_seconds(out_ms)
    out_dt = ms_to_utc_dt(out_ms)

    # Most recent record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": out_dt}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No matching punch-in found for this punch-out",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That record is already punched out",
        )

    in_ms = dt_to_epoch_ms(record["punch_in"])
    if out_ms <= in_ms:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at must be strictly after punch_in",
        )
    if (out_ms - in_ms) > 24 * 3600 * 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punched_at cannot be more than 24 hours after punch_in",
        )

    work_hrs = compute_work_hours(in_ms, out_ms)
    ot_min = compute_overtime(out_ms, emp["shift_start"], emp["shift_end"], record["date"])
    half_d = compute_half_day(work_hrs)

    # Concurrency safe: only one simultaneous punch-out succeeds
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=True,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That record is already punched out",
        )

    return format_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to:
        try:
            d_from = dt_date.fromisoformat(date_from)
            d_to = dt_date.fromisoformat(date_to)
            if d_from > d_to:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="date_from must be <= date_to",
                )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid date format",
            )

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        date_q: Dict[str, str] = {}
        if date_from:
            date_q["$gte"] = date_from
        if date_to:
            date_q["$lte"] = date_to
        q["date"] = date_q
    if status_filter:
        q["status"] = status_filter

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Attendance record for employee '{emp_code}' and date '{date}' not found",
        )

    # Validate rules for status vs punch times
    new_status = body.status if body.status is not None else record["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Setting status '{new_status}' cannot supply punch_in or punch_out",
            )
        target_in_ms = None
        target_out_ms = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_in_ms = truncate_to_seconds(body.punch_in)
        else:
            target_in_ms = dt_to_epoch_ms(record.get("punch_in"))

        if target_in_ms is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Presence status requires a punch_in",
            )

        # R1: punch_in must stay on the record's attendance date
        computed_date = get_attendance_date(target_in_ms, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date '{computed_date}', not '{date}'",
            )

        if body.punch_out is not None:
            target_out_ms = truncate_to_seconds(body.punch_out)
        else:
            target_out_ms = dt_to_epoch_ms(record.get("punch_out"))

        target_late_minutes = compute_late_minutes(target_in_ms, emp["shift_start"], date)

        if target_out_ms is not None:
            if target_out_ms <= target_in_ms:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in",
                )
            if (target_out_ms - target_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            target_work_hours = compute_work_hours(target_in_ms, target_out_ms)
            target_overtime_minutes = compute_overtime(
                target_out_ms, emp["shift_start"], emp["shift_end"], date
            )
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

    # Compute changes diff
    old_in_ms = dt_to_epoch_ms(record.get("punch_in"))
    old_out_ms = dt_to_epoch_ms(record.get("punch_out"))
    old_status = record.get("status")
    old_work_hours = record.get("work_hours")
    old_late_minutes = record.get("late_minutes", 0)
    old_overtime_minutes = record.get("overtime_minutes", 0)
    old_half_day = record.get("half_day", False)

    changes_mongo: Dict[str, Any] = {}
    if new_status != old_status:
        changes_mongo["status"] = {"from": old_status, "to": new_status}
    if target_in_ms != old_in_ms:
        changes_mongo["punch_in"] = {
            "from": record.get("punch_in"),
            "to": ms_to_utc_dt(target_in_ms) if target_in_ms is not None else None,
        }
    if target_out_ms != old_out_ms:
        changes_mongo["punch_out"] = {
            "from": record.get("punch_out"),
            "to": ms_to_utc_dt(target_out_ms) if target_out_ms is not None else None,
        }
    if target_work_hours != old_work_hours:
        changes_mongo["work_hours"] = {"from": old_work_hours, "to": target_work_hours}
    if target_late_minutes != old_late_minutes:
        changes_mongo["late_minutes"] = {"from": old_late_minutes, "to": target_late_minutes}
    if target_overtime_minutes != old_overtime_minutes:
        changes_mongo["overtime_minutes"] = {
            "from": old_overtime_minutes,
            "to": target_overtime_minutes,
        }
    if target_half_day != old_half_day:
        changes_mongo["half_day"] = {"from": old_half_day, "to": target_half_day}

    if not changes_mongo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request changes nothing",
        )

    history_entry = {
        "at": datetime.now(tz=UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_mongo,
    }

    current_history_len = len(record.get("history", []))
    update_doc = {
        "status": new_status,
        "punch_in": ms_to_utc_dt(target_in_ms) if target_in_ms is not None else None,
        "punch_out": ms_to_utc_dt(target_out_ms) if target_out_ms is not None else None,
        "work_hours": target_work_hours,
        "late_minutes": target_late_minutes,
        "overtime_minutes": target_overtime_minutes,
        "half_day": target_half_day,
    }

    # Optimistic concurrency: ensure history has not been appended concurrently
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "history": {"$size": current_history_len},
        },
        {
            "$set": update_doc,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Concurrent modification detected on this attendance record",
        )

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> tuple[str, str, int]:
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    return f"{month_str}-01", f"{month_str}-{last_day:02d}", last_day


def count_working_days(month_str: str, joined_on: str) -> int:
    """R7: Monday-Friday calendar days, counted from joined_on for mid-month joiners."""
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    working_days = 0
    for day in range(1, last_day + 1):
        d_str = f"{month_str}-{day:02d}"
        if d_str >= joined_on:
            weekday = dt_date(year, m, day).weekday()
            if weekday < 5:  # Monday to Friday
                working_days += 1
    return working_days


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found",
        )

    working_days = count_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = round_half_up(row.get("present_days", 0.0), 2)
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month: str, department: Optional[str] = None
) -> list[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "work_hours_list": {
                    "$filter": {
                        "input": "$flat_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$flat_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list.work_hours"},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = round_half_up(avg_wh, 2)
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r.get("present_days", 0.0), 2),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str, limit: int = 10, department: Optional[str] = None
) -> list[Dict[str, Any]]:
    start_date, end_date, _ = get_month_range(month)
    pipeline: list[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        items.append({
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str, date_from: str, date_to: str
) -> list[Dict[str, Any]]:
    dt_from = datetime.fromisoformat(date_from).replace(tzinfo=UTC)
    dt_to_next = datetime.fromisoformat(date_to).replace(tzinfo=UTC) + timedelta(days=1)

    return [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$project": {
                "date": 1,
                "date_dt": {"$dateFromString": {"dateString": "$date"}},
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
            }
        },
        {
            "$group": {
                "_id": "$date_dt",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        {"$project": {"date_dt": "$_id", "present_count": 1, "late_count": 1, "_id": 0}},
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_next],
                },
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "emp_count",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$emp_count.count", 0]}, 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' not found",
        )

    try:
        d_from = dt_date.fromisoformat(from_date)
        d_to = dt_date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid date format",
        )

    if d_to < d_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' must be >= 'from'",
        )

    if (d_to - d_from).days > 92:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Range cannot exceed 92 days",
        )

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in results:
        rate = r.get("attendance_rate")
        if rate is not None:
            rate = round_half_up(rate, 4)

        m_avg = r.get("moving_avg_7d")
        if m_avg is not None:
            m_avg = round_half_up(m_avg, 4)

        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r.get("present_count", 0.0), 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": rate,
            "moving_avg_7d": m_avg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[StatusType] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to:
            try:
                if dt_date.fromisoformat(date_from) > dt_date.fromisoformat(date_to):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="date_from must be <= date_to",
                    )
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid date format",
                )

        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            date_q: Dict[str, str] = {}
            if date_from:
                date_q["$gte"] = date_from
            if date_to:
                date_q["$lte"] = date_to
            q["date"] = date_q
        if status_filter:
            q["status"] = status_filter

        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code is required for employee_monthly explain",
            )
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for employee_monthly explain",
            )

        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for department_summary explain",
            )

        pipeline = build_department_summary_pipeline(month, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_doc,
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for late_leaderboard explain",
            )

        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    elif endpoint == "department_trend":
        target_dept = department
        target_from = from_date
        target_to = to_date

        if not target_dept or not target_from or not target_to:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department, from, and to are required for department_trend explain",
            )

        try:
            d_from = dt_date.fromisoformat(target_from)
            d_to = dt_date.fromisoformat(target_to)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid date format",
            )

        if d_to < d_from:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'to' must be >= 'from'",
            )
        if (d_to - d_from).days > 92:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Range cannot exceed 92 days",
            )

        pipeline = build_department_trend_pipeline(target_dept, target_from, target_to)
        explain_doc = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_doc,
        }

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Unknown explain endpoint '{endpoint}'",
    )
