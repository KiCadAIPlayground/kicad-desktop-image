"""krt-mcp: a small MCP server (suites "krt" and "happy") that exposes PCB tools to remote agents.

Suite krt = KiCadRoutingTools, suite happy = kicad-happy (the scripts of its skills).

Runs from the kicad-desktop image with a different command than the desktop itself
(`/opt/krt-mcp/venv/bin/python /opt/krt-mcp/server.py`), as its own Deployment that mounts the KiCad home volume
at /config. Streamable HTTP, stateless, 0.0.0.0:3335, MCP endpoint /mcp, unauthenticated GET /healthz.

Every /mcp request needs `Authorization: Bearer $KRT_MCP_TOKEN`; the server refuses to start without the token.

Environment:
  KRT_MCP_TOKEN   shared secret (required)
  KRT_ROOT        KiCadRoutingTools clone   (default /opt/kicad-plugins/KiCadRoutingTools)
  KH_ROOT         kicad-happy clone         (default /opt/kicad-happy)
  KRT_WORKSPACE   the only tree the tools may read or write (default /config/projects)
  GITHUB_APP_ID, GITHUB_APP_KEY_FILE, GITHUB_ORG   GitHub App credentials; all three set = git features on
                  (https only, installation tokens, project pickup, publish = PR), otherwise off
  KRT_PICKUP_SECONDS (300), KRT_PICKUP_TEMPLATE (KiCadAIPlayground/kicad-project-template),
  KRT_PICKUP_EXTRA (Test-KiCAD)   which installation repos the pickup loop clones
  KRT_ENV_ALLOW   env var names run_tool may set (FANOUT_ESCAPE,PYTHONHASHSEED,KRT_SEED)

Safety model: tools run only by registry name (never a path from the caller), without a shell, with cwd inside the
workspace, and every path-looking argument must resolve inside the workspace (symlinks included). One run at a time.

`python3 server.py --selftest` checks the path guard and that both registries list tools (needs no token).
"""
import ast
import asyncio
import base64
import hmac
import json
import urllib.error
import urllib.request
import threading
from datetime import datetime, timezone
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(os.environ.get("KRT_ROOT", "/opt/kicad-plugins/KiCadRoutingTools"))
KH_ROOT = Path(os.environ.get("KH_ROOT", "/opt/kicad-happy"))
WORKSPACE = Path(os.environ.get("KRT_WORKSPACE", "/config/projects"))
GH_API = "https://api.github.com"
APP_ID = os.environ.get("GITHUB_APP_ID", "")
APP_KEY_FILE = os.environ.get("GITHUB_APP_KEY_FILE", "")
ORG = os.environ.get("GITHUB_ORG", "")
GIT_ON = bool(APP_ID and APP_KEY_FILE and ORG and Path(APP_KEY_FILE).is_file())
# whatever the app has been granted, a token only ever carries these
TOKEN_PERMS = {"contents": "write", "pull_requests": "write", "workflows": "write", "metadata": "read"}
PICKUP_SECONDS = int(os.environ.get("KRT_PICKUP_SECONDS", "300"))
PICKUP_TEMPLATE = os.environ.get("KRT_PICKUP_TEMPLATE", "KiCadAIPlayground/kicad-project-template")
PICKUP_EXTRA = [n for n in os.environ.get("KRT_PICKUP_EXTRA", "Test-KiCAD").split(",") if n]
ENV_ALLOW = {n for n in os.environ.get("KRT_ENV_ALLOW", "FANOUT_ESCAPE,PYTHONHASHSEED,KRT_SEED").split(",") if n}
ENV_VALUE = re.compile(r"[A-Za-z0-9_.,:/=+-]{0,200}")
MAX_FILE = 50 * 1024 * 1024
TAIL = 20000
PATH_EXTS = (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_prl", ".kicad_dru", ".json", ".txt", ".csv", ".png",
             ".svg", ".gif", ".mp4", ".zip", ".md", ".yaml", ".yml")

INSTRUCTIONS = (
    "PCB tools in two suites: `krt` (KiCadRoutingTools: placement and routing) and `happy` (kicad-happy: schematic/PCB "
    "analysis, EMC, SPICE, datasheets, BOM). The kicad-happy skills' SKILL.md files describe how to use these scripts: "
    "call skill_doc, then tool_help first. Tools run on projects (git repos) under the workspace; use project_pull "
    "before and project_publish after.")
