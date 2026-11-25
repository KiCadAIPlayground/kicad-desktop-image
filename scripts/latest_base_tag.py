#!/usr/bin/env python3
"""Print the newest stable linuxserver/kicad tag (X.Y.Z-rN-lsNNN) from GHCR, then the Dockerfile's pinned tag.

Usage: latest_base_tag.py [--selftest]   (output: "<newest> <current>")
"""
import json, re, sys, urllib.request

STABLE = re.compile(r"^\d+\.\d+\.\d+-r\d+-ls\d+$")
PIN = re.compile(r"^ARG KICAD_IMAGE=.*:(\S+)$", re.M)


def key(tag):
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def newest(tags):
    stable = [t for t in tags if STABLE.match(t)]
    return max(stable, key=key) if stable else None


def get(url, token=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def selftest():
    tags = ["10.0.3-r0-ls9", "10.0.3-r0-ls159", "10.0.2-r1-ls200", "9.9.9-r9-ls999", "10.0.3-r1-ls1",
            "amd64-10.0.4-r0-ls1", "arm64-10.0.4-r0-ls1", "version-10.0.4", "latest", "nightly", "10.0.4-r0-ls1-x"]
    assert newest(tags) == "10.0.3-r1-ls1", newest(tags)
    assert newest(tags[:2]) == "10.0.3-r0-ls159"  # numeric, not lexical, on ls
    assert newest(["latest", "amd64-1.0.0-r0-ls1"]) is None
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    token = get("https://ghcr.io/token?scope=repository:linuxserver/kicad:pull")["token"]
    tags = get("https://ghcr.io/v2/linuxserver/kicad/tags/list?n=5000", token)["tags"]
    latest = newest(tags)
    if not latest:
        sys.exit("no stable tag found")
    print(latest, PIN.search(open("Dockerfile").read()).group(1))
