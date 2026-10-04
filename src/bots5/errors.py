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


class ApprovalInvalidatedError(Bots5Error):
    """A one-shot approval has already been consumed (or is otherwise spent).

    Typed refusal for preflight-bound execution (Phase 10 M0.2; MUTATION_FENCE
    contract: new error subclass, no existing error semantics change). Raised
    when an approval marker already exists, so callers can distinguish a spent
    approval from any other storage failure.
    """


class ProviderError(Bots5Error):
    # Rule D-9 (F-04 repair) classification flag: True only when the provider
    # DEFINITIVELY did not accept the request. A plain ProviderError stays
    # conservatively ambiguous (the provider-side outcome is unknown), so
    # existing adapters raising it keep today's fail-safe behaviour.
    definitive_rejection: bool = False


class ProviderHttpError(ProviderError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)

    @property
    def definitive_rejection(self) -> bool:
        # Definitive non-acceptance: any 4xx except 408 (request timeout) and
        # 429 (rate limit) — those two leave the provider-side outcome unknown.
        return 400 <= self.status_code < 500 and self.status_code not in (408, 429)


class ContextAdmissionError(ProviderError):
    """The provider rejected the selected context; no automatic rebudget/retry."""

    definitive_rejection = True


class ProviderResponseError(ProviderError):
    # A response WAS received (but is unusable): the provider-side outcome is
    # not unknown — displayed as failed-but-received, distinct from an
    # ambiguous transport failure (rule D-9).
    definitive_rejection = True


class ProviderTimeoutError(ProviderError):
    pass


class RunError(Bots5Error):
    pass


def provider_side_outcome_unknown(exc: BaseException) -> bool:
    """Rule D-9 fail-safe: is the provider-side outcome of this failure unknown?

    Conservative by construction: only an exception explicitly classified as a
    definitive rejection (``definitive_rejection`` truthy) counts as known.
    Every unclassified failure — a plain ProviderError, a timeout, or an
    unexpected exception raised after dispatch — is ambiguous (True) and is
    never auto-retried.
    """
    return not getattr(exc, "definitive_rejection", False)
