"""Hardship-context loader.

Thin wrapper — hardship.yaml is mostly static facts (CalFresh case,
banking vehicles, other-evidence list). The loader validates the shape
and tags ``registry_loaded`` so /snapshot can show "no hardship data
on file" vs the populated case.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .models import HardshipContext


_LOG = logging.getLogger("samus.finance.hardship")
_DEFAULT_HARDSHIP_PATH = Path(__file__).resolve().parent / "hardship.yaml"


def hardship_path():
    override = os.getenv("SAMUS_HARDSHIP_PATH")
    return Path(override) if override else _DEFAULT_HARDSHIP_PATH


def load_context() -> HardshipContext:
    path = hardship_path()
    if not path.exists():
        _LOG.info("hardship.yaml not present at %s -- empty context", path)
        return HardshipContext(registry_loaded=False)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw["registry_loaded"] = True
    return HardshipContext.model_validate(raw)
