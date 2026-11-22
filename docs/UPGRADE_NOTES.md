# SkillLoop backend update notes (v1.2)

## Meeting decisions applied
- User deletion is now a soft delete: the User row is retained, `account_status` is set to `Deleted`, and `deleted_at` is recorded. Existing access tokens are refused by the shared authentication dependency; login and refresh are also refused.
- Forgot-password is not implemented.
- WhatsApp-style chat REST and WebSocket endpoints have been removed from the active API. Connections remain for learning requests and session scheduling only. The legacy Conversation/Message tables remain in the database schema to avoid destructive changes to linked data. New connection acceptance no longer creates Conversation rows.
- Admin suspension/blocking remains supported. User-to-user blocking is left in place pending the team's discussion about whether it duplicates admin moderation.
- Profile-photo upload remains optional/undecided; the existing local-disk implementation is retained for now.

## Database migration
Run `docs/migrations/002_soft_delete_and_remove_chat.sql` against the existing SkillLoop database after taking a backup. It adds `deleted_at`, expands the account-status check constraint, and replaces the connection-acceptance function so it does not create new chat conversations.

## Validation performed in the build environment
- `python -m compileall -q app tests`: PASS.
- FastAPI app import and SQLite metadata creation: PASS (an import-only psycopg shim was used because psycopg is not installed in this build environment).
- Soft-delete API smoke test: PASS. The User row remained in the database with `account_status='Deleted'` and a non-null `deleted_at`.
- Login after deletion: HTTP 403 (PASS).
- Refresh-token exchange after deletion: HTTP 403 (PASS).
- Existing access-token request after deletion: HTTP 403 (PASS).
- OpenAPI generation: PASS; 48 HTTP paths and no active chat message or WebSocket routes.
- Full PostgreSQL pytest integration suite: NOT RUN. It requires psycopg and a disposable PostgreSQL database whose name contains `test`; do not run it against the real project database.
