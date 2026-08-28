from __future__ import annotations


class Bots5Error(Exception):
    """Base class for expected B.O.T.S. 5 failures."""


class InvalidJsonError(Bots5Error):
    pass


class ValidationError(Bots5Error):
    pass


class FileValidationError(Bots5Error):
    pass


class StorageError(Bots5Error):
    pass


class ProviderError(Bots5Error):
    pass


class ProviderHttpError(ProviderError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class ProviderResponseError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class RunError(Bots5Error):
    pass
