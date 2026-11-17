# Contributor rules (people and agents)

- Python 3.10+. Igris must start and pass its tests on the standard library alone. Third-party
  libraries are allowed where they clearly improve speed, accuracy or design (owner's decision,
  2026-11-15), on three conditions: the import is optional with a standard-library fallback, the
  library is listed with a pinned version in `requirements-optional.txt` and in README.md, and it
  never weakens a security rule (owner-scoped data, untrusted content, no secrets, no paid usage
  without `IGRIS_ALLOW_PAID`). Never `eval` or `sympify` raw text. Tests use `pytest`.
- English for code, comments, docstrings, CLI text and documentation. Exception: the web interface
  (`igris/static/`) and `igris/roadmap.json` are user-facing and written in Turkish.
- Every module ships with tests in `tests/test_<module>.py`. Tests never call a real model, the
  network or a real CLI; inject fakes (for example a `runner` callable instead of `subprocess.run`).
- Keep modules independent: a module may import only the modules listed as its dependencies in
  README.md's architecture table (modules listed with "none" import nothing from `igris`).
- Do not store secrets in the repository; settings come from `IGRIS_*` environment variables
  and secrets from the OS keyring (environment variables as a fallback).
