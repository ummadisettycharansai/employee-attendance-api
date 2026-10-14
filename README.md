# Employee Attendance & Analytics API

FastAPI service for employee attendance records, manual corrections, MongoDB-backed analytics, and contract validation tests.

## Configure and run (Windows PowerShell)

The API reads `MONGO_URI` and `MONGO_DB` from environment variables. A local `.env` is supported; real environment variables take precedence. Do not commit `.env`.

### Start a local MongoDB server

If MongoDB Community Server is installed as a Windows service, run PowerShell as needed for service permissions:

```powershell
Get-Service MongoDB
Start-Service MongoDB
```

Or run the server in a dedicated terminal (with `mongod.exe` on `PATH`):

```powershell
$dbPath = Join-Path $env:LOCALAPPDATA "MongoDB\data"
New-Item -ItemType Directory -Force -Path $dbPath | Out-Null
mongod --dbpath $dbPath --bind_ip 127.0.0.1 --port 27017
```

For MongoDB Atlas, set `MONGO_URI` to the Atlas SRV URI with your credentials and permitted client IP configured in Atlas. Do not store a real URI in source control.

### Seed and run

From the project root, in a second terminal:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
.\.venv\Scripts\python.exe .\sample_seed.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

For Atlas, replace the local URI with the Atlas URI. Verify readiness from another PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Start MongoDB separately before seeding or starting the API. `GET /health` returns `200` only when MongoDB responds. The seed script inserts sample documents that are missing and leaves existing documents untouched; it does not reset the database.

## Tests

Run the full suite from the project root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Validation status

Validated on 14 October 2026 against an isolated MongoDB 8.0.34 container. The full suite passed: `25 passed` (11 dependency/deprecation warnings). A real Uvicorn process served `/health` successfully and returned the corrected monthly results over HTTP.

`validate_live.py` checks seed idempotency, live analytics against independent calculations, all five executionStats explain commands, created indexes, and eight concurrent punch-in/punch-out requests (exactly one success per operation). Results and explain documents are saved in `live_validation_results.json`. The employee attendance query uses `IXSCAN`; small-fixture plans are not performance evidence at production scale. MongoDB 6.0 itself has not been tested.

Live validation found and fixed an IST weekday bug: Monday attendance was excluded from monthly/department present-day totals because `$dayOfWeek` defaulted to UTC. Both pipelines now explicitly use `Asia/Kolkata`.

To reproduce in an isolated container:

```powershell
docker run --detach --name sdt-mongo-validation --publish 127.0.0.1:27018:27017 mongo:8.0
.\.venv\Scripts\python.exe validate_live.py
```

The validator always uses `mongodb://127.0.0.1:27018` and database `sdt_live_validation`; it replaces only its synthetic concurrency attendance records. The local validation API uses that database at `http://127.0.0.1:8000` (Swagger UI: `/docs`).

## Notes
- Application code remains in `app/main.py` as required by the assignment. Do not publish `.venv`, `.env`, credentials, or database dumps.
- The supplied `sample_data/` documents are fixtures, not representative coverage of hidden analytics cases.
