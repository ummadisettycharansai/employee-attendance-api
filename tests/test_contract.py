from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


class FakeCursor:
    def __init__(self, items):
        self.items = list(items)

    def sort(self, key, *args):
        if isinstance(key, str):
            direction = args[0] if args else 1
            sort_spec = [(key, direction)]
        else:
            sort_spec = key
        for field, direction in sort_spec:
            reverse = direction < 0
            self.items = sorted(self.items, key=lambda d: d.get(field, 0) if d.get(field) is not None else "", reverse=reverse)
        return self

    def skip(self, amount):
        self.items = self.items[amount:]
        return self

    def limit(self, amount):
        self.items = self.items[:amount]
        return self

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.pipelines = []
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))
        return len(self.indexes)

    def count_documents(self, query=None):
        return len(self._filter(query or {}))

    def _filter(self, query):
        def matches(document, field, value):
            actual = document.get(field)
            if not isinstance(value, dict):
                return actual == value
            for operator, expected in value.items():
                if operator == "$gte" and not (actual is not None and actual >= expected):
                    return False
                if operator == "$lte" and not (actual is not None and actual <= expected):
                    return False
                if operator == "$ne" and actual == expected:
                    return False
                if operator == "$in" and actual not in expected:
                    return False
                if operator == "$exists" and ((field in document) != expected):
                    return False
            return True

        docs = list(self.docs)
        for key, value in query.items():
            if key == "$and":
                for clause in value:
                    docs = [d for d in docs if all(matches(d, k, v) for k, v in clause.items())]
                continue
            docs = [d for d in docs if matches(d, key, value)]
        return docs

    def find(self, query=None, projection=None):
        return FakeCursor(self._filter(query or {}))

    def find_one(self, query=None, sort=None):
        filtered = self._filter(query or {})
        if sort is not None:
            if isinstance(sort, list):
                for field, direction in sorted(sort, key=lambda x: x[1], reverse=True):
                    filtered = sorted(filtered, key=lambda d: d.get(field), reverse=(direction < 0))
            else:
                filtered = sorted(filtered, key=lambda d: d.get(sort), reverse=False)
        return filtered[0] if filtered else None

    def insert_one(self, doc):
        self.docs.append(doc)
        class Inserted:
            inserted_id = object()
        return Inserted()

    def update_one(self, query, update):
        matches = self._filter(query)
        for doc in matches:
            for key, value in update.get("$set", {}).items():
                doc[key] = value
            if "$push" in update:
                for field, val in update["$push"].items():
                    doc.setdefault(field, []).append(val)
        class Result:
            matched_count = len(matches)
        return Result()

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        if pipeline and pipeline[0].get("$match", {}).get("emp_code") == "EMP0001":
            return [{
                "_id": None,
                "present_days": 2.5,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 35,
                "total_overtime_minutes": 40,
            }]
        if any("$setWindowFields" in stage for stage in pipeline):
            return [{
                "_id": "EMP0001",
                "rank": 1,
                "name": "Asha Rao",
                "department": "Engineering",
                "total_late_minutes": 35,
                "late_count": 1,
            }]
        if pipeline and pipeline[0].get("$match", {}).get("joined_on"):
            return [{
                "_id": "Engineering",
                "headcount": 1,
                "present_days": 2.5,
                "total_work_hours": 8.5,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 35,
                "leave_count": 0,
                "on_duty_count": 1,
            }]
        return []


