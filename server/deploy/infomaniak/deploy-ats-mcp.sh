#!/usr/bin/env bash
# deploy-ats-mcp.sh — ATS validator MCP server on the Infomaniak VPS.
#
#   Claude ──HTTPS/OAuth──▶ ats.lopes.me ─▶ Caddy :443 ─▶ 127.0.0.1:8430
#                                                          ats-mcp (Docker)
#
# Sits alongside the existing stack and touches none of it:
#   8420 obsidian-web-mcp · 5984 CouchDB · 61208 Glances · 8430 this
#
# Follows the conventions already on this box — a drop-in Caddy vhost under
# /etc/caddy/conf.d, a 0640 env file owned by root:$DEPLOY_USER, loopback-bound
# backend, `caddy validate` before any reload.
#
# Idempotent: safe to re-run. Re-running rebuilds the image and restarts the
# container; it never regenerates the passphrase or drops OAuth state.
#
# Prerequisites
#   1. bootstrap.sh from the Obsidian infra repo has run (Caddy + ufw).
#   2. Docker is installed (it is — CouchDB runs on it).
#   3. DNS: an A record for ats.lopes.me pointing at this VPS, resolving
#      BEFORE you run this, or Let's Encrypt validation fails.
#
# Usage
#   sudo ./deploy-ats-mcp.sh
#   sudo ATS_HOST=ats.example.com ./deploy-ats-mcp.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ATS_HOST="${ATS_HOST:-ats.lopes.me}"
ATS_PORT="${ATS_PORT:-8430}"
DEPLOY_USER="${DEPLOY_USER:-obsidian}"
ENV_DIR="/etc/ats-mcp"
ENV_FILE="${ENV_DIR}/ats-mcp.env"
CONF_D="/etc/caddy/conf.d"
CADDYFILE="/etc/caddy/Caddyfile"
IMPORT_LINE="import ${CONF_D}/*.caddy"
COMPOSE_DIR="${REPO_ROOT}/server/deploy/infomaniak"

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo $0)."
command -v caddy  >/dev/null 2>&1 || die "caddy not found — run bootstrap.sh from the Obsidian infra repo first."
command -v docker >/dev/null 2>&1 || die "docker not found — CouchDB uses it, so something is wrong."
command -v python3 >/dev/null 2>&1 || die "python3 not found — needed to hash the passphrase."
command -v curl >/dev/null 2>&1 || die "curl not found — needed for the verification steps."

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "neither 'docker compose' nor 'docker-compose' is available."
fi

id -u "$DEPLOY_USER" >/dev/null 2>&1 || {
    warn "user $DEPLOY_USER not found; the env file will be root-owned only"
    DEPLOY_USER="root"
}

# ----------------------------------------------------------------------------
# 1. Port check — do not silently collide with the existing stack
# ----------------------------------------------------------------------------
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -qE "127\.0\.0\.1:${ATS_PORT}\b"; then
    if docker ps --format '{{.Names}}' | grep -qx 'ats-mcp'; then
        log "Port ${ATS_PORT} is ours (ats-mcp already running) — will restart it"
    else
        die "127.0.0.1:${ATS_PORT} is already in use by something else. Set ATS_PORT."
    fi
fi

# ----------------------------------------------------------------------------
# 2. DNS sanity — Let's Encrypt will fail without it, with a worse message
# ----------------------------------------------------------------------------
_public_ip="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo '')"
_resolved="$(getent ahostsv4 "$ATS_HOST" 2>/dev/null | awk 'NR==1{print $1}' || echo '')"
if [ -z "$_resolved" ]; then
    warn "$ATS_HOST does not resolve yet. Add the A record before Caddy tries to"
    warn "issue a certificate, or TLS will fail and Caddy may back off for a while."
elif [ -n "$_public_ip" ] && [ "$_resolved" != "$_public_ip" ]; then
    warn "$ATS_HOST resolves to $_resolved but this host is $_public_ip."
    warn "If that is not a proxy in front, certificate issuance will fail."
