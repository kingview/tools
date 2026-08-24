from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_ARTIFACT = "invalid_artifact"
    HASH_MISMATCH = "hash_mismatch"
    UNSUPPORTED_MEDIA = "unsupported_media"
    LIMIT_EXCEEDED = "limit_exceeded"
    ANALYSIS_FAILED = "analysis_failed"
    GENERATION_FAILED = "generation_failed"
    AUTHORIZATION_REQUIRED = "authorization_required"
    WATERMARK_DETECTION_FAILED = "watermark_detection_failed"
    WATERMARK_REMOVAL_FAILED = "watermark_removal_failed"
    CONFIGURATION_ERROR = "configuration_error"


class AnalyzerError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
