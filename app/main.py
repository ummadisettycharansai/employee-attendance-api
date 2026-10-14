"""Employee Attendance & Analytics API."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUS = {"PRESENT", "WFH", "ON_DUTY"}
MAX_EPOCH_MS = 4102444800000
MIN_EPOCH_MS = 100000000000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
logger = logging.getLogger(__name__)
indexes_ready = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def round_half_up(value: float | Decimal, digits: int) -> float:
    q = Decimal(str(value)).quantize(Decimal("0.01" if digits == 2 else "0.0001"), rounding=ROUND_HALF_UP)
    return float(q)


def to_utc_datetime(epoch_ms: int) -> datetime:
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "timestamp must be an integer epoch in milliseconds")
    if epoch_ms < MIN_EPOCH_MS or epoch_ms > MAX_EPOCH_MS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "timestamp is out of range")
    truncated_ms = (epoch_ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def utc_now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    utc_value = value.astimezone(timezone.utc).replace(microsecond=0)
    return int(utc_value.timestamp() * 1000)


def start_of_month(month: str) -> date:
    try:
        return date.fromisoformat(month + "-01")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month must be a valid YYYY-MM value") from exc


def parse_calendar_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{field_name} must be a valid YYYY-MM-DD date",
        ) from exc
    if parsed.isoformat() != value:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{field_name} must be a valid YYYY-MM-DD date",
        )
    return parsed


def last_day_of_month(month: str) -> date:
    year, mon = map(int, month.split("-"))
    first = date(year, mon, 1)
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)


def iter_month_days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def working_days_in_month(joined_on: date, month_start: date, month_end: date) -> int:
    count = 0
    for day in iter_month_days(max(joined_on, month_start), month_end):
        if day.isoweekday() <= 5:
            count += 1
    return count


def iso_date(dt_utc: datetime) -> str:
    return dt_utc.astimezone(IST).strftime("%Y-%m-%d")


def attendance_date_for_punch(emp: dict[str, Any], punch_in_utc: datetime) -> str:
    punch_ist = punch_in_utc.astimezone(IST)
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]
    if shift_end <= shift_start:
        if punch_ist.strftime("%H:%M") < shift_end:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
        return punch_ist.date().isoformat()
    return punch_ist.date().isoformat()


def shift_start_datetime(emp: dict[str, Any], date_value: str) -> datetime:
    day = date.fromisoformat(date_value)
    hour, minute = map(int, emp["shift_start"].split(":"))
    if emp["shift_end"] <= emp["shift_start"]:
        if date_value == day.isoformat():
            return datetime.combine(day, time(hour, minute), tzinfo=IST)
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str | None = None) -> int:
    punch_ist = punch_in_utc.astimezone(IST)
    if shift_end is not None and shift_end <= shift_start:
        ref_day = punch_ist.date() if punch_ist.strftime("%H:%M") >= shift_end else punch_ist.date() - timedelta(days=1)
    else:
        ref_day = punch_ist.date()
    start_hour, start_minute = map(int, shift_start.split(":"))
    start = datetime.combine(ref_day, time(start_hour, start_minute), tzinfo=IST)
    delta_seconds = (punch_ist - start).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(delta_seconds // 60)


def compute_overtime(punch_out_utc: datetime, shift_end: str, record_date: str, shift_start: str) -> int:
    end_day = date.fromisoformat(record_date)
    if shift_end <= shift_start:
        end_day += timedelta(days=1)
    end_hour, end_minute = map(int, shift_end.split(":"))
    end_dt = datetime.combine(end_day, time(end_hour, end_minute), tzinfo=IST)
    delta_seconds = int((punch_out_utc.astimezone(IST) - end_dt).total_seconds())
    if delta_seconds < 1800:
        return 0
    return delta_seconds // 60


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    duration_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    duration_hours = Decimal(duration_seconds) / Decimal(3600)
    return float(round_half_up(duration_hours, 2))


def rederive_record(status: str, punch_in_utc: Optional[datetime], punch_out_utc: Optional[datetime], emp: dict[str, Any], record_date: str) -> dict[str, Any]:
    late = 0
    work_hours: Optional[float] = None
    overtime = 0
    half_day = False

    if status in PRESENCE_STATUS and punch_in_utc is not None:
        late = compute_late_minutes(punch_in_utc, emp["shift_start"], emp["shift_end"])

    if punch_in_utc is not None and punch_out_utc is not None:
        work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
        overtime = compute_overtime(punch_out_utc, emp["shift_end"], record_date, emp["shift_start"])
        half_day = work_hours < 4.5

    if status not in PRESENCE_STATUS:
        late = 0
        work_hours = None
        overtime = 0
        half_day = False

    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_ms = to_epoch_ms(created_at)
    else:
        created_ms = created_at
    joined_on = doc.get("joined_on")
    if isinstance(joined_on, date):
        joined_on = joined_on.isoformat()
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": joined_on,
        "created_at": created_ms,
    }


def serialize_history_value(field: str, value: Any) -> Any:
    if field in {"punch_in", "punch_out"} and isinstance(value, datetime):
        return to_epoch_ms(value)
    return value


def serialize_history(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, payload in (entry.get("changes", {}) or {}).items():
        old_val = serialize_history_value(key, payload.get("from"))
        new_val = serialize_history_value(key, payload.get("to"))
        changes[key] = {"from": old_val, "to": new_val}
    return {
        "at": to_epoch_ms(entry["at"]) if isinstance(entry.get("at"), datetime) else entry.get("at"),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def serialize_record(doc: dict[str, Any]) -> dict[str, Any]:
    history = doc.get("history", []) or []
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc["punch_in"]) if doc.get("punch_in") is not None else None,
        "punch_out": to_epoch_ms(doc["punch_out"]) if doc.get("punch_out") is not None else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history(item) for item in history],
    }


def fetch_employee_or_404(emp_code: str) -> dict[str, Any]:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "employee not found")
    return emp


def fetch_attendance_or_404(emp_code: str, date_str: str) -> dict[str, Any]:
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attendance record not found")
    return doc


def ensure_attendance_date_within_shift(emp: dict[str, Any], punch_in: Optional[datetime], record_date: str) -> None:
    if punch_in is None:
        return
    if attendance_date_for_punch(emp, punch_in) != record_date:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_in must fall on the attendance date")


def validate_epoch(value: Optional[int], field_name: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return to_utc_datetime(int(value))
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field_name} must be epoch milliseconds") from exc


def ensure_indexes() -> None:
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: date

    @model_validator(mode="after")
    def validate_shift_boundaries(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[StrictInt] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field_name in ("status", "punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def startup() -> None:
    global indexes_ready
    indexes_ready = False
    try:
        ensure_indexes()
    except PyMongoError:
        logger.exception("MongoDB indexes could not be created during startup")
    else:
        indexes_ready = True


@app.get("/health")
def health() -> dict[str, str]:
    global indexes_ready
    try:
        db.command("ping")
        if not indexes_ready:
            ensure_indexes()
            indexes_ready = True
    except PyMongoError as exc:  # pragma: no cover - depends on running MongoDB
        logger.exception("MongoDB readiness check failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(status.HTTP_409_CONFLICT, "emp_code already exists")
    payload = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on.isoformat(),
        "created_at": utc_now_seconds(),
    }
    try:
        db.employees.insert_one(payload)
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "emp_code already exists") from exc
    return serialize_employee(payload)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    docs = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    emp = fetch_employee_or_404(body.emp_code)
    punched_at = utc_now_seconds() if body.punched_at is None else to_utc_datetime(int(body.punched_at))
    record_date = attendance_date_for_punch(emp, punched_at)
    existing = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": record_date})
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "already punched in for this date") from exc
    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    emp = fetch_employee_or_404(body.emp_code)
    punched_at = utc_now_seconds() if body.punched_at is None else to_utc_datetime(int(body.punched_at))
    record = db.attendance_logs.find_one(
        {"$and": [{"emp_code": body.emp_code}, {"punch_in": {"$ne": None}}, {"punch_in": {"$lte": punched_at}}]},
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that record is already punched out")
    if punched_at <= record["punch_in"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be after punch_in")
    if (punched_at - record["punch_in"]).total_seconds() > 86400:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out cannot be more than 24 hours after punch_in")

    derived = rederive_record(record["status"], record["punch_in"], punched_at, emp, record["date"])
    update_result = db.attendance_logs.update_one(
        {"emp_code": body.emp_code, "date": record["date"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            }
        },
    )
    if update_result.matched_count == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "that record is already punched out")
    updated = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": record["date"]})
    return serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attendance_status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    parsed_from = parse_calendar_date(date_from, "date_from") if date_from is not None else None
    parsed_to = parse_calendar_date(date_to, "date_to") if date_to is not None else None
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from must be <= date_to")

    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if attendance_status:
        query["status"] = attendance_status
    if parsed_from is not None or parsed_to is not None:
        query["date"] = {}
        if parsed_from:
            query["date"]["$gte"] = parsed_from.isoformat()
        if parsed_to:
            query["date"]["$lte"] = parsed_to.isoformat()

    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_record(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
) -> dict[str, Any]:
    parse_calendar_date(date, "date")
    emp = fetch_employee_or_404(emp_code)
    record = fetch_attendance_or_404(emp_code, date)

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "request changes nothing")

    new_status = record.get("status") if body.status is None else body.status

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ABSENT and LEAVE records cannot include punch times")
        final_punch_in = None
        final_punch_out = None
    else:
        new_punch_in = record.get("punch_in") if body.punch_in is None else to_utc_datetime(body.punch_in)
        new_punch_out = record.get("punch_out") if body.punch_out is None else to_utc_datetime(body.punch_out)
        if new_punch_in is None and record.get("punch_in") is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "presence record requires a punch_in")
        final_punch_in = new_punch_in if new_punch_in is not None else record.get("punch_in")
        final_punch_out = new_punch_out if new_punch_out is not None else record.get("punch_out")
        if final_punch_in is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "presence record requires a punch_in")
        ensure_attendance_date_within_shift(emp, final_punch_in, date)
        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "punch_out must be within 24 hours of punch_in")

    derived = rederive_record(new_status, final_punch_in, final_punch_out, emp, date)
    changes = {}
    current_status = record.get("status")
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")
    current_work = record.get("work_hours")
    current_late = record.get("late_minutes", 0)
    current_overtime = record.get("overtime_minutes", 0)
    current_half_day = bool(record.get("half_day", False))

    for field_name, old_value, new_value in [
        ("status", current_status, new_status),
        ("punch_in", current_punch_in, final_punch_in),
        ("punch_out", current_punch_out, final_punch_out),
        ("work_hours", current_work, derived["work_hours"]),
        ("late_minutes", current_late, derived["late_minutes"]),
        ("overtime_minutes", current_overtime, derived["overtime_minutes"]),
        ("half_day", current_half_day, derived["half_day"]),
    ]:
        if old_value != new_value:
            changes[field_name] = {
                "from": serialize_history_value(field_name, old_value),
                "to": serialize_history_value(field_name, new_value),
            }

    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "request changes nothing")

    history_entry = {
        "at": utc_now_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_filter = {"emp_code": emp_code, "date": date}
    if "history" in record:
        update_filter["history"] = record["history"]
    else:
        update_filter["history"] = {"$exists": False}
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": final_punch_in,
            "punch_out": final_punch_out,
            "work_hours": derived["work_hours"],
            "late_minutes": derived["late_minutes"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        },
        "$push": {"history": history_entry},
    }
    result = db.attendance_logs.update_one(update_filter, update_doc)
    if result.matched_count == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "record was modified concurrently")

    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    return serialize_record(updated)


def build_employee_monthly_pipeline(emp_code: str, month_start: date, month_end: date) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
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
                                            {"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}, "timezone": "Asia/Kolkata"}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> dict[str, Any]:
    emp = fetch_employee_or_404(emp_code)
    month_start = start_of_month(month)
    month_end = last_day_of_month(month)
    joined_on = date.fromisoformat(emp["joined_on"])
    working_days = working_days_in_month(joined_on, month_start, month_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    result = list(db.attendance_logs.aggregate(pipeline))
    row = result[0] if result else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}

    pct = None
    if working_days:
        pct = round_half_up(Decimal(str(row["present_days"])) * 100 / working_days, 4)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(row["present_days"]),
        "leave_days": int(row["leave_days"]),
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "total_overtime_minutes": int(row["total_overtime_minutes"]),
        "attendance_pct": pct,
    }


def build_department_summary_pipeline(
    month_start: date,
    month_end: date,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
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
                                                        {"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}, "timezone": "Asia/Kolkata"}},
                                                        [2, 3, 4, 5, 6],
                                                    ]
                                                },
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly_logs",
            }
        },
        {
            "$set": {
                "present_days": {"$ifNull": [{"$first": "$monthly_logs.present_days"}, 0]},
                "leave_count": {"$ifNull": [{"$first": "$monthly_logs.leave_count"}, 0]},
                "on_duty_count": {"$ifNull": [{"$first": "$monthly_logs.on_duty_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$monthly_logs.late_count"}, 0]},
                "total_late_minutes": {"$ifNull": [{"$first": "$monthly_logs.total_late_minutes"}, 0]},
                "total_work_hours": {"$ifNull": [{"$first": "$monthly_logs.total_work_hours"}, 0]},
                "work_hours_count": {"$ifNull": [{"$first": "$monthly_logs.work_hours_count"}, 0]},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$present_days"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "total_work_hours": {"$sum": "$total_work_hours"},
                "work_hours_count": {"$sum": "$work_hours_count"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    month_start = start_of_month(month)
    month_end = last_day_of_month(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    rows = list(db.employees.aggregate(pipeline))
    items = []
    for row in rows:
        average = None
        if row["work_hours_count"]:
            average = round_half_up(
                Decimal(str(row["total_work_hours"])) / row["work_hours_count"],
                2,
            )
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": float(row["present_days"]),
            "avg_work_hours": average,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month_start: date,
    month_end: date,
    limit: int,
    department: Optional[str] = None,
) -> list[dict[str, Any]]:
    return [
        {"$match": {"date": {"$gte": month_start.isoformat(), "$lte": month_end.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department} if department is not None else {"employee": {"$exists": True}}},
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
                "rank": 1,
            }
        },
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    month_start = start_of_month(month)
    month_end = last_day_of_month(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    rows = list(db.attendance_logs.aggregate(pipeline))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["_id"],
            "name": r.get("name") or "",
            "department": r.get("department"),
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {
            "$documents": [
                {
                    "range_start": datetime.combine(start, time.min, tzinfo=IST),
                    "range_end": datetime.combine(end, time.min, tzinfo=IST),
                }
            ]
        },
        {
            "$set": {
                "days": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {"$dateDiff": {"startDate": "$range_start", "endDate": "$range_end", "unit": "day"}},
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {"$dateAdd": {"startDate": "$range_start", "unit": "day", "amount": "$$offset"}},
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": {"date": "$days", "timezone": "Asia/Kolkata"}}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day"]}]}}},
                    {"$group": {"_id": None, "headcount": {"$sum": 1}}},
                ],
                "as": "headcount_summary",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "daily_logs",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$headcount_summary.headcount"}, 0]},
                "present_count": {"$ifNull": [{"$first": "$daily_logs.present_count"}, 0]},
                "late_count": {"$ifNull": [{"$first": "$daily_logs.late_count"}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}},
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_unrounded": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$moving_avg_unrounded", None]},
                        None,
                        {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_unrounded", 10000]}, 0.5]}}, 10000]},
                    ]
                }
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
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    start = parse_calendar_date(from_date, "from")
    end = parse_calendar_date(to_date, "to")
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "to must be >= from")
    if (end - start).days > 92:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")

    pipeline = build_department_trend_pipeline(department, start, end)
    rows = list(db.aggregate(pipeline))
    return {
        "department": department,
        "items": [
            {
                "date": row["date"],
                "is_working_day": bool(row["is_working_day"]),
                "headcount": int(row["headcount"]),
                "present_count": float(row["present_count"]),
                "late_count": int(row["late_count"]),
                "attendance_rate": row["attendance_rate"],
                "moving_avg_7d": row["moving_avg_7d"],
            }
            for row in rows
        ],
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(None, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    attendance_status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unknown endpoint")

    if endpoint == "attendance_list":
        parsed_from = parse_calendar_date(date_from, "date_from") if date_from is not None else None
        parsed_to = parse_calendar_date(date_to, "date_to") if date_to is not None else None
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from must be <= date_to")
        query = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if attendance_status:
            query["status"] = attendance_status
        if parsed_from is not None or parsed_to is not None:
            query["date"] = {}
            if parsed_from:
                query["date"]["$gte"] = parsed_from.isoformat()
            if parsed_to:
                query["date"]["$lte"] = parsed_to.isoformat()
        collection = "attendance_logs"
        explain_target = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "emp_code and month are required")
        month_start = start_of_month(month)
        month_end = last_day_of_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
        collection = "attendance_logs"
        explain_target = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month is required")
        month_start = start_of_month(month)
        month_end = last_day_of_month(month)
        pipeline = build_department_summary_pipeline(month_start, month_end, department)
        collection = "employees"
        explain_target = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "month is required")
        month_start = start_of_month(month)
        month_end = last_day_of_month(month)
        pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit or 10, department)
        collection = "attendance_logs"
        explain_target = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "department, from and to are required")
        parsed_from = parse_calendar_date(from_date, "from")
        parsed_to = parse_calendar_date(to_date, "to")
        if parsed_to < parsed_from:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "to must be >= from")
        if (parsed_to - parsed_from).days > 92:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date range cannot exceed 92 days")
        pipeline = build_department_trend_pipeline(department, parsed_from, parsed_to)
        collection = "database"
        explain_target = {"aggregate": 1, "pipeline": pipeline, "cursor": {}}

    explain = db.command({"explain": explain_target, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


# --------------------------------------------------------------------------- #
# Startup guard
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
