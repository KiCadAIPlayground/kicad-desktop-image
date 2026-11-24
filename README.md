# kicad-desktop-image

The browser KiCad desktop for the team ([k3s-cluster](https://github.com/KiCadAIPlayground/k3s-cluster) `apps/kicad`): `lscr.io/linuxserver/kicad` plus plugins that need native parts or extra Python packages.

- **KiCadRoutingTools**: the Rust router (`grid_router`, a pyo3 extension) is built for musl in the first stage, because upstream only ships glibc builds. scipy, shapely and Pillow come from Alpine packages, since KiCad's Python is the system Python. Staged at `/opt/kicad-plugins/KiCadRoutingTools`; the pod's init container copies it into `~/.local/share/kicad/10.0/3rdparty/plugins/` on start.

Bump: change `KICAD_IMAGE`, or `KRT_VERSION` and `KRT_SHA` (the commit of upstream release tag `v<KRT_VERSION>` of drandyhaas/KiCadRoutingTools) in the Dockerfile and merge. The workflow pushes `ghcr.io/kicadaiplayground/kicad-desktop:<kicad tag>-krt<KRT_VERSION>`, e.g. `10.0.3-r0-ls158-krt0.24.2`. Then update the image tag in `k3s-cluster/apps/kicad/kicad.yaml`.

## krt-mcp

The same image also holds an MCP server (`krt` and `happy` suites) over KiCadRoutingTools and [kicad-happy](https://github.com/KiCadAIPlayground/kicad-happy) (baked in at `/opt/kicad-happy`, pinned by `KH_SHA`, with ngspice and poppler-utils), `/opt/krt-mcp/server.py` (Python `mcp` 2.3.0 in `/opt/krt-mcp/venv`, `--system-site-packages` so the apk numpy/scipy/shapely are visible). Run it as its own Deployment with a different command, uid 1000, the KiCad home volume at `/config` and `HOME=/config` (git uses `/config/.ssh` and `/config/.gitconfig`):

```
/opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py
```

Streamable HTTP, stateless, `0.0.0.0:3335`, endpoint `/mcp`. `GET /healthz` is open; everything else needs `Authorization: Bearer $KRT_MCP_TOKEN` (constant-time compare, 401 otherwise). It refuses to start with an empty token.

| Env | Default |
|---|---|
| `KRT_MCP_TOKEN` | required |
| `KRT_ROOT` | `/opt/kicad-plugins/KiCadRoutingTools` |
| `KH_ROOT` | `/opt/kicad-happy` |
| `KRT_WORKSPACE` | `/config/projects` |

Tools: `list_tools(suite, door)`, `tool_help(suite, tool)`, `skill_doc(skill, suite="happy")` (SKILL.md, for clients without skills), `run_tool(suite, tool, args, project, timeout_s=900)` (registry tools only; `happy` tools are named `<skill>/<script>.py`, the runnable ones among `skills/*/scripts/*.py`; no shell, cwd = `KRT_WORKSPACE/<project>`, args that end in a file extension or exist in the project must stay inside the workspace, `..` is rejected, one run at a time, returns exit code, tails of stdout/stderr and changed files), `list_files(project, pattern)`, `upload_file(project, relpath, content_base64)` (to `uploads/<project>/`, max 50 MB), `download_file(path)` (max 50 MB), and git: `project_list()`, `project_pull(project)`, `project_status(project)`, `project_publish(project, message, paths=None)` (add, commit with a `Via: krt-mcp` trailer, push; one pull --rebase and retry on non-fast-forward). Git calls share the run lock with `run_tool`.

KiKit 1.8.1 (backend for the KiKit PCM plugin) is pip-installed into the system Python. `python3 server.py --selftest` checks the path guard and the registry; the Dockerfile runs it at build time.

## Claude Code on the shared desktop

Claude Code (pinned, npm `stable` tag, auto-updater off) is installed in the image. Each person starts it with `claude-me <name>` in a desktop terminal: that sets `CLAUDE_CONFIG_DIR=/config/claude/<name>` (mode 700, created on first use), starts in `/config/projects` and runs `claude --plugin-dir /opt/kicad-happy --add-dir <KiCadRoutingTools>` (kicad-happy skills as a plugin, the routing skills from its `.claude/skills`) with the remaining args, so everyone logs in with their own Anthropic account and no subscription is shared. `claude-me` without a name lists the existing profiles and asks for one on a terminal (a desktop icon can run `foot -e /usr/local/bin/claude-me`); names are `[a-z0-9-]{1,32}`.

## Claude login on the shared desktop (active driver)
`claude-me <name>` runs Claude Code on that person's own profile (`/config/claude/<name>`) and makes them the
**active driver** (`/config/claude/.active`). `/usr/local/sbin/claude` (ahead of the real CLI on `PATH`) is a wrapper:
with `CLAUDE_CONFIG_DIR` set it passes through; without (the KiCadRoutingTools AI buttons, run by the shared KiCad
process) it uses the active driver's profile, and with no driver it refuses. There is never a shared default login.
Whoever ran `claude-me` last pays for AI runs started from KiCad.
