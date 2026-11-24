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
 && rm -rf /src/rust_router/target /src/.git

FROM ${KICAD_IMAGE}
# KiCad's Python is the system python (pip is externally managed): the plugin's deps come from Alpine packages
RUN apk add --no-cache py3-numpy py3-scipy py3-shapely py3-pillow git openssh-client
COPY --from=krt-build /src /opt/kicad-plugins/KiCadRoutingTools
# smoke test: the router extension loads in KiCad's Python
RUN cd /opt/kicad-plugins/KiCadRoutingTools/rust_router && python3 -c "import grid_router; print('grid_router ok')"

# krt-mcp: MCP server over KiCadRoutingTools, same image, different command (see krt-mcp/server.py).
# The venv sees the apk numpy/scipy/shapely; `mcp` is pinned to the last 1.x because 2.x dropped mcp.server.fastmcp.
RUN python3 -m venv --system-site-packages /opt/krt-mcp/venv \
 && /opt/krt-mcp/venv/bin/pip install --no-cache-dir mcp==1.30.0 uvicorn
COPY krt-mcp/server.py /opt/krt-mcp/server.py
RUN KRT_ROOT=/opt/kicad-plugins/KiCadRoutingTools /opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py --selftest
# run: KRT_MCP_TOKEN=... /opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py   (listens on 0.0.0.0:3335)
