# Security review and hardening notes

Reviewed against Backend C2 on 14 November 2026.

## Login protections

- Unknown-email login attempts use the fixed Argon2id `DUMMY_PASSWORD_HASH` and call the same `verify_password` function as known accounts. The dummy value is not a usable account credential.
- The login limiter records failed attempts for both known and unknown emails. Account keys are SHA-256 hashes of the role and normalized email; raw email is not used as the in-memory key.
- Limits currently configured by default: 10 auth requests/IP/endpoint/60 seconds and 5 failed logins/account/900 seconds. Successful login clears the account failure counter.
- The in-memory limiters prune expired entries and cap tracked keys at 20,000. This prevents unbounded growth but does not make limits shared across processes.
- Before running multiple workers/instances, replace both stores with a shared Redis-backed limiter. Behind a proxy, configure trusted proxy IPs explicitly; never accept arbitrary forwarded-IP headers.

## Backend C2 checks retained or adapted

- Separate user and admin authentication dependencies enforce the token role at every protected route.
- A user token must resolve to an existing user whose account status is `Active`; suspended, blocked, and soft-deleted users cannot continue using old access tokens.
- An admin token must resolve to an existing admin record. Admin endpoints depend on the admin-only dependency, so a user token cannot access moderation routes.
- Access and refresh tokens are distinct. Access-token validation now requires an explicit `token_type=access` claim; refresh tokens and untyped JWTs are rejected by protected routes.
- Argon2id remains the password hashing scheme in this backend; Backend C2's bcrypt/passlib implementation was not copied because that would create a second password-hash scheme and complicate compatibility.

## UserBlock migration consolidation

- `docs/migrations/001_user_block.sql` is the only canonical UserBlock migration for existing databases.
- The full schema `docs/skillloop_database.sql` also defines UserBlock for a fresh database; that is schema bootstrap DDL, not a second migration.
- The canonical migration normalizes a legacy quoted `public."UserBlock"` table to `public.userblock`, merges rows if both forms exist, and avoids `CASCADE` when dropping the legacy duplicate.

## Verification

- `PYTHONPATH=. DATABASE_URL=... SECRET_KEY=... pytest -q qa_tests`: 8 passed.
- Python compile check passed for `app/` and `qa_tests/`.
- PostgreSQL integration tests were not run: this execution environment has no PostgreSQL server/client and no installed psycopg driver. Run the suite only against a disposable database named with `test`, following the README instructions.
