"""S15 — security-critical dependencies are pinned EXACT (re-float guard).

Fails if ``cryptography`` / ``jinja2`` / ``structlog`` revert to a floating
spec (>=, <, ~=, *, or a bare name). A silent major drift on these could
change Ed25519/HKDF/HMAC behaviour (cryptography), reintroduce a template
autoescape/sandbox regression (jinja2), or change the audit-trail log-record
shape (structlog).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REQ = Path(__file__).resolve().parent.parent / "requirements.txt"

_SECURITY_CRITICAL = {"cryptography", "jinja2", "structlog"}

_EXACT_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*[0-9][^#\s]*\s*(#.*)?$")


def _parse_specs() -> dict[str, str]:
    specs: dict[str, str] = {}
    for raw in _REQ.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[\[<>=!~ ]", line, maxsplit=1)[0].lower()
        specs[name] = line
    return specs


def test_requirements_file_exists() -> None:
    assert _REQ.exists()


@pytest.mark.parametrize("dep", sorted(_SECURITY_CRITICAL))
def test_security_critical_deps_are_exact_pinned(dep: str) -> None:
    specs = _parse_specs()
    assert dep in specs, f"{dep} missing from requirements.txt"
    assert _EXACT_PIN_RE.match(specs[dep]), (
        f"{dep} must be EXACT-pinned (==X.Y.Z); got: {specs[dep]!r}"
    )


def test_no_security_critical_dep_uses_floating_range() -> None:
    specs = _parse_specs()
    for dep in _SECURITY_CRITICAL:
        line = specs.get(dep, "")
        for floaty in (">=", "<=", "~=", ">", "<", "*"):
            assert floaty not in line, (
                f"{dep} uses a floating spec {floaty!r}: {line!r}"
            )
