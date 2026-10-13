# Design Decisions

## 1. Database

MongoDB stores employee information and attendance records. PyMongo is used to communicate with the database.

## 2. API Framework

FastAPI implements the REST endpoints, and Pydantic models validate incoming request data.

## 3. Employee Uniqueness

A unique index on `emp_code` prevents duplicate employee records. Duplicate-key errors are handled as HTTP 409 Conflict.

## 4. Attendance Uniqueness

A unique compound index on `emp_code` and `date` enforces one attendance record per employee per attendance date.

## 5. Pagination and Sorting

Pagination, sorting, and record counting are performed by MongoDB rather than loading the full collection into application memory. Page numbering starts at 1, and page size is validated.

## 6. Date and Time Handling

API timestamps use epoch milliseconds. MongoDB datetimes are stored in UTC, while attendance calendar dates follow the required IST business rules. Time calculations use the specified shift rules.

## 7. Validation and Error Handling

Pydantic validates incoming employee data. Invalid inputs are rejected, duplicate employee codes return HTTP 409, and missing resources return appropriate HTTP errors.

## 8. Analytics and Indexes

MongoDB aggregation pipelines are used for analytics. Indexes support employee lookups, attendance queries, and department-based filtering. Explain plans are used to inspect query execution and index usage.

## 9. Attendance History

Attendance updates retain an audit-history entry containing the changed fields, reason, user who performed the update, and timestamp.

## 10. Testing

The application was checked manually through Swagger UI at `/docs`. MongoDB connectivity, application startup, and selected API success and error responses were verified. An automated pytest suite was not available in the project root, so comprehensive automated testing remains incomplete.