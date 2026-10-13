# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees()`|Pagination uses `page * page_size`, skipping the first page incorrectly.|Request page 1 with page size 2; the first two employees are skipped. |Changed the calculation to `(page - 1) * page_size`.|
| 2 | `list_employees()`| Total employee count ignores the department filter.|Filter by Engineering; the total incorrectly counts employees in all departments.         |Changed the count query to `count_documents(q)`.    |
| 3 |  `health()`      |  The endpoint returns success without checking MongoDB availability | Stop MongoDB or make it unreachable; the endpoint may still report success. |Added a MongoDB ping and return HTTP 503 when it fails.|
| 4 |`EmployeeIn.validate_shift()`|Employees could be created with identical shift start and end times.|Submit `POST /employees` with both times set to `09:30`; the request should be rejected.|Added a Pydantic model validator that rejects identical shift times with HTTP 422. |
| 5 |  `create_employee()` | The existing employee check alone cannot prevent duplicate codes when simultaneous requests arrive. | Submit two requests with the same `emp_code`; the duplicate request should return HTTP 409.  |Added MongoDB unique-index enforcement and handled `DuplicateKeyError` by returning HTTP 409.|
| 6 |   `list_attendance()` |  The endpoint loaded all matching attendance records into Python before sorting and paginating, which could cause performance problems with large collections.|Query attendance with many records and inspect the implementation for in-memory pagination.|Moved sorting, pagination, and counting to MongoDB using `sort()`, `skip()`, `limit()`, and `count_documents()`. Also excluded `_id` from the response and validated pagination limits.|
| 7 |  `create_indexes()`| Without a unique compound index, simultaneous punch-in requests could create duplicate attendance records for the same employee and date. |Attempt to insert two attendance records with the same `emp_code` and `date`.| Added a unique compound index on `emp_code` and `date` to enforce one attendance record per employee per date.|


## Items Reviewed but Not Considered Defects

- **MongoDB index creation:** Indexes are created during application startup, so repeated startup should not require manual index creation.
- **Duplicate attendance records:** A unique compound index on `emp_code` and `date` is used to prevent duplicate records for an employee on the same attendance date.
- **Attendance history:** PATCH updates retain a history entry describing the changed fields, the reason, and who performed the update.
- **Pagination:** Page numbers start at 1, and page size is limited to the permitted range.
- **Analytics query performance:** Explain plans were checked for the tested analytics queries to confirm index usage and inspect collection scans.

These items were reviewed based on the implementation and manual API checks. They have not all been verified with automated tests or a large production-sized dataset.
Also note anything you looked at and decided was **not** a defect, and why.
