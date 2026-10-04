# Provider-managed context and Archive v3

This additive contract introduces migration `0020_provider_managed_context`,
provider-managed plan version 1, request snapshot version 5 and Archive version 3.
Snapshot 4 was already assigned to Phase 11 generation settings; it is not reused.
Historical migrations and Archive v1/v2 wire languages remain frozen.

## Accounting contracts

| Mode | Registration | Durable evidence | Guarantee |
|---|---|---|---|
| Exact | Existing registered exact adapters, including fake | Existing snapshot 3 and `context_plans` v3 | Existing Phase 6 semantics, unchanged |
| Provider-managed | Normal configured OpenRouter HTTP connections only | Snapshot 5 and `provider_managed_context_plans` v1 | Deterministic local selection; provider final admission |
| Developer test | Explicit `--developer-provider-test-mode` launch only | Existing legacy v2/v4 preparation evidence | Current message only; never production or Phase 6 acceptance |

Unknown capability truth does not opt a provider into weaker accounting. Generic
OpenAI-compatible providers retain their existing exact/fail-closed production
behaviour. The developer flag is neither persisted nor automatically enabled.
Normal OpenRouter readiness and Details say `Context accounting: Provider-managed`.
Only explicit developer sessions display the amber developer-test banner.

## Deterministic policy v1

`bots5.utf8-conservative-context`, version `1`, counts UTF-8 bytes of canonical
JSON for the exact selected message array. The resulting `local_estimated_input`
is a heuristic admission measurement, **not an exact token count or a proven
upper bound on any tokenizer**. JSON escaping, role names and delimiters are
included. Upstream templates, hidden instructions and tokenization are unknown.

Local admission requires:

```
local_estimated_input + output_reserve + configured_headroom
    <= advertised_context_tokens
configured_headroom = max(512, ceil(advertised_context_tokens * 20 / 100))
```

The byte-based estimate and explicit headroom are deliberately conservative for
ordinary text, inexpensive and network-independent. The numerical comparison is
a policy heuristic across different units, not a mathematical fit proof. The
provider remains authoritative and can reject an admitted selection.

The B.O.T.S. instruction envelope, eligible selected text attachments and current
user message are mandatory. History follows the existing explicit-parent lineage
rules. Whole user/assistant turns are removed oldest-first until the newest suffix
fits the local policy. A valid current message is never silently omitted: if
mandatory material exceeds local admission, preparation fails before dispatch.
Every considered source has its identity, role, state, content, ordering,
selection decision and exclusion reason in the canonical plan. The plan captures
parent/lineage, advertised limit and capability provenance, estimator semantics,
output reserve, headroom and `provider_final_admission=true`. Canonical and wire
SHA-256 digests bind the evidence; selected message material is reconstructable
without consulting mutable conversation state.

Snapshot 5 also preserves resolved settings, capability provenance, catalogue and
settings revisions, extended-settings authority and the max-output wire alias.
The start transaction checks that those authorities have not changed. A manual
capability override changes support truth without erasing OpenRouter's independent
catalogue-derived serialization alias (`max_tokens` or `max_completion_tokens`).

## OpenRouter behaviour

