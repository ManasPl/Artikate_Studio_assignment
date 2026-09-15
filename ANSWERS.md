# ANSWERS

## Part B — Diagnose three broken snippets

### Snippet 1 — `overdue_report` view

**1. What's wrong**

- **N+1 queries.** `c.asset.name`, `c.asset.asset_tag`, `c.employee.full_name` each trigger a fresh query per row (no `select_related`). This is exactly the "query per row" pattern the assignment's own overdue endpoint explicitly forbids.
- **Filtering in Python, not SQL.** It pulls *every open* checkout (`returned_at__isnull=True`) into memory, then discards the non-overdue ones with `if c.due_at < timezone.now()`. As the open-checkout set grows this loads far more rows than necessary.
- **Sorting in Python** (`rows.sort(...)`) instead of `order_by('due_at')` — same root cause as above, extra memory + CPU for no reason.
- **No pagination.** The full result set is serialized into one JSON response regardless of size.
- **No authentication.** This is a plain Django view returning `JsonResponse`, not a DRF view — it isn't routed through DRF's authentication/permission classes at all, so if wired directly into `urls.py` it bypasses the "all endpoints require auth" rule.
- **Minor:** `timezone.now()` is called twice (once in the filter condition, again per row for `days_overdue`), so "now" drifts slightly across a long loop instead of being one consistent snapshot.

**2. Why it looks correct locally**

With a handful of seed rows, N+1 queries add single-digit milliseconds — invisible without an active query counter (`django-debug-toolbar`, `assertNumQueries`). A 3-row JSON payload looks identical whether paginated or not. If the developer's manual testing goes through an already-authenticated admin/browser session, the missing DRF auth wiring never surfaces. Correctness of the *output* is identical whether filtering/sorting happens in SQL or Python for small data — only scale or an explicit query-count assertion exposes the difference.

**3. Fix**

```python
from django.db.models import ExpressionWrapper, F, DurationField, Value
from rest_framework import generics
from rest_framework.response import Response
from django.utils import timezone

class OverdueReportView(generics.ListAPIView):
    def get_queryset(self):
        now = timezone.now()
        return (
            CheckOut.objects
            .filter(returned_at__isnull=True, due_at__lt=now)
            .select_related("asset", "employee")
            .annotate(overdue_duration=ExpressionWrapper(
                Value(now) - F("due_at"), output_field=DurationField()))
            .order_by("due_at")
        )

    def list(self, request, *a, **kw):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        rows = [{
            "asset": c.asset.name, "asset_tag": c.asset.asset_tag,
            "employee": c.employee.full_name,
            "days_overdue": c.overdue_duration.days,
        } for c in page]
        return self.get_paginated_response(rows)
```

DRF's default auth/permission classes now apply automatically; the query is filtered, sorted and joined in the database in one round trip; pagination is inherited from `ListAPIView`.

**4. What would have caught it**

- `assertNumQueries(N)` in a test seeded with, say, 25 overdue rows — fails immediately if query count scales with row count.
- A test asserting an anonymous request gets 401/403.
- A test with >20 overdue rows asserting the response is paginated.
- `django-debug-toolbar` or `nplusone` in dev; APM (Sentry performance, New Relic) in staging flags N+1 patterns before they reach production.

---

### Snippet 2 — `check_out_asset` view

**1. What's wrong**

- **The core bug: no transaction, no row lock.** `Asset.objects.get(...)` reads `status`, then much later `asset.save()` writes it — with no `transaction.atomic()` / `select_for_update()` around the whole read-check-write sequence. Two concurrent requests for the same asset both read `AVAILABLE`, both pass the check, both create a `CheckOut` and both set `CHECKED_OUT`. Directly breaks the concurrency rule and the "row must never exist alongside an AVAILABLE asset" rule.
- **No atomicity between the two writes.** Even without concurrency, if `asset.save()` fails after `CheckOut.objects.create()` already succeeded, you're left with a checkout row against an asset still marked `AVAILABLE`.
- **Unhandled `DoesNotExist` → 500, not 404.** `Asset.objects.get(...)` / `Employee.objects.get(...)` raise `Model.DoesNotExist` on a bad `asset_tag`/`employee_code`. DRF's default exception handler converts `Http404`/`PermissionDenied`, but *not* a raw `ObjectDoesNotExist` — this crashes with a 500, violating "unknown asset_tag or employee_code → 404, not 500" explicitly.
- **Rule 2 missing entirely** — no check on `employee.is_active`.
- **Rule 4 missing entirely** — `due_at` is used straight from `request.data` with no future/≤30-day validation, and a missing key would `KeyError` (another unhandled crash).
- **The open-checkout-count check has the same race** as the main bug — two concurrent requests from one employee sitting at 2 open checkouts can both read `count == 2` and both proceed.

