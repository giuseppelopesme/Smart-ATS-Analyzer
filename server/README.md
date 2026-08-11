# ATS MCP server

The validator and checker, exposed as an MCP server you can run on a VPS and
reach from iPhone, iPad, Mac and the web equally.

**Nothing is written to disk.** A document reaches the server three ways —
a one-time upload id, a public HTTPS URL, or base64 — is processed in a temp
directory deleted before the call returns, and is never logged. Staged uploads
live in RAM only, are single-use, and expire. There is no database and no
upload folder.

## Tools

| Tool | Purpose |
|---|---|
| `ats_validate_deliverables` | the pre-archive gate: canonical structure, docx→PDF round trip, single-column PDF, JD coverage |
| `ats_check_resume` | parseability + JD fit for any resume |
| `ats_extract_text` | exactly what an ATS extracts, in layout or content-stream order |

Each accepts exactly one of `upload_id`, `*_url` or `*_base64` — never two,
because silently preferring one would leave you unsure which file was checked.

## Getting a file to it, from any device

| | iPhone | iPad | Mac | Web |
|---|:--:|:--:|:--:|:--:|
| **Upload page** → one-time id | ✅ | ✅ | ✅ | ✅ |
| Shortcut → id, automatically | ✅ | ✅ | ✅ | — |
| Direct-download URL | ✅ | ✅ | ✅ | ✅ |
| base64 in the call | — | — | ✅ | — |

The **upload page** at `<MCP_PATH>/upload` is the route that works everywhere.
Open it in Safari, pick the CV (the picker reads iCloud Drive directly), and
you get a short id:

```
Uploaded 20260811_Giuseppe LOPES_Campari Global AI Director.docx
              N7TltgQN2aBm
In Claude: "validate upload N7TltgQN2aBm"
```

Ids are **single-use** and expire after 30 minutes. Validating consumes the id;
re-running needs a fresh upload. base64 is marked unavailable on phones on
purpose — a model cannot reliably emit a megabyte of it for an attached file.

### A Shortcut, so it is two taps

Runs identically on iPhone, iPad and Mac. Share sheet → your CV → the id is on
the clipboard.

1. **Shortcuts → new shortcut → Share Sheet**, accepting *Files*.
2. **Get Contents of URL**
   - URL `https://YOUR.DOMAIN/mcp/<secret>/upload`
   - Method `POST`, Request Body `Form`
   - Header `Accept` = `application/json`
   - Field: type **File**, name `file`, value *Shortcut Input*
3. **Get Dictionary Value** — key `upload_id`
4. **Copy to Clipboard**

Then in Claude: *"validate upload ⌘V"*.

---

## Read this before you build it

Two constraints decide the design. Both are external, and neither has a
workaround I can honestly recommend.

### 1. Authentication is OAuth 2.1

