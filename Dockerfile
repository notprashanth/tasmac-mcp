# A hosted, shared instance of the MCP server. Works on anything that runs a
# container and sets PORT: Cloud Run, Fly, Render, Railway.
#
# Deliberately different from a local install: snapshot history is off (a
# shared archive is not anybody's history) and upstream responses are cached,
# because one server answering many people must not hammer a government API
# that already struggles under its own load.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE icon.svg ./
COPY tasmac_mcp ./tasmac_mcp
RUN pip install --no-cache-dir .

# TASMAC_DB: the image runs as nobody, whose HOME is /nonexistent, so the
# default catalogue cache path under ~/.local/share is unwritable. Every
# catalogue call - find_product, recommend - died on it before this was set.
ENV PORT=8080 \
    HOST=0.0.0.0 \
    MCP_PATH=/mcp \
    TASMAC_CACHE_TTL=900 \
    TASMAC_DB=/tmp/tasmac-cache.db \
    PYTHONUNBUFFERED=1

EXPOSE 8080
USER nobody
CMD ["tasmac-mcp-http"]
