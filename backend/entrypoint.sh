#!/bin/sh
# Self-contained startup wrapper. Runs Alembic to head BEFORE the API process
# starts so the schema is always current with the deployed image, regardless
# of whether the App Platform spec has a PRE_DEPLOY job, whether someone
# remembered to push .do/app.yaml, or which orchestration path (compose,
# K8s, App Platform) brought the container up. `alembic upgrade head` is
# idempotent: a no-op when the DB is already at head, so this is safe to
# re-run on every container start, restart, or deploy.
#
# A migration failure exits non-zero and aborts startup, which is the desired
# behaviour: the orchestrator keeps the previous version healthy instead of
# rolling out an app pointed at a half-migrated schema.
set -eu

echo "[entrypoint] alembic upgrade head"
alembic upgrade head
echo "[entrypoint] migrations at head; starting: $*"
exec "$@"