claude.ai's custom-connector UI supports **OAuth 2.1 with PKCE only** — there
is no field for a static bearer token or a custom header
([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112),
[#411](https://github.com/anthropics/claude-ai-mcp/issues/411)). So the server
is a real authorization server.

Set `MCP_PUBLIC_URL` and `ATS_OAUTH_PASSWORD_HASH` and you get:

- discovery at `/.well-known/oauth-authorization-server` and
  `/.well-known/oauth-protected-resource`
- **dynamic client registration** — claude.ai registers itself, no client id to
  copy anywhere
- `/authorize` → a **login page**; a code is issued only after your passphrase
  verifies. The authorization endpoint never mints a code on its own, which is
  the difference between an authorization server and an open door
- **PKCE S256 enforced** by the SDK, along with `redirect_uri` matching
- one-hour access tokens, **rotating** refresh tokens, single-use codes,
  revocation
- passphrase stored as **scrypt** (N=2¹⁵), compared in constant time, with
  lockout after 8 failures in 5 minutes

Registered clients and refresh tokens persist to a 0600 file so a restart does
not unpair the connector. Access tokens and codes stay in memory.

**Secret-path mode still exists** for a server with no domain or no OAuth: omit
those two variables and access control becomes an unguessable `MCP_PATH`. The
server refuses to start on a guessable path in that mode, since the URL is then
the only credential. Prefer OAuth.

### 2. The VPS cannot read iCloud, and an iCloud share link is not a file

There is no official iCloud Drive API. `rclone`'s backend needs your real Apple
ID password plus interactive 2FA and its trust token **expires every 30 days**;
`pyicloud` and friends are reverse-engineered against Apple's private endpoints
and are blocked outright by Advanced Data Protection unless you enable web
access. Neither is something to hang a service on.

And an iCloud **"Copy Link"** URL returns a *viewer page*, not the bytes — the
download happens in JavaScript. The server detects this and says so, rather
than validating the HTML and reporting confident nonsense:

```
Error: could not fetch the docx: that iCloud link returned a web page, not the
file. An iCloud 'Copy Link' share URL points at a viewer page whose download
runs in JavaScript, so a server cannot follow it.
```

This is exactly why the upload page exists: it is the one route that works from
every device, and it turns "Apple will not let a Linux box read iCloud" into a
file picker that reads iCloud Drive natively on the device you are already
holding.

---

## Deploying onto the existing Infomaniak VPS

If you already run the Obsidian stack on this box (`vault.lopes.me`,
`couch.lopes.me`, `status.lopes.me`), use this instead of the from-scratch
guide below. It reuses the Caddy that is already there and adds one hostname.

```
Claude ──HTTPS/OAuth──▶ ats.lopes.me ─▶ Caddy :443 ─▶ 127.0.0.1:8430 ats-mcp (Docker)
```

Ports on that host: `8420` obsidian-web-mcp · `5984` CouchDB · `61208` Glances
· **`8430`** this. Nothing existing is modified — the vhost is a drop-in under
`/etc/caddy/conf.d/`, the same pattern `deploy-monitoring.sh` uses.

1. **DNS** — add an A record for `ats.lopes.me` pointing at the VPS, and let it
   resolve before step 3. Port 80 and 443 are already open from the Obsidian
   setup, so nothing changes in the Infomaniak firewall.

2. **Get the code onto the box**

   ```bash
   sudo git clone https://github.com/giuseppelopesme/Smart-ATS-Analyzer.git /opt/ats-mcp-src
   ```

3. **Deploy**

   ```bash
   cd /opt/ats-mcp-src/server/deploy/infomaniak
   sudo ./deploy-ats-mcp.sh
   ```

   It prompts once for a passphrase, stores only its scrypt hash in
   `/etc/ats-mcp/ats-mcp.env` (0640 `root:obsidian`), builds the image, starts
   the container on loopback, writes `/etc/caddy/conf.d/ats.caddy`, runs
   `caddy validate` and reloads. It refuses to touch Caddy if the backend is
   not answering, and refuses to start at all if the port belongs to something
   that is not ours.

   Re-running is safe: it rebuilds and restarts, and never regenerates the
   passphrase or drops OAuth state.

4. **Connect** — Claude → Settings → Connectors → Add custom connector →
   `https://ats.lopes.me/mcp`, Client ID/Secret blank (dynamic registration),
   then sign in. Exactly the flow you used for `vault.lopes.me`.

**Backups.** Deliberately not in the restic set. The only persisted state is
registered OAuth clients and refresh tokens, in a Docker volume; losing it
costs one sign-in. No CV is ever written to disk on the server.

**Updating**

```bash
cd /opt/ats-mcp-src && sudo git pull
cd server/deploy/infomaniak && sudo ./deploy-ats-mcp.sh
```

**Removing it**

```bash
cd /opt/ats-mcp-src/server/deploy/infomaniak && sudo docker compose down -v
sudo rm /etc/caddy/conf.d/ats.caddy && sudo systemctl reload caddy
sudo rm -rf /etc/ats-mcp
```

---

## Provisioning from nothing

Any 1 GB VPS is plenty. Debian 12 assumed.

### 1. Server and DNS

Create the VPS, point an A record at it (`ats.example.com`), and open 80/443.

```bash
ssh root@YOUR_SERVER_IP
apt update && apt install -y python3 python3-venv git
```

### 2. Application

```bash
useradd --system --create-home --home-dir /opt/ats-mcp --shell /usr/sbin/nologin atsmcp
git clone https://github.com/giuseppelopesme/Smart-ATS-Analyzer.git /opt/ats-mcp
cd /opt/ats-mcp
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
chown -R atsmcp:atsmcp /opt/ats-mcp
```

### 3. OAuth configuration

```bash
/opt/ats-mcp/.venv/bin/python /opt/ats-mcp/server/oauth.py --hash-password
# prompts twice, prints: ATS_OAUTH_PASSWORD_HASH=scrypt$...

cat > /etc/ats-mcp.env <<'EOF'
MCP_PUBLIC_URL=https://YOUR.DOMAIN
ATS_OAUTH_PASSWORD_HASH=scrypt$...paste the line above...
EOF
chmod 600 /etc/ats-mcp.env
```

`MCP_PUBLIC_URL` must be the exact external HTTPS origin — it is published as
the OAuth issuer, and a mismatch makes the client reject the metadata.

The passphrase is the only thing between the internet and your documents, so
make it long. To run without OAuth instead, put an unguessable
`MCP_PATH=/mcp/…` in this file and omit the two variables above.

### 4. Service

```bash
cp /opt/ats-mcp/server/deploy/ats-mcp.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now ats-mcp
systemctl status ats-mcp
```

The unit runs with `ProtectSystem=strict`, `PrivateTmp`, no write paths and a
syscall filter — it handles personal documents, so it gets as little of the
machine as it can.

### 5. TLS

```bash
apt install -y caddy
cp /opt/ats-mcp/server/deploy/Caddyfile /etc/caddy/Caddyfile
sed -i 's/ats.example.com/YOUR.DOMAIN/' /etc/caddy/Caddyfile
systemctl reload caddy
```

Certificates are issued and renewed automatically.

### 6. Verify

```bash
source /etc/ats-mcp.env
curl -sS -X POST "https://YOUR.DOMAIN${MCP_PATH}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"1"}}}'
```

With OAuth on, that must return **401** with a `WWW-Authenticate` header
pointing at the resource metadata — that is the handshake working, not a
failure. Check discovery too:

```bash
curl -s https://YOUR.DOMAIN/.well-known/oauth-authorization-server | jq .
```

Then open `https://YOUR.DOMAIN/mcp/upload` on your phone and upload something
small; you should get an id back.

### 7. Connect Claude

**claude.ai / iOS / iPadOS / Web** — Settings → Connectors → Add custom
connector → `https://YOUR.DOMAIN/mcp`. Claude discovers the OAuth endpoints,
registers itself, and opens the login page; enter your passphrase and it is
paired. The same connector then works on every device signed into your account.

**Claude Code** — `claude mcp add --transport http ats https://YOUR.DOMAIN/mcp`
then `/mcp` to run the browser login.

To revoke: remove the connector, or delete `/var/lib/ats-mcp/oauth.json` and
restart, which unpairs every client at once.

### Docker alternative

```bash
docker build -f server/deploy/Dockerfile -t ats-mcp .
docker run -d --name ats-mcp -p 127.0.0.1:8080:8080 \
  -e MCP_PATH=/mcp/$(python3 -c "import secrets;print(secrets.token_urlsafe(24))") \
  --read-only --tmpfs /tmp --memory 1g ats-mcp
```

---

## Running locally instead

No VPS needed for Mac or Claude Code use — stdio, no network at all:

```bash
python3 server/ats_mcp.py          # MCP_TRANSPORT defaults to stdio
claude mcp add ats -- python3 /path/to/Smart-ATS-Analyzer/server/ats_mcp.py
```

This is strictly more private than the VPS: the documents never leave the
machine. The VPS only buys you reachability from the phone.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio`, or `http` for Streamable HTTP |
| `MCP_PATH` | — | required for HTTP; must not be `/mcp` |
| `MCP_HOST` | `127.0.0.1` | bind address; keep it loopback behind a proxy |
| `MCP_PORT` | `8080` | bind port |
| `MCP_UPLOAD_TTL` | `1800` | seconds a staged upload lives. `0` disables the upload page entirely |
| `MCP_PUBLIC_URL` | — | external HTTPS origin; enables OAuth when set with the hash |
| `ATS_OAUTH_PASSWORD_HASH` | — | scrypt hash from `oauth.py --hash-password` |
| `ATS_OAUTH_STATE` | `/var/lib/ats-mcp/oauth.json` | 0600 file holding registered clients and refresh tokens |

Limits: 12 MB per document, 60,000 characters of job description, 20 s fetch
timeout, at most 3 redirects.

## Security notes

- **SSRF.** URL fetching is HTTPS-only. Every hostname is resolved and each
  address checked before connecting; private, loopback, link-local, multicast
  and reserved ranges are refused. Redirects are re-validated at each hop,
  because a public host can redirect to `169.254.169.254`.
- **No content in logs.** Errors never echo document text, and Caddy redacts
  the request URI so the secret path stays out of the log file.
- **Filenames are untrusted.** Only the extension is used, to pick a reader;
  the rest never reaches the filesystem.
- **Bounded work.** Size caps are enforced before allocation, and the PDF
  parser has its own operation ceiling.
- **OAuth.** Codes are single-use and expire in 5 minutes; access tokens last
  an hour; refresh tokens rotate on every use, so a leaked one is good at most
  once. Login has a lockout. The state file holds no passphrase — only its
  scrypt hash lives in the environment, and that never reaches disk.
- **Staging.** In memory only, 12 MB per file, 64 MB and 16 entries in total,
  single-use, 30-minute expiry, swept on every put and take. A restart drops
  everything. It sits under the same secret path as the MCP endpoint, so one
  secret and one proxy rule cover both. Set `MCP_UPLOAD_TTL=0` to remove it.
