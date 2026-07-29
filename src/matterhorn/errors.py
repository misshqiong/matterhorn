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
