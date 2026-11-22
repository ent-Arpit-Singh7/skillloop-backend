# SkillLoop Backend

FastAPI + SQLAlchemy backend for the SkillLoop skill-exchange application.

**Current MVP decisions (meeting: 13 November 2026) override any older feature notes in the project documents:**
- Registration, login, logout, profiles, skill discovery, learning requests, connections for session scheduling, sessions, reviews, notifications, reports, and admin moderation remain in scope.
- User account deletion is a **soft delete**. The user row and linked records remain in the database; `account_status` becomes `Deleted`, `deleted_at` is recorded, and all user access is refused.
- Forgot-password is not included.
- One-to-one text chat is included through authenticated REST endpoints. It reuses the existing Conversation/Message tables, creates a conversation on first use of an accepted connection, and does not use WebSockets. Messages cannot be edited or deleted; only the recipient can mark incoming messages as read.
- User moderation is admin-controlled: a user submits a report, an admin reviews it, and the admin can resolve it with `User Blocked` to lock the reported account or `User Suspended` to suspend it. Users cannot directly lock/block other users.
- Profile photos are stored in the public Supabase Storage bucket configured by `SUPABASE_STORAGE_BUCKET` (default `profile-images`). Configure `SUPABASE_URL` and the server-side `SUPABASE_SECRET_KEY` in the backend environment.

## Stack
- Python 3.10+
- FastAPI
- SQLAlchemy 2.x
- PostgreSQL + psycopg 3
- Pydantic v2
- JWT access and refresh tokens

## Project layout

```text
app/
  main.py                 FastAPI application
  models.py               SQLAlchemy models matching the database schema
  schemas.py              Request/response validation
  deps.py                 Authentication and authorization dependencies
  core/                   Settings, security, auth rate limiting
  db/                     SQLAlchemy engine/session
  routers/                Auth, profile, search, requests, sessions,
                          notifications, admin, and user safety
  scripts/                Admin creation helper
docs/
  skillloop_database.sql  Full PostgreSQL schema for a fresh database
  migrations/             Additive migrations for an existing database
tests/                    PostgreSQL integration tests
```

## Setup on Windows (PowerShell)

Open this folder in VS Code, then run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Edit `.env` and set your PostgreSQL connection and a long random secret:

```env
DATABASE_URL=postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/skillloop
SECRET_KEY=replace-with-a-long-random-secret
```

The canonical base schema in `docs/skillloop_database.sql` is copied from the team's `skillloop_database_pgadmin(2).sql`. It creates the original 16 tables. Apply the two additive migrations after it to bring the database to the current backend contract (17 tables and soft-delete support). The second migration stops connection acceptance from creating conversations automatically; the chat API creates them on demand.

For a new development database, create an empty PostgreSQL database and run these files in order using pgAdmin or:

```powershell
psql -U postgres -d skillloop -v ON_ERROR_STOP=1 -f docs/skillloop_database.sql
psql -U postgres -d skillloop -v ON_ERROR_STOP=1 -f docs/migrations/001_user_block.sql
psql -U postgres -d skillloop -v ON_ERROR_STOP=1 -f docs/migrations/002_soft_delete_and_remove_chat.sql
```

For an existing SkillLoop database, back it up first, then run the migrations in order if they have not already been applied:

```powershell
psql -U postgres -d skillloop -v ON_ERROR_STOP=1 -f docs/migrations/001_user_block.sql
psql -U postgres -d skillloop -v ON_ERROR_STOP=1 -f docs/migrations/002_soft_delete_and_remove_chat.sql
```

`docs/migrations/001_user_block.sql` is the **single canonical UserBlock migration**. It safely normalizes the legacy quoted `"UserBlock"` table name and merges duplicate rows when both legacy and canonical tables exist. Do not run or maintain a second `user_blocks` migration. The team base SQL contains 16 tables and does **not** create UserBlock. Migration 001 creates the canonical `public.userblock` table, and must be applied after the base SQL on fresh databases as well as on existing databases that do not already have it.

### PostgreSQL integration tests

The integration suite **drops and recreates the `public` schema** in its target database. Use only a disposable database whose name contains `test`; never point `TEST_DATABASE_URL` at the team's development/production database.

```powershell
createdb skillloop_test
$env:TEST_DATABASE_URL="postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/skillloop_test"
pytest -q tests
```

The test fixture loads the exact team base schema from `docs/skillloop_database.sql`, then applies migrations `001_user_block.sql` and `002_soft_delete_and_remove_chat.sql`. This ensures PostgreSQL triggers, constraints, and the current backend schema contract are exercised together.

Start the API:

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- Swagger UI: http://127.0.0.1:8000/docs
- OpenAPI JSON: http://127.0.0.1:8000/openapi.json
- Health check: http://127.0.0.1:8000/api/health

## Authentication security

- Login performs Argon2 verification against a fixed dummy Argon2id hash when the submitted email does not exist. This keeps the expensive password-check path similar and reduces timing-based email enumeration.
- Login throttling has two independent layers: **10 authentication requests per IP per endpoint per 60 seconds**, plus **5 failed login attempts per normalized account per 15 minutes** for both user and admin login. Account keys are SHA-256 digests, not stored raw email addresses. A successful login clears that account's failure counter.
- Both limiters are in-process memory stores. They are suitable for a single-worker development deployment, but limits are not shared across multiple workers/instances and reset on restart. Use a shared Redis-backed store before horizontal scaling. If deployed behind a reverse proxy, configure trusted proxy handling at the deployment layer; do not blindly trust arbitrary `X-Forwarded-For` headers.
- Access and refresh JWTs have distinct required `token_type` claims. Protected routes reject refresh tokens, untyped tokens, wrong-role tokens, deleted/suspended users, and accounts that no longer exist. Admin routes require an admin-role token and an existing admin record.

