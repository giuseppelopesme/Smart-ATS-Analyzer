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
# WHERE TO RUN THIS: on the VPS, over SSH, as root.
#
#   ssh root@179.237.107.22
#   cd /opt/ats-mcp-src/server/deploy/infomaniak
#   sudo ./deploy-ats-mcp.sh
#
#   sudo ATS_HOST=ats.example.com ./deploy-ats-mcp.sh   # different hostname

set -euo pipefail

# ---------------------------------------------------------------------------
# WHERE THIS RUNS: on the VPS, over SSH, as root.
# Not on your Mac -- it inspects this host's own network stack and firewall.
# ---------------------------------------------------------------------------
if [ "$(uname -s)" = "Darwin" ]; then
    printf '\033[1;31mxx\033[0m  This script runs ON THE VPS, not on macOS.\n\n' >&2
    printf '    ssh root@179.237.107.22\n' >&2
    printf '    cd /opt/ats-mcp-src/server/deploy/infomaniak && sudo %s\n\n' \
        "$(basename "$0")" >&2
    exit 2
fi


REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ATS_HOST="${ATS_HOST:-ats.lopes.me}"
ATS_PORT="${ATS_PORT:-8430}"
DEPLOY_USER="${DEPLOY_USER:-obsidian}"
ENV_DIR="/etc/ats-mcp"
ENV_FILE="${ENV_DIR}/ats-mcp.env"
HASH_FILE="${ENV_DIR}/password.hash"
CONF_D="/etc/caddy/conf.d"
CADDYFILE="/etc/caddy/Caddyfile"
IMPORT_LINE="import ${CONF_D}/*.caddy"
COMPOSE_DIR="${REPO_ROOT}/server/deploy/infomaniak"
# The container runs as this uid (see server/deploy/Dockerfile). The hash file
# is bind-mounted in and read by that user, so host ownership must match --
# root:obsidian 0640 is unreadable to it and the container exits at start-up.
CONTAINER_UID="${CONTAINER_UID:-10001}"

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

# Dual-stack is supported, but Let's Encrypt resolves AAAA first and does NOT
# fall back to IPv4 when the v6 challenge fails. So when an AAAA is published,
# the v6 path has to be right -- these are the parts that can be checked from
# here. Inbound reachability cannot be, which is what check-ipv6.sh is for.
_v6="$(getent ahostsv6 "$ATS_HOST" 2>/dev/null | awk 'NR==1{print $1}' || echo '')"
if [ -n "$_v6" ]; then
    log "$ATS_HOST publishes AAAA $_v6 — validating the IPv6 path"
    _local_v6="$(ip -6 addr show scope global 2>/dev/null \
                 | awk '/inet6/{print $2}' | cut -d/ -f1 | grep -v '^fe80' || true)"
    if [ -z "$_local_v6" ]; then
        die "$ATS_HOST has an AAAA record but this host has no global IPv6 address.
     Certificate issuance will fail and Caddy will back off.
     Either enable IPv6 on the VPS or remove the AAAA record."
    elif ! grep -qxF "$_v6" <<<"$_local_v6"; then
        die "$ATS_HOST resolves to $_v6, which is not an address on this host:
$(sed 's/^/       /' <<<"$_local_v6")
     Fix the AAAA record before continuing."
    else
        log "IPv6 address matches this host"
    fi
    if ! ip -6 route show default 2>/dev/null | grep -q .; then
        warn "No default IPv6 route — ACME over IPv6 will fail."
    fi
    if command -v ufw >/dev/null 2>&1 && ! grep -qi '^IPV6=yes' /etc/default/ufw 2>/dev/null; then
        warn "IPV6 is not enabled in /etc/default/ufw, so the 80/443 rules do not"
        warn "cover IPv6. Fix, then re-run:"
        warn "  sudo sed -i 's/^IPV6=.*/IPV6=yes/' /etc/default/ufw"
        warn "  sudo ufw disable && sudo ufw --force enable"
    fi
    warn "Local IPv6 config looks right, but inbound reachability through the"
    warn "Infomaniak panel firewall cannot be checked from this host. If the"
    warn "certificate does not issue, that is the first place to look."
    warn "  ./check-ipv6.sh   prints the external test to run"
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
# The passphrase hash lives in password.hash, NOT here: Compose interpolates
# "\$" in env_file values and would truncate it at its own separator.
MCP_PUBLIC_URL=https://${ATS_HOST}
EOF
    printf '%s\n' "$_hash" > "$HASH_FILE"
    unset _hash
