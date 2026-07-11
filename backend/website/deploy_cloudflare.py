"""Cloudflare Pages deploy adapter for the headless site generator.

Takes a ``GeneratedSite`` (from ``site_builder``) and publishes it to Cloudflare
Pages. Two parts:

  * ``prepare_build_dir`` — writes the site files PLUS a ``_headers`` file. The
    headers are the win: CSP / HSTS / X-Frame-Options / X-Content-Type-Options /
    Referrer-Policy / Permissions-Policy — exactly the six the passive security
    audit flags, so a CF-Pages site reaches **security grade A** (impossible on
    a Wix subdomain, where headers are platform-owned).

  * ``deploy_via_wrangler`` — runs ``wrangler pages deploy`` (the supported
    programmatic path) with ``CLOUDFLARE_API_TOKEN`` (scope: Account ->
    Cloudflare Pages -> Edit) + ``CLOUDFLARE_ACCOUNT_ID``, and parses the
    ``*.pages.dev`` deployment URL from the output. Requires node/wrangler in
    the runtime; returns a structured result either way (never raises).

Dormant + keyless-safe: ``deploy_site`` does nothing without
``website_deploy_enabled`` + token/account/project. Building the dir + the
``_headers`` are pure and unit-tested; the live ``wrangler`` call needs a real
token to verify end-to-end.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .site_builder import GeneratedSite

_LOG = logging.getLogger("samus.website.deploy_cloudflare")
_CF_API = "https://api.cloudflare.com/client/v4"
_PAGES_URL_RE = re.compile(r"https://[\w.-]+\.pages\.dev\S*")

# Security headers that take a static site to grade A. CSP is permissive enough
# for the Tailwind Play CDN + Google Fonts the generator uses; tighten it once
# the build compiles Tailwind locally (then drop 'unsafe-eval').
_SECURITY_HEADERS: list[tuple[str, str]] = [
    (
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' https: data:; "
        "frame-src https://docs.google.com https://forms.gle; "
        "frame-ancestors 'self'",
    ),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload"),
    ("X-Frame-Options", "SAMEORIGIN"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ("Permissions-Policy", "geolocation=(), camera=(), microphone=()"),
]


@dataclass
class DeployResult:
    ok: bool
    url: str = ""
    project: str = ""
    log: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "url": self.url,
            "project": self.project,
            "log": self.log[-800:],
            "error": self.error,
        }


def build_security_headers() -> str:
    """Render the Cloudflare Pages ``_headers`` file (all paths -> security headers)."""
    lines = ["/*"]
    for name, value in _SECURITY_HEADERS:
        lines.append(f"  {name}: {value}")
    return "\n".join(lines) + "\n"


def prepare_build_dir(
    site: GeneratedSite, *, out_dir: str | Path, security_headers: bool = True
) -> Path:
    """Write the generated site + a _headers file into a build directory."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out_resolved = out.resolve()
    for name, content in site.files.items():
        path = out / name
        # PATCH-D4-PREPDIR-03 — a generated file name is data; refuse any path
        # that escapes the build dir (path-traversal / absolute-path write).
        if not path.resolve().is_relative_to(out_resolved):
            raise ValueError(f"refusing build path outside out_dir: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if security_headers:
        (out / "_headers").write_text(build_security_headers(), encoding="utf-8")
    return out


def create_project(
    *,
    account_id: str,
    api_token: str,
    project: str,
    production_branch: str = "main",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Best-effort create the Pages project (POST /pages/projects). 409 == exists."""
    url = f"{_CF_API}/accounts/{account_id}/pages/projects"
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                url,
                headers={"Authorization": f"Bearer {api_token}"},
                json={"name": project, "production_branch": production_branch},
            )
        return {"status": r.status_code, "body": (r.json() if r.content else {})}
    except httpx.HTTPError as exc:
        return {"status": None, "error": str(exc)}


def _run(cmd: list[str], *, env: dict[str, str], timeout: float):
    """subprocess wrapper (monkeypatched in tests). utf-8 so wrangler's emoji
    output doesn't trip the host's default (cp1252) decoder."""
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def deploy_via_wrangler(
    build_dir: str | Path,
    *,
    project: str,
    api_token: str,
    account_id: str,
    branch: str = "main",
    timeout: float = 300.0,
) -> DeployResult:
    """Deploy a build dir to Cloudflare Pages via ``wrangler pages deploy``."""
    env = os.environ.copy()
    env["CLOUDFLARE_API_TOKEN"] = api_token
    env["CLOUDFLARE_ACCOUNT_ID"] = account_id
    # Resolve npx to its full path so Windows finds npx.cmd (subprocess without
    # shell won't apply PATHEXT). On Linux/Docker this is just /usr/bin/npx.
    npx = shutil.which("npx") or "npx"
    cmd = [
        npx,
        "wrangler",
        "pages",
        "deploy",
        str(build_dir),
        f"--project-name={project}",
        f"--branch={branch}",
    ]
    try:
        proc = _run(cmd, env=env, timeout=timeout)
    except FileNotFoundError:
        return DeployResult(
            False, project=project, error="wrangler/npx not found (node runtime required)"
        )
    except subprocess.TimeoutExpired:
        return DeployResult(False, project=project, error=f"deploy timed out after {timeout}s")
    out = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    if getattr(proc, "returncode", 1) != 0:
        return DeployResult(
            False, project=project, log=out, error=f"wrangler exit {proc.returncode}"
        )
    m = _PAGES_URL_RE.search(out)
    return DeployResult(True, url=m.group(0) if m else "", project=project, log=out)


def deploy_site(
    site: GeneratedSite, *, settings: Any, out_dir: str | Path | None = None
) -> DeployResult:
    """Dormant orchestrator: prepare the build dir + deploy. Fail-closed."""
    if not getattr(settings, "website_deploy_enabled", False):
        return DeployResult(False, error="disabled")
    host = str(getattr(settings, "website_deploy_host", "cloudflare") or "cloudflare")
    if host != "cloudflare":
        return DeployResult(False, error=f"unsupported deploy host: {host}")
    token = str(getattr(settings, "cloudflare_api_token", "") or "")
    account = str(getattr(settings, "cloudflare_account_id", "") or "")
    project = str(getattr(settings, "cloudflare_pages_project", "") or "")
    if not (token and account and project):
        return DeployResult(False, error="cloudflare token/account/project unset")

    if out_dir is None:
        from backend.common.state_paths import state_path

        out_dir = state_path("website", "build")
    build_dir = prepare_build_dir(site, out_dir=out_dir)
    create_project(account_id=account, api_token=token, project=project)  # best-effort
    return deploy_via_wrangler(build_dir, project=project, api_token=token, account_id=account)


__all__ = [
    "DeployResult",
    "build_security_headers",
    "prepare_build_dir",
    "create_project",
    "deploy_via_wrangler",
    "deploy_site",
]
