#!/usr/bin/env bash
# Install (or re-configure) the official WorkAdventure on a Raspberry Pi 4 or any arm64/amd64 Linux box.
#   ./install.sh                                              interactive
#   DOMAIN=wa.example.com ADMIN_EMAIL=me@example.com ADMIN_PASSWORD=... ./install.sh -y
# Safe to re-run: existing secrets, users and maps are kept.
set -euo pipefail
# shellcheck source=scripts/lib.sh
source "$(dirname "$0")/scripts/lib.sh"

DEFAULT_VERSION=v1.33.10
yes=false; [[ ${1:-} == -y ]] && yes=true
ask() { # ask VAR "Question" default
  local __v=$1 q=$2 d=${3:-} a
  if $yes || [[ -n ${!__v:-} ]]; then printf -v "$__v" '%s' "${!__v:-$d}"; return; fi
  read -rp "$q${d:+ [$d]}: " a
  printf -v "$__v" '%s' "${a:-$d}"
}

# ── 1. Platform ─────────────────────────────────────────────────────────────
case $(uname -m) in
  aarch64|arm64|x86_64) ;;
  armv7l|armv6l) die "WorkAdventure images are 64-bit only. Flash Raspberry Pi OS (64-bit) and try again." ;;
  *) die "Unsupported CPU architecture $(uname -m)." ;;
esac

