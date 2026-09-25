#!/usr/bin/env bash
# Upgrade to another WorkAdventure release, following upstream's procedure:
# fetch that release's docker-compose.prod.yaml, set VERSION, add new .env settings, recreate.
#   scripts/update.sh v1.34.0
#   scripts/update.sh            (shows the latest release and asks)
set -euo pipefail
# shellcheck source=SCRIPTDIR/lib.sh
source "$(dirname "$0")/lib.sh"
[[ -f $ENV_FILE ]] || die "Run ./install.sh first."

current=$(env_get VERSION)
target=${1:-}
if [[ -z $target ]]; then
  target=$(git ls-remote --tags --refs https://github.com/workadventure/workadventure.git 'v1.*' |
    awk -F/ '{print $3}' | sort -V | tail -1)
  echo "Installed: $current   Latest: $target"
  read -rp "Upgrade to $target? [y/N] " ok
  [[ $ok =~ ^[Yy]$ ]] || exit 0
fi

echo "Release notes: https://github.com/workadventure/workadventure/releases/tag/$target"
echo "Upgrade notes: https://github.com/workadventure/workadventure/blob/$target/UPGRADE.md"
echo "Compose changes: https://github.com/workadventure/workadventure/compare/$current...$target (contrib/docker/)"

cp "$DEPLOY/docker-compose.yaml" "$DEPLOY/docker-compose.yaml.$current.bak"
fetch_upstream "$target"
merge_env_template
env_set VERSION "$target"
render_configs
compose pull
compose up -d --remove-orphans
info "Now running WorkAdventure $target. Check the release notes above for manual steps."
