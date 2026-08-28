from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class BrowserAction(StrEnum):
    OBSERVE = "observe"
    NAVIGATE = "navigate"
    CLICK = "click"
    INPUT = "input"
    PRESS = "press"
    SCROLL = "scroll"
    BACK = "back"
    FORWARD = "forward"
    RELOAD = "reload"
    WAIT = "wait"


class BrowserInteractiveElement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    tag: str
    role: str | None = None
    name: str | None = None
    text: str | None = None
    input_type: str | None = None
    disabled: bool = False


class BrowserOperationInput(BaseModel):
    """One bounded UI operation in an already authorized BitBrowser profile."""

    model_config = ConfigDict(extra="forbid")

    session_ref: str = Field(
        pattern=r"^sess_(?:x|douyin|xhs)_[A-Za-z0-9_-]{20,80}$",
        max_length=96,
    )
    action: BrowserAction
    url: HttpUrl | None = None
    element_ref: str | None = Field(default=None, pattern=r"^e[1-9][0-9]{0,2}$")
    selector: str | None = Field(default=None, min_length=1, max_length=500)
    role: Literal[
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "option",
        "tab",
        "menuitem",
    ] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=300)
    text: str | None = Field(default=None, min_length=1, max_length=2_000)
    value: str | None = Field(default=None, max_length=10_000)
    key: Literal[
        "Enter",
        "Escape",
        "Tab",
        "PageUp",
        "PageDown",
        "Home",
        "End",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Backspace",
        "Delete",
        "Space",
    ] | None = None
    scroll_y: int = Field(default=900, ge=-5_000, le=5_000)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    wait_after_ms: int = Field(default=600, ge=0, le=5_000)
    max_elements: int = Field(default=40, ge=0, le=100)
    text_excerpt_chars: int = Field(default=4_000, ge=0, le=12_000)

    @model_validator(mode="after")
    def validate_action_fields(self) -> BrowserOperationInput:
        targets = [self.element_ref, self.selector, self.role, self.text]
        target_count = sum(value is not None for value in targets)
        if self.action is BrowserAction.NAVIGATE:
            if self.url is None:
                raise ValueError("url is required for navigate")
            if self.url.scheme != "https" or self.url.username or self.url.password:
                raise ValueError("navigate only accepts credential-free HTTPS URLs")
        elif self.url is not None:
            raise ValueError("url is only valid for navigate")

        if self.action in {BrowserAction.CLICK, BrowserAction.INPUT}:
            if target_count != 1:
                raise ValueError("click/input requires exactly one target")
            if self.role is not None and not self.name:
                raise ValueError("name is required when role is used")
        elif target_count:
            raise ValueError("targets are only valid for click/input")

        if self.action is BrowserAction.INPUT:
            if self.value is None:
                raise ValueError("value is required for input")
        elif self.value is not None:
            raise ValueError("value is only valid for input")

        if self.action is BrowserAction.PRESS:
            if self.key is None:
                raise ValueError("key is required for press")
        elif self.key is not None:
            raise ValueError("key is only valid for press")

        if self.action is BrowserAction.SCROLL and self.scroll_y == 0:
            raise ValueError("scroll_y cannot be zero")
        return self


class BrowserOperationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: BrowserAction
    url: str
    title: str
    text_excerpt: str = ""
    interactive_elements: list[BrowserInteractiveElement] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

