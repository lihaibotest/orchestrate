# Offline migration

From any current directory on the target host:

```sh
tar -xzf portable-migration.tar.gz
cd portable-migration
./scripts/check-host-prerequisites.sh
cp config/.env.example .env
./scripts/prepare-runtime-secrets.sh
./scripts/start-all.sh
./scripts/status-all.sh
```

The bundle is an unsigned development bundle: SHA256 detects corruption but does not authenticate the source. Images are loaded locally and startup never invokes `docker pull`, `pip install`, `npm install`, or a network build. Planning Agent state is stored under the named `planning-agent-state` volume and the last request is available at `/last`. Gateway capabilities must be healthy before Orchestrator starts.

Business-agent artifacts remain in the approved relative data directories and are consumed by the Word, Pytest, and Report Agent services.

Use `./scripts/stop-all.sh` to stop services without deleting state. Use `./scripts/migrate-state.sh backup` before moving SQLite state; it quiesces Orchestrator, uses SQLite backup semantics, runs integrity checks, records row counts, and keeps a rollback copy on restore.
