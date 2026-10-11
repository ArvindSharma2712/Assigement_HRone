# Employee Attendance & Analytics API

A FastAPI + MongoDB backend service implementing employee attendance management, shift tracking, regularization audit trails, and high-performance analytical aggregation pipelines.

## Requirements & Stack
- **Python**: 3.11+
- **MongoDB**: 6.0+ (Tested on MongoDB 8.0)
- **FastAPI**, **Uvicorn**, **PyMongo**, **Pydantic**

## Setup & Running

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment variables (optional defaults apply):**
   - `MONGO_URI`: MongoDB connection string (defaults to `mongodb://localhost:27017`)
   - `MONGO_DB`: Target database name (defaults to `attendance_db`)

3. **Seed initial sample data (optional):**
   ```bash
   python sample_seed.py
   ```

4. **Run the API service:**
   ```bash
   uvicorn app.main:app --port 8000
   ```

All database indexes are created idempotently at application startup during the FastAPI lifespan event.

## Key Features & Endpoints
- **Readiness probe**: `GET /health` (pings MongoDB, returns 200/503)
- **Employee management**: `POST /employees`, `GET /employees` (unique index, paginated, sorted)
- **Attendance operations**:
  - `POST /attendance/punch-in` (race-safe duplicate prevention, R1 overnight support, R2 grace period)
  - `POST /attendance/punch-out` (race-safe atomic updates, R3 overtime, R4 half-up work hours, R5 half-day)
  - `GET /attendance` (index-backed sorting and range filtering)
  - `PATCH /attendance/{emp_code}/{date}` (regularization with audit trail and optimistic concurrency control)
- **Analytics Aggregations**:
  - `GET /analytics/employees/{emp_code}/monthly` (working days, present days, attendance percentage)
  - `GET /analytics/departments/summary` (cross-collection aggregation, zero-log headcount support)
  - `GET /analytics/leaderboard/late` (standard competition ranking with tie preservation via `$rank`)
  - `GET /analytics/departments/{department}/trend` (gap-filling via `$densify`, 7-day moving average via `$setWindowFields`)
- **Admin Diagnostics**:
  - `GET /admin/explain/{endpoint}` (executionStats explain plans proving index usage without collection scans)

## Project Deliverables
- [`app/main.py`](app/main.py): Complete application implementation in a single file.
- [`REVIEW.md`](REVIEW.md): Code review of starter defects, failing symptoms, and applied fixes.
- [`DECISIONS.md`](DECISIONS.md): Architectural decisions covering indexes, race handling, ties, headcount, and scalability.