**2. Why it looks correct locally**

Manual/sequential testing (one curl at a time) never produces two requests landing on the same row simultaneously — the race is invisible without a deliberately scripted concurrent test. A bad `asset_tag` is never tried by a developer testing against data they just seeded, so the 500 path is never exercised. If `due_at` is always set to something sane by hand, the missing validation is silent until a real or hostile client sends something else.

**3. Fix** — see `checkouts/views.py::CheckOutCreateView` in this repo for the full version; the essential shape:

```python
with transaction.atomic():
    asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)   # locked
    employee = Employee.objects.select_for_update().get(pk=employee.pk)  # consistent lock order
    if asset.status != "AVAILABLE":
        return Response({"detail": "not available"}, status=409)
    if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
        return Response({"detail": "limit reached"}, status=409)
    checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
    asset.status = "CHECKED_OUT"
    asset.save(update_fields=["status", "updated_at"])
```
Plus: `get_object_or_404` for the lookups (→ automatic 404), an explicit `is_active` check (→ 400), and a serializer validating `due_at` is in `(now, now+30d]` (→ 400).

**4. What would have caught it**

- The exact two-thread concurrency test the assignment requires (A5) — would show two 201s instead of one 201 + one 409.
- A test posting an unknown `asset_tag`/`employee_code` asserting 404 (currently 500).
- Tests for `is_active=False` → 400 and bad `due_at` → 400.
- A team convention/lint rule: "any read-check-write against a row that another request could also mutate must be inside `transaction.atomic()` with `select_for_update()`" — a code-review checklist item, since nothing short of a real concurrency test or careful review catches this class of bug.

---

### Snippet 3 — `send_overdue_notices` task

**1. What's wrong**

- **Not actually idempotent.** It calls `OverdueNotice.objects.create(...)` unconditionally for every overdue row with no existence check. If a `(checkout, notice_date)` uniqueness constraint exists, the very first duplicate on a second run raises `IntegrityError` and the task **dies mid-loop** — it doesn't just skip the duplicate, it stops processing every checkout after that point. If no such constraint existed, it would instead silently create duplicate notices. Either way, "run it five times, no five notices" fails.
- **`deliver_email.delay(c.employee, c)` passes Django model instances as task arguments.** Celery's default JSON serializer can't serialize a model instance — this call fails at enqueue time. It should pass primitive IDs (`c.employee_id`, `c.id`) and let `deliver_email` re-fetch them.
- **No idempotency on the email side either** — even once the argument bug is fixed, a retried task re-sends `deliver_email.delay(...)` for every checkout it touches, including ones already emailed on a prior partial run — duplicate emails to real employees.
- **Query-per-row / broker-call-per-row at scale.** At "tens of thousands of rows," the loop issues tens of thousands of individual `INSERT`s and tens of thousands of individual broker publishes inside one task invocation — slow, and if the broker's visibility/ack timeout is shorter than the total runtime, the task itself can be considered lost and redelivered mid-flight, causing a second overlapping execution.
- **No `.iterator()`/chunking** — the queryset is fully materialized in memory (all columns, including the `condition_note` text field) before iterating.
- **`overdue.count()` at the end re-runs the query** a second time just to report a number that isn't even accurate about what was actually sent (it's the total overdue count, not "successfully notified," and means nothing after a partial failure).

**2. Why it looks correct locally**

With a handful of dev rows, the per-row loop and broker calls run in milliseconds — scale problems don't show up. If the developer tests via `CELERY_TASK_ALWAYS_EAGER=True` or (as the assignment itself suggests as a time-saving fallback) by calling the function directly in a shell, task arguments are never actually serialized through a real broker — so the model-instance-as-argument bug never fires locally and only appears against a real worker/broker in staging or production. Running the task exactly once (the natural manual test) never exercises the retry/duplicate path at all.

**3. Fix**

