from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import app.main as app_main


def test_to_utc_datetime_truncates_subsecond_precision():
    dt = app_main.to_utc_datetime(1704067200123)
    assert dt == datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_to_epoch_ms_truncates_microseconds():
    dt = datetime(2024, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc)
    assert app_main.to_epoch_ms(dt) == 1704067200000


def test_to_utc_datetime_rejects_out_of_range_values():
    with pytest.raises(HTTPException):
        app_main.to_utc_datetime(app_main.MIN_EPOCH_MS - 1)
    with pytest.raises(HTTPException):
        app_main.to_utc_datetime(app_main.MAX_EPOCH_MS + 1)
    with pytest.raises(HTTPException):
        app_main.to_utc_datetime(True)


def test_mongodb_client_returns_timezone_aware_datetimes():
    assert app_main.client.get_database("attendance_db").codec_options.tz_aware is True


def test_server_generated_instants_are_truncated_to_seconds():
    now = app_main.utc_now_seconds()
    assert now.tzinfo == timezone.utc
    assert now.microsecond == 0


def test_work_hours_round_half_up_before_half_day_check():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert app_main.compute_work_hours(start, start + timedelta(hours=1, seconds=18)) == 1.01

    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    record = app_main.rederive_record(
        "PRESENT",
        start,
        start + timedelta(hours=4, minutes=29, seconds=42),
        employee,
        "2024-01-01",
    )
    assert record["work_hours"] == 4.5
    assert record["half_day"] is False