missing=()
for c in curl openssl python3 envsubst; do command -v "$c" >/dev/null || missing+=("$c"); done
if ((${#missing[@]})); then
  command -v apt-get >/dev/null || die "Please install: ${missing[*]}"
  info "Installing ${missing[*]}"
  pkgs=("${missing[@]/envsubst/gettext-base}")
  sudo apt-get update -qq && sudo apt-get install -y -qq "${pkgs[@]}"
fi

if ! command -v docker >/dev/null; then
  $yes || { read -rp "Docker is not installed. Install it now with get.docker.com? [Y/n] " ok; [[ ${ok:-Y} =~ ^[Yy]$ ]] || exit 1; }
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
  DOCKER=(sudo docker)
fi
"${DOCKER[@]}" compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the docker-compose-plugin package)."

mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if ((mem_mb < 3500)); then
  echo "⚠ This machine has ${mem_mb} MB RAM. The stack idles at about 1 GB; a 4 GB+ Pi 4 is recommended."
  echo "  On a 2 GB Pi, enlarge swap first: sudo dphys-swapfile swapoff && sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile && sudo dphys-swapfile setup && sudo dphys-swapfile swapon"
fi

# ── 2. Questions ────────────────────────────────────────────────────────────
first_run=false; [[ -f $ENV_FILE ]] || first_run=true
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

DOMAIN=${DOMAIN:-$(env_get DOMAIN)}
if [[ -z $DOMAIN ]] && ! $yes; then
  echo
  echo "Where will people open WorkAdventure?"
  echo "  • A domain pointing at this machine (ports 80/443 forwarded) gets a free Let's Encrypt certificate."
  echo "  • Or press Enter to use the LAN address $LAN_IP. Browsers will need to trust deploy/certs/ca.crt once."
fi
ask DOMAIN "Domain or IP" "$LAN_IP"
DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN%%/*}
[[ -n $DOMAIN ]] || die "A domain or IP is required."

ADMIN_EMAIL=${ADMIN_EMAIL:-$(env_get ACME_EMAIL)}
ask ADMIN_EMAIL "Admin e-mail (your login; also used for Let's Encrypt notices)" ""
[[ $ADMIN_EMAIL == *@* ]] || die "A valid admin e-mail is required."
ADMIN_EMAIL=${ADMIN_EMAIL,,}

VERSION=${VERSION:-$(env_get VERSION)}
[[ -n $VERSION && $VERSION != master ]] || VERSION=$DEFAULT_VERSION

# ── 3. Official files + .env ───────────────────────────────────────────────
mkdir -p "$DEPLOY/data" "$GEN" "$CERTS"
chmod 700 "$DEPLOY/data" "$GEN"
if [[ ! -f $DEPLOY/docker-compose.yaml || $(env_get VERSION) != "$VERSION" ]]; then
  info "Downloading the official docker-compose.prod.yaml for WorkAdventure $VERSION"
  fetch_upstream "$VERSION"
fi
if $first_run; then
  cp "$DEPLOY/.env.template" "$ENV_FILE"
else
  merge_env_template
fi
chmod 600 "$ENV_FILE"

env_set VERSION "$VERSION"
env_set DOMAIN "$DOMAIN"
env_set LAN_IP "$LAN_IP"
env_set ACME_EMAIL "$ADMIN_EMAIL"
env_set COMPOSE_PROJECT_NAME workadventure
[[ -n ${HTTP_PORT:-} ]] && env_set HTTP_PORT "$HTTP_PORT"
[[ -n ${HTTPS_PORT:-} ]] && env_set HTTPS_PORT "$HTTPS_PORT"

# Secrets: generated once, kept on re-runs.
env_default SECRET_KEY "$(rand)"
env_default ROOM_API_SECRET_KEY "$(rand)"
env_default MAP_STORAGE_AUTHENTICATION_USER admin
env_default MAP_STORAGE_AUTHENTICATION_PASSWORD "$(rand 12)"
env_default MAP_STORAGE_AUTHENTICATION_TOKEN "$(rand)"
env_default OPENID_CLIENT_SECRET "$(rand)"
env_default AUTHELIA_SYNAPSE_CLIENT_SECRET "$(rand)"
env_default AUTHELIA_JWT_SECRET "$(rand)"
env_default AUTHELIA_SESSION_SECRET "$(rand)"
env_default AUTHELIA_STORAGE_KEY "$(rand)"
env_default AUTHELIA_OIDC_HMAC_SECRET "$(rand)"
env_default SYNAPSE_REGISTRATION_SECRET "$(rand)"
env_default SYNAPSE_MACAROON_SECRET "$(rand)"
env_default SYNAPSE_FORM_SECRET "$(rand)"
env_default MATRIX_ADMIN_USER wa-admin
env_default MATRIX_ADMIN_PASSWORD "$(rand 16)"
env_default LIVEKIT_API_KEY workadventure
env_default LIVEKIT_API_SECRET "$(rand)"
env_default TURN_STATIC_AUTH_SECRET "$(rand)"

# Wiring between the services (depends on DOMAIN, so always rewritten).
env_set OPENID_CLIENT_ID workadventure
env_set OPENID_CLIENT_ISSUER "https://$DOMAIN/auth"
env_set OPENID_USERNAME_CLAIM name
env_set MATRIX_API_URI "http://synapse:8008/"
env_set MATRIX_PUBLIC_URI "https://$DOMAIN/"
env_set MATRIX_DOMAIN "$DOMAIN"
env_set LIVEKIT_HOST "https://$DOMAIN/livekit"
env_set TURN_SERVER "turn:$DOMAIN:3478?transport=udp,turn:$DOMAIN:3478?transport=tcp"
env_set MAP_STORAGE_ENABLE_BEARER_AUTHENTICATION true
env_set MAP_STORAGE_ENABLE_BASIC_AUTHENTICATION true
if is_lan_host "$DOMAIN"; then
  env_set TURN_EXTERNAL_IP "$LAN_IP"
else
  public_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}')
  [[ -n $public_ip ]] || die "$DOMAIN does not resolve. Point its DNS A record at your public IP first."
  env_set TURN_EXTERNAL_IP "$public_ip/$LAN_IP"
fi

if $first_run; then
  # Self-hosted and free: switch off everything that needs a paid or third-party account.
  # (Admin API/SaaS, Sentry and recording are already empty in the template.)
  env_set JITSI_URL ""                 # meeting areas use our LiveKit instead of meet.jit.si
  env_set BBB_URL ""; env_set BBB_SECRET ""
  env_set KLAXOON_ENABLED false
  env_set GOOGLE_DRIVE_PICKER_CLIENT_ID ""; env_set GOOGLE_DRIVE_PICKER_APP_ID ""
  env_set ENABLE_TELEMETRY false
  # Guests may walk in; logging in (Authelia) unlocks the Matrix chat and, for listed users, the map editor.
  env_set DISABLE_ANONYMOUS false
  env_set MAP_EDITOR_ALLOW_ALL_USERS false
  env_set MAP_EDITOR_ALLOWED_USERS ""
  env_set LOG_LEVEL ERROR
fi

# ── 4. Certificates and generated configs ──────────────────────────────────
is_lan_host "$DOMAIN" && make_lan_certs "$DOMAIN"
render_configs

# ── 5. First admin account ─────────────────────────────────────────────────
if [[ ! -s $USERS_FILE ]]; then
  info "Creating the admin login $ADMIN_EMAIL"
  compose pull -q synapse
  if [[ -n ${ADMIN_PASSWORD:-} ]]; then
    printf '%s\n' "$ADMIN_PASSWORD" | "$ROOT/bin/wa-user" add "$ADMIN_EMAIL" --name admin --editor
  else
    "$ROOT/bin/wa-user" add "$ADMIN_EMAIL" --name admin --editor
  fi
fi

# ── 6. Start ───────────────────────────────────────────────────────────────
info "Pulling images (the first time takes a while on a Pi)"
compose pull -q
compose up -d --remove-orphans

info "Waiting for the chat server"
for _ in $(seq 90); do
  compose exec -T synapse curl -fsS http://localhost:8008/health >/dev/null 2>&1 && break
  sleep 2
done
compose exec -T synapse register_new_matrix_user -c /config/homeserver.yaml -a \
  -u "$(env_get MATRIX_ADMIN_USER)" -p "$(env_get MATRIX_ADMIN_PASSWORD)" http://localhost:8008 >/dev/null 2>&1 \
  && info "Created the Matrix bot account $(env_get MATRIX_ADMIN_USER)" || true

# ── 7. Office map (maps/office) ────────────────────────────────────────────
curl_opts=(-fsS -o /dev/null --max-time 5 --resolve "$DOMAIN:${HTTPS_PORT:-443}:127.0.0.1")
is_lan_host "$DOMAIN" && curl_opts+=(--cacert "$CERTS/ca.crt")
# Only on the first install (older installs have the starter kit's marker); later: scripts/upload-map.sh
if [[ ! -f $DEPLOY/data/.office-map-uploaded && ! -f $DEPLOY/data/.starter-map-uploaded ]]; then
  info "Waiting for map-storage"
  for _ in $(seq 60); do
    code=$(curl "${curl_opts[@]}" -w '%{http_code}' "https://$DOMAIN/map-storage/" 2>/dev/null || true)
    [[ $code == 401 || $code == 200 ]] && break
    sleep 2
  done
  if "$ROOT/scripts/upload-map.sh" --start; then
    touch "$DEPLOY/data/.office-map-uploaded"
  else
    echo "⚠ Could not upload the office map; using the GitHub-hosted starter kit instead. Retry with scripts/upload-map.sh --start"
  fi
fi

# ── Done ───────────────────────────────────────────────────────────────────
cat <<MSG

✔ WorkAdventure $VERSION is running: https://$DOMAIN/
   Log in as $ADMIN_EMAIL to use the chat and the map editor (press E in the game).
MSG
if is_lan_host "$DOMAIN"; then cat <<MSG

   LAN mode: install deploy/certs/ca.crt as a trusted certificate authority on each device
   (or accept the browser warning), otherwise camera, microphone and chat won't work.
MSG
fi
cat <<MSG

Ports to open on your router/firewall for people outside your network:
   TCP 80, 443                   web (80 is only for Let's Encrypt and the redirect)
   TCP 7881, UDP 7882            meetings (LiveKit)
   TCP/UDP 3478, UDP 49160-49200 TURN relay for proximity video
   TCP 50051                     Room API (optional, for scripts/integrations)

Users:   bin/wa-user add <email> [--editor] | list | passwd | delete | editor <email> on|off
Maps:    scripts/upload-map.sh [--start] [maps folder, starter-kit project or git URL] [directory]
         map-storage UI: https://$DOMAIN/map-storage/  (user/password: MAP_STORAGE_AUTHENTICATION_* in deploy/.env)
Upgrade: scripts/update.sh      Backup: scripts/backup.sh      Logs: cd deploy && docker compose logs -f
MSG