mcp = MCPServer("krt-mcp", instructions=INSTRUCTIONS)
_lock = asyncio.Lock()


def guard(path, base=None):
    """Resolve `path` (symlinks followed) and return it only if it lies inside `base` (default: the workspace)."""
    base = (base or WORKSPACE).resolve()
    p = Path(path).resolve()
    if p != base and base not in p.parents:
        raise ValueError(f"path escapes the workspace: {path}")
    return p


def check_arg(cwd, a):
    """Reject a tool argument that is (or, as --flag=value, carries) a path outside the workspace.
    A path is: contains a '..' segment (always rejected), ends in a known file extension, or exists relative to cwd.
    Plain values such as /GND or F.Cu are left alone."""
    v = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
    if ".." in v.split("/"):
        raise ValueError(f"'..' in argument: {a}")
    if v.lower().endswith(PATH_EXTS) or (cwd / v).exists():
        guard(cwd / v)


_happy_cache = None


def _first_doc_line(f):
    try:
        doc = ast.get_docstring(ast.parse(Path(f).read_text(encoding="utf-8", errors="replace"))) or ""
    except SyntaxError:
        doc = ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _happy_registry():
    """Runnable top-level skills/*/scripts/*.py (answers --help with usage), computed once and cached."""
    global _happy_cache
    if _happy_cache is None:
        files = sorted(f for f in KH_ROOT.glob("skills/*/scripts/*.py") if not f.name.startswith(("_", "test_")))

        def probe(f):
            try:
                r = subprocess.run([sys.executable, "-X", "utf8", str(f), "--help"], capture_output=True, text=True,
                                   timeout=20, stdin=subprocess.DEVNULL, cwd=KH_ROOT)
            except subprocess.TimeoutExpired:
                return None
            if r.returncode or "usage" not in (r.stdout + r.stderr).lower():
                return None
            return {"path": f"{f.parent.parent.name}/{f.name}", "purpose": _first_doc_line(f),
                    "scope": [f.parent.parent.name], "kind": "script", "file": str(f.relative_to(KH_ROOT))}
        with ThreadPoolExecutor(8) as ex:
            _happy_cache = [t for t in ex.map(probe, files) if t]
    return _happy_cache


def _suite(suite):
    if suite not in ("krt", "happy"):
        raise ValueError("suite must be 'krt' or 'happy'")
    return ROOT if suite == "krt" else KH_ROOT


_krt_cache = None


def _krt_registry():
    """krt_registry.py --json, computed once and cached: it probes --help of every tool (~80 s in the pod)."""
    global _krt_cache
    if _krt_cache is None:
        cmd = [sys.executable, "-X", "utf8", str(ROOT / "krt_registry.py"), "--json", "--root", str(ROOT)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"krt_registry.py failed: {r.stderr[-2000:]}")
        _krt_cache = json.loads(r.stdout)["tools"]
    return _krt_cache


def _registry(suite="krt", door=None):
    _suite(suite)
    if suite == "happy":
        return _happy_registry()
    return [t for t in _krt_registry() if not door or door in t.get("scope", [])]


def _tool_file(suite, name):
    """Registry entry's script path; the name must be a registry path or (krt) its basename."""
    hits = [t for t in _registry(suite) if t["path"] == name or
            (suite == "krt" and Path(t["path"]).name in (name, name + ".py"))]
    if len(hits) != 1:
        raise ValueError(f"unknown or ambiguous tool {name!r}: {[t['path'] for t in hits] or 'use list_tools'}")
    return guard(_suite(suite) / hits[0].get("file", hits[0]["path"]), _suite(suite))


def _project(project):
    p = guard(WORKSPACE / project)
    if not p.is_dir():
        raise ValueError(f"project not found: {project}")
    return p


@mcp.tool()
def list_tools(suite: str = "krt", door: str | None = None) -> list[dict]:
    """Tools of a suite ('krt' or 'happy') as name, purpose, scope, kind; door (krt only): placement, routing, combined."""
    return [{"name": t["path"], "purpose": t["purpose"], "scope": t["scope"], "kind": t["kind"]}
            for t in _registry(suite, door)]


