-- Extensions for the lae database. Needs a role allowed to create them (a superuser, or the
-- database owner for the trusted ones). `make up` applies this file on every run; every
-- statement is idempotent. Production applies it once after `createdb` (deploy/README.md).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
