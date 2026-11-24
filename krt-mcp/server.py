"""krt-mcp: a small MCP server that exposes KiCadRoutingTools (KRT) to remote agents.

Runs from the kicad-desktop image with a different command than the desktop itself
(`/opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py`), as its own Deployment that mounts the KiCad home volume
at /config. Streamable HTTP, stateless, 0.0.0.0:3335, MCP endpoint /mcp, unauthenticated GET /healthz.

Every /mcp request needs `Authorization: Bearer $KRT_MCP_TOKEN`; the server refuses to start without the token.

Environment:
  KRT_MCP_TOKEN   shared secret (required)
  KRT_ROOT        KiCadRoutingTools clone   (default /opt/kicad-plugins/KiCadRoutingTools)
  KRT_WORKSPACE   the only tree the tools may read or write (default /config/projects)

Safety model: tools run only by registry name (never a path from the caller), without a shell, with cwd inside the
workspace, and every path-looking argument must resolve inside the workspace (symlinks included). One run at a time.

`python3 server.py --selftest` checks the path guard and that the registry lists tools (needs no token).
"""
import asyncio
import base64
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(os.environ.get("KRT_ROOT", "/opt/kicad-plugins/KiCadRoutingTools"))
WORKSPACE = Path(os.environ.get("KRT_WORKSPACE", "/config/projects"))
MAX_FILE = 50 * 1024 * 1024
TAIL = 20000
PATH_EXTS = (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".json", ".txt", ".csv")

mcp = FastMCP(
    "krt-mcp", host="0.0.0.0", port=3335, stateless_http=True, streamable_http_path="/mcp",
    max_request_body_size=2 * MAX_FILE,  # upload_file carries up to 50 MB as base64
)
_lock = asyncio.Lock()


def guard(path, base=None):
    """Resolve `path` (symlinks followed) and return it only if it lies inside `base` (default: the workspace)."""
    base = (base or WORKSPACE).resolve()
    p = Path(path).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"path escapes the workspace: {path}")
    return p


def _registry(door=None):
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "krt_registry.py"), "--json", "--root", str(ROOT)]
    if door:
        cmd += ["--door", door]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=ROOT)
    if r.returncode:
        raise RuntimeError(f"krt_registry.py failed: {r.stderr[-2000:]}")
    return json.loads(r.stdout)["tools"]


def _tool_file(name):
    """Registry entry's script path; the name must be a registry path or its basename."""
    hits = [t for t in _registry() if t["path"] == name or Path(t["path"]).name in (name, name + ".py")]
    if len(hits) != 1:
        raise ValueError(f"unknown or ambiguous tool {name!r}: {[t['path'] for t in hits] or 'use list_tools'}")
    return guard(ROOT / hits[0]["path"], ROOT)


def _project(project):
    p = guard(WORKSPACE / project)
    if not p.is_dir():
        raise ValueError(f"project not found: {project}")
    return p


@mcp.tool()
def list_tools(door: str | None = None) -> list[dict]:
    """KRT tools (name, purpose, scope, kind); door is placement, routing or combined."""
    return [{"name": t["path"], "purpose": t["purpose"], "scope": t["scope"], "kind": t["kind"]}
            for t in _registry(door)]


