#!/usr/bin/env python3
"""Operational scratch: verify Docker Hub repository visibility, emit no secrets."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request


REPO_URL = "https://hub.docker.com/v2/repositories/king2eze/polymath-local-extraction/"


def request(url: str, *, method: str = "GET", body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"JWT {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {}


credential = json.loads(
    subprocess.run(
        ["docker-credential-desktop", "get"],
        input="https://index.docker.io/v1/\n",
        text=True,
        check=True,
        capture_output=True,
    ).stdout
)
status, login = request(
    "https://hub.docker.com/v2/users/login/",
    method="POST",
    body={"username": credential["Username"], "password": credential["Secret"]},
)
if status != 200 or not login.get("token"):
    raise SystemExit("authenticated Docker Hub login failed")
auth_status, repo = request(REPO_URL, token=login["token"])
unauth_status, _ = request(REPO_URL)
if auth_status != 200 or repo.get("is_private") is not True:
    raise SystemExit("Docker Hub repository is not verified private")
if unauth_status != 404:
    raise SystemExit("Docker Hub repository is visible without authentication")
print(
    json.dumps(
        {
            "authenticated_status": auth_status,
            "is_private": True,
            "name": repo.get("name"),
            "namespace": repo.get("namespace"),
            "unauthenticated_status": unauth_status,
            "secret_values_emitted": 0,
        },
        sort_keys=True,
    )
)
