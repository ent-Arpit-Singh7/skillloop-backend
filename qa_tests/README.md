# Offline QA checks

Run without PostgreSQL: `python -m unittest discover -s qa_tests -v`

Offline QA also checks dummy-hash verification and per-account limiter behavior. The integration suite under `tests/` still requires a disposable PostgreSQL database named with `test` in its name.
