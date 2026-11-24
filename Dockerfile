# Browser KiCad desktop: linuxserver/kicad + the team's KiCad plugins that need native parts or extra Python
# packages. Plugins are staged under /opt/kicad-plugins; the pod's init container copies them into the user's
# 3rdparty/plugins folder on every start (k3s-cluster apps/kicad).
ARG KICAD_IMAGE=lscr.io/linuxserver/kicad:10.0.3-r0-ls158

# KiCadRoutingTools: Rust A* router as a pyo3 extension, built for musl (upstream only ships glibc builds)
FROM docker.io/library/alpine:3.24 AS krt-build
# upstream release tag v0.24.2 (annotated tag; this is the commit it points to)
ARG KRT_VERSION=0.24.2
ARG KRT_SHA=ebc832aba77b44e574bbe093ce35296bd8a9b8b0
RUN apk add --no-cache rust cargo python3 python3-dev build-base git
RUN git clone https://github.com/drandyhaas/KiCadRoutingTools.git /src \
 && git -C /src checkout "${KRT_SHA}"
ENV RUSTFLAGS="-C target-feature=-crt-static" PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
# mimalloc defaults to initial-exec TLS, which musl refuses in a dlopen()ed library (Python extension):
# "initial-exec TLS resolves to dynamic definition". Its local_dynamic_tls feature exists for exactly this.
RUN sed -i 's/^mimalloc = { version = "0.1", default-features = false }/mimalloc = { version = "0.1", default-features = false, features = ["local_dynamic_tls"] }/' /src/rust_router/Cargo.toml \
 && grep -q 'local_dynamic_tls' /src/rust_router/Cargo.toml
RUN cargo build --release --manifest-path /src/rust_router/Cargo.toml \
 && cp /src/rust_router/target/release/libgrid_router.so /src/rust_router/grid_router.so \
 && cp /src/rust_router/grid_router.so /src/rust_router/grid_router-linux-x86_64.so \
 && rm -rf /src/rust_router/target

FROM ${KICAD_IMAGE}
# KiCad's Python is the system python (pip is externally managed): the plugin's deps come from Alpine packages
RUN apk add --no-cache py3-numpy py3-scipy py3-shapely py3-pillow git openssh-client openssh-server openssl
COPY --from=krt-build /src /opt/kicad-plugins/KiCadRoutingTools
# KiKit backend for the KiKit PCM plugin (com.github.yaqwsx.kikit), into KiCad's system Python. numpy and shapely
# come from apk above, so pip leaves them alone; the rest is pinned to what kikit 1.8.1 resolves to.
RUN pip install --no-cache-dir --break-system-packages kikit==1.8.1 click==8.5.1 commentjson==0.9.0 euclid3==0.1 \
 lark-parser==0.7.8 markdown2==2.5.5 ply==3.11 prettytable==0.7.2 pybars3==0.9.7 PyMeta3==0.5.1 pypng==0.0.19 \
 solidpython==1.1.3
# uid/gid 1000 for abc at build time (the base creates it as 911; its init-adduser remaps it to PUID/PGID, which is
# 1000 on the desktop, with usermod -o so this is a no-op there). Pods that skip s6 (krt-mcp) run as uid 1000 and
# need the passwd entry, or ssh/git fail with "No user exists for uid 1000". sshd is installed, not configured.
RUN groupmod -o -g 1000 abc && usermod -o -u 1000 abc && id abc | grep -q 'uid=1000(abc) gid=1000'
# krt_registry.py inventories the clone with `git ls-files`, so /src keeps its .git; the pod runs as another uid
RUN git config --system --add safe.directory '*'
# Claude Code, pinned to the npm `stable` dist-tag (npm is an officially supported route; it pulls the musl binary).
# Version is fixed by the image, so the auto-updater is off.
RUN apk add --no-cache nodejs npm \
 && npm install -g --no-fund --no-audit @anthropic-ai/claude-code@2.1.325 \
 && printf 'export DISABLE_AUTOUPDATER=1\n' > /etc/profile.d/claude.sh
ENV DISABLE_AUTOUPDATER=1
COPY claude-me /usr/local/bin/claude-me
COPY git-credential-k3s /usr/local/bin/git-credential-k3s
COPY claude-wrapper /usr/local/sbin/claude
RUN chmod 755 /usr/local/bin/claude-me /usr/local/sbin/claude /usr/local/bin/git-credential-k3s && [ "$(command -v claude)" = /usr/local/sbin/claude ] \
 && CLAUDE_CONFIG_DIR=/tmp claude --version && { HOME=/nonexistent claude --version >/dev/null 2>&1; [ $? = 1 ]; } && /usr/local/bin/claude --version && /usr/local/bin/claude --help | grep -e --plugin-dir && /usr/local/bin/claude --help | grep -e --add-dir \
 && claude-me --help && { claude-me </dev/null >/dev/null 2>&1; [ $? = 2 ]; }
# smoke test: the router extension and kikit load in KiCad's Python
RUN cd /opt/kicad-plugins/KiCadRoutingTools/rust_router && python3 -c "import grid_router; print('grid_router ok')" && python3 -c "import kikit; print('kikit ok')"

# kicad-happy: review/analysis skills and scripts (also loaded by claude-me as a plugin). Deps are stdlib plus
# optional jsonschema/yaml (apk); ngspice and poppler-utils are the SPICE and PDF-text backends.
ARG KH_SHA=6b9767e19979cdef91631573753b1547efdbeab9
RUN apk add --no-cache ngspice poppler-utils py3-jsonschema py3-yaml \
 && git clone https://github.com/KiCadAIPlayground/kicad-happy.git /opt/kicad-happy \
 && git -C /opt/kicad-happy checkout "${KH_SHA}" && rm -rf /opt/kicad-happy/.git \
 && python3 /opt/kicad-happy/skills/kicad/scripts/analyze_schematic.py --help | head -3

# krt-mcp: MCP server over KiCadRoutingTools, same image, different command (see krt-mcp/server.py).
# The venv sees the apk numpy/scipy/shapely; `mcp`, uvicorn and their dependencies are all pinned exactly.
RUN python3 -m venv --system-site-packages /opt/krt-mcp/venv \
 && /opt/krt-mcp/venv/bin/pip install --no-cache-dir mcp==2.3.0 uvicorn==0.54.0 \
    annotated-types==0.8.0 anyio==4.16.0 attrs==26.1.0 click==8.5.1 h11==0.16.0 httpcore2==2.16.2 httpx2==2.16.2 \
    idna==3.20 jsonschema==4.26.0 jsonschema-specifications==2025.9.1 mcp-types==2.3.0 opentelemetry-api==1.45.2 \
    pydantic==2.14.4 pydantic-core==2.50.5 pyjwt==2.15.1 python-multipart==0.0.34 referencing==0.37.0 \
    rpds-py==2026.9.1 sse-starlette==3.7.1 starlette==1.10.0 truststore==0.10.5 typing-extensions==4.16.0 \
    typing-inspection==0.4.5
COPY krt-mcp/server.py /opt/krt-mcp/server.py
RUN KRT_ROOT=/opt/kicad-plugins/KiCadRoutingTools KH_ROOT=/opt/kicad-happy /opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py --selftest
# run: KRT_MCP_TOKEN=... /opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py   (listens on 0.0.0.0:3335)
