"""Pydantic schemas for Mori Advisor."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ConsultAdvisorInput(BaseModel):
    """Input schema for the consult_advisor tool."""

    question: str = Field(description="The question or problem needing review")
    context: str = Field(default="", description="Additional context or constraints")
    files: list[str] = Field(
        default=[], description="Server-local file paths (co-located deployments only)"
    )
    file_contents: list[dict] | None = Field(
        default=None,
        description='Client-supplied file bodies as {"name": str, "content": str} dicts',
    )
    focus: Literal["general", "architecture", "security", "performance", "style"] = Field(
        default="general", description="Area of focus for the review"
    )
    depth: Literal["quick", "balanced", "deep"] = Field(
        default="balanced", description="Review depth"
    )


class ConsultStatusInput(BaseModel):
    """Input schema for the consult_status tool."""

    job_id: str = Field(description="Job ID returned by consult_advisor")


class MemoryFile(BaseModel):
    """Schema for a single memory entry stored in SQLite."""

    id: int | None = None
    name: str = Field(description="kebab-case unique identifier")
    title: str = Field(description="Human-readable title")
    description: str = Field(default="", description="One-line summary")
    type: Literal["project", "profile", "pattern", "decision"] = Field(
        default="project", description="Memory classification"
    )
    body: str = Field(default="", description="Markdown content")
    tags: list[str] = Field(default=[], description="Tags for filtering")
    origin_session_id: str | None = Field(default=None, description="UUID of creating session")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SessionEvent(BaseModel):
    """A single event in a session log."""

    timestamp: str = Field(description="ISO 8601 timestamp")
    event: Literal["tool_use", "user_prompt", "assistant_response", "system"] = Field(
        description="Event type"
    )
    session_id: str = Field(description="Session UUID")
    data: dict = Field(default={}, description="Event-specific payload")