@mcp.tool()
def tool_help(suite: str, tool: str) -> str:
    """The --help text of a registry tool (name as given by list_tools)."""
    r = subprocess.run([sys.executable, "-X", "utf8", str(_tool_file(suite, tool)), "--help"],
                       capture_output=True, text=True, timeout=60, cwd=_suite(suite))
    return r.stdout + r.stderr


@mcp.tool()
def skill_doc(skill: str = "", suite: str = "happy", offset: int = 0, max_chars: int = 40000) -> str:
    """SKILL.md of a skill (happy: skills/<skill>; krt: .claude/skills/<skill>): how to use its scripts.
    Paged (some are 260k chars): follow the 'next offset' line. Empty skill lists the suite's skills.
    The first page starts with a table of contents (## headings with their offsets) to jump to a step."""
    root = _suite(suite) / ("skills" if suite == "happy" else ".claude/skills")
    if not skill:
        return "\n".join(sorted(d.name for d in root.iterdir() if (d / "SKILL.md").is_file()))
    if not re.fullmatch(r"[a-z0-9-]+", skill):
        raise ValueError("invalid skill name")
    f = root / skill / "SKILL.md"
    if not f.is_file():
        raise ValueError(f"no such skill: {skill}")
    text = f.read_text(encoding="utf-8", errors="replace")
    offset, max_chars = max(0, offset), min(max(1000, max_chars), 80000)
    end = min(len(text), offset + max_chars)
    head = f"[{skill}: chars {offset}-{end} of {len(text)}"
    head += f"; next: skill_doc(skill={skill!r}, suite={suite!r}, offset={end})]" if end < len(text) else "; end]"
    if offset == 0 and end < len(text):
        toc = [f"  {m.start():>7}  {m.group(0)}" for m in re.finditer(r"^## .+$", text, re.M)]
        head += "\nContents (offset, heading):\n" + "\n".join(toc)
    return head + "\n\n" + text[offset:end]


def _snapshot(d):
    return {str(f): (f.stat().st_mtime_ns, f.stat().st_size) for f in d.rglob("*") if f.is_file()}


@mcp.tool()
async def run_tool(suite: str, tool: str, args: list[str], project: str, timeout_s: int = 900,
                   env: dict[str, str] | None = None) -> dict:
    """Run a tool of a suite (krt or happy) with args in WORKSPACE/<project>. Path-like args must stay inside the workspace.
    env: extra environment, only names in KRT_ENV_ALLOW (FANOUT_ESCAPE, PYTHONHASHSEED, KRT_SEED by default)."""
    script, cwd, extra = _tool_file(suite, tool), _project(project), check_env(env)
    for a in args:
        check_arg(cwd, a)
    if _lock.locked():
        return {"error": "busy: another run is in progress"}
    async with _lock:
        before, t0 = _snapshot(cwd), time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-X", "utf8", str(script), *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **extra, "PYTHONUNBUFFERED": "1"})
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


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(app_id, key_file, now=None):
    """GitHub App JWT (RS256, 9 minutes), signed by the openssl binary: no crypto library in the image."""
    now = int(now or time.time())
    head = _b64url(b'{"alg":"RS256","typ":"JWT"}') + "." + _b64url(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_file)], input=head.encode(),
                         capture_output=True, check=True, timeout=20).stdout
    return head + "." + _b64url(sig)


def gh_api(method, path, token, body=None):
    """One GitHub REST call; errors carry status and a body snippet, never the token."""
    req = urllib.request.Request(
        GH_API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "krt-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {method} {path}: {e.code} {e.read()[:300].decode('utf-8', 'replace')}") from None


_tok = {"token": "", "exp": 0.0}
_tok_lock = threading.Lock()


