# fastapi-insurance-policies

<p>
  <img alt="Python" src="https://img.shields.io/badge/python-3.12%20%7C%203.14-blue?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.128-009688?logo=fastapi&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <a href="https://github.com/GDelpo/fastapi-insurance-policies/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/GDelpo/fastapi-insurance-policies/actions/workflows/ci.yml/badge.svg"></a>
</p>

> FastAPI microservice that reads insurance policies from a legacy SQL Server core system, caches them in Redis, maps their PDF files to a mounted network share and reports when a day's policies are ready to be sent.

## Features

- REST API under `/api/v1/`; every request is authenticated against an external identity service
- **Policies** with their endorsements, insured, producer and PDF files, filtered by emission date, PDF creation date or date range
- **Readiness** per emission date: how many policies have their PDFs, how many are already uploaded to cloud storage, and a `ready` flag a sender service can wait on
- **PDF path mapping**: UNC paths stored in the database (`\\fileserver\policies\...`) become paths under a read-only mount (`/media/polizas/...`); unavailable files and unmounted shares are flagged instead of returned
- **Storage metadata**: a downstream service reports where each file was uploaded; it is kept in Redis and merged into every policy response
- **Mark as sent**: writes `sn_envio` / `fec_envio` back to the core system after a successful send
- **Redis cache** with startup warmup and a background sync loop for recent days
- **Admin dashboard** (`/dashboard/`, Spanish UI): overview, policy search and detail, readiness check, cache diagnostics and management
- Works against **SQL Server 2008 R2** through FreeTDS (no `OFFSET/FETCH`, `ROW_NUMBER()` pagination)

## Architecture

The service sits between a read-mostly SQL Server database and the services that send policies to customers:

