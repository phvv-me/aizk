#!/usr/bin/env bash
# Mounted at /docker-entrypoint-initdb.d/ in docker-compose.yml, so Postgres runs this once
# against a genuinely fresh volume, before any alembic migration ever connects. It runs as the
# POSTGRES_USER superuser (docker-compose's `aizk`), the same role every migration runs as, so the
# default privileges below apply automatically to every table and sequence a migration creates
# from here on; 0001_init's own per-table GRANT in `apply_scoped_rls` stays a harmless belt over
# this suspenders. No IF NOT EXISTS guard: Postgres only ever runs
# /docker-entrypoint-initdb.d/ scripts once, against an empty data directory, never on restart.
#
# NOSUPERUSER NOBYPASSRLS is the moat itself: the app connects as a role that cannot see past row
# level security even by accident, while every table it reads and writes is owned by the migration
# role rather than by aizk_app, so FORCE ROW LEVEL SECURITY has teeth.
#
# A .sh script rather than a plain .sql file so the password is read from AIZK_APP_PASSWORD, the
# same variable name `config/settings.py`'s app_password field reads and `.env.example` documents,
# with the bash "${VAR:-default}" fallback docker-compose.yml's own interpolations use, rather
# than a literal baked into a committed file. docker-compose.yml's `env_file:` on the db service
# is what makes AIZK_APP_PASSWORD visible to this shell in the first place.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE ROLE aizk_app LOGIN PASSWORD '${AIZK_APP_PASSWORD:-aizk_app}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

	GRANT USAGE ON SCHEMA public TO aizk_app;

	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aizk_app;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO aizk_app;
SQL
