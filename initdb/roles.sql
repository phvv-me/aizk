-- Mounted at /docker-entrypoint-initdb.d/ in docker-compose.yml, so Postgres runs this once
-- against a genuinely fresh volume, before any alembic migration ever connects. It runs as the
-- POSTGRES_USER superuser (docker-compose's `aizk`), the same role every migration runs as, so the
-- default privileges below apply automatically to every table and sequence a migration creates
-- from here on; 0001_init's own per-table GRANT in `apply_scoped_rls` stays a harmless belt over
-- this suspenders. No IF NOT EXISTS guard: Postgres only ever runs
-- /docker-entrypoint-initdb.d/ scripts once, against an empty data directory, never on restart.
--
-- NOSUPERUSER NOBYPASSRLS is the moat itself: the app connects as a role that cannot see past row
-- level security even by accident, while every table it reads and writes is owned by the migration
-- role rather than by aizk_app, so FORCE ROW LEVEL SECURITY has teeth. Rotate the password in prod.
CREATE ROLE aizk_app LOGIN PASSWORD 'aizk_app' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

GRANT USAGE ON SCHEMA public TO aizk_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aizk_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO aizk_app;
