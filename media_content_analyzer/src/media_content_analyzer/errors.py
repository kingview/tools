from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_ARTIFACT = "invalid_artifact"
    HASH_MISMATCH = "hash_mismatch"
    UNSUPPORTED_MEDIA = "unsupported_media"
    LIMIT_EXCEEDED = "limit_exceeded"
    ANALYSIS_FAILED = "analysis_failed"
    CONFIGURATION_ERROR = "configuration_error"


class AnalyzerError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
