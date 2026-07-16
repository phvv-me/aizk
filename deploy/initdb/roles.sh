#!/usr/bin/env bash
# PostgreSQL runs this script once while initializing an empty Compose volume. The
# migration owner remains separate from both long-lived application roles. `aizk_app`
# can reach Aizk tables only through forced row security. `logto` owns only the separate
# Logto database, so an identity-service compromise cannot bypass Aizk authorization.
set -euo pipefail

: "${AIZK_APP_PASSWORD:?AIZK_APP_PASSWORD is required}"
: "${AIZK_LOGTO_DB_PASSWORD:?AIZK_LOGTO_DB_PASSWORD is required}"

psql \
  -v ON_ERROR_STOP=1 \
  -v app_password="$AIZK_APP_PASSWORD" \
  -v logto_password="$AIZK_LOGTO_DB_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" <<-'SQL'
	CREATE ROLE aizk_app LOGIN PASSWORD :'app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
	CREATE ROLE logto LOGIN PASSWORD :'logto_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

	GRANT USAGE ON SCHEMA public TO aizk_app;

	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aizk_app;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public
	  GRANT USAGE, SELECT ON SEQUENCES TO aizk_app;

	CREATE DATABASE logto OWNER logto;
SQL
