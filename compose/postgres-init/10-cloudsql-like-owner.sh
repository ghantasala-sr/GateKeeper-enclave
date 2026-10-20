#!/bin/sh
# Make Postgres look like Cloud SQL: the database owner is NOT a superuser.
#
# The official image's POSTGRES_USER is a superuser, and superusers skip
# row-level security entirely, which hides bugs that only appear on Cloud SQL
# (whose `postgres` user has CREATEROLE/CREATEDB but not SUPERUSER).
# This script runs once, as that superuser, on a fresh data directory.
set -eu

: "${GATEKEEPER_OWNER_PASSWORD:?set GATEKEEPER_OWNER_PASSWORD}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE gatekeeper_owner LOGIN CREATEROLE CREATEDB PASSWORD '${GATEKEEPER_OWNER_PASSWORD}';
CREATE DATABASE gatekeeper OWNER gatekeeper_owner;
SQL

# On Cloud SQL the admin user creates extensions; do the same here.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname gatekeeper -c "CREATE EXTENSION IF NOT EXISTS vector;"