- **Core system (SQL Server)**: the source of truth. The service targets a core insurance system with these tables: `pv_header` (policies and endorsements), `timpresion_pdf` (generated PDF files and their sent flag), `maseg_header` and `mpersona` (insured), `magente` (producers), `mpersona_telef` (emails) and `tramo` (lines of business). All SQL lives in `app/queries.py`; adapt it there if your schema differs.
- **Redis**: cache-aside store for policies, date indexes and storage metadata.
- **File share**: the folder the core system writes PDFs to, mounted read-only into the container. The service never opens the files; it only returns the local path.
- **Identity service** ([`fastapi-identity-service`](https://github.com/GDelpo/fastapi-identity-service) or your own). `GET {IDENTITY_SERVICE_URL}/me` validates the bearer token (results cached in memory for 5 minutes); the dashboard login posts to `{IDENTITY_EXTERNAL_URL}/login`.
- **Downstream consumers** (a "dispatcher" in the code comments): read policies and readiness, upload files to cloud storage, then call the storage-metadata and mark-sent endpoints.

```
app/
├── main.py           # App, lifespan (SQL client, Redis, warmup), middleware, /health
├── config.py         # Pydantic settings (env vars)
├── api.py            # FastAPI router /api/v1/
├── service.py        # Business logic, cache-aside, readiness, background sync
├── repository.py     # Runs queries through the SQL client
├── queries.py        # All SQL + row mappers (SQL Server 2008 R2 compatible)
├── sql_client.py     # pyodbc client (thread pool, retries)
├── cache.py          # Redis CacheManager (policies, indexes, stats)
├── schemas.py        # Pydantic models (camelCase), name/email cleanup, path mapping
├── auth.py           # Token validation against the identity service
├── dependencies.py   # Pagination, filters, role dependencies
├── middleware.py     # ProxyHeadersMiddleware + RequestLoggingMiddleware (pure ASGI)
├── exceptions.py     # ServiceException + handlers
├── logger.py         # JSON / text logging
└── dashboard/        # Jinja2 admin UI (self-hosted Tailwind, Lucide, fonts)
docker/               # Dockerfile (dev), Dockerfile.prod, dev and standalone compose files
```

## Quickstart

### Requirements

- Python 3.12 or newer (the Docker images use 3.12; CI also tests 3.14)
- An ODBC driver manager and a SQL Server driver: FreeTDS + unixODBC on Linux (the images install them), the Microsoft ODBC Driver on Windows (set `SQLSERVER_DRIVER` to its name)
- Redis
- Network access to the SQL Server and the identity service

### Install

```bash
git clone https://github.com/GDelpo/fastapi-insurance-policies.git
cd fastapi-insurance-policies
python -m venv env
source env/bin/activate          # Linux/Mac
# .\env\Scripts\Activate.ps1     # Windows
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit SQLSERVER_HOST, SQLSERVER_DATABASE, SQLSERVER_USER, SQLSERVER_PASSWORD, REDIS_HOST, IDENTITY_SERVICE_URL, ...
```

See [Configuration](#configuration) for the full list.

### Run

```bash
uvicorn app.main:app --reload --port 8082
```

- Dashboard: http://localhost:8082/dashboard/
- API docs: http://localhost:8082/docs
- Health: http://localhost:8082/health

On startup the service checks SQL Server, connects to Redis and preloads the last `CACHE_WARMUP_DAYS` days of policies (730 by default; set it to `0` or a small number while developing).

### Docker

| File | Usage |
|---|---|
| `docker/docker-compose.dev.yml` | Local dev: app with `--reload` on port 8082, Redis, `../app` bind-mounted, PDFs from `POLICIES_PDF_HOST_PATH` (default `pdfs/` at the repo root, git-ignored) |
| `docker/docker-compose.prod.standalone.yml` | Production without a reverse proxy: gunicorn + Uvicorn worker, Redis with AOF, reads `.env` |

```bash
docker compose --env-file .env -f docker/docker-compose.prod.standalone.yml up -d --build
```

The PDF share is mounted read-only. Mount the network share on the host first (for example `\\fileserver\policies` through CIFS), then point `POLICIES_PDF_HOST_PATH` at it:

```yaml
volumes:
  - ${POLICIES_PDF_HOST_PATH:-/media/polizas}:/media/polizas:ro
```

`LOCAL_PATH_PREFIX` must match the container side of that mount. The production image runs a single worker on purpose: each worker would run its own cache warmup.

## Configuration

Environment variables read by `app/config.py` (`.env.example` has a starting set):

| Variable | Description | Default |
|----------|-------------|---------|
| `SQLSERVER_HOST` / `SQLSERVER_PORT` | SQL Server address | `localhost` / `1433` |
| `SQLSERVER_DATABASE` | Database name | `insurance_core` |
| `SQLSERVER_USER` / `SQLSERVER_PASSWORD` | SQL login (read access, plus `UPDATE` on `timpresion_pdf` for mark-sent) | `polizas_reader` / empty |
| `SQLSERVER_DRIVER` / `SQLSERVER_TDS_VERSION` | ODBC driver name and TDS version (7.3 for SQL Server 2008+) | `FreeTDS` / `7.3` |
| `SQLSERVER_TIMEOUT` | Declared, not used by the client yet | `30` |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis connection | `localhost` / `6379` / `1` / none |
| `CACHE_TTL_HOURS` | TTL of cached policies and indexes | `24` |
| `CACHE_WARMUP_DAYS` | Days preloaded on startup (`0` disables) | `730` |
| `CACHE_SYNC_INTERVAL_MINUTES` | Interval of the background sync of recent days | `5` |
| `IDENTITY_SERVICE_URL` | Identity API base used server-to-server (`/me`) | `http://localhost:8080/api/v1` |
| `IDENTITY_EXTERNAL_URL` | Browser-reachable identity API base (Swagger UI `tokenUrl`, dashboard login); falls back to `IDENTITY_SERVICE_URL` | empty |
| `LOCAL_PATH_PREFIX` | Container path where the PDF share is mounted; empty returns the original paths | `/media/polizas` |
| `EXCLUDED_PATHS` | Path prefixes on shares that are not mounted (JSON list); those files are reported as `excluded` | `[]` |
| `CORS_ORIGINS` | Origins allowed to call the API from a browser (JSON list). Empty means same-origin only, which is all the dashboard needs; credentials are never allowed cross-origin | `[]` |
| `DOCS_URL` | Optional link shown in the dashboard sidebar | empty |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | Declared, not enforced yet | `true` / `100` / `60` |
| `DEBUG` / `ENVIRONMENT` | Debug mode; environment defaults to `development` or `production` from `DEBUG` | `false` / derived |
| `LOG_LEVEL` / `LOG_FORMAT` / `LOG_FILE` | Logging (`json` or `text`; optional file) | `INFO` (`DEBUG` with `DEBUG=true`) / `json` / none |
| `PROJECT_NAME` / `PROJECT_VERSION` / `PROJECT_DESCRIPTION` | OpenAPI metadata | `Pólizas Service` / `1.0.0` / `Insurance policy query microservice` |
| `API_PREFIX` | API mount point | `/api/v1` |

## API

Auth column: **service** = role `service` or `admin`; **admin** = the same two roles (kept as a separate dependency). Lists return `{"data": [...], "meta": {...}}` with `limit` / `skip` query parameters.

| Method | Path | Description | Auth |
|--------|------|-------------|------|
| GET | `/api/v1/policies` | List policies; filters `emission_date`, `creation_date`, `start_date` + `end_date`, `has_files`, `has_unavailable`, `sort_by`, `sort_order` | service |
| GET | `/api/v1/policies/readiness?emission_date=` | Readiness for a date (`include_missing`, `refresh`) | service |
| GET | `/api/v1/policies/{nro_pol}` | Policy with endorsements, files, insured and producer | service |
| PATCH | `/api/v1/policies/{policy_id}/files/{file_id}/storage` | Save `storageId` / `storageLink` for a file | service |
| DELETE | `/api/v1/policies/{policy_id}/files/{file_id}/storage` | Remove storage metadata for a file | service |
| POST | `/api/v1/policies/mark-sent` | Set `sn_envio = 1`, `fec_envio = sentDate` for the given policies (only rows not yet marked) | service |
| GET | `/api/v1/producers` / `/api/v1/producers/{code}` | Producers (search by name or email) | admin |
| GET | `/api/v1/insureds` / `/api/v1/insureds/{code}` | Insureds (search by name or email) | admin |
| GET | `/api/v1/cache/stats` | Cache counters and last sync | admin |
| GET | `/api/v1/cache/diagnostics` | Files pending upload by age, unavailable files (scans all keys) | admin |
| POST | `/api/v1/cache/refresh?days=` | Reload the last N days (1-90) | admin |
| POST | `/api/v1/cache/warmup?days=` | Preload the last N days (1-90) | admin |
| DELETE | `/api/v1/cache/policies/{nro_pol}` | Invalidate one policy | admin |
| DELETE | `/api/v1/cache/date/{date}` | Invalidate one emission date | admin |
| DELETE | `/api/v1/cache/clear` | Clear all policy keys | admin |
| GET | `/health` | SQL Server and Redis status (both critical); 503 when either is down | none |

`ready` is `true` only when the date has policies, none is missing a usable PDF and every available file is in storage. The field-by-field definition is in the `ReadinessResponse` docstring (`app/schemas.py`).

Files the core system no longer has are stored with a path that starts with the literal Spanish marker `ARCHIVO NO DISPONIBLE` ("file not available"); the service reports them with `available: false` and `unavailableReason: "unavailable"`.

The dashboard pages are served without server-side auth; their JavaScript keeps the token in `localStorage` and calls the API. Full schema at `/docs`.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check .
pytest
```

The tests replace the policy service and the identity service with mocks and call the API through FastAPI's `TestClient`; no SQL Server or Redis is needed (only the unixODBC library that `pyodbc` loads on import). They cover:

- the policy, readiness, storage-metadata, mark-sent, producer, insured, cache and health endpoints (response shape, pagination meta, validation errors, 404 / 503)
- name and email cleanup of legacy data (title case, particles, legal suffixes, placeholder emails)
- secrets: `repr()` and `model_dump()` of the settings never contain the SQL Server or Redis password
- CORS: no origin allowed by default and never with credentials; identity errors return a generic 401 message
- path mapping: a database path with `..` segments is never mapped into the mount
- dashboard guards: no template interpolates values into inline `on*=` handlers, server values reach `<script>` through `|tojson`, the `esc()` helper escapes quotes

CI (`.github/workflows/ci.yml`) runs lint and tests on Python 3.12 and 3.14.

## License

[MIT](LICENSE) © 2026 Guido Delponte
