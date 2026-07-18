#!/usr/bin/env bash
# PostgreSQL runs this script while initializing an empty Compose volume. Operators also run
# it after a role restore so current deployment secrets replace archived password hashes.
# The migration owner remains separate from both long-lived application roles.
set -euo pipefail

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${AIZK_APP_PASSWORD:?AIZK_APP_PASSWORD is required}"
: "${AIZK_LOGTO_DB_PASSWORD:?AIZK_LOGTO_DB_PASSWORD is required}"

psql \
  -v ON_ERROR_STOP=1 \
  -v admin_user="$POSTGRES_USER" \
  -v admin_password="$POSTGRES_PASSWORD" \
  -v app_password="$AIZK_APP_PASSWORD" \
  -v logto_password="$AIZK_LOGTO_DB_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<-'SQL'
	SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'admin_user', :'admin_password')
	\gexec

	SELECT format(
	  'CREATE ROLE aizk_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
	  :'app_password'
	)
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aizk_app')
	\gexec
	ALTER ROLE aizk_app LOGIN PASSWORD :'app_password'
	  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

	SELECT format(
	  'CREATE ROLE logto LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
	  :'logto_password'
	)
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'logto')
	\gexec
	ALTER ROLE logto LOGIN PASSWORD :'logto_password'
	  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

	GRANT USAGE ON SCHEMA public TO aizk_app;

	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aizk_app;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO aizk_app;

	SELECT 'CREATE DATABASE logto OWNER logto'
	WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'logto')
	\gexec
	ALTER DATABASE logto OWNER TO logto;
SQL