```python
@shared_task
def send_overdue_notices():
    today = timezone.localdate()
    overdue_ids = list(
        CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=timezone.now())
        .values_list("id", flat=True)
    )
    already = set(
        OverdueNotice.objects.filter(notice_date=today, checkout_id__in=overdue_ids)
        .values_list("checkout_id", flat=True)
    )
    new_ids = [cid for cid in overdue_ids if cid not in already]
    OverdueNotice.objects.bulk_create(
        [OverdueNotice(checkout_id=cid, notice_date=today) for cid in new_ids],
        ignore_conflicts=True,
    )
    for checkout_id in new_ids:
        deliver_email.delay(checkout_id)  # primitive, re-fetch inside the task
    return f"flagged {len(new_ids)} new notice(s)"
```
Pre-filters already-notified checkouts (no per-row DB write in a loop), bulk-inserts, passes only a primitive ID to the email task, and `ignore_conflicts=True` is a backstop against a race between two overlapping runs rather than the sole idempotency mechanism.

**4. What would have caught it**

- Exactly the idempotency test A5 requires, generalized: run the task twice, assert one notice per checkout *and* (via `mock.patch` on `deliver_email.delay`) exactly one email dispatch per checkout.
- Running tests against a real (non-eager) Celery config at least once in CI, so argument serialization is actually exercised — this is a well-known Celery anti-pattern ("never pass model instances as task arguments") that a reviewer familiar with Celery catches on sight.
- A volume/load test seeding tens of thousands of overdue rows and timing the task before it ships.

---

## Part C — Optimise a slow PostgreSQL query

### 1. Rewritten query

```sql
SELECT c.id, c.asset_id, c.employee_id, c.checked_out_at, c.due_at
FROM checkouts c
INNER JOIN employees e ON e.id = c.employee_id
WHERE c.returned_at IS NULL
  AND e.is_active = true
  AND c.checked_out_at >= '2026-01-01T00:00:00+00'
  AND c.checked_out_at <  '2026-07-01T00:00:00+00'
ORDER BY c.due_at ASC;
```

