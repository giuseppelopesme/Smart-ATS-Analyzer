FROM python:3.12-slim

# Non-root, and no login shell: the container serves HTTP and nothing else.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 atsmcp \
 && mkdir -p /var/lib/ats-mcp \
 && chown atsmcp:atsmcp /var/lib/ats-mcp \
 && chmod 700 /var/lib/ats-mcp

WORKDIR /opt/ats-mcp

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# The analyser lives in the skill directory and is imported by the server, so
# both have to be in the image. Neither needs to be writable.
COPY server/ server/
COPY .claude/skills/ats-check/ .claude/skills/ats-check/

USER atsmcp

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    MCP_PATH=/mcp \
    ATS_OAUTH_STATE=/var/lib/ats-mcp/oauth.json \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# MCP_PUBLIC_URL and ATS_OAUTH_PASSWORD_HASH come from the env file. Without
# them the server starts unauthenticated, and then refuses a guessable
# MCP_PATH rather than quietly serving an open endpoint.
CMD ["python", "server/ats_mcp.py"]
