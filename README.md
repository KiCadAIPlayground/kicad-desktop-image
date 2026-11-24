# kicad-desktop-image

The browser KiCad desktop for the team ([k3s-cluster](https://github.com/KiCadAIPlayground/k3s-cluster) `apps/kicad`): `lscr.io/linuxserver/kicad` plus plugins that need native parts or extra Python packages.

- **KiCadRoutingTools**: the Rust router (`grid_router`, a pyo3 extension) is built for musl in the first stage, because upstream only ships glibc builds. scipy, shapely and Pillow come from Alpine packages, since KiCad's Python is the system Python. Staged at `/opt/kicad-plugins/KiCadRoutingTools`; the pod's init container copies it into `~/.local/share/kicad/10.0/3rdparty/plugins/` on start.

Bump: change `KICAD_IMAGE`, or `KRT_VERSION` and `KRT_SHA` (the commit of upstream release tag `v<KRT_VERSION>` of drandyhaas/KiCadRoutingTools) in the Dockerfile and merge. The workflow pushes `ghcr.io/kicadaiplayground/kicad-desktop:<kicad tag>-krt<KRT_VERSION>`, e.g. `10.0.3-r0-ls158-krt0.24.2`. Then update the image tag in `k3s-cluster/apps/kicad/kicad.yaml`.

## krt-mcp

The same image also holds an MCP server (`krt` and `happy` suites) over KiCadRoutingTools and [kicad-happy](https://github.com/KiCadAIPlayground/kicad-happy) (baked in at `/opt/kicad-happy`, pinned by `KH_SHA`, with ngspice and poppler-utils), `/opt/krt-mcp/server.py` (Python `mcp` 2.3.0 in `/opt/krt-mcp/venv`, `--system-site-packages` so the apk numpy/scipy/shapely are visible). Run it as its own Deployment with a different command, uid 1000, the KiCad home volume at `/config` and `HOME=/config` (git reads `/config/.gitconfig`):

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
| `GITHUB_APP_ID`, `GITHUB_APP_KEY_FILE`, `GITHUB_ORG` | all three set = git features on, otherwise off (the server still runs) |
| `KRT_PICKUP_SECONDS` | `300` |
| `KRT_PICKUP_TEMPLATE` | `KiCadAIPlayground/kicad-project-template` |
| `KRT_PICKUP_EXTRA` | `Test-KiCAD` (comma list of repo names) |
| `KRT_ENV_ALLOW` | `FANOUT_ESCAPE,PYTHONHASHSEED,KRT_SEED` |

Tools: `list_tools(suite, door)`, `tool_help(suite, tool)`, `skill_doc(skill, suite="happy")` (SKILL.md, for clients without skills), `run_tool(suite, tool, args, project, timeout_s=900)` (registry tools only; `happy` tools are named `<skill>/<script>.py`, the runnable ones among `skills/*/scripts/*.py`; no shell, cwd = `KRT_WORKSPACE/<project>`, args that end in a file extension or exist in the project must stay inside the workspace, `..` is rejected, one run at a time, returns exit code, tails of stdout/stderr and changed files), `list_files(project, pattern)`, `upload_file(project, relpath, content_base64)` (to `uploads/<project>/`, max 50 MB), `download_file(path)` (max 50 MB), and git: `project_list()`, `project_pull(project)`, `project_status(project)`, `project_pickup()`, `project_publish(project, message, paths=None)`. Git calls share the run lock with `run_tool`.

`run_tool` takes `env` (dict): only names in `KRT_ENV_ALLOW`, values matching `^[A-Za-z0-9_.,:/=+-]{0,200}$`, anything else is rejected.

### Git service (GitHub App)
Git is HTTPS only, with a GitHub App installation token: JWT (RS256, signed with the `openssl` binary) from `GITHUB_APP_ID` + `GITHUB_APP_KEY_FILE`, installation looked up via `GET /orgs/$GITHUB_ORG/installation`, token requested with only `contents:write, pull_requests:write, workflows:write, metadata:read` and cached until 5 minutes before expiry. The token reaches git through the environment of that one process (a credential helper that echoes `$KRT_GIT_TOKEN`), never a file, URL or log line. An `origin` of `git@github.com:o/r` is switched to `https://github.com/o/r.git` on first use.
- **Pickup**: every `KRT_PICKUP_SECONDS` a background task lists the installation's repos (`GET /installation/repositories`) and clones into the workspace those missing ones whose `template_repository` is `KRT_PICKUP_TEMPLATE` or whose name is in `KRT_PICKUP_EXTRA`. Logs only repo names. `project_pickup()` runs it once and returns `cloned` / `present` / `failed`.
- **`POST /git-token`** (same bearer token as `/mcp`): body in git-credential format; only `protocol=https` + `host=github.com` gets `username=x-access-token\npassword=<token>`, anything else 403. `/usr/local/bin/git-credential-k3s` (in the image) is the matching helper for the desktop: on `get` it POSTs stdin there with `Authorization: Bearer $KRT_MCP_TOKEN`; `store`/`erase` are ignored. Set it with `git config --global credential.https://github.com.helper /usr/local/bin/git-credential-k3s`.
- **`project_publish` = pull request**: commits (trailer `Via: krt-mcp`) on a new branch `cluster/<UTC yyyymmdd-HHMM>-<slug of message>`, pushes that branch and opens a PR against the default branch, returning the PR URL. It never pushes to the default branch. Afterwards the clone is switched back to the default branch (`git checkout <default>`); the pushed `cluster/...` branch stays on the remote (and as a local branch). If the push fails, or the default branch cannot be looked up, the clone stays on the new branch and the message says so.

The user `abc` has uid/gid 1000 in the image (the base creates 911, its init remaps to `PUID`/`PGID` at runtime), so pods that skip the s6 init, like krt-mcp, have a passwd entry. `openssh-server` is installed for later, not configured or started.

KiKit 1.8.1 (backend for the KiKit PCM plugin) is pip-installed into the system Python. `python3 server.py --selftest` checks the path guard, the registry, branch slugs, the env allow-list, the credential reply and the JWT (with a throwaway key); the Dockerfile runs it at build time.

## Claude Code on the shared desktop

Claude Code (pinned, npm `stable` tag, auto-updater off) is installed in the image. Each person starts it with `claude-me <name>` in a desktop terminal: that sets `CLAUDE_CONFIG_DIR=/config/claude/<name>` (mode 700, created on first use), starts in `/config/projects` and runs `claude --plugin-dir /opt/kicad-happy --add-dir <KiCadRoutingTools>` (kicad-happy skills as a plugin, the routing skills from its `.claude/skills`) with the remaining args, so everyone logs in with their own Anthropic account and no subscription is shared. `claude-me` without a name lists the existing profiles and asks for one on a terminal (a desktop icon can run `foot -e /usr/local/bin/claude-me`); names are `[a-z0-9-]{1,32}`.

## Claude login on the shared desktop (active driver)
`claude-me <name>` runs Claude Code on that person's own profile (`/config/claude/<name>`) and makes them the
**active driver** (`/config/claude/.active`). `/usr/local/sbin/claude` (ahead of the real CLI on `PATH`) is a wrapper:
with `CLAUDE_CONFIG_DIR` set it passes through; without (the KiCadRoutingTools AI buttons, run by the shared KiCad
process) it uses the active driver's profile, and with no driver it refuses. There is never a shared default login.
Whoever ran `claude-me` last pays for AI runs started from KiCad.
