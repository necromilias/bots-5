"""BLK-04 bounded-egress proof for the ONE controlled egress consumer.

The contract (decision 7 / reference workflow "one controlled egress consumer
proving the bounded egress semantics"):

* refusal (a) — no grant at all → ``NO_GRANT``, decided before the seam is
  consulted and before any transport;
* refusal (b) — a destination not on the grant's exact allowlist →
  ``SCOPE_MISMATCH`` (and a foreign grant object → ``NO_GRANT``);
* refusal (c) — the grant's bounded use exhausted → ``EXHAUSTED``;
* success — exactly one allowlisted destination, one authorized effect.

Everything runs through an **injected fake transport**: the consumer itself
performs no real network I/O by default (it never imports ``socket``), so the
proof is deterministic and offline.  The session-wide socket block in
``conftest.py`` stays armed as a second line of defence.
"""

from __future__ import annotations

import pytest

from bots5.core.capabilities import (
    CapabilityAuthority,
    DenialReason,
    EgressDestination,
    EgressScope,
    GrantRequest,
    GrantStatus,
    Subject,
    WORKSPACE_READ,
)
from bots5.core.egress import (
    ControlledEgressConsumer,
    EgressOutcome,
    EgressRequest,
    EgressResult,
)

ALLOWED = EgressDestination(scheme="https", host="api.example.com", port=443)
OTHER_HOST = EgressDestination(scheme="https", host="evil.example.com", port=443)
OTHER_PORT = EgressDestination(scheme="https", host="api.example.com", port=8443)
OTHER_SCHEME = EgressDestination(scheme="http", host="api.example.com", port=443)


class RecordingTransport:
    """A deterministic in-process transport; touches no network stack."""

    def __init__(self) -> None:
        self.calls: list[tuple[EgressDestination, bytes]] = []

    def __call__(self, destination: EgressDestination, payload: bytes):
        self.calls.append((destination, payload))
        return {"status": "ok"}

    @property
    def call_count(self) -> int:
        return len(self.calls)


def _authority() -> CapabilityAuthority:
    return CapabilityAuthority()


def _egress_grant(authority: CapabilityAuthority, *, destinations, effects: int = 1):
    return authority.grant(
        GrantRequest(
            subject=Subject(kind="egress-consumer", identity="bounded-egress-test"),
            kind="egress",
            scope=EgressScope(destinations=tuple(destinations), credential_reference=None),
            ttl_seconds=60.0,
            max_effects=effects,
        )
    )


def _consumer(*, authority=None, transport=None):
    authority = authority if authority is not None else _authority()
    return authority, ControlledEgressConsumer(authority=authority, transport=transport)


# ---------------------------------------------------------------------------
# refusal (a): no grant
# ---------------------------------------------------------------------------


def test_refusal_without_any_grant_is_no_grant_and_zero_transport():
    authority, consumer = _consumer(transport=RecordingTransport())
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=None)
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.NO_GRANT
    # Nothing was attempted anywhere: deny-by-default decides before effects.
    assert consumer.journal() == (result,)
    transport = consumer._transport
    assert transport.call_count == 0
    # No budget event even reached the seam: the authority logged nothing.
    assert authority.audit_trail() == ()


def test_foreign_grant_object_is_no_grant():
    issuing_authority = _authority()
    grant = _egress_grant(issuing_authority, destinations=(ALLOWED,))
    other_authority = _authority()
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=other_authority, transport=transport)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.NO_GRANT
    assert transport.call_count == 0


def test_wrong_kind_grant_is_refused_before_the_seam_spends_it():
    authority = _authority()
    workspace_grant = authority.grant(
        GrantRequest(
            subject=Subject(kind="tool", identity="mislabelled"),
            kind=WORKSPACE_READ,
            scope=None,
            ttl_seconds=60.0,
            max_effects=1,
        )
    )
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    result = consumer.send(
        EgressRequest(destination=ALLOWED, payload="hi"), grant=workspace_grant
    )
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.SCOPE_MISMATCH
    assert transport.call_count == 0
    # The mislabelled grant's budget is untouched — no silent cross-kind spend.
    assert workspace_grant.remaining_effects == 1


