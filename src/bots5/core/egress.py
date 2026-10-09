"""The one controlled egress consumer for B.O.T.S. v0.2 (decision 7).

This module implements exactly ONE intentionally boring consumer of the
capability seam's ``egress`` kind, proving the bounded egress semantics:

* every attempt is decided by the **shared** :class:`CapabilityAuthority`
  through ``authority.effect(grant, target=EgressDestination)`` — there is no
  local destination table, no ambient bypass, and no second authority;
* a call without any grant object is refused with ``NO_GRANT`` before the
  seam is even consulted (the seam itself additionally refuses any foreign
  grant object with ``NO_GRANT``);
* a destination that is not on the grant's exact ``(scheme, host, port)``
  allowlist is refused with ``SCOPE_MISMATCH``;
* once the grant's bounded use is spent or recorded terminal the attempt is
  refused with ``EXHAUSTED`` (or ``NOT_FORWARD`` / ``EXPIRED`` for the other
  terminal lifecycle states) — again *before* any transport is touched;
* exactly the allowlisted destinations succeed, one authorized effect each.

Transport discipline (deliberately strong): this consumer performs **no real
network I/O at all**.  It never imports ``socket``, never opens a connection,
and has no default transport.  The byte-moving step is an injected callable
(``transport``) supplied by the composition root or a test; without one, an
otherwise-authorized send fails closed.  That is what makes the proof
deterministic and offline while keeping the authorization decisions genuine
seam decisions rather than fakes.

Threat model note (same locked decision as the seam): accidental, defective,
compromised-in-scope and out-of-authority behaviour — not a hostile in-process
attacker.  A compromised caller holding the authority object could mint its own
grants; the seam documents this and this module does not pretend otherwise.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    CapabilityGrant,
    CapabilitySystemError,
    DenialReason,
    EGRESS,
    EgressDestination,
    EgressScope,
)
from bots5.core.errors import CoreError

__all__ = [
    "EgressConsumerError",
    "EgressOutcome",
    "EgressRefusal",
    "EgressRequest",
    "EgressResult",
    "EgressTransportError",
    "ControlledEgressConsumer",
]


class EgressConsumerError(CoreError):
    """Base class for controlled-egress consumer failures."""


class EgressTransportError(EgressConsumerError):
    """An authorized send was attempted but the injected transport failed."""


class EgressOutcome(StrEnum):
    """Closed outcome vocabulary for one egress attempt.

    ``REFUSED`` is always a definite non-application decided *before* the
    transport was touched; it is never rewritten to success and never
    reported as a transport failure.
    """

    SENT = "SENT"
    REFUSED = "REFUSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EgressRequest:
    """One bounded egress request: bytes to one destination.

    The payload must be text (this consumer exists for API-shaped calls, not
    arbitrary streams) and is size-bounded so a single authorized effect can
    never become an unbounded resource commitment.
    """

    destination: EgressDestination
    payload: str
    max_bytes: int = 65536

    def __post_init__(self) -> None:
        if not isinstance(self.destination, EgressDestination):
            raise CapabilitySystemError(
                "EgressRequest.destination must be an EgressDestination"
            )
        if type(self.payload) is not str:
            raise CapabilitySystemError("EgressRequest.payload must be a string")
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise CapabilitySystemError("EgressRequest.max_bytes must be a positive integer")
        encoded_len = len(self.payload.encode("utf-8"))
        if encoded_len < 1:
            raise CapabilitySystemError("EgressRequest.payload must not be empty")
        if encoded_len > self.max_bytes:
            raise CapabilitySystemError(
                f"EgressRequest.payload exceeds the {self.max_bytes}-byte bound"
            )

    @property
    def payload_digest(self) -> str:
        return hashlib.sha256(self.payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EgressRefusal:
    """Structured record of one refusal (reason + verbatim seam message)."""

    reason: DenialReason
    message: str


@dataclass(frozen=True, slots=True)
class EgressResult:
    """One settled egress attempt. Truth-preserving construction rules."""

    outcome: EgressOutcome
    destination: EgressDestination | None
    payload_digest: str | None
    refusal: EgressRefusal | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is EgressOutcome.REFUSED:
            if self.refusal is None:
                raise EgressConsumerError("a REFUSED egress result must carry a refusal")
            if self.error is not None:
                raise EgressConsumerError(
                    "a REFUSED egress result is a pre-effect refusal, not an error"
                )
        elif self.outcome is EgressOutcome.SENT:
            if self.refusal is not None or self.error is not None:
                raise EgressConsumerError("a SENT egress result carries neither refusal nor error")
        else:
            if not self.error:
                raise EgressConsumerError("a FAILED egress result must carry an error")
            if self.refusal is not None:
                raise EgressConsumerError(
                    "a FAILED egress result crossed into the transport; it is not a refusal"
                )


class ControlledEgressConsumer:
    """The one controlled egress consumer, operating strictly through the seam.

    Construction injects the shared :class:`CapabilityAuthority`; the
    transport is injected per instance (composition root) or per call (tests).
    There is deliberately **no default transport**: with none supplied, every
    otherwise-authorized send fails closed with
    ``EgressOutcome.FAILED`` / ``no transport configured``.  Nothing in this
    module ever touches a network stack.
    """

    def __init__(
        self,
        *,
        authority: CapabilityAuthority,
        transport: Callable[[EgressDestination, bytes], Mapping[str, object]] | None = None,
    ) -> None:
        if not isinstance(authority, CapabilityAuthority):
            raise EgressConsumerError("authority must be a CapabilityAuthority")
        if transport is not None and not callable(transport):
            raise EgressConsumerError("transport must be callable or None")
        self._authority = authority
        self._transport = transport
        self._journal: list[EgressResult] = []

    # ------------------------------------------------------------------
    # introspection (no effects, no grants)
    # ------------------------------------------------------------------

    @property
    def authority(self) -> CapabilityAuthority:
        """The shared authority instance this consumer decides through."""
        return self._authority

    def journal(self) -> tuple[EgressResult, ...]:
        """Append-only view of every settled attempt (nothing rewritten)."""
        return tuple(self._journal)

    # ------------------------------------------------------------------
    # the one sending path
    # ------------------------------------------------------------------

    def send(
        self,
        request: EgressRequest,
        *,
        grant: CapabilityGrant | None,
        transport: Callable[[EgressDestination, bytes], Mapping[str, object]] | None = None,
    ) -> EgressResult:
        """Attempt exactly one bounded egress effect under an explicit grant.

        Decision order (each refusal happens BEFORE any transport is touched):

        1. no grant object → ``NO_GRANT`` (deny-by-default world);
        2. a grant not issued by THIS authority → ``NO_GRANT`` (the seam's
           own foreign-object check — structurally impossible to borrow);
        3. a grant whose kind is not ``egress`` → ``SCOPE_MISMATCH``
           (mislabeling another kind's grant is defective-consumer behaviour
           and is refused here, at the consumer boundary);
        4. a destination outside the grant's exact allowlist → the seam
           raises ``SCOPE_MISMATCH``;
        5. exhausted bounded use → the seam raises ``EXHAUSTED`` (terminal
           states report ``NOT_FORWARD`` / ``EXPIRED`` truthfully).

        Only after the seam authorizes does the transport run, inside the
        seam's in-flight retention (``effect()``), so revocation settles
        honestly through CLEANUP.  Transport exceptions produce ``FAILED``
        with the error carried; a refusal never masquerades as a failure and
        a failure never gets retried.
        """
        if not isinstance(request, EgressRequest):
            raise EgressConsumerError("send requires an EgressRequest")

        denial: CapabilityDenied | None = None
        result: EgressResult | None = None
        response: Mapping[str, object] | None = None

        if grant is None:
            denial = CapabilityDenied(
                reason=DenialReason.NO_GRANT,
                message="no capability grant was supplied to the egress consumer",
            )
        elif grant.kind != "egress":
            denial = CapabilityDenied(
                reason=DenialReason.SCOPE_MISMATCH,
                message=(
                    f"grant kind {grant.kind!r} is not 'egress'; an egress effect "
                    "may not ride another kind's budget"
                ),
            )
        elif not isinstance(grant.scope, EgressScope):
            denial = CapabilityDenied(
                reason=DenialReason.SCOPE_MISMATCH,
                message="an egress grant must carry an EgressScope",
            )
        else:
            active_transport = transport if transport is not None else self._transport
            try:
                with self._authority.effect(grant, units=1, target=request.destination, kind=EGRESS):
                    if active_transport is None:
                        raise EgressTransportError(
                            "no transport configured: the controlled egress consumer "
                            "performs no real network I/O by default"
                        )
                    response = active_transport(request.destination, request.payload.encode("utf-8"))
                    if not isinstance(response, Mapping):
                        raise EgressTransportError("transport returned a non-mapping response")
            except CapabilityDenied as exc:
                denial = exc
            except EgressTransportError as exc:
                result = EgressResult(
                    outcome=EgressOutcome.FAILED,
                    destination=request.destination,
                    payload_digest=request.payload_digest,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 - bounded fail-closed boundary
                result = EgressResult(
                    outcome=EgressOutcome.FAILED,
                    destination=request.destination,
                    payload_digest=request.payload_digest,
                    error=f"{type(exc).__name__}: {exc}",
                )

        if denial is not None:
            result = EgressResult(
                outcome=EgressOutcome.REFUSED,
                destination=request.destination,
                payload_digest=request.payload_digest,
                refusal=EgressRefusal(reason=denial.reason, message=denial.message),
            )
        elif result is None:
            assert response is not None
            result = EgressResult(
                outcome=EgressOutcome.SENT,
                destination=request.destination,
                payload_digest=request.payload_digest,
            )

        self._journal.append(result)
        return result
