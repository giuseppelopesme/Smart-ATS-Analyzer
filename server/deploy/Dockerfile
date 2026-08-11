FROM python:3.12-slim

RUN useradd --create-home --shell /usr/sbin/nologin atsmcp
WORKDIR /opt/ats-mcp

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# The analyser is imported from the skill directory; ship both.
COPY server/ server/
COPY .claude/skills/ats-check/ .claude/skills/ats-check/

USER atsmcp
ENV MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8080
EXPOSE 8080
# MCP_PATH must be supplied at run time; the server refuses to start without it.
CMD ["python", "server/ats_mcp.py"]
