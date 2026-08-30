from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class XPublishInput(BaseModel):
    """One explicitly approved X post composed in an authorized browser profile."""

    model_config = ConfigDict(extra="forbid")

    session_ref: str = Field(
        pattern=r"^sess_x_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    text: str = Field(min_length=1, max_length=25_000)
    media_paths: list[str] = Field(default_factory=list, max_length=4)
    approval_token: str = Field(
        min_length=32,
        max_length=200,
        description="One-time token issued after the user confirms the execution plan.",
    )
    timeout_seconds: float = Field(default=120.0, ge=15.0, le=300.0)

    @model_validator(mode="after")
    def validate_content(self) -> XPublishInput:
        if not self.text.strip():
            raise ValueError("X post text cannot be blank")
        return self


class XPublishOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["published", "failed", "unknown"]
    post_url: str | None = None
    text_length: int = Field(ge=0)
    media_count: int = Field(ge=0, le=4)
    warnings: list[str] = Field(default_factory=list)

