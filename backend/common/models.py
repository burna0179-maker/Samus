"""TaskEnvelope — the canonical inter-service request wrapper.

Every HTTP request between Samus services wraps its payload in a TaskEnvelope.
Per doc §3.19.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskEnvelope(BaseModel):
    task_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