@pytest.fixture
def client(monkeypatch):
    employees = [
        {
            "emp_code": "EMP0001",
            "name": "Asha Rao",
            "email": "asha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2024-01-01",
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        },
        {
            "emp_code": "EMP0002",
            "name": "Raj Mehta",
            "email": "raj@example.com",
            "department": "Finance",
            "shift_start": "09:00",
            "shift_end": "17:00",
            "joined_on": "2024-01-03",
            "created_at": datetime(2024, 1, 3, tzinfo=timezone.utc),
        },
    ]
    attendance = [
        {
            "emp_code": "EMP0001",
            "date": "2024-01-02",
            "status": "PRESENT",
            "punch_in": datetime(2024, 1, 2, 9, 35, tzinfo=timezone.utc),
            "punch_out": datetime(2024, 1, 2, 18, 40, tzinfo=timezone.utc),
            "work_hours": 9.08,
            "late_minutes": 5,
            "overtime_minutes": 10,
            "half_day": False,
            "history": [],
        },
        {
            "emp_code": "EMP0002",
            "date": "2024-01-02",
            "status": "LEAVE",
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        },
    ]
    fake_db = type("FakeDB", (), {})()
    fake_db.employees = FakeCollection(employees)
    fake_db.attendance_logs = FakeCollection(attendance)
    fake_db.commands = []

    def command(*args, **kwargs):
        if args and isinstance(args[0], dict) and "explain" in args[0]:
            fake_db.commands.append(args[0])
            return {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}, "executionStats": {"nReturned": 1}}
        return {"ok": 1}

    fake_db.command = command
    fake_db.aggregate_pipelines = []

    def aggregate(pipeline):
        fake_db.aggregate_pipelines.append(pipeline)
        return [
            {
                "date": f"2024-01-0{day}",
                "is_working_day": day <= 5,
                "headcount": 1,
                "present_count": 0.0,
                "late_count": 0,
                "attendance_rate": 0.0 if day <= 5 else None,
                "moving_avg_7d": 0.0 if day <= 5 else None,
            }
            for day in range(1, 8)
        ]

    fake_db.aggregate = aggregate
    monkeypatch.setattr(app_main, "db", fake_db)
    return TestClient(app_main.app)


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_employee_and_list(client):
    payload = {
        "emp_code": "EMP0003",
        "name": "Nisha",
        "email": "nisha@example.com",
        "department": "Engineering",
        "joined_on": "2024-01-05",
    }
    create = client.post("/employees", json=payload)
    assert create.status_code == 201
    assert create.json()["emp_code"] == "EMP0003"

    listing = client.get("/employees", params={"department": "Engineering"})
    assert listing.status_code == 200
    assert listing.json()["total"] >= 2
    assert listing.json()["items"][0]["emp_code"] in {"EMP0001", "EMP0003"}

    duplicate = client.post("/employees", json=payload)
    assert duplicate.status_code == 409
    invalid = client.post("/employees", json={**payload, "emp_code": "bad-code"})
    assert invalid.status_code == 422


def test_punch_in_and_duplicate_rejected(client):
    before = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1704301200000, "status": "PRESENT"})
    assert before.status_code == 201

    duplicate = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1704301200000, "status": "PRESENT"})
    assert duplicate.status_code == 409


def test_attendance_not_found_and_invalid_punch_out(client):
    unknown_employee = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert unknown_employee.status_code == 404

    no_record = client.post("/attendance/punch-out", json={"emp_code": "EMP0002"})
    assert no_record.status_code == 404

    already_closed = client.post(
        "/attendance/punch-out",
        json={"emp_code": "EMP0001", "punched_at": 1704220000000},
    )
    assert already_closed.status_code == 409


def test_punch_out_flow_updates_derived_values(client):
    client.post("/employees", json={"emp_code": "EMP0003", "name": "Nisha", "email": "nisha@example.com", "department": "Engineering", "joined_on": "2024-01-05"})
    punch_in = client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": 1704301200000, "status": "PRESENT"})
    assert punch_in.status_code == 201

    punch_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": 1704349200000})
    assert punch_out.status_code == 200
    body = punch_out.json()
    assert body["emp_code"] == "EMP0003"
    assert body["work_hours"] is not None
    assert body["late_minutes"] >= 0


