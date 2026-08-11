# ATS MCP server

The validator and checker, exposed as an MCP server you can run on a VPS and
reach from iPhone, iPad, Mac and the web equally.

**Stateless.** A document arrives as base64 or as a public HTTPS URL the server
fetches, is written to a temp directory that is deleted before the call
returns, and is never logged. Nothing survives the request; there is no
database and no upload folder.

## Tools

| Tool | Purpose |
|---|---|
| `ats_validate_deliverables` | the pre-archive gate: canonical structure, docx→PDF round trip, single-column PDF, JD coverage |
| `ats_check_resume` | parseability + JD fit for any resume |
| `ats_extract_text` | exactly what an ATS extracts, in layout or content-stream order |

Each accepts **either** `*_base64` **or** `*_url` — never both, because
silently preferring one would leave you unsure which file was checked.

---

## Read this before you build it

Two constraints decide the design. Both are external, and neither has a
workaround I can honestly recommend.

### 1. claude.ai connectors cannot send an API key

The custom-connector UI supports **OAuth 2.1 with PKCE only**. There is no
field for a static bearer token or a custom header
([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112),
[#411](https://github.com/anthropics/claude-ai-mcp/issues/411)). So the usual
"put an API key on it" plan does not work.

This deployment therefore uses an **unguessable URL path** as the access
control — a capability URL:

```
https://ats.example.com/mcp/8Kd2nQ7xR4vT9pL3mW6yB1cF5hJ0sA
```

The server **refuses to start** on a guessable path, rather than quietly
exposing an open endpoint. Caddy 404s every other path and redacts the URI
from its logs.

Be clear-eyed about what that is: anyone holding the URL can call the server.
It is a bearer token that happens to live in a URL. Treat it like a password —
do not paste it into a shared document, and rotate it by changing `MCP_PATH`
and restarting. If you want real auth, the server needs to become an OAuth 2.1
authorization server; the SDK supports it via `auth_server_provider`, and it is
a significantly larger job.

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

**What works from every Apple surface:**

| Route | iPhone | iPad | Mac | Web |
|---|:--:|:--:|:--:|:--:|
| Attach the file in the conversation, model sends base64 | ~ | ~ | ~ | ~ |
| A direct-download URL (Dropbox `?dl=1`, presigned S3/R2) | ✅ | ✅ | ✅ | ✅ |
| A Shortcut that reads iCloud Drive and uploads to your own host | ✅ | ✅ | ✅ | — |

"~" because base64 only works when something can produce it — fine from a
script or Claude Code, unreliable for a large attached PDF, since the model
would have to emit a megabyte of base64 verbatim.

The honest summary: **the MCP server is reachable from everywhere; getting an
iCloud file to it is the part Apple makes awkward.** A Shortcut (which runs
identically on iPhone, iPad and Mac) that copies the CV to any host serving raw
bytes is the most reliable bridge.

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

### 3. Secret path

```bash
python3 -c "import secrets; print('MCP_PATH=/mcp/' + secrets.token_urlsafe(24))" \
  > /etc/ats-mcp.env
chmod 600 /etc/ats-mcp.env
cat /etc/ats-mcp.env      # note it down; you need it for the connector URL
```

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

A JSON-RPC result means it is live. Any other path must return 404:

```bash
curl -si https://YOUR.DOMAIN/mcp/wrong | head -1   # expect 404
```

### 7. Connect Claude

**claude.ai / iOS / iPadOS / Web** — Settings → Connectors → Add custom
connector → paste `https://YOUR.DOMAIN/mcp/<secret>`, no authentication.

**Claude Code** — `claude mcp add --transport http ats https://YOUR.DOMAIN/mcp/<secret>`

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
