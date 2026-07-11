"""Regression: the intake app must import + construct with a single lifespan.

fix/samus-intake-duplicate-lifespan-20260707 — after the inbox-poll-in-container
merge, ``backend/intake/app.py`` called ``create_base_app(...)`` with a DUPLICATE
``lifespan=_intake_lifespan`` kwarg (once right after ``service_name`` and again
after ``hmac_exempt_paths``). A repeated keyword argument is a parse-time
``SyntaxError: keyword argument repeated: lifespan`` — so the samus-intake
container crash-looped on boot and the pipeline front door (public onboarding
form, lead intake, Gmail poll drain) was fully down.

These guard the whole "intake app module won't import / construct" class of
regression: a duplicate kwarg (or any other import-time break) fails these two
tests immediately, with a name that points straight at the cause.
"""
from __future__ import annotations


def test_intake_app_module_imports():
    # A duplicate kwarg is a SyntaxError at import time, so importing the module
    # at all proves the duplicate ``lifespan=`` is gone.
    import backend.intake.app as app_mod

    assert hasattr(app_mod, "create_app")
    assert hasattr(app_mod, "_intake_lifespan")


def test_intake_create_app_constructs():
    # create_app() calls create_base_app(..., lifespan=_intake_lifespan); with
    # the duplicate kwarg this module could not even be imported. It must now
    # construct a FastAPI app cleanly (exactly one lifespan kwarg).
    from fastapi import FastAPI

    from backend.intake.app import create_app

    app = create_app()
    assert isinstance(app, FastAPI)
