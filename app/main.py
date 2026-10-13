
"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date,datetime, timedelta, timezone
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel,model_validator,StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import calendar
import json
from bson.json_util import dumps, CANONICAL_JSON_OPTIONS
load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        "emp_code",
        unique=True,

    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="unique_emp_code_date",
    )

    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="attendance_date_emp_idx"
    )
    db.employees.create_index(
        [("joined_on", 1)],
        name="employees_joined_on_idx"
    )
    db.employees.create_index(
        [("department", 1)],
        name="employees_department_idx",
    )

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0



def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))




def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    start_hour, start_minute = map(int, shift_start.split(":"))
    end_hour, end_minute = map(int, shift_end.split(":"))

    shift_start_dt = datetime.fromisoformat(date_str).replace(
        hour=start_hour,
        minute=start_minute,
        tzinfo=IST,
    )

    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=end_hour,
        minute=end_minute,
        tzinfo=IST,
    )

    # Handle overnight shifts.
    if shift_end <= shift_start:
        shift_end_dt += timedelta(days=1)

    # Calculate completed overtime minutes.
    overtime_minutes = int(
        (punch_out - shift_end_dt).total_seconds() // 60
    )

    # Overtime is counted only when it reaches 30 minutes.
    if overtime_minutes >= 30:
        return overtime_minutes

    return 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class AttendancePatchIn(BaseModel):
    reason: str
    regularized_by: str
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None

    @model_validator(mode="after")
    def validate_patch(self):
        if not 5 <= len(self.reason.strip()) <= 200:
            raise ValueError("reason must be between 5 and 200 characters")

        if not 1 <= len(self.regularized_by.strip()) <= 50:
            raise ValueError("regularized_by must be between 1 and 50 characters")

        if self.status is not None and self.status not in {
            "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
        }:
            raise ValueError("Invalid attendance status")

        if (
            self.status is None
            and self.punch_in is None
            and self.punch_out is None
        ):
            raise ValueError("Provide at least one field to update")

        return self


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page-1)* page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code",1).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Check whether the employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Validate attendance status
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY"}
    if body.status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")

    # 3. Get the timestamp in UTC and truncate to whole seconds
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000, tz=timezone.utc
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)

    # 4. Convert to Indian Standard Time
    local_ts = ts.astimezone(IST)

    # 5. Determine the attendance date, including overnight shifts
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    if shift_end <= shift_start:
        end_hour, end_minute = map(int, shift_end.split(":"))
        shift_end_time = local_ts.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )

        if local_ts < shift_end_time:
            attendance_date = (local_ts.date() - timedelta(days=1)).isoformat()
        else:
            attendance_date = local_ts.date().isoformat()
    else:
        attendance_date = local_ts.date().isoformat()

    # 6. Calculate lateness using the local attendance time
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(attendance_date).replace(
        hour=h, minute=m, tzinfo=IST
    )
    late_minutes = int((local_ts - shift_start_dt).total_seconds() // 60)
    late_minutes = late_minutes if late_minutes > 10 else 0

    # 7. Create the attendance record
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 8. The unique index prevents duplicate records, including concurrent requests
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this attendance date",
        )

    # 9. Return API timestamps as epoch milliseconds; never expose MongoDB _id
    doc.pop("_id", None)
    doc["punch_in"] = int(ts.timestamp() * 1000)
    return doc