## Authentication endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register` | Create a normal user account |
| POST | `/api/auth/login` | Login and receive access + refresh tokens |
| POST | `/api/auth/logout` | Logout response; client must clear both stored tokens |
| POST | `/api/auth/refresh` | Exchange a refresh token for a new token pair |
| POST | `/api/auth/admin/login` | Admin login |
| DELETE | `/api/me` | Soft-delete the currently logged-in user's account |

Send the access token on protected routes:

```http
Authorization: Bearer YOUR_ACCESS_TOKEN
```

### Soft-delete behavior

`DELETE /api/me` does not issue SQL `DELETE`. It sets:
- `User.account_status = 'Deleted'`
- `User.deleted_at = current timestamp`

The database row and linked data remain. The deleted account cannot log in, refresh its token, or use an already-issued access token. Admin user listings show the account as `Deleted` and include `deleted_at`. Ordinary admin status updates cannot reactivate a deleted account.

## Main API areas

| Area | Main endpoints |
|---|---|
| Profile | `GET/PATCH /api/me`, optional `POST /api/me/avatar` |
| Skills | `/api/me/teaching-skills`, `/api/me/learning-skills` |
| Languages | `/api/me/languages`, `/api/languages` |
| Availability | `/api/me/availability` |
| Discovery | `GET /api/search`, `GET /api/users/{user_id}` |
| Learning requests | `POST/GET /api/requests`, accept/reject/cancel request routes |
| Connections | `GET /api/connections`, `GET /api/connections/{connection_id}` |
| Chat | `GET/POST /api/conversations`, conversation detail, messages, and read routes |
| Sessions | `/api/sessions` and session accept/reject/cancel/complete routes |
| Reviews | Session review endpoint and user review listing |
| Notifications | `/api/notifications` and read/unread endpoints |
| Reports | `/api/reports` |
| Admin | `/api/admin/overview`, `/api/admin/users`, categories and report review |
| User reports | `POST /api/reports` (report another user or content) |
| Admin moderation | `GET /api/admin/reports`, `PATCH /api/admin/reports/{report_id}` (admin can choose `User Blocked` to lock the reported account) |

Chat endpoints:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/conversations` | List the caller's conversations with latest message and unread count |
| POST | `/api/conversations` | Create or open a conversation for an accepted connection |
| GET | `/api/conversations/{conversation_id}` | Get conversation and other participant |
| GET | `/api/conversations/{conversation_id}/messages` | List messages in chronological order (`limit`/`offset`) |
| POST | `/api/conversations/{conversation_id}/messages` | Send a text message |
| POST | `/api/conversations/{conversation_id}/read` | Mark incoming messages in that conversation as read |

Only active participants can access a conversation. Messaging is unavailable when either participant has blocked the other. Sending a message creates a `new_message` notification. Chat is REST/refresh based; there is no WebSocket endpoint.

## Tests

The integration tests use PostgreSQL because they exercise database triggers, functions, indexes, and constraints. They deliberately refuse to run unless `TEST_DATABASE_URL` points to a disposable database whose name contains `test`.

Create a separate test database (never use the team's real database):

```powershell
createdb -U postgres skillloop_test
$env:TEST_DATABASE_URL = "postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/skillloop_test"
pytest -q
```

The test fixture drops and rebuilds the `public` schema in that test database. **Never point `TEST_DATABASE_URL` at the real SkillLoop database.**


## Authentication and security hardening

- Unknown user/admin email addresses still run Argon2 verification against a fixed dummy hash before returning the same `401 Incorrect email or password` response as a bad password. This reduces timing-based account enumeration.
- User and admin login have separate per-account failed-attempt limits in addition to the per-IP middleware limit. Defaults are 5 failures per 15 minutes; successful authentication clears the account failure window. These are temporary throttles, not permanent account locks.
- Registration enforces 8–128 character passwords; login input is capped at 128 characters. Argon2 remains the active password hash. Backend C2 used bcrypt and a 72-byte ceiling; bcrypt-specific limits/format were not copied because this project uses Argon2. Existing C2 checks retained/adapted include role-separated user/admin authorization, rejecting inactive users on login and on every authenticated request, self-request prevention, active teaching-skill ownership validation, and receiver-only request acceptance/rejection.
- Rate-limit counters are in-memory and per process. For multi-worker or multi-instance deployment, move both IP and account counters to Redis before production.

## Notes / known limitations

- Refresh tokens are stateless. Logout clears tokens on the client; server-side token revocation/reuse prevention would need a token-session table.
- Authentication limits are process-local; use Redis before multiple API workers/instances.
- Per-IP limits: `AUTH_RATE_LIMIT_REQUESTS` (default 10) per `AUTH_RATE_LIMIT_WINDOW_SECONDS` (default 60) for each auth route.
- Per-account failed-login limits: `LOGIN_ACCOUNT_RATE_LIMIT_ATTEMPTS` (default 5) per `LOGIN_ACCOUNT_RATE_LIMIT_WINDOW_SECONDS` (default 900). User/admin namespaces are separate; successful login clears that account's failure counter. Email identifiers are hashed before being kept in the in-memory limiter.
- Push notifications need a configured provider and credentials.
- Profile photos currently use local disk storage and are optional; decide whether to keep local storage, move to cloud object storage, or remove the feature before production.
- Direct user-to-user block/unblock endpoints are disabled. Moderation follows report → admin review → admin lock/suspension.
