#!/bin/sh
set -eu

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname postgres \
  --set=n8n_password="$N8N_POSTGRES_PASSWORD" \
  --set=registry_password="$REGISTRY_POSTGRES_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE n8n LOGIN PASSWORD %L', :'n8n_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'n8n') \gexec
SELECT format('ALTER ROLE n8n LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'n8n_password') \gexec

SELECT format('CREATE ROLE agent_registry LOGIN PASSWORD %L', :'registry_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_registry') \gexec
SELECT format('ALTER ROLE agent_registry LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'registry_password') \gexec

SELECT 'CREATE DATABASE n8n OWNER n8n'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'n8n') \gexec
SELECT 'CREATE DATABASE agent_registry OWNER agent_registry'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'agent_registry') \gexec

REVOKE CONNECT ON DATABASE n8n FROM PUBLIC;
GRANT CONNECT ON DATABASE n8n TO n8n;
REVOKE CONNECT ON DATABASE agent_registry FROM PUBLIC;
GRANT CONNECT ON DATABASE agent_registry TO agent_registry;
SQL
