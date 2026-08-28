#!/bin/bash

# Script to perform the initial database setup

set -e

DBNAME="finewiki"
DBUSER="dbuser"
DBPASS="dbpass"

# Start the database
mkdir -p /app/data/pgroonga_test
pg_createcluster 16 finewiki --datadir=/app/data/pgroonga --port=5432 || true
pg_ctlcluster 16 finewiki start
echo "PostgreSQL started."

# Create the user and a new database
sudo -u postgres psql postgres <<SQL
CREATE USER "${DBUSER}" WITH PASSWORD '${DBPASS}';
CREATE DATABASE "${DBNAME}" OWNER "${DBUSER}";
GRANT ALL PRIVILEGES ON DATABASE "${DBNAME}" TO "${DBUSER}";
SQL
echo "If not exists, user '${DBUSER}' and database '${DBNAME}' created."

# Connect to the newly created 'finewiki' database and create the extension
sudo -u postgres psql "${DBNAME}" <<SQL
CREATE EXTENSION pgroonga;
SQL
echo "If not enabled, extension 'pgroonga' created in database '${DBNAME}'."
