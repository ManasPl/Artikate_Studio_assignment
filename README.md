# Field Asset Check-Out Service

A small Django REST Framework service tracking physical equipment checked out to
and returned by employees, built for the Artikate backend take-home assignment.

## Stack

- Django 4.2 + Django REST Framework
- PostgreSQL 16
- Redis 7 (Celery broker/result backend)
- Celery + Celery Beat (`django-celery-beat` database scheduler)
- pytest / pytest-django

**Auth:** DRF `TokenAuthentication`. Every endpoint under `/api/v1/` requires an
`Authorization: Token <key>` header except `GET /api/v1/health/`.

## Running it (Docker — the intended path)

```bash
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_demo_data

# Create a user and an auth token to call the API with:
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py drf_create_token <username>
```

That prints a token. Call the API with it:

```bash
curl http://localhost:8000/api/v1/health/
curl http://localhost:8000/api/v1/assets/ -H "Authorization: Token <token>"
```

Run the test suite inside the `web` container:

```bash
docker compose exec web pytest
```

Services: `web` (Django, :8000), `db` (Postgres, :5432), `redis` (:6379),
`celery-worker`, `celery-beat`. Beat is scheduled via
`config/settings.py:CELERY_BEAT_SCHEDULE` (hourly) and `django-celery-beat`'s
`DatabaseScheduler`, which syncs that static schedule into the DB on startup.

## Running it locally without Docker

This is how I actually developed and verified the service (see **Known
gaps** — Docker isn't installed in the environment I built this in).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Postgres + Redis running locally, matching the defaults in config/settings.py:
#   DB=artikate  USER=artikate  PASSWORD=artikate  HOST=localhost  PORT=5432
#   Redis at localhost:6379

python manage.py migrate
python manage.py seed_demo_data
python manage.py createsuperuser
python manage.py drf_create_token <username>
python manage.py runserver

# In separate terminals:
celery -A config worker --loglevel=INFO
celery -A config beat --loglevel=INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler

pytest
```

## Assumptions

- **Auth mechanism:** DRF token auth — simplest to demo/script against in a
  time-boxed assignment; JWT or session auth would work equally well against
  the same view logic.
- **No `POST`/`GET /employees/` endpoints.** The endpoint table in the brief
  only specifies `GET /employees/{code}/summary/`; employees are provisioned
  via `seed_demo_data` or the Django admin. Easy to add a
  `ListCreateAPIView` if needed.
- **`current_holder`** on asset retrieval is `null` when available, otherwise
  `{"employee_code": ..., "full_name": ...}` of the open check-out's employee.
- **`mean_hold_duration_days`** is `null` (not `0`) when an employee has no
  returned check-outs yet — there's no meaningful average of zero samples,
  and `0` would be misleading (indistinguishable from "returns take no
  time").
- **`GET /reports/overdue/`** is paginated at 20/page like the other list
  endpoints, since the brief states "list endpoints must be paginated"
  generally, even though the per-endpoint description doesn't repeat it.
- **Lock ordering in `CheckOutCreateView`:** always Asset row, then Employee
  row, consistently across every request, so two concurrent requests can
  never deadlock waiting on each other's locks in reverse order.
- **Concurrency defense in depth:** `select_for_update()` inside
  `transaction.atomic()` is the actual mechanism serializing concurrent
  check-outs of the same asset (rule 7). The partial unique constraint
  (`UniqueConstraint` on `CheckOut.asset` where `returned_at IS NULL`) is a
  second, independent DB-level guarantee — if application logic were ever
  bypassed or buggy, the constraint still makes it physically impossible for
  two open check-out rows to exist against one asset; a stray
  `IntegrityError` from it is mapped to 409 by the custom exception handler.
- **A7 lists four Docker services; A4 requires Celery Beat.** I added a
  fifth `celery-beat` service rather than running beat and worker in the
  same process (not a pattern I'd want to model even in a demo).
- **404 precedence on `POST /checkouts/`:** an unknown `asset_tag` and an
  unknown `employee_code` are both checked (and 404 first) before the
  `is_active`/availability/limit business rules, since those checks are
  meaningless against a record that doesn't exist.
- **`seed_demo_data` idempotency:** assets/employees are matched by their
  unique natural key (`update_or_create`); each seeded check-out is matched
  by `(asset, employee)` before creating, so re-running the command never
  tries to open a second check-out against an asset that already has one
  from a prior run.

## Known gaps

- **`docker compose up` has now been run and verified end-to-end**
  (Docker wasn't installed on the machine I originally built this on; it
  is now). From a fully clean state (`docker compose down -v`, no cached
  volumes) I ran `docker compose up -d --build`, then `migrate`, then
  `seed_demo_data`, and confirmed: all 5 containers healthy, the API
  reachable on `:8000` with correct responses from `/health/`,
  `/reports/overdue/` and `/employees/{code}/summary/` against the seeded
  data, the Celery worker consuming `flag_overdue_checkouts` from Redis
  with confirmed idempotency, and `pytest` passing 19/19 **inside the web
  container** against the containerized Postgres.
  - One real bug this caught: `celery-beat` and `celery-worker` had no
    `restart` policy, so on a truly fresh volume `celery-beat` starts
    before `migrate` has run, queries a table that doesn't exist yet
    (`django_celery_beat_crontabschedule`), and exits — and without a
    restart policy it would just stay dead. Fixed by adding
    `restart: unless-stopped` to `web`, `celery-worker` and `celery-beat`
    in `docker-compose.yml`; verified the fix by tearing everything down
    (`docker compose down -v`) and re-running the exact command sequence
    below from scratch — `celery-beat` now retries automatically and
    comes up clean once `migrate` completes, with no manual restart.
- **No GitHub Actions workflow file is checked in.** Part D3 describes the
  CI/CD pipeline I'd set up in prose; I didn't add an actual
  `.github/workflows/` file since it wasn't listed as a Part A deliverable
  and this repo has no GitHub remote yet.
- **`GET /health/` only reports database connectivity**, per the spec's
  literal wording ("reporting database connectivity"). It doesn't also
  check Redis/Celery.
- **No rate limiting/throttling** on the API — not required by the brief,
  but worth naming as absent.

## Screen recording

_Add your Loom (or other) link here before submitting — this needs to be
recorded by whoever submits this, showing: bringing up the stack, running
migrations + seed, exercising check-out/summary/overdue live, the test
suite passing, and narrating one decision you're least sure about._
