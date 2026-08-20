from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    CONFIGURATION_ERROR = "configuration_error"
    UNSAFE_URL = "unsafe_url"
    UNSUPPORTED_URL = "unsupported_url"
    DOWNLOAD_FAILED = "download_failed"
    LIMIT_EXCEEDED = "limit_exceeded"


class CrawlerError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}