The selected model remains the sole requested model. `provider.require_parameters`
is true; same-model provider routing retains its normal behaviour. No model-level
fallback is configured. Provider-managed requests explicitly disable the
`context-compression` plugin so the service is not asked to rewrite selected
history. These controls follow the current official [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
and [message transforms](https://openrouter.ai/docs/guides/features/message-transforms)
contracts, checked on 2026-10-04.

Recognized context-length errors become `ContextAdmissionError`. The attempt and
selected evidence remain durable. B.O.T.S. neither removes more context nor
retries or changes models automatically. Unrecognized errors retain the existing
provider failure handling. Returned model, request metadata and actual usage
retain their existing durable telemetry fields; provider usage is not rewritten
as the local estimate.

## Persistence and lifecycle

The new table has attempt identity, explicit mode and plan version, closed plan
JSON, canonical representation/digest and creation timestamp. An attempt cannot
own both exact and provider-managed plans. Native attachment references continue
through `message_attachments` and `attempt_attachments`; current guards prove
selected identity/order, ready blob and the existing native/imported-continuation
ownership rules. Imported attempts retain the existing separate imported-object
lifecycle, with a tagged provider-managed row in imported context evidence.

Whole-installation Backup v1 continues to capture the internal SQLite database;
its public format is unchanged. Restore validates the new current schema. The
0020 upgrade widens only receiving-side archive-version checks, adds the separate
plan table and extends current guards. Downgrade to0019 is refused if any
provider-managed plan, snapshot5 or Archive v3 import/lineage evidence exists.
An evidence-free downgrade restores0019 guards and archive-version constraints.
No historical migration is edited.

## Archive v3 closed interchange

The format identifier remains `org.necromilias.bots5.chat-archive`; the manifest
`archive_version` is `3`. V3 owns its closed validator. V1 and v2 readers, schema
members, field sets and meanings are unchanged. V3 inherits the established
safe graph/provenance forms and requires this additional logical member:

```
domain/provider-managed-context-plans.jsonl
```

Its closed row fields are `attempt_id`, `accounting_mode` (only
`provider-managed`), `plan`, and `created_at`. `plan` has exactly:
`accounting_mode`, `plan_version`, `canonical_representation`, `canonical_digest`,
`parent_id`, `lineage_id`. The canonical representation has exactly:
`accounting_mode`, `plan_version`, `sources`, `included_sources`, `excluded_sources`,
`wire_sha256`, `policy`, `context_capability`, `history_turns`, `parent_id`,
`lineage_id`. The registered v1 policy parameters and semantics are closed, not
free-form extension points. Source rows retain the existing context source fields
and explicitly distinguish estimated accounting from exact accounting.

Exact rows retain the representation in `domain/context-plans.jsonl`. Their v3
archive request provenance explicitly adds `accounting_mode=exact`; their native
snapshot3 and exact plan are not changed. Provider-managed archive request
provenance uses snapshot5 plus `accounting_mode=provider-managed`, its complete
safe plan, settings revisions, serialization and extended-settings authority.
Each applicable attempt must have precisely its matching plan; legacy attempts
may have no plan. Duplicate/conflicting owners and unknown future versions fail
closed.

All v2 members remain required in v3, including object provenance, history
bindings and continuation history. The feature set adds
`provider-managed-context-v1`. Provenance hops accept archive versions1,2,3 with
the same deterministic ordering, scope, duplicate and cycle checks. V3 requires
canonical JSON/JSONL, complete inventory/digests, and rejects undeclared members,
unknown fields and secret-shaped metadata. Credential material, endpoints and raw
request IDs are excluded. Conversation/attachment content remains user data;
unsafe metadata or source identities that cannot be preserved safely cause export
to fail rather than silently losing canonical evidence.

Export uses one immutable store-owned cut for both plan kinds. Automatic export
selects the lowest lossless version: ordinary v1-compatible native graphs stayv1;
v2 provenance graphs stayv2; provider-managed plans or retained v3 hops requirev3.
Explicit v1/v2 requests for such graphs raise `ArchiveVersionRequired(3)` before
publication. Import and subsequent export preserve the original classification,
canonical plan evidence, source identities, attachment relations and provenance
hops. A later local generation uses its actual selected provider's accounting
mode; imported history does not dictate a future provider or promote evidence to
exact accounting.

## Limitations

There is no exact prompt-token, chat-template or guaranteed-fit claim. This mode
is not Phase 6 exact-accounting compliance. The estimator can reject material
that an upstream model would accept, and a provider can reject locally admitted
material. There is no automatic paid retry/rebudget feature. The existing text
attachment eligibility and whole-turn selection rules remain in effect.
