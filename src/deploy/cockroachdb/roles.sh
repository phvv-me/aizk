#!/bin/sh
set -eu

cockroach sql --insecure --host=db:26257 <<'SQL'
CREATE DATABASE IF NOT EXISTS aizk;
CREATE USER IF NOT EXISTS aizk_admin;
CREATE USER IF NOT EXISTS aizk_app;
GRANT admin TO aizk_admin;
GRANT CONNECT ON DATABASE aizk TO aizk_app;
SQL
