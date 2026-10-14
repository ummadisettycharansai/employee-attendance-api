# DECISIONS.md

1. `employees.emp_code` is unique for natural-key lookup. The unique `(emp_code, date)` attendance index enforces one employee-day record and supports lookup. Department/joined-date and direction-matched date/status indexes support filters and sorts; no text index is warranted.

2. Punch-in checks for a useful early conflict, but the unique database index is authoritative. Concurrent inserts for one natural key yield one 201; the losing request's `DuplicateKeyError` becomes 409.

3. The leaderboard sums stored `late_minutes`, then applies MongoDB `$rank` over total late minutes only, producing competition ranks with skipped positions. The limit filters rank afterward, retaining cutoff ties; final order is total descending, then employee code ascending.

4. Department summary starts with employees joined by month end and looks up monthly logs. Zero-log employees count; later joiners do not. The mean work hours uses eligible records' total and count, not an average of employee averages.

5. At 100 times the data, I would first inspect explain plans and latency. If needed, I would maintain materialized daily/department roll-ups with reliable change capture, rebuild tooling, and reconciliation. Raw attendance and audit history would remain authoritative.