@mcp.tool()
def tool_help(tool: str) -> str:
    """The --help text of a registry tool (name as given by list_tools)."""
    r = subprocess.run([sys.executable, "-X", "utf8", str(_tool_file(tool)), "--help"],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    return r.stdout + r.stderr


def _snapshot(d):
    return {str(f): (f.stat().st_mtime_ns, f.stat().st_size) for f in d.rglob("*") if f.is_file()}


@mcp.tool()
async def run_tool(tool: str, args: list[str], project: str, timeout_s: int = 900) -> dict:
    """Run a registry tool with args in WORKSPACE/<project>. Path-like args must stay inside the workspace."""
    script, cwd = _tool_file(tool), _project(project)
    for a in args:
        v = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if "/" in v or v.lower().endswith(PATH_EXTS):
            guard(cwd / v)
    if _lock.locked():
        return {"error": "busy: another run is in progress"}
    async with _lock:
        before, t0 = _snapshot(cwd), time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(script), *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            err += f"\n[killed after {timeout_s}s timeout]".encode()
        after = _snapshot(cwd)
        return {
            "exit_code": proc.returncode,
            "duration_s": round(time.monotonic() - t0, 1),
            "stdout": out.decode("utf-8", "replace")[-TAIL:],
            "stderr": err.decode("utf-8", "replace")[-TAIL:],
            "changed_files": sorted(str(Path(f).relative_to(cwd)) for f, s in after.items() if before.get(f) != s),
        }


@mcp.tool()
def list_files(project: str, pattern: str = "**/*.kicad_*") -> list[dict]:
    """Files of a project (glob pattern) as relative path and size."""
    d = _project(project)
    return [{"path": str(f.relative_to(d)), "size": f.stat().st_size}
            for f in sorted(d.glob(pattern)) if f.is_file() and f.resolve().is_relative_to(WORKSPACE.resolve())]


@mcp.tool()
def upload_file(project: str, relpath: str, content_base64: str) -> str:
    """Write a file to WORKSPACE/uploads/<project>/<relpath> (max 50 MB). Returns its path relative to the workspace;
    use project="uploads/<project>" to run tools on it."""
    if len(content_base64) > MAX_FILE * 4 // 3 + 8:
        raise ValueError("file larger than 50 MB")
    data = base64.b64decode(content_base64, validate=True)
    if len(data) > MAX_FILE:
        raise ValueError("file larger than 50 MB")
    dest = guard(WORKSPACE / "uploads" / project / relpath)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = guard(dest)  # re-check after mkdir, in case a symlink was traversed
    dest.write_bytes(data)
    return str(dest.relative_to(WORKSPACE.resolve()))


@mcp.tool()
def download_file(path: str) -> dict:
    """Base64 content of a file inside the workspace (max 50 MB). path is relative to the workspace."""
    f = guard(WORKSPACE / path)
    if not f.is_file() or f.stat().st_size > MAX_FILE:
        raise ValueError("not a file, or larger than 50 MB")
    return {"path": path, "size": f.stat().st_size, "content_base64": base64.b64encode(f.read_bytes()).decode()}


async def _git(cwd, *args, timeout=120):
    """Run git in cwd (list args, no shell); returns (exit code, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out = (await proc.communicate())[0] + f"\n[killed after {timeout}s timeout]".encode()
    return proc.returncode, out.decode("utf-8", "replace")[-TAIL:]


@mcp.tool()
async def project_list() -> list[dict]:
    """Git repos directly under the workspace and under uploads/: branch, last commit, dirty flag."""
    res = []
    for d in sorted([*WORKSPACE.glob("*"), *WORKSPACE.glob("uploads/*")]):
        if d.is_dir() and (d / ".git").exists() and d.resolve().is_relative_to(WORKSPACE.resolve()):
            _, branch = await _git(d, "rev-parse", "--abbrev-ref", "HEAD")
            _, last = await _git(d, "log", "-1", "--format=%H%x09%s%x09%cI")
            _, dirty = await _git(d, "status", "--porcelain")
            sha, _, rest = last.strip().partition("\t")
            subject, _, date = rest.rpartition("\t")
            res.append({"project": str(d.relative_to(WORKSPACE)), "branch": branch.strip(),
                        "sha": sha, "subject": subject, "date": date, "dirty": bool(dirty.strip())})
    return res


async def _locked_git(project, *calls):
    """Run git commands in a project under the shared run lock; returns the joined output."""
    cwd = _project(project)
    if _lock.locked():
        return "error: busy: another run is in progress"
    async with _lock:
        return await calls[0](cwd)


@mcp.tool()
async def project_pull(project: str) -> str:
    """git pull --rebase --autostash in the project."""
    async def go(cwd):
        rc, out = await _git(cwd, "pull", "--rebase", "--autostash")
        return f"exit {rc}\n{out}"
    return await _locked_git(project, go)


@mcp.tool()
async def project_status(project: str) -> str:
    """git status --porcelain=v1 plus git diff --stat."""
    async def go(cwd):
        return (await _git(cwd, "status", "--porcelain=v1"))[1] + "\n" + (await _git(cwd, "diff", "--stat"))[1]
    return await _locked_git(project, go)


@mcp.tool()
async def project_publish(project: str, message: str, paths: list[str] | None = None) -> str:
    """Commit (paths inside the project, or everything) with `message` + trailer 'Via: krt-mcp', then git push.
    On a non-fast-forward push: one pull --rebase, one retry. Returns commit sha and push output."""
    if not message.strip():
        raise ValueError("empty commit message")

    async def go(cwd):
        for p in paths or []:
            guard(cwd / p, cwd)
        rc, out = await _git(cwd, "add", "--", *paths) if paths else await _git(cwd, "add", "-A")
        if rc:
            return f"git add failed:\n{out}"
        rc, out = await _git(cwd, "commit", "-m", message.strip(), "-m", "Via: krt-mcp")
        if rc:
            return f"git commit failed (nothing to commit?):\n{out}"
        sha = (await _git(cwd, "rev-parse", "HEAD"))[1].strip()
        rc, out = await _git(cwd, "push")
        if rc:
            prc, pout = await _git(cwd, "pull", "--rebase", "--autostash")
            if prc:
                return f"commit {sha}\npush failed, rebase failed:\n{out}\n{pout}"
            sha = (await _git(cwd, "rev-parse", "HEAD"))[1].strip()
            rc, out = await _git(cwd, "push")
        return f"commit {sha}\npush {'ok' if rc == 0 else 'FAILED'}:\n{out}"
    return await _locked_git(project, go)


class BearerAuth:
    """ASGI middleware: GET /healthz is open, everything else needs the bearer token."""

    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            status, body = 200, b"ok"
        else:
            got = dict(scope["headers"]).get(b"authorization", b"")
            if hmac.compare_digest(got, b"Bearer " + self.token):
                return await self.app(scope, receive, send)
            status, body = 401, b"unauthorized"
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]
                    + ([(b"www-authenticate", b"Bearer")] if status == 401 else [])})
        await send({"type": "http.response.body", "body": body})


def selftest():
    global WORKSPACE
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as outside:
        WORKSPACE = Path(t)
        (WORKSPACE / "p").mkdir()
        (WORKSPACE / "link").symlink_to(outside)
        for ok, path in [(True, WORKSPACE / "p" / "a.kicad_pcb"), (True, WORKSPACE / "p" / ".." / "p"),
                         (False, WORKSPACE / ".." / "x"), (False, Path("/etc/passwd")),
                         (False, WORKSPACE / "link" / "x")]:
            try:
                guard(path)
                good = True
            except ValueError:
                good = False
            assert good == ok, f"guard({path}) expected {'allow' if ok else 'reject'}"
    tools = list_tools()
    assert tools, "registry returned no tools"
    assert "usage" in tool_help(tools[0]["name"]).lower() or tool_help(tools[0]["name"]), "tool_help empty"
    print(f"selftest ok: {len(tools)} tools")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    token = os.environ.get("KRT_MCP_TOKEN", "")
    if not token:
        sys.exit("KRT_MCP_TOKEN is empty: refusing to start")
    import uvicorn
    uvicorn.run(BearerAuth(mcp.streamable_http_app(), token), host="0.0.0.0", port=3335, log_level="info")