else
    log "DNS OK — $ATS_HOST → $_resolved"
fi

# ----------------------------------------------------------------------------
# 3. Environment file — passphrase hash generated once, never overwritten
# ----------------------------------------------------------------------------
install -d -m 750 "$ENV_DIR"
if [ ! -f "$ENV_FILE" ]; then
    log "Creating $ENV_FILE"
    printf '\n'
    printf 'Choose a passphrase for signing in to the ATS connector.\n'
    printf 'It is the only thing between the internet and your CVs — make it long.\n\n'

    _hash=""
    for _try in 1 2 3; do
        read -r -s -p "Passphrase: " _pw1; printf '\n'
        read -r -s -p "Repeat:     " _pw2; printf '\n'
        if [ "$_pw1" != "$_pw2" ]; then warn "They do not match."; continue; fi
        if [ "${#_pw1}" -lt 12 ]; then warn "Use at least 12 characters."; continue; fi
        _hash="$(printf '%s' "$_pw1" | python3 -c 'import sys,hashlib,base64,secrets
pw = sys.stdin.read()
salt = secrets.token_bytes(16)
# maxmem must be raised: these parameters need exactly OpenSSL default ceiling.
d = hashlib.scrypt(pw.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32,
                   maxmem=2 * 128 * (2**15) * 8)
e = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
print("scrypt$" + e(salt) + "$" + e(d))')"
        break
    done
    unset _pw1 _pw2
    [ -n "$_hash" ] || die "No passphrase set — nothing was written."

    umask 077
    cat > "$ENV_FILE" <<EOF
# ats-mcp — generated by deploy-ats-mcp.sh. Keep out of git.
MCP_PUBLIC_URL=https://${ATS_HOST}
ATS_OAUTH_PASSWORD_HASH=${_hash}
EOF
    unset _hash
else
    log "$ENV_FILE already exists — keeping the existing passphrase"
    if ! grep -q '^ATS_OAUTH_PASSWORD_HASH=scrypt\$' "$ENV_FILE"; then
        warn "$ENV_FILE has no scrypt hash — the server will start UNAUTHENTICATED."
        warn "Delete the file and re-run to set a passphrase."
    fi
    if ! grep -q "^MCP_PUBLIC_URL=https://${ATS_HOST}$" "$ENV_FILE"; then
        warn "MCP_PUBLIC_URL in $ENV_FILE does not match https://${ATS_HOST}."
        warn "It is published as the OAuth issuer; a mismatch breaks the connector."
    fi
fi
chown "root:${DEPLOY_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# ----------------------------------------------------------------------------
# 4. Build and start the container
# ----------------------------------------------------------------------------
log "Building the image (this takes a minute on first run)"
( cd "$COMPOSE_DIR" && $COMPOSE build --quiet )

log "Starting ats-mcp on 127.0.0.1:${ATS_PORT}"
( cd "$COMPOSE_DIR" && $COMPOSE up -d )

# Give it a moment, then confirm it is actually serving before touching Caddy.
_ok=""
for _i in $(seq 1 20); do
    if curl -fsS --max-time 3 \
        "http://127.0.0.1:${ATS_PORT}/.well-known/oauth-authorization-server" \
        >/dev/null 2>&1; then
        _ok=1; break
    fi
    sleep 1
done
if [ -z "$_ok" ]; then
    warn "The container is not serving OAuth discovery on 127.0.0.1:${ATS_PORT}."
    warn "Logs:  docker logs ats-mcp --tail 40"
    die  "Not touching Caddy while the backend is down."
fi
log "Backend is up and serving OAuth discovery"

