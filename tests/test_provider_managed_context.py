from dataclasses import replace
import json
import pytest

from bots5.core.context import ContextSource, ContextBuildError
from bots5.core.provider_managed_context import (
    AccountingMode, ProviderManagedContextPlan, accounting_mode,
    build_provider_managed_plan, canonical_json, digest,
)


def build(**overrides):
    values = dict(current_user=ContextSource('current', 'current_user', 'user', 'remember'),
        historical_turns=((ContextSource('u1', 'history', 'user', 'old '*500), ContextSource('a1', 'history', 'assistant', 'old reply')),
                          (ContextSource('u2', 'history', 'user', 'new fact'), ContextSource('a2', 'history', 'assistant', 'acknowledged'))),
        selected_attachments=(ContextSource('attachment', 'attachment', 'user', 'tiny', representation_id=digest('tiny'), representation_digest=digest('tiny')),),
        context_capability=dict(advertised_context_tokens=2048, source='provider_metadata', source_revision=1, field='context_length'),
        output_reserve=128)
    values.update(overrides)
    return build_provider_managed_plan(**values)


def test_modes_are_explicit_and_provider_scoped():
    assert accounting_mode(backend='fake', profile='generic') is AccountingMode.EXACT
    assert accounting_mode(backend='openai_compatible_http', profile='generic') is AccountingMode.EXACT
    assert accounting_mode(backend='openai_compatible_http', profile='openrouter') is AccountingMode.PROVIDER_MANAGED
    assert accounting_mode(backend='openai_compatible_http', profile='openrouter', developer=True) is AccountingMode.DEVELOPER_TEST
    with pytest.raises(ContextBuildError):
        accounting_mode(backend='fake', profile='generic', developer='yes')


def test_deterministic_history_attachment_and_estimate_evidence():
    plan = build()
    assert plan == build()
    assert plan.excluded_sources == ('u1', 'a1')
    assert 'u2' in plan.included_sources and 'attachment' in plan.included_sources
    messages = json.loads(plan.wire_representation)
    assert messages[0]['role'] == 'system'
    assert any(m['content'] == 'new fact' for m in messages)
    assert any(m['content'] == 'tiny' for m in messages)
    assert not any(m['content'] == 'old reply' for m in messages)
    policy = json.loads(plan.canonical_representation)['policy']
    assert policy['local_estimated_input'] == len(plan.wire_representation)
    assert policy['provider_final_admission'] is True
    assert 'exact tokens' in policy['estimator_semantics']
    assert ProviderManagedContextPlan.from_evidence(plan.as_evidence()) == plan


@pytest.mark.parametrize('mutation', ['mode', 'policy', 'source_order', 'attachment', 'partial_turn'])
def test_rehashed_context_mutants_rejected(mutation):
    plan = build()
    value = json.loads(plan.canonical_representation)
    if mutation == 'mode': value['accounting_mode'] = 'exact'
    if mutation == 'policy': value['policy']['estimator_version'] = 'unknown'
    if mutation == 'source_order': value['included_sources'].reverse()
    if mutation == 'attachment': value['sources'][-2]['representation_digest'] = '0'*64
    if mutation == 'partial_turn': value['history_turns'] = [['u1'], ['u2', 'a1', 'a2']]
    text = canonical_json(value)
    with pytest.raises(ContextBuildError):
        replace(plan, canonical_representation=text, canonical_digest=digest(text))


def test_fresh_0020_schema(tmp_path):
    from tests.test_phase6_context_attachments import _open_store, _private_engine_connection
    authority, store = _open_store(tmp_path/'root')
    try:
        with _private_engine_connection(store) as connection:
            assert connection.exec_driver_sql('SELECT version_num FROM alembic_version').scalar_one() == '0020_provider_managed_context'
            assert connection.exec_driver_sql("SELECT count(*) FROM provider_managed_context_plans").scalar_one() == 0
    finally:
        authority.close()


@pytest.mark.parametrize("continuation_kind", ["send", "regenerate"])
def test_production_openrouter_multiturn_attachment_and_durable_plan(tmp_path, continuation_kind):
    import asyncio
    import httpx
    from bots5.infrastructure.secrets import FakeSecretStore
    from bots5.infrastructure.generation.router import BuiltinProviderRouter
    from bots5.domain.provider import CredentialSource
    from tests.test_openrouter_capability_discovery import _setup, _discoverer
    from tests.test_phase5_provider_model import _configured_application, _engine_connection
    from tests.test_phase6_context_attachments import _finish

    async def scenario():
        secrets = FakeSecretStore({'test-ref': 'synthetic'})
        router = BuiltinProviderRouter(secret_stores={'secret_service': secrets})
        app, store = _configured_application(tmp_path, backend=router, secret_store=secrets)
        app._configuration.phase6_enabled = True
        sent = []
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(['temperature', 'max_completion_tokens']))
            from bots5.domain.provider import CapabilityOverride, CapabilityState
            await app.set_capability_override(CapabilityOverride(model.id, 'request.max_output_tokens', CapabilityState.SUPPORTED))
            def handler(request):
                sent.append(json.loads(request.content))
                return httpx.Response(200, text='data: {"model":"test/text-model","id":"request-test","choices":[{"delta":{"content":"remembered"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":19,"completion_tokens":3,"total_tokens":22}}\n\ndata: [DONE]\n\n', headers={'content-type':'text/event-stream'})
            router._transports[connection.id] = httpx.MockTransport(handler)
            chat = await app.create_chat()
            await app.select_model(chat.id, model.id)
            first = await app.send_message(chat.id, 'The code is BLUE-OTTER-73')
            await _finish(app, first.id)
            source = tmp_path/'tiny.txt'; source.write_text('Attachment code: GREEN-OWL-41', encoding='utf8')
            attachment = await app.attach_file(source)
            await app.stage_attachment(chat.id, attachment.id)
            second = await app.send_message(chat.id, 'Recall the first code and attachment code')
            await _finish(app, second.id)
            assert len(sent) == 2
            assert any(m['content'] == 'The code is BLUE-OTTER-73' for m in sent[1]['messages'])
            assert any('GREEN-OWL-41' in m['content'] for m in sent[1]['messages'])
            assert sent[1]['provider'] == {'require_parameters': True}
            assert sent[1]['plugins'] == [{'id':'context-compression','enabled':False}]
            assert 'max_completion_tokens' in sent[1] and 'models' not in sent[1]
            with _engine_connection(store) as db:
                rows = db.exec_driver_sql('SELECT request_snapshot,state,prompt_tokens,total_tokens FROM generation_attempts ORDER BY started_at').fetchall()
                assert len(rows) == 2
                assert all(json.loads(row[0])['snapshot_version'] == 5 and row[1] == 'complete' for row in rows)
                assert rows[-1][2:] == (19,22)
                assert db.exec_driver_sql('SELECT count(*) FROM provider_managed_context_plans').scalar_one() == 2
                assert db.exec_driver_sql('SELECT count(*) FROM context_plans').scalar_one() == 0
                assert db.exec_driver_sql('SELECT attachment_id FROM message_attachments').scalar_one() == attachment.id
                assert db.exec_driver_sql('SELECT attachment_id FROM attempt_attachments').scalar_one() == attachment.id
            from tests.test_phase9_backup import _service
            from bots5.infrastructure.app_paths import resolve_app_paths
            from pathlib import Path
            backup = _service(store._authority, store, resolve_app_paths(Path(store._authority.root)), tmp_path).create_backup(tmp_path/'provider-managed.botsbackup')
            from bots5.infrastructure.data_root_authority import DataRootAuthority
            from bots5.infrastructure.restore_service import RestoreService
            initial = DataRootAuthority(tmp_path/'restored').acquire()
            initial.open_store().close()
            initial.close()
            restored_authority = DataRootAuthority(tmp_path/'restored').acquire()
            try:
                RestoreService(restored_authority).restore(backup.destination)
                restored_store = restored_authority.open_store()
                from tests.test_phase6_context_attachments import _private_engine_connection
                with _private_engine_connection(restored_store) as db:
                    assert db.exec_driver_sql('SELECT count(*) FROM provider_managed_context_plans').scalar_one() == 2
                    assert db.exec_driver_sql('SELECT count(*) FROM attempt_attachments').scalar_one() == 1
                restored_store.close()
            finally:
                restored_authority.close()
            from tests._authority_test_support import downgrade_to
            with pytest.raises(RuntimeError, match='downgrade refused'):
                downgrade_to(tmp_path/'restored/database/state.sqlite3', '0019_phase11_generation_settings')
            from bots5.infrastructure.archive_package import archive_bytes, validate_archive
            from bots5.core.export import ArchiveVersionRequired
            import io, zipfile
            for version in (1, 2):
                with pytest.raises(ArchiveVersionRequired):
                    await app.prepare_archive_export(chat.id, archive_version=version)
            projection = await app.prepare_archive_export(chat.id)
            assert projection.manifest_base['archive_version'] == 3
            raw = archive_bytes(projection)
            validate_archive(io.BytesIO(raw))
            assert_v3_closed_mutants(raw)
            with zipfile.ZipFile(io.BytesIO(raw)) as package:
                plans = [json.loads(line) for line in package.read('domain/provider-managed-context-plans.jsonl').splitlines()]
                assert len(plans) == 2 and all(p['accounting_mode'] == 'provider-managed' for p in plans)
                assert package.read('domain/context-plans.jsonl') == b''
                assert b'synthetic' not in package.read('domain/attempts.jsonl')
            from tests.test_phase6_context_attachments import _configured_application as local_application
            from datetime import datetime, UTC
            receiver, received_store, received_authority = local_application(tmp_path/'received')
            try:
                intake = tmp_path/'intake'; intake.mkdir()
                archive = intake/'source.botsarchive'; archive.write_bytes(raw)
                queued = received_store.enqueue_archive_import(archive, resolver_roots=(intake,), now=datetime.now(UTC))
                imported_chat = received_store.execute_archive_import(queued.id, now=datetime.now(UTC), operation_id='pm-roundtrip')
                exported_again = await receiver.prepare_archive_export(imported_chat)
                assert exported_again.manifest_base['archive_version'] == 3
                repeated = archive_bytes(exported_again)
                validate_archive(io.BytesIO(repeated))
                with zipfile.ZipFile(io.BytesIO(repeated)) as package:
                    assert len(package.read('domain/provider-managed-context-plans.jsonl').splitlines()) == 2
                    assert package.read('domain/context-plans.jsonl') == b''
                head = received_store.get_chat(imported_chat).head_message_id
                local_connection = received_store.list_provider_connections()[0]
                local_model = received_store.list_model_catalogue_entries(local_connection.id)[0]
                await receiver.choose_import_continuation(imported_chat, head, expected_choice_revision=0,
                    connection_id=local_connection.id, model_entry_id=local_model.id,
                    explicit_settings={'temperature':None,'max_output_tokens':None,'reasoning_effort':None,'timeout_seconds':None}, excluded_refs=())
                continuation = (await receiver.regenerate_message(imported_chat, head) if continuation_kind == 'regenerate' else await receiver.send_message(imported_chat, 'Continue locally with exact accounting'))
                await _finish(receiver, continuation.id)
                assert json.loads(received_store.get_generation_attempt(continuation.id).request_snapshot)['snapshot_version'] == 3
                mixed = archive_bytes(await receiver.prepare_archive_export(imported_chat))
                validate_archive(io.BytesIO(mixed))
                with zipfile.ZipFile(io.BytesIO(mixed)) as package:
                    assert len(package.read('domain/provider-managed-context-plans.jsonl').splitlines()) == 2
                    assert len(package.read('domain/context-plans.jsonl').splitlines()) == 1
            finally:
                await receiver.close()
                received_authority.close()
            reopened, reopened_store, reopened_authority = local_application(tmp_path/'received')
            await reopened.close()
            reopened_authority.close()
            from bots5.core.errors import StateError
            with pytest.raises(StateError, match='historical references'):
                store.delete_attachment(attachment.id)
        finally:
            await app.close()
    asyncio.run(scenario())


def test_0020_upgrade_downgrade_preserves_exact_and_imported_graph(tmp_path):
    import asyncio, sqlite3
    from datetime import UTC, datetime
    from tests.test_phase6_context_attachments import _configured_application, _finish
    from tests._authority_test_support import downgrade_to, upgrade_to
    from bots5.infrastructure.archive_package import archive_bytes
    root = tmp_path/'root'
    async def seed():
        app, store, authority = _configured_application(root)
        try:
            chat = await app.create_chat()
            attachment_path = tmp_path/'exact.txt'; attachment_path.write_text('exact attachment')
            attachment = await app.attach_file(attachment_path)
            await app.stage_attachment(chat.id, attachment.id)
            attempt = await app.send_message(chat.id, 'exact historical context')
            await _finish(app, attempt.id)
            intake = tmp_path/'intake'; intake.mkdir()
            path = intake/'exact.botsarchive'; path.write_bytes(archive_bytes(await app.prepare_archive_export(chat.id)))
            queued = store.enqueue_archive_import(path, resolver_roots=(intake,), now=datetime.now(UTC))
            store.execute_archive_import(queued.id, now=datetime.now(UTC), operation_id='exact-import')
        finally:
            await app.close(); authority.close()
    asyncio.run(seed())
    database = root/'database/state.sqlite3'
    def cut():
        with sqlite3.connect(database) as db:
            return {name: db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in (
                'context_plans','generation_attempts','message_attachments','attempt_attachments',
                'archive_import_operations','archive_lineage_nodes','archive_imported_attempts',
                'archive_import_message_attachment_refs','archive_import_attempt_attachment_refs',
            )}
    before = cut()
    downgrade_to(database, '0019_phase11_generation_settings')
    assert cut() == before
    upgrade_to(database, '0020_provider_managed_context')
    assert cut() == before
    app, store, authority = _configured_application(root)
    asyncio.run(app.close()); authority.close()


@pytest.mark.parametrize('stream_error', [False, True])
def test_context_admission_is_typed_and_never_retried(stream_error):
    import asyncio, httpx
    from bots5.providers.openrouter import OpenRouterProvider
    from bots5.providers.base import CompletionRequest
    from bots5.errors import ContextAdmissionError
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        error = {'error': {'code':'context_length_exceeded','message':'maximum context length exceeded'}}
        return httpx.Response(200, text='data: '+json.dumps(error)+'\n\n', headers={'content-type':'text/event-stream'}) if stream_error else httpx.Response(400, json=error)
    async def scenario():
        provider = OpenRouterProvider('synthetic', _transport=httpx.MockTransport(handler))
        request = CompletionRequest(model='test/model', system='', user='hello', temperature=0, max_output_tokens=5, timeout_seconds=30,
            accounting_mode='provider-managed', context_messages=(('system','instruction'),('user','hello')))
        with pytest.raises(ContextAdmissionError, match='not retried'):
            async for _ in provider.stream(request): pass
    asyncio.run(scenario())
    assert len(sent) == 1 and sent[0]['model'] == 'test/model' and 'models' not in sent[0]


def assert_v3_closed_mutants(raw):
    """Rehash semantic mutants so closed validators, not stale ZIP hashes, kill them."""
    import io, zipfile
    from bots5.infrastructure.archive_v3 import archive_v3_bytes
    from bots5.infrastructure.archive_package import validate_archive, ArchivePackageError
    from bots5.core.interchange import canonical_jsonl_bytes
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        manifest = json.loads(package.read('manifest.json'))
        members = {name: package.read(name) for name in package.namelist() if name not in {'manifest.json','COMPLETED'}}
    for mutation in ('missing_member','missing_plan','duplicate','unknown_field','unknown_mode','unknown_version','secret','snapshot_mode','snapshot_future','wrong_ordinal','noncanonical','unsupported_feature','hop_future','missing_message_ref','unbound_attachment','wrong_parent'):
        entries = dict(members); base = dict(manifest)
        plans = [json.loads(line) for line in entries['domain/provider-managed-context-plans.jsonl'].splitlines()]
        attempts = [json.loads(line) for line in entries['domain/attempts.jsonl'].splitlines()]
        if mutation == 'missing_message_ref': entries['domain/message-attachments.jsonl'] = b''
        elif mutation == 'unbound_attachment':
            rows = [json.loads(line) for line in entries['domain/history-bindings.jsonl'].splitlines()]
            for row in rows:
                if row['snapshot_source']['kind'] == 'attachment': row.update(current_binding=None,binding_state='unavailable')
            entries['domain/history-bindings.jsonl'] = canonical_jsonl_bytes(rows)
        elif mutation == 'wrong_parent':
            evidence = plans[0]['plan']; canonical = json.loads(evidence['canonical_representation'])
            canonical['parent_id'] = 'unrelated-parent'; evidence['parent_id'] = 'unrelated-parent'
            evidence['canonical_representation'] = canonical_json(canonical); evidence['canonical_digest'] = digest(evidence['canonical_representation'])
            owner = next(a for a in attempts if a['source_id'] == plans[0]['attempt_id'])
            owner['request_time_provenance']['provider_managed_plan'] = evidence
        elif mutation == 'missing_member': entries.pop('domain/provider-managed-context-plans.jsonl')
        elif mutation == 'missing_plan': plans.pop()
        elif mutation == 'duplicate': plans.append(plans[0])
        elif mutation == 'unknown_field': plans[0]['future'] = True
        elif mutation == 'unknown_mode': plans[0]['accounting_mode'] = 'exact'
        elif mutation == 'unknown_version': plans[0]['plan']['plan_version'] = 2
        elif mutation == 'secret': plans[0]['endpoint'] = 'https://secret.example/key'
        elif mutation == 'snapshot_mode': attempts[0]['request_time_provenance']['accounting_mode'] = 'exact'
        elif mutation == 'snapshot_future': attempts[0]['request_time_provenance']['snapshot_version'] = 9000
        elif mutation == 'wrong_ordinal':
            rows = [json.loads(line) for line in entries['domain/attempt-attachments.jsonl'].splitlines()]
            rows[0]['ordinal'] = 4; entries['domain/attempt-attachments.jsonl'] = canonical_jsonl_bytes(rows)
        elif mutation == 'unsupported_feature': base['features'] = sorted(base['features'] + ['future-context'])
        elif mutation == 'hop_future':
            rows = [json.loads(line) for line in entries['domain/object-provenance.jsonl'].splitlines()]
            rows[0]['source'] = {'kind':'imported','immediate':{'archive_id':'prior','archive_version':4,'logical_content_digest':'0'*64,'object_id':rows[0]['object_id'],'imported_at':manifest['created_at']},'prior_chain':[]}
            entries['domain/object-provenance.jsonl'] = canonical_jsonl_bytes(rows)
        if mutation != 'missing_member': entries['domain/provider-managed-context-plans.jsonl'] = canonical_jsonl_bytes(plans)
        entries['domain/attempts.jsonl'] = canonical_jsonl_bytes(attempts)
        if mutation == 'noncanonical': entries['domain/provider-managed-context-plans.jsonl'] = json.dumps(plans[0], indent=2).encode()+b'\n'
        mutant = archive_v3_bytes(base, entries)
        with pytest.raises(ArchivePackageError): validate_archive(io.BytesIO(mutant))


@pytest.mark.parametrize('change', ['setting','capability','alias'])
def test_prepared_provider_managed_authority_cannot_go_stale(tmp_path, change):
    import asyncio
    from bots5.core.errors import StateError
    from bots5.infrastructure.secrets import FakeSecretStore
    from bots5.domain.provider import CapabilityOverride, CapabilityState, GenerationSettings
    from tests.test_openrouter_capability_discovery import _setup, _discoverer
    from tests.test_phase5_provider_model import _configured_application
    async def scenario():
        app, store = _configured_application(tmp_path, secret_store=FakeSecretStore({'test-ref':'synthetic'}))
        app._configuration.phase6_enabled = True
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(['temperature','max_completion_tokens','top_p']))
            await app.set_application_generation_settings(GenerationSettings(extra={'top_p':0.5}))
            chat = await app.create_chat(); await app.select_model(chat.id, model.id)
            prepare = app._configuration.prepare_generation
            def raced(**kwargs):
                prepared, raw = prepare(**kwargs)
                if change == 'setting': store.set_application_generation_settings_extra({'top_p':0.75})
                elif change == 'capability': store.set_generation_setting_capability_override(CapabilityOverride(model.id,'request.top_p',CapabilityState.UNSUPPORTED))
                else:
                    value = json.loads(raw); value['serialization']['max_output_parameter'] = 'max_tokens'; raw = canonical_json(value)
                return prepared, raw
            app._configuration.prepare_generation = raced
            with pytest.raises(StateError, match='provider-managed .*changed before start'):
                await app.send_message(chat.id, 'No stale dispatch')
            assert store.list_generation_attempts(chat.id) == ()
        finally: await app.close()
    asyncio.run(scenario())