- **Dropped `SELECT *`** → only the columns the report actually displays. Pure win: less I/O and network, and it stops ruling out an index-only scan on columns the query doesn't need.
- **`DATE(c.checked_out_at) BETWEEN ...` → a sargable half-open range** on the raw `timestamptz` column. `DATE(...)` wraps the column in a function, which makes the predicate non-sargable — no plain btree index can be used against it. The rewrite is functionally equivalent *once the boundary and timezone are verified against real data* (see point 5) — note the upper bound is `< '2026-07-01'`, not `<= '2026-06-30'`, to correctly include all of June 30th after converting from a `DATE` comparison to a timestamp range.
- **`employee_id IN (SELECT ... WHERE is_active)` → an explicit `INNER JOIN`.** Functionally identical (a checkout has exactly one employee, so the join can't fan out rows/no `DISTINCT` needed) but makes the semi-join shape unambiguous for the planner and for future readers, and lets it use an index on `employees.is_active`/`employees.id` directly instead of planning a subquery.

### 2. Indexes

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_by_employee_due
ON checkouts (employee_id, due_at)
WHERE returned_at IS NULL;
```
`returned_at IS NULL` is almost certainly the most selective predicate here — on a 4.2M-row *historical* table, open (unreturned) checkouts are very likely a small fraction, since most equipment eventually comes back. A **partial index** scoped to only open rows stays small and cheap to maintain regardless of how large the historical table grows, and Postgres can match the query's `returned_at IS NULL` clause directly against the index's predicate. Leading on `employee_id` accelerates the join against `employees`.

```sql
CREATE INDEX CONCURRENTLY idx_checkouts_open_checked_out_at
ON checkouts (checked_out_at)
WHERE returned_at IS NULL;
```
A second partial index, this time supporting the (now sargable) `checked_out_at` range filter directly. Whether the planner ends up preferring this one or the one above for a given date range is exactly the kind of thing to confirm with `EXPLAIN ANALYZE` on real data rather than assume.

I deliberately did **not** propose a full (non-partial) composite index covering the whole table: most rows are closed/irrelevant to this report, so indexing them anyway wastes space and slows down every write against a table that already takes ~8,000 inserts/day plus a return-update for nearly every row — a partial index is the textbook right call when the filtering predicate is static and highly selective. I also didn't bother indexing `employees.is_active` — at 12k rows a sequential scan there is already fast; all the cost in this query lives in the 4.2M-row table.

### 3. What EXPLAIN (ANALYZE, BUFFERS) would show

**Before:** a `Seq Scan on checkouts` (none of the existing indexes — PK, `asset_id`, `employee_id` FK indexes — match `returned_at IS NULL` or the `DATE()`-wrapped predicate), a large `Rows Removed by Filter`, a `Sort` node for `ORDER BY due_at` (`Sort Method: external merge` if it spills past `work_mem`), and a hash/subplan join against `employees`. `Buffers: shared hit=... read=...` would show block counts roughly proportional to the full table, with `Execution Time:` near the reported ~8s.

**After:** an `Index Scan` (or `Bitmap Index Scan`) using one of the new partial indexes, `Rows Removed by Filter` near zero (the index's predicate already excludes closed checkouts), a much cheaper join against a pre-filtered row set, and — if the chosen index's column order lines up with the final ordering — no separate `Sort` node at all.

**The one line that confirms the fix worked:** the scan node's `rows=` in the **actual** (not estimated) portion — e.g. `Index Scan using idx_checkouts_open_by_employee_due on checkouts (... actual time=... rows=<small> loops=1)` — dropping from millions to the true open-checkout count. That's the concrete evidence the index is doing the filtering rather than the application; the top-level `Execution Time:` dropping under the 10s timeout is the outcome, but the scan node's actual row count is the diagnostic.

### 4. What breaks first as the table grows (+8,000/day)

The report itself likely stays fast, because a partial index scoped to "currently open" rows grows with the count of *open* checkouts, which is bounded by how much equipment and how many active employees exist — not by total historical row count. What actually breaks first: **autovacuum falling behind** on a table taking this much write volume (every checkout creation and every return is a write, so `n_dead_tup` accumulates), degrading everything on the table, not just this query; and **unbounded storage/bloat growth** on a table that's never pruned, which slows down *other* reports that do scan by date range across all history. Before that becomes a real incident I'd monitor `pg_stat_user_tables` for autovacuum lag and tune its thresholds for this specific hot table, and consider **range-partitioning `checkouts` by `checked_out_at`** (e.g. monthly) so historical partitions can be archived/detached cheaply and other date-range reports only touch relevant partitions.

### 5. What I'd measure before committing

The actual selectivity of `returned_at IS NULL` on the real table: `SELECT count(*) FILTER (WHERE returned_at IS NULL), count(*) FROM checkouts;`. My entire indexing strategy assumes open checkouts are a small fraction of 4.2M rows — true for this domain in general, but an assumption, not a measurement. If in reality a large share of rows are still open (e.g. a data-quality issue, or "returned" tracking adopted late), the partial index stops being small/cheap and I'd need a different approach (e.g. BRIN on `checked_out_at`, or accept the full composite index). I'd also confirm the session/application timezone before trusting the `DATE()`-to-range rewrite is exactly equivalent.

---

## Part D — Production reasoning

### D1. Zero-downtime migration

I'd use the expand/contract pattern across three deploys.

**Deploy 1 — schema, additive only.** Add `location_id` as a **nullable** FK (`ADD COLUMN location_id bigint NULL REFERENCES locations(id)`). In Postgres 11+ this is a fast, metadata-only change — no table rewrite. I'd add the FK constraint as `NOT VALID` first, then `VALIDATE CONSTRAINT` separately (that only takes `SHARE UPDATE EXCLUSIVE`, non-blocking) rather than a single blocking `ADD CONSTRAINT`. App code is unchanged; old code running against the new schema is fine because it simply never references the new nullable column.

**Deploy 2 — app code, dual-write + backfill.** Ship code that writes `location_id` on every new/updated `CheckOut` while still tolerating NULL on read. Roll out across all 4 instances normally — because the column stays nullable, instances running old vs. new code can coexist mid-rollout without either crashing. Separately, backfill the 4.2M existing rows in small batched transactions (a few thousand rows at a time, looping with a short pause between batches) rather than one giant `UPDATE`, so no single transaction holds a long lock or bloats the WAL.

**Deploy 3 — schema, enforce NOT NULL.** Once `SELECT count(*) FROM checkouts WHERE location_id IS NULL` returns 0 and dual-write has been live long enough to guarantee no in-flight request could still produce a NULL, run `SET NOT NULL`. To avoid a full-table scan under lock, I'd first add a `CHECK (location_id IS NOT NULL) NOT VALID` constraint, `VALIDATE` it (cheap lock), then `SET NOT NULL` — Postgres 12+ recognizes the already-validated CHECK and skips re-scanning the table.

**In-flight requests:** safe throughout, because every step is backward-compatible with the *previous* app version until the final contract step, and that step only runs once backfill + dual-write are both confirmed complete.

**What would lock the table if done wrong:** running `ALTER TABLE checkouts ALTER COLUMN location_id SET NOT NULL` directly, without a pre-validated CHECK constraint — that forces Postgres to take an `ACCESS EXCLUSIVE` lock and scan all 4.2M rows to verify no NULLs exist, blocking every read and write against `checkouts` for the scan's duration.

### D2. Latency triage

In order: (1) compare current open/overdue checkout counts against historical baseline — did the working set just get much bigger? (2) check `pg_stat_activity` for long-running or blocked queries/transactions contending for the same table. (3) run `EXPLAIN (ANALYZE, BUFFERS)` on the live query right now and compare the plan shape to what I'd expect — this is the most informative single check. (4) check `last_autovacuum`/`last_analyze`/`n_dead_tup` on `checkouts` — stale statistics from autovacuum falling behind can flip the planner's choice even with data shape roughly unchanged. (5) confirm the index this query relies on still exists and isn't bloated (`pg_stat_user_indexes`). (6) check infra-level metrics — DB CPU/IOPS/connections over the last 48h, since "no deploy" doesn't rule out an infra-side change (autoscaling event, minor-version auto-upgrade, noisy neighbor, backup window). (7) check whether call volume to this specific endpoint changed (a new poller, a dashboard auto-refreshing).

Two most likely causes given no code changed: **(A) the query plan flipped** because the open-checkout row count or its distribution crossed a threshold where the planner's cost model now prefers a worse plan — confirmed by comparing today's `EXPLAIN ANALYZE` row estimates/scan type against a known-good baseline. **(B) autovacuum fell behind** on a frequently-written table, leaving stale planner statistics and/or bloat — confirmed by the vacuum/analyze timestamps and `n_dead_tup`, and cheaply testable by running a manual `ANALYZE checkouts` (non-disruptive) and seeing if latency drops immediately without any other change.

### D3. CI/CD and safety

**On pull request:** lint/format check; spin up ephemeral Postgres + Redis via GitHub Actions `services:`; `makemigrations --check --dry-run` to catch missing migrations; full pytest suite (including the concurrency test) against the ephemeral DB; build the Docker image to catch Dockerfile breakage early. All required as branch-protected status checks, plus one human review.

**On merge to main:** re-run the same suite (protects against a stale/rebased PR), build and tag the image with the commit SHA, push to the registry, auto-deploy to **staging**, run a smoke-test suite there (health check plus a couple of real endpoints).

**What gates production:** staging smoke tests passing, plus a manual approval step (GitHub Actions `environment: production` with required reviewers) — I wouldn't fully automate the prod deploy trigger at this stage. For migration-touching PRs specifically, I'd add an automated check that flags risky raw operations (`ADD COLUMN ... NOT NULL` without a default, a direct `SET NOT NULL`) via `sqlmigrate` output, rather than relying on reviewers to remember the D1 discipline.

**Migrations relative to code going live:** migrations run as a dedicated, serialized step — a one-off job, not something 4 instances race to run on startup — that must complete before the deploy tool starts rolling the new app version across instances. This ordering is what makes the expand/contract pattern in D1 actually safe: the schema change lands and is proven backward-compatible before any instance runs code that assumes it.

**Rollback when the schema is already migrated:** because every migration in this pipeline is required to be backward-compatible with the previous app version (expand first, contract only once nothing depends on the old shape), rolling the **app** back to the previous image/tag is safe on its own in the common case — no schema rollback needed, since the old code was already written to tolerate the expanded schema. If a genuinely bad contract-phase migration shipped, I would not run an automatic reverse migration against a live table (a reverse migration can't undo lost data from a dropped column anyway); instead, pause deploys and ship a forward-fix migration (re-add the column, relax `NOT NULL` back to nullable — both cheap) and redeploy. The real safety net is never shipping a non-backward-compatible migration in the same deploy as the code that needs it, which is exactly what this pipeline's ordering is designed to prevent.
