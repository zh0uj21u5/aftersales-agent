"""Translate coordination denials into consistent gateway responses."""

from fastapi import HTTPException

from aftersales.core.llm import InferenceUnavailable
from aftersales.core.redis_coord import AdmissionDenied, CoordinationLost


def coordination_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (AdmissionDenied, InferenceUnavailable)):
        return HTTPException(429, str(exc), headers={"Retry-After": "60"})
    message = str(exc) if isinstance(exc, CoordinationLost) else "会话协调暂不可用，请稍后重试"
    return HTTPException(503, message)