def test_regularization_appends_history_and_rejects_noop(client):
    resp = client.patch(
        "/attendance/EMP0001/2024-01-02",
        json={"status": "PRESENT", "punch_in": 1704160200000, "punch_out": 1704200000000, "reason": "biometric correction", "regularized_by": "hr.admin"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["history"][0]["by"] == "hr.admin"
    assert payload["history"][0]["reason"] == "biometric correction"

    noop = client.patch(
        "/attendance/EMP0001/2024-01-02",
        json={"status": "PRESENT", "reason": "same reason again", "regularized_by": "hr.admin"},
    )
    assert noop.status_code == 422


def test_regularization_to_absent_clears_punches(client):
    resp = client.patch(
        "/attendance/EMP0001/2024-01-02",
        json={"status": "ABSENT", "reason": "wrongly marked present", "regularized_by": "hr.admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ABSENT"
    assert body["punch_in"] is None
    assert body["punch_out"] is None
    assert body["history"][0]["changes"]["punch_in"]["to"] is None

    invalid = client.patch(
        "/attendance/EMP0001/2024-01-02",
        json={
            "status": "LEAVE",
            "punch_in": 1704187800000,
            "reason": "conflicting fields",
            "regularized_by": "hr.admin",
        },
    )
    assert invalid.status_code == 422


def test_regularization_supports_seeded_record_without_history(client):
    record = app_main.db.attendance_logs.docs[0]
    record.pop("history")
    resp = client.patch(
        "/attendance/EMP0001/2024-01-02",
        json={"punch_in": 1704187800000, "reason": "correct punch in", "regularized_by": "hr.admin"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["history"]) == 1


def test_invalid_attendance_date_and_status_return_422(client):
    bad_date = client.get("/attendance", params={"date_from": "2024-02-30"})
    bad_status = client.get("/attendance", params={"status": "UNKNOWN"})
    assert bad_date.status_code == 422
    assert bad_status.status_code == 422

    reversed_range = client.get("/attendance", params={"date_from": "2024-02-10", "date_to": "2024-02-01"})
    assert reversed_range.status_code == 422


def test_contract_routes_and_indexes_are_present(client):
    paths = app_main.app.openapi()["paths"]
    assert {
        "/health",
        "/employees",
        "/attendance",
        "/attendance/punch-in",
        "/attendance/punch-out",
        "/attendance/{emp_code}/{date}",
        "/analytics/employees/{emp_code}/monthly",
        "/analytics/departments/summary",
        "/analytics/leaderboard/late",
        "/analytics/departments/{department}/trend",
        "/admin/explain/{endpoint}",
    }.issubset(paths)

    app_main.ensure_indexes()
    employee_indexes = [index for args, _ in app_main.db.employees.indexes for index in args]
    attendance_indexes = [index for args, _ in app_main.db.attendance_logs.indexes for index in args]
    assert "emp_code" in employee_indexes
    assert [("emp_code", 1), ("date", 1)] in attendance_indexes
    assert [("date", -1), ("emp_code", 1)] in attendance_indexes


def test_analytics_use_required_database_stages(client):
    summary = client.get("/analytics/departments/summary", params={"month": "2024-01"})
    assert summary.status_code == 200
    summary_pipeline = app_main.db.employees.pipelines[-1]
    assert "$lookup" in summary_pipeline[1]
    assert "$group" in summary_pipeline[-2]

    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2024-01", "limit": 1})
    assert leaderboard.status_code == 200
    leaderboard_pipeline = app_main.db.attendance_logs.pipelines[-1]
    assert any("$setWindowFields" in stage for stage in leaderboard_pipeline)
    assert any(stage.get("$match", {}).get("rank") == {"$lte": 1} for stage in leaderboard_pipeline)

    trend = client.get(
        "/analytics/departments/Engineering/trend",
        params={"from": "2024-01-01", "to": "2024-01-07"},
    )
    assert trend.status_code == 200
    trend_pipeline = app_main.db.aggregate_pipelines[-1]
    assert "$documents" in trend_pipeline[0]
    assert any("$setWindowFields" in stage for stage in trend_pipeline)


def test_attendance_listing_filters_and_pagination(client):
    resp = client.get("/attendance", params={"emp_code": "EMP0001", "status": "PRESENT", "date_from": "2024-01-01", "date_to": "2024-01-10", "page": 1, "page_size": 10})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] >= 1
    assert payload["page"] == 1
    assert payload["page_size"] == 10


def test_monthly_employee_analytics(client):
    resp = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2024-01"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["emp_code"] == "EMP0001"
    assert "present_days" in payload
    assert "attendance_pct" in payload


def test_department_summary_leaderboard_and_trend(client):
    summary = client.get("/analytics/departments/summary", params={"month": "2024-01"})
    assert summary.status_code == 200
    assert "items" in summary.json()

    leaderboard = client.get("/analytics/leaderboard/late", params={"month": "2024-01", "limit": 10})
    assert leaderboard.status_code == 200
    assert "items" in leaderboard.json()

    trend = client.get("/analytics/departments/Engineering/trend", params={"from": "2024-01-01", "to": "2024-01-07"})
    assert trend.status_code == 200
    payload = trend.json()
    assert payload["department"] == "Engineering"
    assert "items" in payload


def test_explain_routes_are_available(client):
    resp = client.get("/admin/explain/attendance_list", params={"date_from": "2024-01-01", "date_to": "2024-01-10"})
    assert resp.status_code == 200
    assert resp.json()["endpoint"] == "attendance_list"
    assert resp.json()["explain"]["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN"
    assert app_main.db.commands[-1]["verbosity"] == "executionStats"
    assert app_main.db.commands[-1]["explain"]["find"] == "attendance_logs"

    monthly_params = {"emp_code": "EMP0001", "month": "2024-01"}
    monthly_actual = client.get("/analytics/employees/EMP0001/monthly", params={"month": monthly_params["month"]})
    assert monthly_actual.status_code == 200
    monthly_pipeline = app_main.db.attendance_logs.pipelines[-1]
    monthly = client.get("/admin/explain/employee_monthly", params={"emp_code": "EMP0001", "month": "2024-01"})
    assert monthly.status_code == 200
    assert monthly.json()["endpoint"] == "employee_monthly"
    assert app_main.db.commands[-1]["explain"]["pipeline"] == monthly_pipeline

    summary_params = {"month": "2024-01", "department": "Engineering"}
    summary_actual = client.get("/analytics/departments/summary", params=summary_params)
    assert summary_actual.status_code == 200
    summary_pipeline = app_main.db.employees.pipelines[-1]
    summary = client.get("/admin/explain/department_summary", params=summary_params)
    assert summary.status_code == 200
    assert app_main.db.commands[-1]["explain"] == {
        "aggregate": "employees",
        "pipeline": summary_pipeline,
        "cursor": {},
    }

    leaderboard_params = {"month": "2024-01", "limit": 1, "department": "Engineering"}
    leaderboard_actual = client.get("/analytics/leaderboard/late", params=leaderboard_params)
    assert leaderboard_actual.status_code == 200
    leaderboard_pipeline = app_main.db.attendance_logs.pipelines[-1]
    leaderboard = client.get("/admin/explain/late_leaderboard", params=leaderboard_params)
    assert leaderboard.status_code == 200
    assert app_main.db.commands[-1]["explain"]["pipeline"] == leaderboard_pipeline

    trend_params = {"from": "2024-01-01", "to": "2024-01-07"}
    trend_actual = client.get("/analytics/departments/Engineering/trend", params=trend_params)
    assert trend_actual.status_code == 200
    trend_pipeline = app_main.db.aggregate_pipelines[-1]
    trend = client.get(
        "/admin/explain/department_trend",
        params={"department": "Engineering", **trend_params},
    )
    assert trend.status_code == 200
    assert app_main.db.commands[-1]["explain"] == {
        "aggregate": 1,
        "pipeline": trend_pipeline,
        "cursor": {},
    }


def test_overnight_shift_rules(client):
    emp = app_main.attendance_date_for_punch({"shift_start": "22:00", "shift_end": "06:00"}, datetime(2024, 1, 1, 18, 30, tzinfo=timezone.utc))
    assert emp == "2024-01-01"

    after_midnight = datetime(2024, 1, 2, 0, 30, tzinfo=timezone.utc)
    assert app_main.attendance_date_for_punch(
        {"shift_start": "22:00", "shift_end": "06:00"}, after_midnight
    ) == "2024-01-02"
    assert app_main.compute_late_minutes(after_midnight, "22:00", "06:00") == 0


def test_late_and_overtime_boundaries():
    shift_start = datetime(2024, 1, 1, 4, 0, tzinfo=timezone.utc)
    at_grace_boundary = shift_start + timedelta(minutes=10)
    past_grace_boundary = at_grace_boundary + timedelta(seconds=1)
    assert app_main.compute_late_minutes(at_grace_boundary, "09:30") == 0
    assert app_main.compute_late_minutes(past_grace_boundary, "09:30") == 10

    shift_end = datetime(2024, 1, 1, 13, 0, tzinfo=timezone.utc)
    assert app_main.compute_overtime(shift_end + timedelta(seconds=1799), "18:30", "2024-01-01", "09:30") == 0
    assert app_main.compute_overtime(shift_end + timedelta(minutes=30), "18:30", "2024-01-01", "09:30") == 30


def test_invalid_epoch_and_validation_errors(client):
    invalid_epoch = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 123, "status": "PRESENT"})
    assert invalid_epoch.status_code == 422

    missing = client.get("/analytics/employees/EMP999/monthly", params={"month": "2024-01"})
    assert missing.status_code == 404
