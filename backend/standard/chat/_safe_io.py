"""Tiny string-based atomic-write helper local to the STANDARD chat package."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union


def atomic_write_text(
    path: Union[str, os.PathLike[str]],
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = ["atomic_write_text"]
