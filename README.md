# kicad-desktop-image

The browser KiCad desktop for the team ([k3s-cluster](https://github.com/KiCadAIPlayground/k3s-cluster) `apps/kicad`): `lscr.io/linuxserver/kicad` plus plugins that need native parts or extra Python packages.

- **KiCadRoutingTools**: the Rust router (`grid_router`, a pyo3 extension) is built for musl in the first stage, because upstream only ships glibc builds. scipy, shapely and Pillow come from Alpine packages, since KiCad's Python is the system Python. Staged at `/opt/kicad-plugins/KiCadRoutingTools`; the pod's init container copies it into `~/.local/share/kicad/10.0/3rdparty/plugins/` on start.

Bump: change `KICAD_IMAGE` or `KRT_SHA` in the Dockerfile and merge. The workflow pushes `ghcr.io/kicadaiplayground/kicad-desktop:<kicad tag>-krt<sha7>`. Then update the image tag in `k3s-cluster/apps/kicad/kicad.yaml`.
