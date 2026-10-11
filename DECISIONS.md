# DECISIONS.md

1. **Indexes.**
Created unique `{emp_code: 1}` on `employees` for identity guarantees and `{department: 1, joined_on: 1}` for headcount filtering. On `attendance_logs`, created unique `{emp_code: 1, date: 1}` to block duplicate punch-ins; `{date: -1, emp_code: 1}` for list sorting; `{emp_code: 1, date: -1}` for monthly summaries; and `{date: 1, late_minutes: 1}` for leaderboard scans. Rejected indexing `history` since audit trails are never searched.

2. **Punch-in race.**
Both requests attempt an atomic insert. The unique index on `(emp_code, date)` guarantees MongoDB commits exactly one document (201) and aborts the other with `DuplicateKeyError`. Our handler catches this and returns 409 Conflict.

3. **Ties.**
Using `$rank` implements competition ranking (1, 2, 2, 4). Filtering `rank <= limit` returns all tied employees at the cutoff boundary (yielding >limit rows if tied), ordered secondarily by `emp_code` ascending.

4. **Headcount.**
The aggregation starts on `employees` matching `joined_on <= month_end` and `$lookup`s attendance logs. Every active employee contributes to departmental headcount even with an empty logs array.

5. **One thing you would change for 100x data.**
At 100x scale, shard `attendance_logs` by hashed `emp_code` and pre-aggregate daily departmental stats via background workers instead of on-demand `$lookup` aggregations.