def installation_token():
    """Scoped installation token of the org, cached until 5 minutes before it expires (blocking: use to_thread)."""
    if not GIT_ON:
        raise RuntimeError("git features are off (GITHUB_APP_ID, GITHUB_APP_KEY_FILE, GITHUB_ORG)")
    with _tok_lock:
        if _tok["token"] and _tok["exp"] - time.time() > 300:
            return _tok["token"]
        jwt = make_jwt(APP_ID, APP_KEY_FILE)
        inst = gh_api("GET", f"/orgs/{ORG}/installation", jwt)["id"]
        r = gh_api("POST", f"/app/installations/{inst}/access_tokens", jwt, {"permissions": TOKEN_PERMS})
        _tok["token"] = r["token"]
        _tok["exp"] = datetime.strptime(r["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return _tok["token"]


def credential_answer(request_body, token):
    """git-credential reply for a `get` request; only host=github.com gets a token. Returns (status, text)."""
    fields = dict(l.split("=", 1) for l in request_body.splitlines() if "=" in l)
    if fields.get("host") != "github.com":
        return 403, "host not allowed\n"
    return 200, f"username=x-access-token\npassword={token}\n"


def slug(text, n=40):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-") or "change"


def branch_name(message, now=None):
    return f"cluster/{(now or datetime.now(timezone.utc)):%Y%m%d-%H%M}-{slug(message.strip().splitlines()[0])}"


def check_env(env):
    """run_tool env: only allow-listed names, values of a boring character set."""
    for k, v in (env or {}).items():
        if k not in ENV_ALLOW:
            raise ValueError(f"env var not allowed: {k} (allowed: {sorted(ENV_ALLOW)})")
        if not ENV_VALUE.fullmatch(str(v)):
            raise ValueError(f"env value for {k} has characters outside A-Za-z0-9_.,:/=+- or is longer than 200")
    return {k: str(v) for k, v in (env or {}).items()}


GH_URL = "https://github.com"
# helper fed from the env of this one git process: the token is never on a command line or on disk
GIT_HELPER = '!f() { echo username=x-access-token; echo "password=$KRT_GIT_TOKEN"; }; f'


async def _git(cwd, *args, timeout=120, net=False):
    """Run git in cwd (list args, no shell); returns (exit code, combined output).
    net=True (clone, pull, push) authenticates to github.com with the installation token when git is on."""
    env, pre, token = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}, [], ""
    if net and GIT_ON:
        token = await asyncio.to_thread(installation_token)
        env["KRT_GIT_TOKEN"] = token
        # the empty values reset helpers from /config/.gitconfig (the desktop's helper would call this server)
        pre = ["-c", "credential.helper=", "-c", f"credential.{GH_URL}.helper=", "-c", f"credential.{GH_URL}.helper={GIT_HELPER}"]
    proc = await asyncio.create_subprocess_exec(
        "git", *pre, *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        out = (await proc.communicate())[0] + f"\n[killed after {timeout}s timeout]".encode()
    out = out.decode("utf-8", "replace")
    return proc.returncode, (out.replace(token, "***") if token else out)[-TAIL:]


async def _https_remote(cwd):
    """Convert an SSH github.com origin to https (first use); returns 'owner/repo' of origin, or '' if not github."""
    _, url = await _git(cwd, "remote", "get-url", "origin")
    m = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", url.strip())
    if not m:
        return ""
    if not url.strip().startswith("https://"):
        await _git(cwd, "remote", "set-url", "origin", f"{GH_URL}/{m.group(1)}.git")
    return m.group(1)


def _pickup_repos():
    token = installation_token()
    repos, page = [], 1
    while True:
        r = gh_api("GET", f"/installation/repositories?per_page=100&page={page}", token)["repositories"]
        repos += r
        if len(r) < 100:
            return repos
        page += 1


async def pickup_once():
    """Clone the installation's missing project repos (created from the template, or named in KRT_PICKUP_EXTRA)."""
    res = {"cloned": [], "present": [], "failed": []}
    for r in await asyncio.to_thread(_pickup_repos):
        name = r["name"]
        if not (((r.get("template_repository") or {}).get("full_name") == PICKUP_TEMPLATE) or name in PICKUP_EXTRA):
            continue
        dest = WORKSPACE / name
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", name) or dest.exists():
            res["present" if dest.exists() else "failed"].append(name)
            continue
        rc, _ = await _git(WORKSPACE, "clone", f"{GH_URL}/{r['full_name']}.git", name, timeout=600, net=True)
        res["cloned" if rc == 0 else "failed"].append(name)
    return res


async def pickup_loop():
    while True:
        try:
            res = await pickup_once()
            if res["cloned"] or res["failed"]:
                print(f"pickup: {res}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"pickup error: {e}", file=sys.stderr, flush=True)
        await asyncio.sleep(PICKUP_SECONDS)


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
    """git pull --rebase --autostash in the project (github.com over https with the app token)."""
    async def go(cwd):
        if GIT_ON:
            await _https_remote(cwd)
        rc, out = await _git(cwd, "pull", "--rebase", "--autostash", net=True)
        return f"exit {rc}\n{out}"
    return await _locked_git(project, go)


@mcp.tool()
async def project_status(project: str) -> str:
    """git status --porcelain=v1 plus git diff --stat."""
    async def go(cwd):
        return (await _git(cwd, "status", "--porcelain=v1"))[1] + "\n" + (await _git(cwd, "diff", "--stat"))[1]
    return await _locked_git(project, go)


@mcp.tool()
async def project_pickup() -> dict:
    """Run the pickup once: clone installation repos made from the project template (or in KRT_PICKUP_EXTRA) that are
    not in the workspace yet. Returns cloned / present / failed repo names."""
    if not GIT_ON:
        raise ValueError("git features are off")
    return await pickup_once()


@mcp.tool()
async def project_publish(project: str, message: str, paths: list[str] | None = None) -> str:
    """Commit (paths inside the project, or everything) with `message` + trailer 'Via: krt-mcp' on a new branch
    cluster/<UTC date-time>-<slug>, push that branch and open a pull request against the default branch (never pushed
    to). The clone stays on the new branch. Returns the PR URL."""
    if not message.strip():
        raise ValueError("empty commit message")
    if not GIT_ON:
        raise ValueError("git features are off (GITHUB_APP_* env missing)")

    async def go(cwd):
        for p in paths or []:
            guard(cwd / p, cwd)
        repo = await _https_remote(cwd)
        if not repo:
            return "error: origin is not a github.com repository"
        rc, out = await _git(cwd, "add", "--", *paths) if paths else await _git(cwd, "add", "-A")
        if rc:
            return f"git add failed:\n{out}"
        files = (await _git(cwd, "diff", "--cached", "--name-only"))[1].split()
        if not files:
            return "nothing to commit"
        branch = branch_name(message)
        for args in (("checkout", "-b", branch), ("commit", "-m", message.strip(), "-m", "Via: krt-mcp")):
            rc, out = await _git(cwd, *args)
            if rc:
                return f"git {args[0]} failed:\n{out}"
        sha = (await _git(cwd, "rev-parse", "HEAD"))[1].strip()
        rc, out = await _git(cwd, "push", "-u", "origin", f"HEAD:refs/heads/{branch}", net=True)
        if rc:
            return f"commit {sha} on {branch}\npush FAILED:\n{out}"
        token = await asyncio.to_thread(installation_token)
        try:
            base = (await asyncio.to_thread(gh_api, "GET", f"/repos/{repo}", token))["default_branch"]
            title = message.strip().splitlines()[0][:200]
            body = ("Opened by krt-mcp from the k3s cluster (project_publish).\n\nChanged files:\n"
                    + "\n".join(f"- `{f}`" for f in files[:100]) + ("\n- ..." if len(files) > 100 else ""))
            pr = await asyncio.to_thread(gh_api, "POST", f"/repos/{repo}/pulls", token,
                                         {"title": title, "head": branch, "base": base, "body": body})
        except RuntimeError as e:
            return f"commit {sha}, pushed {branch}, opening the PR failed: {e}"
        return pr["html_url"]
    return await _locked_git(project, go)


class BearerAuth:
    """ASGI middleware: GET /healthz is open, everything else needs the bearer token."""

    def __init__(self, app, token):
        self.app, self.token = app, token.encode()
        self.bg = None

    async def _git_token(self, receive):
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body") or len(body) > 8192:
                break
        if not GIT_ON:
            return 503, b"git features are off\n"
        try:
            status, text = credential_answer(body[:8192].decode("utf-8", "replace"), await asyncio.to_thread(installation_token))
        except Exception as e:
            print(f"git-token error: {e}", file=sys.stderr, flush=True)
            return 502, b"token mint failed\n"
        return status, text.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            if GIT_ON and self.bg is None:
                self.bg = asyncio.create_task(pickup_loop())
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # no websockets: the MCP app only speaks HTTP
            return
        if scope["path"] == "/healthz" and scope["method"] == "GET":
            status, body = 200, b"ok"
        else:
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, b"Bearer " + self.token):
                status, body = 401, b"unauthorized"
            elif scope["path"] == "/git-token":  # for the desktop's git credential helper
                status, body = await self._git_token(receive) if scope["method"] == "POST" else (405, b"POST only")
            else:
                return await self.app(scope, receive, send)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain"), (b"content-length", str(len(body)).encode())]
                    + ([(b"www-authenticate", b"Bearer")] if status == 401 else [])})
        await send({"type": "http.response.body", "body": body})