else
    log "$ENV_FILE already exists — keeping the existing passphrase"

    # Migrate hashes written by earlier versions of this script, which put them
    # in the env file where Compose truncated them at the second "$".
    if [ ! -f "$HASH_FILE" ] && grep -q '^ATS_OAUTH_PASSWORD_HASH=' "$ENV_FILE"; then
        _old="$(sed -n 's/^ATS_OAUTH_PASSWORD_HASH=//p' "$ENV_FILE" | head -n1)"
        # tr, not awk -F'$': a single-character FS is meant to be literal but
        # implementations differ on regex metacharacters, and this one is '$'.
        if [ "$(printf '%s' "$_old" | tr -cd '$' | wc -c)" -eq 2 ]; then
            log "Migrating the passphrase hash out of the env file into $HASH_FILE"
            printf '%s\n' "$_old" > "$HASH_FILE"
            sed -i '/^ATS_OAUTH_PASSWORD_HASH=/d' "$ENV_FILE"
            printf '# Hash moved to password.hash (Compose eats "$" in env_file).\n' \
                >> "$ENV_FILE"
        else
            warn "The hash in $ENV_FILE is not a complete scrypt\$salt\$digest."
            warn "It was almost certainly truncated by Compose. Delete both"
            warn "$ENV_FILE and $HASH_FILE, then re-run to set a new passphrase."
        fi
        unset _old
    fi
    if [ ! -f "$HASH_FILE" ]; then
        warn "No $HASH_FILE — the server will refuse to start."
        warn "Delete $ENV_FILE and re-run to set a passphrase."
    fi
    if ! grep -q "^MCP_PUBLIC_URL=https://${ATS_HOST}$" "$ENV_FILE"; then
        warn "MCP_PUBLIC_URL in $ENV_FILE does not match https://${ATS_HOST}."
        warn "It is published as the OAuth issuer; a mismatch breaks the connector."
    fi
fi
chown "root:${DEPLOY_USER}" "$ENV_FILE"
chmod 640 "$ENV_FILE"

# If a previous run let Docker create the bind-mount source, it is a directory.
if [ -d "$HASH_FILE" ]; then
    log "Removing the empty directory Docker created at $HASH_FILE"
    rmdir "$HASH_FILE" 2>/dev/null || rm -rf "$HASH_FILE"
fi

if [ -f "$HASH_FILE" ]; then
    # Owned by the container uid, not by root:$DEPLOY_USER: the reader is the
    # process inside the container. 0400 means nothing else on the host can
    # read it either, which is tighter than what it replaces.
    chown "${CONTAINER_UID}:${CONTAINER_UID}" "$HASH_FILE"
    chmod 400 "$HASH_FILE"
    # Catch the truncation before the container does, whatever wrote it.
    if [ "$(tr -cd '$' < "$HASH_FILE" | wc -c)" -ne 2 ]; then
        die "$HASH_FILE is not a valid scrypt\$salt\$digest hash.
     Delete $ENV_FILE and $HASH_FILE, then re-run to set a new passphrase."
    fi
fi

# The bind mount must have a real file to point at. Without one Docker silently
# creates a directory, and the container then fails to start reading it.
if [ ! -f "$HASH_FILE" ]; then
    die "$HASH_FILE is missing, so the container cannot be given a passphrase.
     Delete $ENV_FILE and re-run this script to set one."
fi

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
    warn "Its own last words:"
    docker logs ats-mcp --tail 20 2>&1 | sed 's/^/       /' >&2
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
