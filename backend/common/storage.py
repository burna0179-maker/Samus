"""Lightweight artifact storage abstraction.

Local-first: writes under ``E:\\Hustleforge\\Samus\\data\\artifacts\\`` on the
operator's host, or wherever ``SAMUS_ARTIFACT_ROOT`` env points (the docker
compose stack sets this to ``/opt/samus/data/artifacts``).

On GCP Cloud Run, ``SAMUS_ARTIFACT_ROOT`` may be a ``gs://bucket/prefix/``
URI. In that mode, :func:`put` / :func:`get` / :func:`exists` go to GCS
(via lazy ``google-cloud-storage`` import) and :func:`root` returns a
local ``/tmp`` shadow used only by callers that still build paths with
``root() / subdir`` for short-lived working files.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_ROOT = r"E:\Hustleforge\Samus\data\artifacts"

# Back-compat alias for tests / callers that did ``monkeypatch.setattr(
# "backend.common.storage._ROOT", tmp_path)``. When set, takes precedence
# over the env var so the legacy test pattern still routes writes to
# the patched root. Reset to ``None`` to fall back to env-var resolution.
_ROOT: Path | None = None


def _env_root() -> str:
    """Raw value of ``SAMUS_ARTIFACT_ROOT`` (or the default)."""
    return os.getenv("SAMUS_ARTIFACT_ROOT", _DEFAULT_ROOT)


def _is_gcs() -> bool:
    """True iff the configured artifact root is a ``gs://`` URI.

    ``_ROOT`` monkeypatching wins (local Path) so tests stay offline.
    """
    if _ROOT is not None:
        return False
    return _env_root().startswith("gs://")


def _parse_gs(uri: str) -> tuple[str, str]:
    """Split ``gs://bucket/prefix/sub`` into ``(bucket, "prefix/sub")``."""
    assert uri.startswith("gs://"), uri
    bare = uri[len("gs://") :]
    bucket, _, prefix = bare.partition("/")
    return bucket, prefix.strip("/")


def _gcs_bucket():
    """Cached GCS bucket handle for the configured artifact root."""
    global _BUCKET_HANDLE  # noqa: PLW0603 - module-level cache
    cached = globals().get("_BUCKET_HANDLE")
    if cached is not None:
        return cached
    try:
        from google.cloud import storage as _gcs  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "SAMUS_ARTIFACT_ROOT=gs://... needs the 'google-cloud-storage' "
            "package, which is not installed. It ships in the Cloud Run image; "
            "for local use point SAMUS_ARTIFACT_ROOT at a filesystem path."
        ) from exc
    bucket_name, _ = _parse_gs(_env_root())
    handle = _gcs.Client().bucket(bucket_name)
    _BUCKET_HANDLE = handle
    return handle


def _gcs_blob_name(name: str, subdir: str | None) -> str:
    _, prefix = _parse_gs(_env_root())
    parts = [p for p in (prefix, subdir, name) if p]
    return "/".join(parts)


def root() -> Path:
    """Return (and create) the artifact root directory.

    Resolution order:
      1. module-level ``_ROOT`` if monkeypatched (back-compat with tests
         that did ``setattr(storage._ROOT, tmp_path)`` against origin's
         eager-resolved root)
      2. ``SAMUS_ARTIFACT_ROOT`` env var (canonical path used in compose)
      3. ``_DEFAULT_ROOT`` (host fallback)

    When the env var is a ``gs://`` URI, callers should prefer
    :func:`put` / :func:`get` for durable artifacts. ``root()`` returns
    ``/tmp/samus-artifacts`` so legacy ``root() / subdir`` path-builders
    still work for short-lived files (lost on instance recycle).

    Env var is read lazily on every call so tests can monkeypatch the env
    mid-process and so secret-rotation-style env reload tooling can swap
    roots without restarting the process.
    """
    if _ROOT is not None:
        target = Path(_ROOT) if not isinstance(_ROOT, Path) else _ROOT
    elif _is_gcs():
        target = Path("/tmp/samus-artifacts")
    else:
        target = Path(_env_root())
    target.mkdir(parents=True, exist_ok=True)
    return target


def put(name: str, data: bytes | str, *, subdir: str | None = None) -> Path:
    """Write ``data`` to ``[root]/[subdir]/name`` and return the path/URI.

    On GCS the returned ``Path`` wraps the ``gs://...`` URI as a string;
    callers that only use it for logging are unaffected. Callers that
    pass it back to :func:`get` / :func:`exists` should use ``name`` +
    ``subdir`` (the URI round-trip isn't supported).
    """
    if _is_gcs():
        blob_name = _gcs_blob_name(name, subdir)
        blob = _gcs_bucket().blob(blob_name)
        if isinstance(data, str):
            blob.upload_from_string(data, content_type="text/plain; charset=utf-8")
        else:
            blob.upload_from_string(data, content_type="application/octet-stream")
        bucket_name, _ = _parse_gs(_env_root())
        return Path(f"gs://{bucket_name}/{blob_name}")
    target_dir = root() / subdir if subdir else root()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / name
    mode = "wb" if isinstance(data, bytes) else "w"
    with path.open(mode, encoding=None if isinstance(data, bytes) else "utf-8") as fh:
        fh.write(data)
    return path


def get(name: str, *, subdir: str | None = None) -> bytes | None:
    if _is_gcs():
        blob = _gcs_bucket().blob(_gcs_blob_name(name, subdir))
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    target_dir = root() / subdir if subdir else root()
    path = target_dir / name
    if not path.exists():
        return None
    return path.read_bytes()


def exists(name: str, *, subdir: str | None = None) -> bool:
    if _is_gcs():
        return _gcs_bucket().blob(_gcs_blob_name(name, subdir)).exists()
    target_dir = root() / subdir if subdir else root()
    return (target_dir / name).exists()
