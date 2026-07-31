from __future__ import annotations


class MatterhornError(Exception):
    """Base class for structured protocol-facing errors."""

    code = "MATTERHORN_ERROR"
    status_code = 400


class ResourceNotFoundError(MatterhornError):
    code = "NOT_FOUND"
    status_code = 404


class ImportRefusedError(MatterhornError):
    code = "IMPORT_REFUSED"
    status_code = 400


class SubjectMergeConflictError(MatterhornError, ValueError):
    code = "SUBJECT_MERGE_CONFLICT"
    status_code = 409


class IngestFormatError(MatterhornError, ValueError):
    code = "UNRECOGNIZED_INGEST_FORMAT"
    status_code = 400


class RateLimitExceededError(MatterhornError):
    code = "RATE_LIMIT_EXCEEDED"
    status_code = 429


class ChatUnavailableError(MatterhornError):
    code = "CHAT_UNAVAILABLE"
    status_code = 503