# ---------------------------------------------------------------------------
# refusal (b): destination not on the exact allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "off_list", [OTHER_HOST, OTHER_PORT, OTHER_SCHEME], ids=["host", "port", "scheme"]
)
def test_refusal_for_destination_off_the_exact_allowlist(off_list):
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,))
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    result = consumer.send(EgressRequest(destination=off_list, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.SCOPE_MISMATCH
    assert transport.call_count == 0
    # SCOPE_MISMATCH is a pre-effect refusal: the seam consumed no budget.
    assert grant.remaining_effects == 1


def test_empty_egress_scope_denies_everything():
    authority = _authority()
    grant = _egress_grant(authority, destinations=())
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.SCOPE_MISMATCH
    assert transport.call_count == 0


# ---------------------------------------------------------------------------
# refusal (c): bounded use exhausted
# ---------------------------------------------------------------------------


def test_refusal_once_bounded_use_is_exhausted():
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=1)
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)

    first = consumer.send(EgressRequest(destination=ALLOWED, payload="one"), grant=grant)
    assert first.outcome is EgressOutcome.SENT
    assert transport.call_count == 1

    second = consumer.send(EgressRequest(destination=ALLOWED, payload="two"), grant=grant)
    assert second.outcome is EgressOutcome.REFUSED
    assert second.refusal.reason is DenialReason.EXHAUSTED
    # The refusal is post-decision: the transport ran exactly once overall.
    assert transport.call_count == 1
    assert grant.status is GrantStatus.EXHAUSTED


def test_revoked_grant_reports_not_forward():
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=2)
    authority.revoke(grant, "operator revoked")
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.REFUSED
    assert result.refusal.reason is DenialReason.NOT_FORWARD
    assert transport.call_count == 0


# ---------------------------------------------------------------------------
# the one allowlisted success
# ---------------------------------------------------------------------------


def test_success_for_the_allowlisted_destination_with_injected_transport():
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=1)
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hello"), grant=grant)
    assert result.outcome is EgressOutcome.SENT
    assert result.refusal is None and result.error is None
    # Exactly one transport call, to exactly the granted triple, with the
    # exact payload bytes.
    assert transport.calls == [(ALLOWED, b"hello")]
    # One unit of bounded use consumed through the shared seam, audited there.
    assert grant.remaining_effects == 0
    reasons = [entry.reason for entry in authority.audit_trail()]
    assert "OK" in reasons


def test_journal_is_append_only_and_truthful():
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=1)
    transport = RecordingTransport()
    consumer = ControlledEgressConsumer(authority=authority, transport=transport)
    refused = consumer.send(
        EgressRequest(destination=OTHER_HOST, payload="x"), grant=grant
    )
    sent = consumer.send(EgressRequest(destination=ALLOWED, payload="y"), grant=grant)
    late = consumer.send(EgressRequest(destination=ALLOWED, payload="z"), grant=grant)
    assert [r.outcome for r in consumer.journal()] == [
        EgressOutcome.REFUSED,
        EgressOutcome.SENT,
        EgressOutcome.REFUSED,
    ]
    assert consumer.journal() == (refused, sent, late)
    # A REFUSED record can never carry an error and a SENT one never a
    # refusal — the dataclass enforces it at construction.
    with pytest.raises(Exception):
        EgressResult(
            outcome=EgressOutcome.REFUSED,
            destination=ALLOWED,
            payload_digest="0" * 64,
        )


# ---------------------------------------------------------------------------
# offline discipline: no default transport, no real I/O
# ---------------------------------------------------------------------------


def test_module_never_imports_socket_or_urllib():
    import sys
    import bots5.core.egress as egress_module

    source = open(egress_module.__file__, encoding="utf-8").read()
    assert "import socket" not in source
    assert "urllib" not in source
    assert "http.client" not in source
    assert "requests" not in source


def test_authorized_send_without_transport_fails_closed():
    # No default transport exists: an otherwise-authorized send fails closed
    # rather than inventing real network behaviour.
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=1)
    consumer = ControlledEgressConsumer(authority=authority)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.FAILED
    assert "no transport configured" in result.error
    # The seam already spent its one unit — that is honest accounting: the
    # attempt was authorized and then failed inside the effect span.
    assert grant.remaining_effects == 0


def test_transport_exception_becomes_failed_never_a_refusal_or_retry():
    authority = _authority()
    grant = _egress_grant(authority, destinations=(ALLOWED,), effects=2)

    def broken(destination, payload):
        raise RuntimeError("transport exploded")

    consumer = ControlledEgressConsumer(authority=authority, transport=broken)
    result = consumer.send(EgressRequest(destination=ALLOWED, payload="hi"), grant=grant)
    assert result.outcome is EgressOutcome.FAILED
    assert result.refusal is None
    assert "RuntimeError" in result.error
    # No auto-retry: exactly one attempt per send() call.
    assert grant.remaining_effects == 1


def test_request_validation_bounds_payload():
    from bots5.core.capabilities import CapabilitySystemError

    with pytest.raises(CapabilitySystemError):
        EgressRequest(destination=ALLOWED, payload="")
    with pytest.raises(CapabilitySystemError):
        EgressRequest(destination=ALLOWED, payload="x" * 10, max_bytes=4)