def selftest():
    global WORKSPACE
    with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as outside:
        WORKSPACE = Path(t)
        (WORKSPACE / "p").mkdir()
        for ok, a in [(True, "/GND"), (True, "F.Cu"), (True, "board.kicad_pcb"), (True, "--out=out.png"),
                      (False, "--out=/tmp/x.png"), (False, "../x"), (False, "/etc/passwd"), (False, "--in=a/../b")]:
            try:
                check_arg(WORKSPACE / "p", a)
                good = True
            except ValueError:
                good = False
            assert good == ok, f"check_arg({a}) expected {'allow' if ok else 'reject'}"
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
    assert slug("Route: GND / 5V!!") == "route-gnd-5v" and slug("!!!") == "change" and len(slug("a" * 99)) == 40
    assert branch_name("Fix  the board\nmore", datetime(2026, 11, 24, 9, 5, tzinfo=timezone.utc)) == "cluster/20261124-0905-fix-the-board"
    assert check_env({"FANOUT_ESCAPE": "1", "KRT_SEED": "a.b,c:/d=+-"}) == {"FANOUT_ESCAPE": "1", "KRT_SEED": "a.b,c:/d=+-"}
    for bad in ({"PATH": "x"}, {"KRT_SEED": "a b"}, {"KRT_SEED": "$(x)"}, {"KRT_SEED": "x" * 201}, {"LD_PRELOAD": "x"}):
        try:
            check_env(bad)
        except ValueError:
            continue
        raise AssertionError(f"check_env accepted {bad}")
    assert credential_answer("protocol=https\nhost=github.com\n", "T") == (200, "username=x-access-token\npassword=T\n")
    assert credential_answer("host=evil.com\n", "T")[0] == 403 and credential_answer("", "T")[0] == 403
    with tempfile.TemporaryDirectory() as k:
        key = Path(k) / "k.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
        pub = Path(k) / "k.pub"
        subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
        h, p, sg = make_jwt(123, key, 1000000).split(".")
        assert json.loads(base64.urlsafe_b64decode(p + "==")) == {"iat": 999940, "exp": 1000540, "iss": "123"}
        (Path(k) / "sig").write_bytes(base64.urlsafe_b64decode(sg + "=" * (-len(sg) % 4)))
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(Path(k) / "sig")],
                           input=f"{h}.{p}".encode(), capture_output=True)
        assert r.returncode == 0, "JWT signature does not verify"
    tools, happy = list_tools(), list_tools("happy")
    assert tools, "krt registry returned no tools"
    assert happy, "happy registry returned no tools"
    assert "usage" in tool_help("happy", happy[0]["name"]).lower(), "tool_help empty"
    assert "name:" in skill_doc("kicad"), "skill_doc empty"
    # paging: the pages of the biggest krt skill add up to the whole file, each page within the limit
    big = ROOT / ".claude/skills/plan-pcb-routing/SKILL.md"
    if big.is_file():
        text, got, off = big.read_text(encoding="utf-8", errors="replace"), "", 0
        while True:
            page = skill_doc("plan-pcb-routing", "krt", off)
            body = page.split("\n\n", 1)[1]
            assert len(body) <= 40000
            got += body
            m = re.search(r"offset=(\d+)\)\]", page.split("\n", 1)[0])
            if not m:
                break
            off = int(m.group(1))
        assert got == text, "skill_doc paging lost text"
    assert "plan-pcb-routing" in skill_doc("", "krt"), "skill list empty"
    print(f"selftest ok: {len(tools)} krt tools, {len(happy)} happy tools")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    token = os.environ.get("KRT_MCP_TOKEN", "")
    if not token:
        sys.exit("KRT_MCP_TOKEN is empty: refusing to start")
    import uvicorn
    _happy_registry()  # probe both registries once at startup (the pod is Ready afterwards)
    _krt_registry()
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="0.0.0.0",
                                  max_request_body_size=2 * MAX_FILE)  # upload_file: 50 MB as base64
    uvicorn.run(BearerAuth(app, token), host="0.0.0.0", port=3335, log_level="info")