@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from cannot be later than date_to",
        )

    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1",
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100",
        )

    allowed_statuses = {
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    }

    if status and status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )

    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    def to_epoch_ms(value):
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return int(value)

        if isinstance(value, str):
            value = datetime.fromisoformat(value)

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return int(value.timestamp() * 1000)

    for record in page_docs:
        record["punch_in"] = to_epoch_ms(record.get("punch_in"))
        record["punch_out"] = to_epoch_ms(record.get("punch_out"))

        for entry in record.get("history", []):
            if "at" in entry:
                entry["at"] = to_epoch_ms(entry["at"])

            for field_changes in entry.get("changes", {}).values():
                if isinstance(field_changes, dict):
                    for key in ("from", "to"):
                        value = field_changes.get(key)

                        if key in field_changes and (
                            value is None
                            or isinstance(value, (datetime, str))
                        ):

                            for entry in record.get("history", []):
                                if entry.get("at") is not None:
                                    entry["at"] = to_epoch_ms(entry["at"])

                                changes = entry.get("changes", {})
                                for field, change in changes.items():
                                    if not isinstance(change, dict):
                                        continue

                                    for side in ("from", "to"):
                                        value = change.get(side)
                                        if field in ("punch_in", "punch_out") and value is not None:
                                            change[side] = to_epoch_ms(value)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        )
    else:
        ts = datetime.now(timezone.utc)

    ts = ts.replace(microsecond=0)

    # Convert the timestamp to IST to determine the calendar date.
    local_ts = ts.astimezone(IST)
    today = local_ts.date().isoformat()


    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No open attendance record found",
        )
    if record["punch_out"] is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out",
        )

    punch_in_ts = record["punch_in"]

    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)

    # Reject punch-outs before or at punch-in.
    if ts <= punch_in_ts:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    # Reject durations longer than 24 hours.
    if ts - punch_in_ts > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    work_hours = compute_work_hours(punch_in_ts, ts)


    overtime_minutes = compute_overtime(
        ts.astimezone(IST),
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )

    # A punch-out before the scheduled shift end cannot earn overtime.
    if ts.astimezone(IST) < datetime.fromisoformat(
        record["date"]
    ).replace(
        hour=int(emp["shift_end"].split(":")[0]),
        minute=int(emp["shift_end"].split(":")[1]),
        tzinfo=IST,
    ) + (
        timedelta(days=1)
        if emp["shift_end"] <= emp["shift_start"]
        else timedelta()
    ):
        overtime_minutes = 0



    half_day = work_hours < 4.5

    # Update only if another request has not already punched out.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Attendance record has already been punched out",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    return updated



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month format: YYYY-MM
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        year, month_num = map(int, month.split("-"))
        if not 1 <= month_num <= 12:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    # Check employee exists
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Calculate month boundaries
    first_day = f"{year:04d}-{month_num:02d}-01"
    last_day = f"{year:04d}-{month_num:02d}-{calendar.monthrange(year, month_num)[1]:02d}"

    # Working days start from the later of joined_on and first_day
    start_date = max(emp["joined_on"], first_day)
    working_days = 0
    if start_date <= last_day:
        start = datetime.fromisoformat(start_date).date()
        end = datetime.fromisoformat(last_day).date()
        current = start
        while current <= end:
            if current.weekday() < 5:
                working_days += 1
            current += timedelta(days=1)

    # Aggregate stored attendance values for this employee and month
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day},
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
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    "$half_day",
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    stats = results[0] if results else {}

    present_days = stats.get("present_days", 0)
    attendance_pct = (
        float(
            (Decimal(str(present_days)) / Decimal(working_days) * Decimal("100"))
            .quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        )
        if working_days > 0
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate month format: YYYY-MM
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        year, month_num = map(int, month.split("-"))

        if not 1 <= month_num <= 12:
            raise ValueError

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    first_day = f"{year:04d}-{month_num:02d}-01"
    last_day = (
        f"{year:04d}-{month_num:02d}-"
        f"{calendar.monthrange(year, month_num)[1]:02d}"
    )

    pipeline = [
        {
            "$match": {
                "joined_on": {"$lte": last_day},
                **({"department": department} if department else {}),
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "employee_joined": "$joined_on",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]},
                                    {"$gte": ["$date", "$$employee_joined"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.date", None]},
                                    {
                                        "$lt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            7,
                                        ]
                                    },
                                    {
                                        "$gt": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_hours_total": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,

                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_total",
                                "$work_hours_count",
                            ]
                        },
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

    items = list(db.employees.aggregate(pipeline))

    # Apply round-half-up rounding to average work hours.
    for item in items:
        value = item["avg_work_hours"]
        if value is not None:
            item["avg_work_hours"] = float(
                Decimal(str(value)).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

    return {"month": month, "items": items}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    # Validate month format.
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        year, month_num = map(int, month.split("-"))

        if not 1 <= month_num <= 12:
            raise ValueError

    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    if not 1 <= limit <= 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    first_day = f"{year:04d}-{month_num:02d}-01"
    last_day = (
        f"{year:04d}-{month_num:02d}-"
        f"{calendar.monthrange(year, month_num)[1]:02d}"
    )

    pipeline = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
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
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": items}




@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = __import__("fastapi").Query(alias="from"),
    to_date: str = __import__("fastapi").Query(alias="to")
):
    from datetime import date, datetime, time, timedelta, timezone

    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)

        if start.isoformat() != from_date or end.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="from and to must use YYYY-MM-DD format"
        )

    days = (end - start).days + 1
    if days <= 0 or days > 92:
        raise HTTPException(
            status_code=422,
            detail="to must be on or after from and range cannot exceed 92 days"
        )

    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Department not found")

    start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(
        end + timedelta(days=1), time.min, tzinfo=timezone.utc
    )

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", from_date]},
                                    {"$lte": ["$date", to_date]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "joined_on": 1,
                "logs": 1
            }
        },
        {
            "$set": {
                "joined_date": {
                    "$dateFromString": {
                        "dateString": "$joined_on",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC"
                    }
                }
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$set": {
                "day": {
                    "$dateFromString": {
                        "dateString": {
                            "$ifNull": ["$logs.date", from_date]
                        },
                        "format": "%Y-%m-%d",
                        "timezone": "UTC"
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$day",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]},
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$densify": {
                "field": "_id",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive]
                }
            }
        },
        {
            "$fill": {
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0}
                }
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_id"}, 6]}
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$_id"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$set": {
                            "joined_date": {
                                "$dateFromString": {
                                    "dateString": "$joined_on",
                                    "format": "%Y-%m-%d",
                                    "timezone": "UTC"
                                }
                            }
                        }
                    },
                    {
                        "$match": {
                            "$expr": {"$lte": ["$joined_date", "$$day"]}
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "headcount_result"
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {
                            "$round": [
                                {"$divide": ["$present_count", "$headcount"]},
                                4
                            ]
                        },
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_id",
                        "timezone": "UTC"
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$round": ["$attendance_rate", 4]},
                "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]}
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    return {"department": department, "items": items}



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    payload: AttendancePatchIn
):
    from datetime import date as Date, datetime, timedelta, timezone

    # 1. Validate attendance date
    try:
        attendance_day = Date.fromisoformat(date)
        if attendance_day.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format"
        )

    # 2. Find employee
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # 3. Find attendance record
    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if not record:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found"
        )

    # 4. Normalize existing MongoDB datetimes to timezone-aware UTC
    def normalize_datetime(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    old_punch_in = normalize_datetime(record.get("punch_in"))
    old_punch_out = normalize_datetime(record.get("punch_out"))

    old_status = record.get("status", "PRESENT")
    new_status = (
        payload.status
        if payload.status is not None
        else old_status
    )

    # 5. Build new punch values
    if new_status in {"ABSENT", "LEAVE"}:
        if payload.punch_in is not None or payload.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times"
            )

        new_punch_in = None
        new_punch_out = None

    else:
        new_punch_in = old_punch_in
        new_punch_out = old_punch_out

        if payload.punch_in is not None:
            new_punch_in = datetime.fromtimestamp(
                payload.punch_in / 1000,
                tz=timezone.utc
            ).replace(microsecond=0)

        if payload.punch_out is not None:
            new_punch_out = datetime.fromtimestamp(
                payload.punch_out / 1000,
                tz=timezone.utc
            ).replace(microsecond=0)

        # Presence statuses require punch-in
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence statuses require punch_in"
            )

        # Punch-in must fall on the attendance date in IST
        punch_in_local = new_punch_in.astimezone(IST)

        if punch_in_local.date() != attendance_day:
            raise HTTPException(
                status_code=422,
                detail="punch_in must fall on the attendance date in IST"
            )

        # Validate punch-out
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in"
                )

            if new_punch_out - new_punch_in > timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="Punch duration cannot exceed 24 hours"
                )

    # 6. Recalculate derived attendance values
    late_minutes = 0
    overtime_minutes = 0
    work_hours = None
    half_day = False

    if new_status in {"PRESENT", "WFH", "ON_DUTY"}:
        shift_start = datetime.combine(
            attendance_day,
            datetime.strptime(
                employee["shift_start"], "%H:%M"
            ).time(),
            tzinfo=IST
        )

        punch_in_local = new_punch_in.astimezone(IST)

        late_difference = (
            punch_in_local - shift_start
        ).total_seconds() / 60

        # R2: Late only when strictly more than 10 minutes
        if late_difference > 10:
            late_minutes = int(late_difference // 1)

        if new_punch_out is not None:
            work_hours = compute_work_hours(
                new_punch_in,
                new_punch_out
            )

            overtime_minutes = compute_overtime(
                new_punch_out,
                employee["shift_start"],
                employee["shift_end"],
                date
            )

            # R5: half-day when rounded work hours < 4.50
            half_day = work_hours < 4.50

    updated = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "work_hours": work_hours,
        "half_day": half_day
    }

    # 7. Create history for fields that actually changed
    changes = {}

    for field, new_value in updated.items():
        old_value = record.get(field)

        if field in {"punch_in", "punch_out"}:
            old_value = normalize_datetime(old_value)

        if old_value != new_value:
            if field in {"punch_in", "punch_out"}:
                changes[field] = {
                    "from": (
                        int(old_value.timestamp() * 1000)
                        if old_value is not None else None
                    ),
                    "to": (
                        int(new_value.timestamp() * 1000)
                        if new_value is not None else None
                    )
                }
            else:
                changes[field] = {
                    "from": old_value,
                    "to": new_value
                }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The requested update makes no changes"
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": payload.regularized_by.strip(),
        "reason": payload.reason.strip(),
        "changes": changes
    }

    # 8. Atomically update the record and append history
    # Match the original values to detect concurrent changes.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out")
        },
        {
            "$set": updated,
            "$push": {"history": history_entry}
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the request"
        )

    # 9. Return updated record without MongoDB's internal _id
    saved = db.attendance_logs.find_one({
        "_id": record["_id"]
    })

    saved.pop("_id", None)

    # API timestamps must be epoch milliseconds
    for field in ("punch_in", "punch_out"):
        if saved.get(field) is not None:
            saved[field] = int(
                normalize_datetime(saved[field]).timestamp() * 1000
            )

    # Convert history timestamps and remove MongoDB datetime objects
    for entry in saved.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = int(
                normalize_datetime(entry["at"]).timestamp() * 1000
            )

    return saved



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = 1,
    page_size: int = 20
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Unsupported endpoint type"
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for department_summary"
            )

        try:
            if (
                len(month) != 7
                or month[4] != "-"
                or not month[:4].isdigit()
                or not month[5:].isdigit()
            ):
                raise ValueError

            year, month_num = map(int, month.split("-"))

            if not 1 <= month_num <= 12:
                raise ValueError

        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format"
            )

        first_day = f"{year:04d}-{month_num:02d}-01"
        last_day = (
            f"{year:04d}-{month_num:02d}-"
            f"{calendar.monthrange(year, month_num)[1]:02d}"
        )

        employee_match = {
            "joined_on": {"$lte": last_day}
        }

        if department:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "employee_code": "$emp_code",
                        "employee_joined": "$joined_on"
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {
                                            "$eq": [
                                                "$emp_code",
                                                "$$employee_code"
                                            ]
                                        },
                                        {"$gte": ["$date", first_day]},
                                        {"$lte": ["$date", last_day]},
                                        {
                                            "$gte": [
                                                "$date",
                                                "$$employee_joined"
                                            ]
                                        }
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"]
                                            ]
                                        },
                                        {"$ne": ["$logs.date", None]},
                                        {
                                            "$lt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                7
                                            ]
                                        },
                                        {
                                            "$gt": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {
                                                            "dateString": "$logs.date"
                                                        }
                                                    }
                                                },
                                                1
                                            ]
                                        }
                                    ]
                                },
                                {
                                    "$cond": [
                                        "$logs.half_day",
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$logs.late_minutes", 0]},
                                        0
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {
                "$project": {
                    "_id": 1,
                    "headcount": {"$size": "$headcount"},
                    "present_days": 1,
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1
                }
            },
            {"$sort": {"_id": 1}}
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        explain_json = json.loads(
            dumps(
                explain_result,
                json_options=CANONICAL_JSON_OPTIONS
            )
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json
        }

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required for employee_monthly"
            )

        try:
            if (
                len(month) != 7
                or month[4] != "-"
                or not month[:4].isdigit()
                or not month[5:].isdigit()
            ):
                raise ValueError

            year, month_num = map(int, month.split("-"))

            if not 1 <= month_num <= 12:
                raise ValueError

        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format"
            )

        first_day = f"{year:04d}-{month_num:02d}-01"
        last_day = (
            f"{year:04d}-{month_num:02d}-"
            f"{calendar.monthrange(year, month_num)[1]:02d}"
        )

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": first_day,
                        "$lte": last_day
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "record_count": {"$sum": 1},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$status",
                                        ["PRESENT", "WFH", "ON_DUTY"]
                                    ]
                                },
                                1,
                                0
                            ]
                        }
                    }
                }
            }
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )

        explain_json = json.loads(
            dumps(
                explain_result,
                json_options=CANONICAL_JSON_OPTIONS
            )
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required for late_leaderboard",
            )

        try:
            if (
                len(month) != 7
                or month[4] != "-"
                or not month[:4].isdigit()
                or not month[5:].isdigit()
            ):
                raise ValueError

            year, month_num = map(int, month.split("-"))

            if not 1 <= month_num <= 12:
                raise ValueError

        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        if not 1 <= limit <= 50:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 50",
            )

        first_day = f"{year:04d}-{month_num:02d}-01"
        last_day = (
            f"{year:04d}-{month_num:02d}-"
            f"{calendar.monthrange(year, month_num)[1]:02d}"
        )

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": first_day,
                        "$lte": last_day,
                    },
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": "$late_minutes",
                    },
                    "late_count": {"$sum": 1},
                }
            },
            {
                "$match": {
                    "total_late_minutes": {"$gt": 0},
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department is not None:
            pipeline.append(
                {
                    "$match": {
                        "employee.department": department,
                    }
                }
            )

        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {
                            "total_late_minutes": -1,
                        },
                        "output": {
                            "rank": {"$rank": {}},
                        },
                    }
                },
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "_id": 1,
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$project": {
                        "_id": 0,
                        "rank": 1,
                        "emp_code": "$_id",
                        "name": "$employee.name",
                        "department": "$employee.department",
                        "total_late_minutes": 1,
                        "late_count": 1,
                    }
                },
            ]
        )

        explain_result = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        explain_json = json.loads(
            dumps(
                explain_result,
                json_options=CANONICAL_JSON_OPTIONS,
            )
        )

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json,
        }
    if endpoint == "department_trend":
        if not department or not date_from or not date_to:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required for department_trend",
            )

        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)

            if start.isoformat() != date_from or end.isoformat() != date_to:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="from and to must use YYYY-MM-DD format",
            )

        days = (end - start).days + 1
        if days <= 0 or days > 92:
            raise HTTPException(
                status_code=422,
                detail="to must be on or after from and range cannot exceed 92 days",
            )

        start_dt = datetime.combine(
            start, datetime.min.time(), tzinfo=timezone.utc
        )
        end_exclusive = datetime.combine(
            end + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        pipeline = [
            {"$match": {"department": department}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"code": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", date_from]},
                                        {"$lte": ["$date", date_to]},
                                    ]
                                }
                            }
                        },
                        {
                            "$project": {
                                "_id": 0,
                                "date": 1,
                                "status": 1,
                                "half_day": 1,
                                "late_minutes": 1,
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
            {
                "$project": {
                    "emp_code": 1,
                    "joined_on": 1,
                    "logs": 1,
                }
            },
            {
                "$set": {
                    "joined_date": {
                        "$dateFromString": {
                            "dateString": "$joined_on",
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }
                    }
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$set": {
                    "day": {
                        "$dateFromString": {
                            "dateString": {
                                "$ifNull": ["$logs.date", date_from]
                            },
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": "$day",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$logs.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gt": [
                                        {"$ifNull": ["$logs.late_minutes", 0]},
                                        0,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$densify": {
                    "field": "_id",
                    "range": {
                        "step": 1,
                        "unit": "day",
                        "bounds": [start_dt, end_exclusive],
                    },
                }
            },
            {
                "$fill": {
                    "output": {
                        "present_count": {"value": 0},
                        "late_count": {"value": 0},
                    }
                }
            },
            {
                "$set": {
                    "is_working_day": {
                        "$and": [
                            {"$gte": [{"$dayOfWeek": "$_id"}, 2]},
                            {"$lte": [{"$dayOfWeek": "$_id"}, 6]},
                        ]
                    }
                }
            },
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$_id"},
                    "pipeline": [
                        {"$match": {"department": department}},
                        {
                            "$set": {
                                "joined_date": {
                                    "$dateFromString": {
                                        "dateString": "$joined_on",
                                        "format": "%Y-%m-%d",
                                        "timezone": "UTC",
                                    }
                                }
                            }
                        },
                        {
                            "$match": {
                                "$expr": {"$lte": ["$joined_date", "$$day"]}
                            }
                        },
                        {"$count": "count"},
                    ],
                    "as": "headcount_result",
                }
            },
            {
                "$set": {
                    "headcount": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$headcount_result.count", 0]},
                            0,
                        ]
                    }
                }
            },
            {
                "$set": {
                    "attendance_rate": {
                        "$cond": [
                            {
                                "$and": [
                                    "$is_working_day",
                                    {"$gt": ["$headcount", 0]},
                                ]
                            },
                            {
                                "$round": [
                                    {"$divide": ["$present_count", "$headcount"]},
                                    4,
                                ]
                            },
                            None,
                        ]
                    }
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"_id": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$_id",
                            "timezone": "UTC",
                        }
                    },
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": {"$round": ["$attendance_rate", 4]},
                    "moving_avg_7d": {"$round": ["$moving_avg_7d", 4]},
                }
            },
            {"$sort": {"date": 1}},
        ]

        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

        explain_json = json.loads(
            dumps(
                explain_result,
                json_options=CANONICAL_JSON_OPTIONS,
            )
        )

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json,
        }

    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(
            status_code=422,
            detail="page must be >= 1 and page_size must be between 1 and 100"
        )

    if from_date and not date_from:
        date_from = from_date

    if to_date and not date_to:
        date_to = to_date

    filters = {}

    if emp_code:
        filters["emp_code"] = emp_code

    if date_from:
        filters.setdefault("date", {})["$gte"] = date_from

    if date_to:
        filters.setdefault("date", {})["$lte"] = date_to

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to"
        )

    if status:
        allowed_statuses = {
            "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
        }

        if status not in allowed_statuses:
            raise HTTPException(
                status_code=422,
                detail="Invalid attendance status"
            )

        filters["status"] = status

    query = {
        "find": "attendance_logs",
        "filter": filters,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size
    }

    explain_result = db.command(
        "explain",
        query,
        verbosity="executionStats"
    )

    explain_json = json.loads(
        dumps(
            explain_result,
            json_options=CANONICAL_JSON_OPTIONS
        )
    )

    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": explain_json
    }




# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #




