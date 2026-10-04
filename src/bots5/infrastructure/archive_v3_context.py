"""Archive v3's closed provider-managed evidence; v1/v2 stay unchanged."""
from copy import deepcopy
import json

from bots5.core.provider_managed_context import ProviderManagedContextPlan, parse_json
from bots5.core.interchange import parse_jsonl
from .archive_package import ArchivePackageError, _validate_attempt, _validate_provenance, _timestamp


def validate_safe_plan(value):
    try:
        plan = ProviderManagedContextPlan.from_evidence(value)
    except (ValueError, TypeError, KeyError) as exc:
        raise ArchivePackageError("invalid provider-managed plan") from exc
    from bots5.core.export import _safe_installation_id
    for identity in (*plan.included_sources, *plan.excluded_sources, plan.parent_id, plan.lineage_id):
        if identity is not None and _safe_installation_id(identity) != (identity, 'available'):
            raise ArchivePackageError('unsafe provider-managed source identity')
    # Provider/model free-form reasons and field labels are not an archive
    # extension point. The current registered policy accepts this catalogue field.
    if parse_json(plan.canonical_representation)['context_capability']['field'] not in {'context_length', 'context_tokens', 'manual'}:
        raise ArchivePackageError('unsafe provider-managed capability metadata')
    return plan


def safe_sources(value):
    plan = validate_safe_plan(value)
    return [{**{key: item for key, item in source.items() if key != 'reason'},
             'source_id_status':'available', 'state_status':'available', 'selection_reason':source['reason']}
            for source in parse_json(plan.canonical_representation)['sources']]


def validate_provenance(value, **kwargs):
    if type(value) is dict and value.get('snapshot_version') == 3:
        if value.get('accounting_mode') != 'exact':
            raise ArchivePackageError('v3 exact provenance requires an explicit mode')
        base = {k: v for k, v in value.items() if k != 'accounting_mode'}
        _validate_provenance(base, **kwargs)
        return value
    if type(value) is not dict or value.get('snapshot_version') != 5:
        return _validate_provenance(value, **kwargs)
    required = {'status','snapshot_version','attribution','settings','settings_provenance','capabilities','manual_overrides','omitted_settings',
                'accounting_mode','provider_managed_plan','settings_revisions','serialization','generation_authority'}
    if set(value) not in (required, required | {'generation_settings'}) or value['status'] != 'available' or value['accounting_mode'] != 'provider-managed':
        raise ArchivePackageError('v3 provider-managed provenance is not the closed schema')
    plan = validate_safe_plan(value['provider_managed_plan'])
    base = {k: v for k, v in value.items() if k not in {'accounting_mode','provider_managed_plan','settings_revisions','serialization','generation_authority'}}
    base['snapshot_version'] = 4 if 'generation_settings' in value else 2
    _validate_provenance(base, **kwargs)
    policy = parse_json(plan.canonical_representation)
    fact = next(c for c in value['capabilities'] if c['key'] == 'limits.context_tokens')
    cap = policy['context_capability']
    if fact['state'] != 'supported' or (fact['value'],fact['source'],fact['source_revision']) != (cap['advertised_context_tokens'],cap['source'],cap['source_revision']) or value['settings']['max_output_tokens'] != policy['policy']['output_reserve']:
        raise ArchivePackageError('v3 provider-managed accounting provenance mismatch')
    serialization = value['serialization']
    if type(serialization) is not dict or set(serialization) != {'max_output_parameter','catalogue_revision'} or serialization['max_output_parameter'] not in {'max_tokens','max_completion_tokens'} or serialization['catalogue_revision'] != value['attribution']['catalogue_revision']:
        raise ArchivePackageError('v3 serialization provenance mismatch')
    revisions = value['settings_revisions']
    if type(revisions) is not dict or set(revisions) != {'application','model','chat'} or type(revisions['application']) is not int or revisions['application'] < 1 or any(v is not None and (type(v) is not int or v < 1) for v in revisions.values()):
        raise ArchivePackageError('v3 settings revisions malformed')
    from .persistence.provider_managed_validation import validate_generation_authority
    try:
        validate_generation_authority(value['generation_authority'])
    except ValueError as exc:
        raise ArchivePackageError('v3 extended settings authority malformed') from exc
    return value


def validate_attempt(value, *args, **kwargs):
    provenance = value.get('request_time_provenance') if type(value) is dict else None
    if type(provenance) is dict and provenance.get('snapshot_version') == 3:
        validate_provenance(provenance, **kwargs)
        base = deepcopy(value); base['request_time_provenance'].pop('accounting_mode')
        _validate_attempt(base, *args, **kwargs)
        return value
    if type(provenance) is not dict or provenance.get('snapshot_version') != 5:
        return _validate_attempt(value, *args, **kwargs)
    validate_provenance(provenance, **kwargs)
    # Reuse only the legacy outcome/settings/graph validation. No exact plan
    # is manufactured or returned by this structural validation view.
    base = deepcopy(value)
    p = base['request_time_provenance']
    for key in ('accounting_mode','provider_managed_plan','settings_revisions','serialization','generation_authority'):
        p.pop(key)
    p['snapshot_version'] = 4 if 'generation_settings' in p else 2
    _validate_attempt(base, *args, **kwargs)
    return value


def validate_provider_managed_rows(contents, attempts, attachments, exact_owners):
    from .archive_v3 import _rows, _mapping
    owners = {}
    refs = _rows(contents, 'domain/attempt-attachments.jsonl')
    message_refs = _rows(contents, 'domain/message-attachments.jsonl')
    for row in _rows(contents, 'domain/provider-managed-context-plans.jsonl'):
        _mapping(row, {'attempt_id','accounting_mode','plan','created_at'}, 'provider-managed persisted plan')
        identity = row['attempt_id']
        if identity in owners or identity in exact_owners or identity not in attempts or row['accounting_mode'] != 'provider-managed':
            raise ArchivePackageError('v3 conflicting context plan ownership')
        provenance = attempts[identity]['request_time_provenance']
        if provenance.get('snapshot_version') != 5 or provenance.get('accounting_mode') != 'provider-managed' or provenance.get('provider_managed_plan') != row['plan']:
            raise ArchivePackageError('v3 provider-managed snapshot/plan mismatch')
        plan = validate_safe_plan(row['plan'])
        _timestamp(row['created_at'], 'provider-managed creation timestamp', milliseconds=True)
        selected = [s for s in plan.sources if s.kind == 'attachment' and s.selected]
        related = sorted((r for r in refs if r['attempt_id']==identity), key=lambda r:r['ordinal'])
        if len(related) != len(selected):
            raise ArchivePackageError('v3 selected attachment coverage mismatch')
        for ordinal, (source, ref) in enumerate(zip(selected,related,strict=True)):
            attachment = attachments.get(ref['attachment_id'])
            if ref['ordinal'] != ordinal or attachment is None or source.representation_digest != attachment['text_digest']:
                raise ArchivePackageError('v3 selected attachment ordinal/digest mismatch')
            # Source IDs survive import. Current graph identity is proven by
            # v3's history bindings and retained provenance hops below.
        owners[identity] = row
    for identity, attempt in attempts.items():
        provenance = attempt['request_time_provenance']
        if provenance.get('snapshot_version') == 5 and identity not in owners:
            raise ArchivePackageError('v3 provider-managed attempt lacks its plan')
    return owners
