# Shared helpers for install.sh, bin/wa-user and scripts/*.sh. Source it, don't run it.
# shellcheck shell=bash

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DEPLOY="$ROOT/deploy"
ENV_FILE="$DEPLOY/.env"
GEN="$DEPLOY/generated"
CERTS="$DEPLOY/certs"
USERS_FILE="$DEPLOY/data/users.tsv"   # email <TAB> bcrypt hash <TAB> display name <TAB> uuid
UPSTREAM_RAW="https://raw.githubusercontent.com/workadventure/workadventure"

die() { echo "✘ $*" >&2; exit 1; }
info() { echo "→ $*"; }

# Use sudo for docker when the current user isn't in the docker group yet.
if docker info >/dev/null 2>&1; then DOCKER=(docker)
elif sudo -n true 2>/dev/null || [[ -t 0 ]]; then DOCKER=(sudo docker)
else DOCKER=(docker); fi
compose() { (cd "$DEPLOY" && "${DOCKER[@]}" compose "$@"); }

rand() { openssl rand -hex "${1:-32}"; }

# .env access that doesn't source the file (upstream values may contain shell metacharacters).
env_get() { [[ -f $ENV_FILE ]] && grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- || true; }
env_set() {
  local key=$1 val=$2
  if grep -q "^$key=" "$ENV_FILE"; then
    local tmp; tmp=$(mktemp)
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k && !done {print k "=" v; done=1; next} {print}' "$ENV_FILE" >"$tmp"
    cat "$tmp" >"$ENV_FILE" && rm -f "$tmp"
  else
    printf '%s=%s\n' "$key" "$val" >>"$ENV_FILE"
  fi
}
# Set only if missing or empty, so re-running the installer keeps existing secrets.
env_default() { [[ -n $(env_get "$1") ]] || env_set "$1" "$2"; }

