#!/usr/bin/env bash
# Back up everything needed to restore this install into backups/<date>.tar.gz:
# deploy/.env, users, certificates, and the Docker volumes (maps, chat, logins, variables).
#   scripts/backup.sh
# Restore: stop the stack, untar volumes/*.tar into the matching volumes, copy the files back, start it.
set -euo pipefail
# shellcheck source=SCRIPTDIR/lib.sh
source "$(dirname "$0")/lib.sh"
[[ -f $ENV_FILE ]] || die "Run ./install.sh first."

project=$(env_get COMPOSE_PROJECT_NAME)
stamp=$(date +%Y%m%d-%H%M%S)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$ROOT/backups" "$work/volumes"

info "Stopping the stack briefly so the databases are consistent"
compose stop
for vol in map-storage-data synapse-data authelia-data redisdata; do
  "${DOCKER[@]}" run --rm -v "${project}_$vol:/v:ro" -v "$work/volumes:/out" alpine tar -C /v -cf "/out/$vol.tar" .
done
compose start

cp -r "$ENV_FILE" "$DEPLOY/data" "$CERTS" "$work/"
[[ -d $DEPLOY/wa ]] && "${DOCKER[@]}" run --rm -v "$DEPLOY/wa:/wa:ro" -v "$work:/out" alpine tar -C / -cf /out/letsencrypt.tar wa
tar -C "$work" -czf "$ROOT/backups/$stamp.tar.gz" .
chmod 600 "$ROOT/backups/$stamp.tar.gz"
info "Backup written to backups/$stamp.tar.gz (contains secrets - keep it private)"
