# ADR-0017: Native installation only; no Docker

**Status:** Accepted · **Date:** 2026-11-22 · **Decided by:** Rob Sanderson · **Recorded by:** Claude

## Context
The plan used Docker Compose as the reference setup and for CI service containers. The development machine has no Docker, and every component (PostgreSQL with `pgvector`, QLever, the Python processes) can be installed natively.

## Decision
No Docker, Compose, container images, or `testcontainers` anywhere: development, CI, and production install everything natively. PostgreSQL 16+ with `pgvector` comes from Postgres.app or Homebrew on macOS and PGDG packages on Debian/Ubuntu. QLever comes from Homebrew on macOS and native packages or a source build on Linux, with the version recorded in `deploy/QLEVER_VERSION`. The `lae` processes run under a Procfile runner (`honcho`) in development and systemd units in production. CI installs PostgreSQL, `pgvector`, and (from Phase 1) QLever natively on the runner. pgBackRest is installed natively and restore verification uses a scratch PostgreSQL cluster.

## Consequences
`deploy/` contains `Qleverfile`, `Procfile`, `systemd/`, `postgres/init.sql`, `pgbackrest/`, and `README.md` with native install instructions. Tests read `LAE_TEST_DATABASE_URL` and create and drop their own database. Phase guides and the spec were updated to remove container references.