is_lan_host() { [[ $1 =~ ^[0-9.]+$ || $1 == *.local || $1 == *.lan || $1 == *.home.arpa || $1 == localhost ]]; }
is_ip() { [[ $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

# Download the official docker-compose.prod.yaml and .env template for a release.
fetch_upstream() {
  local version=$1
  curl -fsSL "$UPSTREAM_RAW/$version/contrib/docker/docker-compose.prod.yaml" -o "$DEPLOY/docker-compose.yaml.new" \
    || die "Could not download docker-compose.prod.yaml for $version"
  curl -fsSL "$UPSTREAM_RAW/$version/contrib/docker/.env.prod.template" -o "$DEPLOY/.env.template.new" \
    || die "Could not download .env.prod.template for $version"
  mv "$DEPLOY/docker-compose.yaml.new" "$DEPLOY/docker-compose.yaml"
  mv "$DEPLOY/.env.template.new" "$DEPLOY/.env.template"
}

# Add keys that are new in the upstream template to .env, keeping existing values.
merge_env_template() {
  local line key
  while IFS= read -r line; do
    [[ $line =~ ^([A-Z0-9_]+)= ]] || continue
    key=${BASH_REMATCH[1]}
    grep -q "^$key=" "$ENV_FILE" || { echo "$line" >>"$ENV_FILE"; echo "  new setting from upstream: $key"; }
  done <"$DEPLOY/.env.template"
}

# Render everything in deploy/generated/ from deploy/templates/ and .env.
render_configs() {
  mkdir -p "$GEN" "$CERTS" "$(dirname "$USERS_FILE")"
  touch "$USERS_FILE"
  local domain; domain=$(env_get DOMAIN)

  export DOMAIN=$domain
  local v
  for v in SYNAPSE_REGISTRATION_SECRET SYNAPSE_MACAROON_SECRET SYNAPSE_FORM_SECRET LIVEKIT_API_KEY LIVEKIT_API_SECRET \
           AUTHELIA_SYNAPSE_CLIENT_SECRET AUTHELIA_JWT_SECRET AUTHELIA_SESSION_SECRET AUTHELIA_STORAGE_KEY AUTHELIA_OIDC_HMAC_SECRET; do
    export "$v=$(env_get "$v")"
  done

  # shellcheck disable=SC2016
  envsubst '$DOMAIN $AUTHELIA_SYNAPSE_CLIENT_SECRET $SYNAPSE_REGISTRATION_SECRET $SYNAPSE_MACAROON_SECRET $SYNAPSE_FORM_SECRET' \
    <"$DEPLOY/templates/synapse.yaml" >"$GEN/synapse.yaml"

  # Authelia: signing key, hashed client secrets, then config + users database.
  local adata="$DEPLOY/data/authelia"
  mkdir -p "$adata" "$GEN/authelia" && chmod 700 "$adata"
  [[ -f $adata/oidc.key ]] || openssl genrsa -out "$adata/oidc.key" 2048 2>/dev/null
  export AUTHELIA_OIDC_KEY AUTHELIA_WA_CLIENT_DIGEST AUTHELIA_SYNAPSE_CLIENT_DIGEST
  AUTHELIA_OIDC_KEY=$(sed 's/^/          /' "$adata/oidc.key")
  AUTHELIA_WA_CLIENT_DIGEST=$(client_secret_digest workadventure "$(env_get OPENID_CLIENT_SECRET)")
  AUTHELIA_SYNAPSE_CLIENT_DIGEST=$(client_secret_digest synapse "$AUTHELIA_SYNAPSE_CLIENT_SECRET")
  # shellcheck disable=SC2016
  envsubst '$DOMAIN $AUTHELIA_JWT_SECRET $AUTHELIA_SESSION_SECRET $AUTHELIA_STORAGE_KEY $AUTHELIA_OIDC_HMAC_SECRET $AUTHELIA_OIDC_KEY $AUTHELIA_WA_CLIENT_DIGEST $AUTHELIA_SYNAPSE_CLIENT_DIGEST' \
    <"$DEPLOY/templates/authelia.yaml" >"$GEN/authelia/configuration.yml"
  {
    echo "# Generated from deploy/data/users.tsv by bin/wa-user - do not edit."
    echo "users:"
    local email hash name
    while IFS=$'\t' read -r email hash name _; do
      [[ -n $email ]] || continue
      # Same identifier WorkAdventure uses for Matrix (email with "@" -> "_"); people log in with their email.
      printf '  "%s":\n    displayname: "%s"\n    email: "%s"\n    password: "%s"\n' "${email/@/_}" "$name" "$email" "$hash"
    done <"$USERS_FILE"
  } >"$GEN/authelia/users.yml.new"
  [[ -s $USERS_FILE ]] || echo "users: {}" >"$GEN/authelia/users.yml.new"
  # Rewrite in place (same inode) so Authelia's file watcher inside the container sees the change.
  cat "$GEN/authelia/users.yml.new" >"$GEN/authelia/users.yml" && rm -f "$GEN/authelia/users.yml.new"

  # LiveKit runs on the Docker bridge; tell it which address browsers should use for media.
  export LIVEKIT_RTC_ADDRESS
  if is_ip "$domain"; then LIVEKIT_RTC_ADDRESS="node_ip: $domain"
  elif is_lan_host "$domain"; then LIVEKIT_RTC_ADDRESS="node_ip: $(env_get LAN_IP)"
  else LIVEKIT_RTC_ADDRESS="use_external_ip: true"; fi
  # shellcheck disable=SC2016
  envsubst '$LIVEKIT_API_KEY $LIVEKIT_API_SECRET $LIVEKIT_RTC_ADDRESS' <"$DEPLOY/templates/livekit.yaml" >"$GEN/livekit.yaml"

  # LAN mode: Traefik also loads our own certificate as its default. The official compose
  # defines Traefik's flags as a list, which an override can only replace, so re-emit it + one flag.
  if is_lan_host "$domain"; then
    compose -f docker-compose.yaml config --format json 2>/dev/null |
      python3 -c '
import json, sys
cmd = json.load(sys.stdin)["services"]["reverse-proxy"]["command"]
cmd.append("--providers.file.filename=/etc/traefik/tls.yaml")
print("# Generated by scripts/lib.sh (LAN mode) - do not edit.")
print("services:\n  reverse-proxy:\n    command:")
for c in cmd: print("      - " + json.dumps(c))
print("    volumes:\n      - ./certs:/certs:ro\n      - ./templates/traefik-tls.yaml:/etc/traefik/tls.yaml:ro")
' >"$GEN/compose.lan.yaml" || die "Could not generate $GEN/compose.lan.yaml"
    env_set COMPOSE_FILE "docker-compose.yaml:docker-compose.override.yaml:generated/compose.lan.yaml"
    env_set NODE_EXTRA_CA_CERTS /certs/ca.crt
  else
    rm -f "$GEN/compose.lan.yaml"
    env_set COMPOSE_FILE "docker-compose.yaml:docker-compose.override.yaml"
    env_set NODE_EXTRA_CA_CERTS ""
  fi
  # The directory is private (700); the files must stay readable by the containers' own users.
  chmod 700 "$GEN" && chmod 755 "$GEN/authelia" && chmod 644 "$GEN"/*.yaml "$GEN"/authelia/*
}

# pbkdf2 digest of an OIDC client secret, as Authelia wants it. Cached, recomputed if the secret changes.
client_secret_digest() {
  local cache="$DEPLOY/data/authelia/$1.digest" sum
  sum=$(printf '%s' "$2" | sha256sum | cut -d' ' -f1)
  if [[ ! -f $cache || $(head -1 "$cache") != "$sum" ]]; then
    local digest
    digest=$(compose -f docker-compose.yaml -f docker-compose.override.yaml run --rm --no-deps -T --entrypoint authelia authelia \
      crypto hash generate pbkdf2 --variant sha512 --password "$2" 2>/dev/null | sed -n 's/^Digest: //p')
    [[ $digest == \$pbkdf2* ]] || die "Could not hash the $1 client secret with Authelia."
    printf '%s\n%s\n' "$sum" "$digest" >"$cache"
  fi
  sed -n 2p "$cache"
}

# Local certificate authority + server certificate for LAN installs (no Let's Encrypt possible).
make_lan_certs() {
  local domain=$1 san
  if is_ip "$domain"; then san="IP:$domain"; else san="DNS:$domain"; fi
  if [[ ! -f $CERTS/ca.crt ]]; then
    info "Creating a local certificate authority in deploy/certs/"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 -subj "/CN=WorkAdventure local CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" 2>/dev/null
  fi
  if [[ ! -f $CERTS/server.crt ]] || ! openssl x509 -in "$CERTS/server.crt" -noout -ext subjectAltName 2>/dev/null | grep -q "${san#*:}"; then
    info "Issuing a certificate for $domain"
    openssl req -newkey rsa:2048 -nodes -subj "/CN=$domain" -keyout "$CERTS/server.key" -out "$CERTS/server.csr" 2>/dev/null
    # 825 days is the longest validity Apple devices accept.
    openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" -CAcreateserial \
      -days 825 -sha256 -out "$CERTS/server.crt" \
      -extfile <(printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$san") 2>/dev/null
    rm -f "$CERTS/server.csr"
  fi
  chmod 600 "$CERTS/ca.key" "$CERTS/server.key"
  chmod 644 "$CERTS/ca.crt" "$CERTS/server.crt"
}
