#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ACTION="${1:-backup}"
SOURCE_DB="${ORCHESTRATOR_DATABASE_PATH:-$ROOT_DIR/state/orchestrator.sqlite3}"
BACKUP_DIR="${STATE_BACKUP_DIR:-$ROOT_DIR/state/backups}"
PG_BACKUP_FILE="${POSTGRES_BACKUP_FILE:-$BACKUP_DIR/n8n.dump}"
mkdir -p "$BACKUP_DIR"
case "$ACTION" in
  backup)
    docker compose -f "$ROOT_DIR/docker-compose.migration.yml" stop orchestrator-agent >/dev/null 2>&1 || true
    python3 - "$SOURCE_DB" "$BACKUP_DIR/orchestrator.sqlite3" <<'PY'
import sqlite3,sys
source,target=sys.argv[1:]
src=sqlite3.connect(source); dst=sqlite3.connect(target)
src.backup(dst); dst.commit()
if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise SystemExit('sqlite integrity check failed')
tables=[row[0] for row in dst.execute("select name from sqlite_master where type='table'")]
counts={name: dst.execute(f'select count(*) from "{name}"').fetchone()[0] for name in tables}
open(target+'.row-counts.json','w',encoding='utf-8').write(__import__('json').dumps(counts,sort_keys=True)+'\n')
src.close(); dst.close()
PY
    echo "SQLite backup complete: state/backups/orchestrator.sqlite3"
    if docker exec postgres pg_dump -U n8n -d n8n --format=custom > "$PG_BACKUP_FILE" 2>/dev/null; then
      docker exec postgres psql -U n8n -d n8n -At -c "select 'workflow_entity', count(*) from workflow_entity union all select 'execution_entity', count(*) from execution_entity" > "$PG_BACKUP_FILE.row-counts"
      echo "Postgres backup complete: state/backups/n8n.dump"
    else
      echo "Postgres backup skipped: postgres service unavailable" >&2
    fi
    ;;
  restore)
    test -f "$BACKUP_DIR/orchestrator.sqlite3" || { echo 'backup missing' >&2; exit 1; }
    test ! -e "$SOURCE_DB" || cp "$SOURCE_DB" "$SOURCE_DB.rollback"
    cp "$BACKUP_DIR/orchestrator.sqlite3" "$SOURCE_DB"
    python3 - "$SOURCE_DB" <<'PY'
import sqlite3,sys
db=sqlite3.connect(sys.argv[1])
assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
PY
    ;;
  restore-postgres)
    test -f "$PG_BACKUP_FILE" || { echo 'postgres backup missing' >&2; exit 1; }
    docker exec -i postgres pg_restore -U n8n -d n8n --clean --if-exists < "$PG_BACKUP_FILE"
    ;;
  *) echo "usage: $0 backup|restore|restore-postgres" >&2; exit 2;;
esac
