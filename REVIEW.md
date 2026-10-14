# Review notes

This is a record of concrete issues found in the implementation during this audit. The unit suite uses a fake database; additional live validation now covers MongoDB 8.0.34.

| # | Where | Defect | Evidence / fix |
|---|---|---|---|
| 1 | Mongo client setup | PyMongo's default naive datetimes can cause aware/naive comparison errors and timezone-dependent conversion after reads. | Configured `tz_aware=True`; added a five-second server selection timeout. |
| 2 | Regularization to `ABSENT` / `LEAVE` | Existing punch values were treated as a validation error, although changing status is required to clear them. | Reject supplied non-null punch fields, clear stored punch values, and recalculate derived fields. |
| 3 | Regularization CAS filter | Seeded records may omit `history`; matching them as `history: []` prevented correction. | Compare the current history array when present, or require `history` to remain absent. |
| 4 | Regularization of overnight records | Comparing the punch-in's IST calendar date directly to `date` rejected punches belonging to the prior overnight shift date. | Validate using the same overnight-aware attendance-date helper as punch-in. |
| 5 | Date filters | String comparison accepted invalid calendar dates, and attendance status was not constrained to the contract enum. | Parse strict ISO calendar dates and validate status against the five allowed values. |
| 6 | Department summary | It fetched employees and assembled headcount in Python, excluding the zero-log case from the aggregation itself. | Reworked to start from eligible employees and `$lookup` their monthly logs; headcount and metrics are grouped in MongoDB. |
| 7 | Late leaderboard | Ranking was performed in Python and the result limit could not correctly express competition-rank ties. | Use MongoDB `$rank`, filter after ranking, and sort tied rows by employee code. |
| 8 | Department trend | The application generated dates, headcounts, rates, and moving averages in Python instead of using a database gap-filling/window pipeline. | Reworked as a MongoDB `$documents` date generator, lookups, and `$setWindowFields` pipeline. |
| 9 | Sample seeding | The original script deleted both collections before importing fixtures, destroying unrelated local records. | Use unique natural keys and `$setOnInsert`; reruns leave existing records untouched. |
| 10 | Index coverage | The original list sort used mixed date/employee directions not covered by the old compound index. | Added direction-matched date/employee and status/date/employee indexes, plus employee listing and joined-date indexes. |
| 11 | Explain endpoint | The previous response returned query metadata rather than MongoDB's explain document, and analytic explain operations used abbreviated pipelines. | It now calls MongoDB `explain` with `executionStats`, returns the raw result, and shares the analytics pipeline builders. Focused tests compare the exact pipeline sent to each analytics operation and its explain command. |

| 12 | Monthly and department weekday checks | IST midnight becomes the previous UTC date; `$dayOfWeek` without a timezone excluded Monday attendance. | Explicit `Asia/Kolkata` timezone in both weekday expressions. Live sample EMP0001 now has 2 present days instead of 1. Independent live calculations cover this regression. |

## Live validation (14 October 2026)

- MongoDB 8.0.34: all four analytics routes execute; monthly/department totals, work-hour means, leaderboard ranks, and trend dates/headcounts/presence match independent calculations.
- All five `executionStats` explain commands succeed, including the collectionless trend. Employee attendance lookup uses `IXSCAN`; index definitions are captured in `live_validation_results.json`.
- Repeated seeding preserves existing records.
- Eight parallel punch-in requests yield one 201 and seven 409 responses; punch-out yields one 200 and seven 409 responses.
- Real Uvicorn HTTP `/health` returns 200; EMP0001 July monthly result returns 2 present days and 8.6957 attendance percentage.
- Unit suite: 25 passed, 11 deprecation warnings.

## Remaining verification

- MongoDB 6.0 compatibility and production-scale query performance were not tested; this run used 8.0.34 and small fixtures.
- Concurrency validation covers punch-in and punch-out; simultaneous manual corrections remain untested live.
- The actual starter revision was not available as a Git diff: this folder has no `.git` directory. The notes above describe confirmed issues in the current reviewed code, not an exhaustive reconstruction of every original starter defect.
