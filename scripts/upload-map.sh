#!/usr/bin/env bash
# Build maps and upload them to this server's map-storage, the same way the official
# "npm run upload" does (docs: map-building/tiled-editor/publish/wa-hosted).
#
#   scripts/upload-map.sh [--start] [--reset-areas] [source] [directory]
#
#   source     default: maps/office (this repo's office). Can be:
#              - a folder with only .tmj maps (+ optional <map>.wam.template, + optional tilesets/ with extra
#                tileset images): built with the pinned starter kit's tilesets and build tools
#              - a folder or git URL of a full map-starter-kit project (has package.json)
#   directory  where the maps go on the server (default: office) -> https://<domain>/~/<directory>/<map>.wam
#   --start    also make the (first) uploaded map the start room
#   --reset-areas  upload the <map>.wam.template even if the map exists, replacing its areas (this discards
#                  changes made with the in-browser map editor)
#
# A <map>.wam.template (areas such as meeting rooms) is only uploaded when that map doesn't exist on the server
# yet, so re-uploading never overwrites what people changed with the in-browser map editor.
set -euo pipefail
# shellcheck source=SCRIPTDIR/lib.sh
source "$(dirname "$0")/lib.sh"
[[ -f $ENV_FILE ]] || die "Run ./install.sh first."

STARTER_KIT_REPO=https://github.com/workadventure/map-starter-kit.git
STARTER_KIT_TAG=v3.3.18

make_start=false
reset_areas=false
while [[ ${1:-} == --* ]]; do
  case $1 in
    --start) make_start=true ;;
    --reset-areas) reset_areas=true ;;
    *) die "Unknown option $1" ;;
  esac
  shift
done
src=${1:-$ROOT/maps/office}
dir=${2:-office}
domain=$(env_get DOMAIN)
token=$(env_get MAP_STORAGE_AUTHENTICATION_TOKEN)
[[ -n $token ]] || die "MAP_STORAGE_AUTHENTICATION_TOKEN is empty in deploy/.env"

curl_opts=(-s -o /dev/null -w '%{http_code}' --max-time 10)
is_lan_host "$domain" && curl_opts+=(--cacert "$CERTS/ca.crt")

args=(--rm -e UPLOAD_MODE=MAP_STORAGE -e "MAP_STORAGE_URL=https://$domain/map-storage/"
      -e "MAP_STORAGE_API_KEY=$token" -e "UPLOAD_DIRECTORY=$dir"
      -e "NODE_EXTRA_CA_CERTS=$(env_get NODE_EXTRA_CA_CERTS)" -v "$CERTS:/certs:ro")
is_ip "$domain" || args+=(--add-host "$domain:host-gateway")

first_map=""
if [[ -d $src && ! -f $src/package.json ]]; then
  # Maps-only folder: drop its maps into the pinned starter kit and build that.
  shopt -s nullglob
  tmjs=("$src"/*.tmj)
  ((${#tmjs[@]})) || die "No .tmj maps in $src"
  first_map=$(basename "${tmjs[0]}" .tmj)
  wams=""
  for tpl in "$src"/*.wam.template; do
    name=$(basename "$tpl" .wam.template)
    code=$(curl "${curl_opts[@]}" "https://$domain/map-storage/$dir/$name.wam" || true)
    if [[ $code == 404 ]]; then
      wams+=" $name"
      info "$name.wam: first upload, including its areas (meeting rooms, links...)"
    elif $reset_areas; then
      wams+=" $name"
      info "$name.wam exists (HTTP $code): replacing its areas (--reset-areas)"
    else
      info "$name.wam already exists on the server (HTTP $code): keeping it, so map-editor changes are preserved"
    fi
  done
  args+=(-v "$(cd "$src" && pwd):/overlay:ro" -e "DOMAIN=$domain" -e "WAMS=$wams" -e "REPO=$STARTER_KIT_REPO" -e "TAG=$STARTER_KIT_TAG")
  # shellcheck disable=SC2016  # expanded inside the container
  fetch='apk add --no-cache git >/dev/null && echo "  fetching the starter kit $TAG" && git -c advice.detachedHead=false clone -q --depth 1 --branch "$TAG" "$REPO" /map
    rm -f /map/*.tmj /map/office.png /map/conference.png
    cp /overlay/*.tmj /map/
    if [ -d /overlay/tilesets ]; then cp /overlay/tilesets/* /map/tilesets/; fi
    mkdir -p /map/public
    for n in $WAMS; do sed "s/__DOMAIN__/$DOMAIN/g" "/overlay/$n.wam.template" > "/map/public/$n.wam"; done'
elif [[ -d $src ]]; then
  args+=(-v "$(cd "$src" && pwd):/src:ro")
  fetch='cp -r /src /map'
else
  args+=(-e "REPO=$src")
  # shellcheck disable=SC2016  # expanded inside the container
  fetch='apk add --no-cache git >/dev/null && git clone --depth 1 "$REPO" /map'
fi

info "Building and uploading $src to https://$domain/~/$dir/ (takes a few minutes on a Pi)"
"${DOCKER[@]}" run "${args[@]}" node:22-alpine sh -euc "
    $fetch
  cd /map
  rm -f .env.secret
  # pngquant-bin has no prebuilt arm64 musl binary, so npm ci compiles it from source
  apk add --no-cache pngquant build-base libpng-dev zlib-dev python3 >/dev/null
  echo \"  installing build tools (npm ci, up to a few minutes on a Pi)\"
  npm ci --no-audit --no-fund --loglevel=error || true
  rm -rf node_modules/pngquant-bin
  npm run build
  echo \"  building and uploading\"
  npm run upload
"

if $make_start; then
  [[ -n $first_map ]] || first_map=office
  env_set START_ROOM_URL "/~/$dir/$first_map.wam"
  compose up -d play >/dev/null 2>&1
  info "Start room is now https://$domain/~/$dir/$first_map.wam"
fi
info "Done. Maps: https://$domain/map-storage/ (login with MAP_STORAGE_AUTHENTICATION_USER/PASSWORD from deploy/.env)"