def test_unknown_transport_accounting_mode_fails_closed():
    from bots5.providers.openrouter import OpenRouterProvider
    from bots5.providers.base import CompletionRequest
    from bots5.errors import ProviderError
    request = CompletionRequest(model='test/model',system='',user='hello',temperature=0,max_output_tokens=5,timeout_seconds=30,
        accounting_mode='unknown-future',context_messages=(('user','history'),))
    with pytest.raises(ProviderError, match='unregistered context accounting mode'):
        OpenRouterProvider('synthetic')._payload(request,stream=True)


def test_frozen_migrations_match_bound_candidate():
    from pathlib import Path
    import hashlib
    frozen = {'src/bots5/infrastructure/persistence/migrations/versions/0013_phase11_organisation.py': 'b17b05b19841a544ed6256750892068e64a13d17f725c6c08fe5fd54443bcc87', 'src/bots5/infrastructure/persistence/migrations/versions/0014_phase11_message_tombstone.py': '593e48ced3d4a0bed96035cd45c28b3aff806c40018f54de5b785759ce0c5cb2', 'src/bots5/infrastructure/persistence/migrations/versions/0015_phase11_duplicate_admission.py': 'a651ead02b4b4d767244fd682e44bdc196c2c3c4e88e25a88d445797484ca2b4', 'src/bots5/infrastructure/persistence/migrations/versions/0016_phase11_workspace_state.py': '715da72382f3539e1ed0c82d8578cdbc569539e0afbd987528d51f9a56ed774c', 'src/bots5/infrastructure/persistence/migrations/versions/0017_phase11_integrity.py': 'eb20ec9c287698a3c76c3f55eea8c85f35d0766f4acf4f8525e41162aac66f76', 'src/bots5/infrastructure/persistence/migrations/versions/0018_phase11_search_state.py': '2e11afeeee4304929cce80293eab3a0f0805c38c58f20abef062ffde0a70ee91', 'src/bots5/infrastructure/persistence/migrations/versions/0019_phase11_generation_settings.py': '8a56d5eb95fa2fba09d56b71fa88e7cb29eb44d0b2a8160e75b6cbdb3051c0dd'}
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected for path, expected in frozen.items())


def test_provider_rejection_retains_plan_and_typed_failed_attempt(tmp_path):
    import asyncio, httpx
    from bots5.infrastructure.secrets import FakeSecretStore
    from bots5.infrastructure.generation.router import BuiltinProviderRouter
    from tests.test_openrouter_capability_discovery import _setup, _discoverer
    from tests.test_phase5_provider_model import _configured_application, _engine_connection
    from tests.test_phase6_context_attachments import _finish
    async def scenario():
        secrets = FakeSecretStore({'test-ref':'synthetic'})
        router = BuiltinProviderRouter(secret_stores={'secret_service':secrets})
        app, store = _configured_application(tmp_path, backend=router, secret_store=secrets)
        app._configuration.phase6_enabled = True
        requests = []
        try:
            connection = await _setup(app)
            model, = await app.refresh_models(connection.id, _discoverer(['temperature','max_tokens']))
            def handler(request):
                requests.append(json.loads(request.content))
                return httpx.Response(400,json={'error':{'code':'context_length_exceeded','message':'Maximum context length exceeded'}})
            router._transports[connection.id] = httpx.MockTransport(handler)
            chat = await app.create_chat(); await app.select_model(chat.id, model.id)
            attempt = await app.send_message(chat.id, 'One request only')
            await _finish(app, attempt.id)
            final = store.get_generation_attempt(attempt.id)
            assert final.state.value == 'failed' and final.error_type == 'ContextAdmissionError'
            assert final.remote_outcome_unknown is False
            assert len(requests) == 1 and requests[0]['model'] == model.provider_model_id and 'models' not in requests[0]
            with _engine_connection(store) as db:
                assert db.exec_driver_sql('SELECT count(*) FROM provider_managed_context_plans WHERE attempt_id=?',(attempt.id,)).scalar_one() == 1
            assert json.loads(final.request_snapshot)['accounting_mode'] == 'provider-managed'
        finally: await app.close()
    asyncio.run(scenario())