# ----------------------------------------------------------------------------
# 5. Caddy vhost — drop-in, same pattern as status.lopes.me
# ----------------------------------------------------------------------------
install -d "$CONF_D"
log "Writing ${CONF_D}/ats.caddy"
cat > "${CONF_D}/ats.caddy" <<EOF
# ats.lopes.me — ATS CV validator MCP server (deploy-ats-mcp.sh).
# Authentication is OAuth 2.1 handled by the app itself, so every path here is
# public by design: discovery, dynamic client registration, the login page and
# the MCP endpoint. The MCP endpoint 401s without a bearer token.
${ATS_HOST} {
	# A 12 MB document is ~16 MB as base64 or multipart. Nothing needs more.
	request_body {
		max_size 20MB
	}
	reverse_proxy 127.0.0.1:${ATS_PORT} {
		# Streamable HTTP can hold a response open; do not cut it short.
		flush_interval -1
		transport http {
			read_timeout 300s
			write_timeout 300s
		}
	}
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		-Server
	}
}
EOF
chmod 644 "${CONF_D}/ats.caddy"

if [ -f "$CADDYFILE" ] && ! grep -qF "$IMPORT_LINE" "$CADDYFILE"; then
    log "Adding conf.d import to $CADDYFILE"
    printf '\n# Drop-in site blocks generated on the box (deploy-ats-mcp.sh)\n%s\n' \
        "$IMPORT_LINE" >> "$CADDYFILE"
fi

if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy 2>/dev/null || systemctl restart caddy 2>/dev/null || \
        warn "Could not reload caddy — check 'systemctl status caddy'"
    log "Caddy reloaded"
else
    warn "caddy validate failed — NOT reloading, the running config is untouched."
    warn "Inspect ${CONF_D}/ats.caddy, then: sudo systemctl reload caddy"
fi

# ----------------------------------------------------------------------------
# 6. Verify from the outside
# ----------------------------------------------------------------------------
printf '\n'
log "Verifying https://${ATS_HOST}"
sleep 3
_disc="$(curl -fsS --max-time 20 "https://${ATS_HOST}/.well-known/oauth-authorization-server" 2>/dev/null || echo '')"
if [ -n "$_disc" ]; then
    log "OAuth discovery is live"
    printf '    %s\n' "$(printf '%s' "$_disc" | head -c 160)"
else
    warn "Could not reach discovery over HTTPS yet."
    warn "Certificate issuance can take a minute on a new hostname; watch with:"
    warn "  sudo journalctl -u caddy -f"
fi

_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST \
    "https://${ATS_HOST}/mcp" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>/dev/null || echo '000')"
case "$_code" in
    401) log "MCP endpoint returns 401 without a token — authentication is on" ;;
    200) warn "MCP endpoint answered 200 UNAUTHENTICATED. Check ATS_OAUTH_PASSWORD_HASH in $ENV_FILE." ;;
    000) warn "MCP endpoint unreachable over HTTPS yet (see the certificate note above)." ;;
    *)   warn "MCP endpoint returned HTTP $_code — expected 401." ;;
esac

cat <<EOF

==> ats-mcp deployed.

  Connector URL : https://${ATS_HOST}/mcp
                  Claude → Settings → Connectors → Add custom connector.
                  Leave Client ID/Secret blank (dynamic registration), then
                  sign in with the passphrase you set.

  Upload page   : https://${ATS_HOST}/mcp/upload
                  The route that works from iPhone and iPad — pick the file
                  from iCloud Drive, get a one-time id, paste it to Claude.

  Passphrase    : only its scrypt hash is stored, in ${ENV_FILE}.
                  Forgotten? Delete that file and re-run this script.

  Logs          : docker logs ats-mcp -f
  Restart       : cd ${COMPOSE_DIR} && ${COMPOSE} restart
  Unpair all    : docker volume rm ats-mcp_ats-mcp-state  (stop it first)

  Not backed up on purpose: the only state is registered OAuth clients and
  refresh tokens. Losing it costs one sign-in. No CV is ever written to disk.

EOF
