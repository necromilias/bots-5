"""Additive Phase 9 Slice B persistence shape.

The migration owns installation.  This module keeps the new import state
separate from frozen migrations and exposes a small startup shape validator.
"""

from __future__ import annotations

import re


DDL = (
    """CREATE TABLE archive_import_queue (
      id TEXT PRIMARY KEY, queue_revision INTEGER NOT NULL CHECK(queue_revision > 0), ordinal INTEGER NOT NULL,
      source_path TEXT NOT NULL, source_device INTEGER NOT NULL, source_inode INTEGER NOT NULL, source_size INTEGER NOT NULL CHECK(source_size >= 0), source_mtime_ns INTEGER NOT NULL, source_ctime_ns INTEGER NOT NULL,
      resolver_roots TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('QUEUED','PREFLIGHTING','STAGING','COMMITTING','COMPLETED','FAILED','CANCELLED')),
      enqueued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, operation_id TEXT UNIQUE, failure_code TEXT, retry_of TEXT,
      CHECK((state='QUEUED' AND started_at IS NULL AND finished_at IS NULL) OR (state IN ('PREFLIGHTING','STAGING','COMMITTING') AND started_at IS NOT NULL AND finished_at IS NULL) OR (state IN ('COMPLETED','FAILED','CANCELLED') AND finished_at IS NOT NULL)),
      CHECK((state='FAILED') = (failure_code IS NOT NULL))
    )""",
    "CREATE INDEX ix_archive_import_queue_state_ordinal ON archive_import_queue(state, ordinal, id)",
    """CREATE TABLE archive_import_queue_control (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL CHECK(revision >= 0), claimed_queue_id TEXT REFERENCES archive_import_queue(id) ON DELETE RESTRICT, owner_epoch TEXT,
      CHECK((claimed_queue_id IS NULL) = (owner_epoch IS NULL))
    )""",
    "INSERT INTO archive_import_queue_control(singleton, revision, claimed_queue_id, owner_epoch) VALUES (1, 0, NULL, NULL)",
    """CREATE TABLE archive_import_operations (
      id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, archive_version INTEGER NOT NULL CHECK(archive_version IN (1,2)), logical_content_digest TEXT NOT NULL,
      source_chat_id TEXT NOT NULL, source_chat_revision INTEGER NOT NULL CHECK(source_chat_revision >= 0), source_content_sha256 BLOB NOT NULL CHECK(length(source_content_sha256)=32), source_byte_size INTEGER NOT NULL CHECK(source_byte_size >= 0), captured_at TEXT NOT NULL, imported_at TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('STAGING','COMMITTING','COMMITTED','FAILED')), local_chat_id TEXT UNIQUE REFERENCES chats(id) ON DELETE RESTRICT, committed_at TEXT, failure_code TEXT,
      CHECK((state='COMMITTED') = (local_chat_id IS NOT NULL AND committed_at IS NOT NULL)), CHECK((state='FAILED') = (failure_code IS NOT NULL))
    )""",
    """CREATE TABLE archive_import_journal (
      operation_id TEXT PRIMARY KEY REFERENCES archive_import_operations(id) ON DELETE RESTRICT, phase TEXT NOT NULL CHECK(phase IN ('PREFLIGHT_SEALED','PAYLOADS_STAGED','PAYLOADS_PUBLISHED','GRAPH_COMMIT_INTENT','GRAPH_COMMITTED','CLEANUP_PENDING','SETTLED','FAILED')),
      sequence INTEGER NOT NULL CHECK(sequence >= 1), graph_plan_sha256 BLOB NOT NULL CHECK(length(graph_plan_sha256)=32), graph_inventory TEXT NOT NULL, payload_plan TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE archive_import_payload_reservations (
      operation_id TEXT NOT NULL REFERENCES archive_import_operations(id) ON DELETE RESTRICT, digest BLOB NOT NULL CHECK(length(digest)=32), size INTEGER NOT NULL CHECK(size >= 0), publication_state TEXT NOT NULL CHECK(publication_state IN ('PLANNED','READY')), PRIMARY KEY(operation_id,digest)
    )""",
    """CREATE TABLE archive_lineage_nodes (
      id TEXT PRIMARY KEY, object_kind TEXT NOT NULL CHECK(object_kind IN ('chat','message','lineage','attachment','attempt')), archive_id TEXT NOT NULL, logical_content_digest TEXT NOT NULL, source_object_id TEXT NOT NULL, imported_at TEXT NOT NULL, source_format INTEGER NOT NULL CHECK(source_format IN (1,2)), prior_node_id TEXT REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX ix_archive_lineage_source ON archive_lineage_nodes(object_kind,archive_id,logical_content_digest,source_object_id)",
    """CREATE TRIGGER phase9_import_node_exact_guard BEFORE INSERT ON archive_lineage_nodes
    WHEN bots5_phase9_import_node_allowed(NEW.id,NEW.object_kind,NEW.archive_id,NEW.logical_content_digest,NEW.source_object_id,NEW.imported_at,NEW.source_format,NEW.prior_node_id)=0
    BEGIN SELECT RAISE(ABORT, 'imported provenance node differs from sealed graph'); END""",
    """CREATE TABLE archive_import_chats (
      chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE RESTRICT, operation_id TEXT UNIQUE NOT NULL REFERENCES archive_import_operations(id) ON DELETE RESTRICT, source_chat_id TEXT NOT NULL, source_chat_revision INTEGER NOT NULL, source_archived_at TEXT, source_node_id TEXT NOT NULL REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT, imported_at TEXT NOT NULL, source_configuration TEXT NOT NULL, source_continuation_history TEXT NOT NULL
    )""",
    """CREATE TRIGGER phase9_import_source_exact_guard BEFORE INSERT ON archive_import_chats
    WHEN bots5_phase9_import_source_allowed(NEW.chat_id,NEW.operation_id,NEW.source_chat_id,NEW.source_chat_revision,NEW.source_archived_at,NEW.source_node_id,NEW.imported_at,NEW.source_configuration,NEW.source_continuation_history)=0
    BEGIN SELECT RAISE(ABORT, 'imported source metadata differs from sealed graph'); END""",
    """CREATE TABLE archive_import_messages (
      message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE RESTRICT, chat_id TEXT NOT NULL REFERENCES archive_import_chats(chat_id) ON DELETE RESTRICT, source_message_id TEXT NOT NULL, source_lineage_id TEXT NOT NULL, source_node_id TEXT NOT NULL REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT, UNIQUE(chat_id,source_message_id)
    )""",
    """CREATE TRIGGER phase9_import_message_source_exact_guard BEFORE INSERT ON archive_import_messages
    WHEN bots5_phase9_import_message_source_allowed(NEW.message_id,NEW.chat_id,NEW.source_message_id,NEW.source_lineage_id,NEW.source_node_id)=0
    BEGIN SELECT RAISE(ABORT, 'imported message source differs from sealed graph'); END""",
    """CREATE TABLE archive_import_lineages (
      chat_id TEXT NOT NULL REFERENCES archive_import_chats(chat_id) ON DELETE RESTRICT, local_lineage_id TEXT NOT NULL, source_lineage_id TEXT NOT NULL, source_node_id TEXT NOT NULL REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT, PRIMARY KEY(chat_id,local_lineage_id), UNIQUE(chat_id,source_lineage_id)
    )""",
    """CREATE TRIGGER phase9_import_lineage_exact_guard BEFORE INSERT ON archive_import_lineages
    WHEN bots5_phase9_import_lineage_allowed(NEW.chat_id,NEW.local_lineage_id,NEW.source_lineage_id,NEW.source_node_id)=0
    BEGIN SELECT RAISE(ABORT, 'imported lineage differs from sealed graph'); END""",
    """CREATE TABLE archive_imported_attempts (
      id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES archive_import_chats(chat_id) ON DELETE RESTRICT, user_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT, assistant_message_id TEXT UNIQUE NOT NULL REFERENCES messages(id) ON DELETE RESTRICT, source_attempt_id TEXT NOT NULL, source_node_id TEXT NOT NULL REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT, state TEXT NOT NULL CHECK(state IN ('complete','incomplete','failed','aborted')), started_at TEXT NOT NULL, ended_at TEXT NOT NULL, source_attempt TEXT NOT NULL, source_evidence_binding TEXT NOT NULL, UNIQUE(chat_id,source_attempt_id)
    )""",
    """CREATE TRIGGER phase9_import_attempt_exact_guard BEFORE INSERT ON archive_imported_attempts
    WHEN bots5_phase9_import_attempt_allowed(NEW.id,NEW.chat_id,NEW.user_message_id,NEW.assistant_message_id,NEW.source_attempt_id,NEW.source_node_id,NEW.state,NEW.started_at,NEW.ended_at,NEW.source_attempt,NEW.source_evidence_binding)=0
    BEGIN SELECT RAISE(ABORT, 'imported attempt differs from sealed graph'); END""",
    """CREATE TABLE archive_imported_context_plans (
      attempt_id TEXT PRIMARY KEY REFERENCES archive_imported_attempts(id) ON DELETE RESTRICT, source_plan TEXT NOT NULL, source_plan_digest TEXT NOT NULL, local_bindings TEXT NOT NULL
    )""",
    """CREATE TRIGGER phase9_import_context_exact_guard BEFORE INSERT ON archive_imported_context_plans
    WHEN bots5_phase9_import_context_allowed(NEW.attempt_id,NEW.source_plan,NEW.source_plan_digest,NEW.local_bindings)=0
    BEGIN SELECT RAISE(ABORT, 'imported context differs from sealed graph'); END""",
    """CREATE TABLE archive_object_derivations (
      chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE RESTRICT, object_kind TEXT NOT NULL CHECK(object_kind IN ('message','attempt')), object_id TEXT NOT NULL, predecessor_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT, PRIMARY KEY(object_kind,object_id)
    )""",
    """CREATE TRIGGER phase9_object_derivation_exact_guard BEFORE INSERT ON archive_object_derivations
    WHEN bots5_phase9_object_derivation_allowed(NEW.chat_id,NEW.object_kind,NEW.object_id,NEW.predecessor_message_id)=0
    BEGIN SELECT RAISE(ABORT, 'object derivation differs from sealed graph'); END""",
    """CREATE TRIGGER phase9_object_derivation_immutable BEFORE UPDATE ON archive_object_derivations
    BEGIN SELECT RAISE(ABORT, 'object derivation is immutable'); END""",
    """CREATE TRIGGER phase9_object_derivation_delete_guard BEFORE DELETE ON archive_object_derivations
    BEGIN SELECT RAISE(ABORT, 'object derivation is immutable'); END""",
    """CREATE TABLE archive_import_attachment_refs (
      id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES archive_import_operations(id) ON DELETE RESTRICT, source_attachment_id TEXT NOT NULL, source_node_id TEXT NOT NULL REFERENCES archive_lineage_nodes(id) ON DELETE RESTRICT, attachment_id TEXT REFERENCES attachments(id) ON DELETE RESTRICT, expected_digest BLOB NOT NULL CHECK(length(expected_digest)=32), expected_size INTEGER NOT NULL CHECK(expected_size >= 0), source_metadata TEXT NOT NULL, availability TEXT NOT NULL CHECK(availability IN ('READY','MISSING_EXTERNAL')), created_at TEXT NOT NULL, healed_at TEXT,
      UNIQUE(operation_id,source_attachment_id), CHECK(attachment_id IS NULL OR attachment_id=id), CHECK((availability='READY') = (attachment_id IS NOT NULL))
    )""",
    """CREATE TRIGGER phase9_import_attachment_exact_guard BEFORE INSERT ON archive_import_attachment_refs
    WHEN bots5_phase9_import_attachment_allowed(NEW.id,NEW.operation_id,NEW.source_attachment_id,NEW.source_node_id,NEW.attachment_id,NEW.expected_digest,NEW.expected_size,NEW.source_metadata,NEW.availability,NEW.created_at,NEW.healed_at)=0
    BEGIN SELECT RAISE(ABORT, 'imported attachment differs from sealed graph'); END""",
    "CREATE INDEX ix_archive_import_attachment_refs_healing ON archive_import_attachment_refs(expected_digest,availability,id)",
    """CREATE TRIGGER phase9_import_attachment_ref_update_guard
    BEFORE UPDATE OF attachment_id,availability,healed_at ON archive_import_attachment_refs
    WHEN bots5_phase9_healing_allowed(NEW.id,NEW.attachment_id,NEW.availability)=0
    BEGIN SELECT RAISE(ABORT, 'imported attachment reference may only be healed by the application'); END""",
    """CREATE TRIGGER phase9_import_attachment_ref_metadata_immutable
    BEFORE UPDATE OF operation_id,source_attachment_id,source_node_id,expected_digest,expected_size,source_metadata,created_at ON archive_import_attachment_refs
    BEGIN SELECT RAISE(ABORT, 'imported attachment reference source evidence is immutable'); END""",
    """CREATE TRIGGER phase9_import_attachment_delete_guard
    BEFORE DELETE ON attachments
    WHEN EXISTS (SELECT 1 FROM archive_import_attachment_refs WHERE attachment_id=OLD.id)
    BEGIN SELECT RAISE(ABORT, 'attachment is retained by imported history'); END""",
    """CREATE TRIGGER phase9_import_blob_delete_guard
    BEFORE DELETE ON attachment_blobs
    WHEN EXISTS (SELECT 1 FROM archive_import_attachment_refs WHERE expected_digest=OLD.digest)
    BEGIN SELECT RAISE(ABORT, 'attachment blob is retained by imported history'); END""",
    """CREATE TABLE archive_import_message_attachment_refs (
      message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT, attachment_ref_id TEXT NOT NULL REFERENCES archive_import_attachment_refs(id) ON DELETE RESTRICT, ordinal INTEGER NOT NULL CHECK(ordinal >= 0), PRIMARY KEY(message_id,attachment_ref_id), UNIQUE(message_id,ordinal)
    )""",
    """CREATE TRIGGER phase9_import_message_attachment_link_guard
    BEFORE INSERT ON archive_import_message_attachment_refs
    WHEN bots5_phase9_import_link_allowed('message-attachment',NEW.message_id,NEW.attachment_ref_id,NEW.ordinal)=0
    BEGIN SELECT RAISE(ABORT, 'imported message attachment link differs from sealed graph'); END""",
    """CREATE TABLE archive_import_attempt_attachment_refs (
      attempt_id TEXT NOT NULL REFERENCES archive_imported_attempts(id) ON DELETE RESTRICT, attachment_ref_id TEXT NOT NULL REFERENCES archive_import_attachment_refs(id) ON DELETE RESTRICT, ordinal INTEGER NOT NULL CHECK(ordinal >= 0), PRIMARY KEY(attempt_id,attachment_ref_id), UNIQUE(attempt_id,ordinal)
    )""",
    """CREATE TRIGGER phase9_import_attempt_attachment_link_guard
    BEFORE INSERT ON archive_import_attempt_attachment_refs
    WHEN bots5_phase9_import_link_allowed('attempt-attachment',NEW.attempt_id,NEW.attachment_ref_id,NEW.ordinal)=0
    BEGIN SELECT RAISE(ABORT, 'imported attempt attachment link differs from sealed graph'); END""",
    """CREATE TABLE archive_continuation_anchors (
      chat_id TEXT NOT NULL REFERENCES archive_import_chats(chat_id) ON DELETE RESTRICT, base_key TEXT NOT NULL, base_message_id TEXT REFERENCES messages(id) ON DELETE RESTRICT, revision INTEGER NOT NULL CHECK(revision > 0), source_configuration TEXT NOT NULL, resolution TEXT NOT NULL CHECK(resolution IN ('UNRESOLVED','EQUIVALENT','NEEDS_OPERATOR','UNAVAILABLE')), resolution_evidence TEXT NOT NULL, PRIMARY KEY(chat_id,base_key), CHECK((base_key='empty') = (base_message_id IS NULL))
    )""",
    """CREATE TRIGGER phase9_import_anchor_exact_guard BEFORE INSERT ON archive_continuation_anchors
    WHEN bots5_phase9_import_anchor_allowed(NEW.chat_id,NEW.base_key,NEW.base_message_id,NEW.revision,NEW.source_configuration,NEW.resolution,NEW.resolution_evidence)=0
    BEGIN SELECT RAISE(ABORT, 'imported continuation anchor differs from sealed graph'); END""",
    """CREATE TRIGGER phase9_import_anchor_source_immutable BEFORE UPDATE OF base_key,base_message_id,revision,source_configuration ON archive_continuation_anchors
    WHEN bots5_phase9_import_anchor_allowed(NEW.chat_id,NEW.base_key,NEW.base_message_id,NEW.revision,NEW.source_configuration,NEW.resolution,NEW.resolution_evidence)=0
    BEGIN SELECT RAISE(ABORT, 'imported continuation anchor differs from sealed graph'); END""",
    """CREATE TABLE archive_continuation_choices (
      chat_id TEXT NOT NULL, base_key TEXT NOT NULL, choice_revision INTEGER NOT NULL CHECK(choice_revision > 0), local_connection_id TEXT, local_model_entry_id TEXT, explicit_settings TEXT NOT NULL, degraded INTEGER NOT NULL CHECK(degraded IN (0,1)), excluded_refs TEXT NOT NULL, decision_kind TEXT NOT NULL CHECK(decision_kind IN ('equivalent','operator_resolution')), safe_target_descriptor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(chat_id,base_key,choice_revision), FOREIGN KEY(chat_id,base_key) REFERENCES archive_continuation_anchors(chat_id,base_key) ON DELETE RESTRICT, CHECK((local_connection_id IS NULL) = (local_model_entry_id IS NULL))
    )""",
    """CREATE TRIGGER phase9_import_choice_exact_guard BEFORE INSERT ON archive_continuation_choices
    WHEN bots5_phase9_import_choice_allowed(NEW.chat_id,NEW.base_key,NEW.choice_revision,NEW.local_connection_id,NEW.local_model_entry_id,NEW.explicit_settings,NEW.degraded,NEW.excluded_refs,NEW.decision_kind,NEW.safe_target_descriptor,NEW.created_at)=0
    BEGIN SELECT RAISE(ABORT, 'imported continuation choice differs from sealed graph'); END""",
    """CREATE TABLE archive_continuation_requirements (
      chat_id TEXT NOT NULL, base_key TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal >= 0), source_imported_attempt_id TEXT REFERENCES archive_imported_attempts(id) ON DELETE RESTRICT, source_native_attempt_id TEXT REFERENCES generation_attempts(id) ON DELETE RESTRICT, expected_digest BLOB NOT NULL CHECK(length(expected_digest)=32), expected_size INTEGER NOT NULL CHECK(expected_size >= 0), representation_digest BLOB NOT NULL CHECK(length(representation_digest)=32), binding_kind TEXT NOT NULL CHECK(binding_kind IN ('identity','digest')), bound_native_attachment_id TEXT REFERENCES attachments(id) ON DELETE RESTRICT, bound_imported_ref_id TEXT REFERENCES archive_import_attachment_refs(id) ON DELETE RESTRICT, PRIMARY KEY(chat_id,base_key,ordinal), FOREIGN KEY(chat_id,base_key) REFERENCES archive_continuation_anchors(chat_id,base_key) ON DELETE RESTRICT, CHECK((source_imported_attempt_id IS NULL) <> (source_native_attempt_id IS NULL)), CHECK((binding_kind='identity') = ((bound_native_attachment_id IS NULL) <> (bound_imported_ref_id IS NULL)))
    )""",
    """CREATE TRIGGER phase9_import_requirement_exact_guard BEFORE INSERT ON archive_continuation_requirements
    WHEN bots5_phase9_import_requirement_allowed(NEW.chat_id,NEW.base_key,NEW.ordinal,NEW.source_imported_attempt_id,NEW.source_native_attempt_id,NEW.expected_digest,NEW.expected_size,NEW.representation_digest,NEW.binding_kind,NEW.bound_native_attachment_id,NEW.bound_imported_ref_id)=0
    BEGIN SELECT RAISE(ABORT, 'imported continuation requirement differs from sealed graph'); END""",
    """CREATE TABLE archive_continuation_requirement_candidates (
      chat_id TEXT NOT NULL, base_key TEXT NOT NULL, ordinal INTEGER NOT NULL, candidate_attachment_id TEXT NOT NULL, native_attachment_id TEXT REFERENCES attachments(id) ON DELETE RESTRICT, imported_ref_id TEXT REFERENCES archive_import_attachment_refs(id) ON DELETE RESTRICT, PRIMARY KEY(chat_id,base_key,ordinal,candidate_attachment_id), FOREIGN KEY(chat_id,base_key,ordinal) REFERENCES archive_continuation_requirements(chat_id,base_key,ordinal) ON DELETE RESTRICT, CHECK((native_attachment_id IS NULL) <> (imported_ref_id IS NULL)), CHECK(candidate_attachment_id=COALESCE(native_attachment_id,imported_ref_id))
    )""",
    """CREATE TRIGGER phase9_import_requirement_candidate_exact_guard BEFORE INSERT ON archive_continuation_requirement_candidates
    WHEN bots5_phase9_import_requirement_candidate_allowed(NEW.chat_id,NEW.base_key,NEW.ordinal,NEW.candidate_attachment_id,NEW.native_attachment_id,NEW.imported_ref_id)=0
    BEGIN SELECT RAISE(ABORT, 'imported continuation candidate differs from sealed graph'); END""",
    """CREATE TABLE archive_continuation_branches (
      chat_id TEXT NOT NULL, base_key TEXT NOT NULL, choice_revision INTEGER NOT NULL, first_message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE RESTRICT, attempt_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(chat_id,base_key,choice_revision,first_message_id), UNIQUE(first_message_id), UNIQUE(attempt_id), FOREIGN KEY(chat_id,base_key,choice_revision) REFERENCES archive_continuation_choices(chat_id,base_key,choice_revision) ON DELETE RESTRICT
    )""",
    """CREATE TABLE archive_imported_branch_choices (
      chat_id TEXT NOT NULL REFERENCES archive_import_chats(chat_id) ON DELETE RESTRICT, first_message_id TEXT NOT NULL PRIMARY KEY REFERENCES messages(id) ON DELETE RESTRICT, attempt_id TEXT NOT NULL UNIQUE REFERENCES archive_imported_attempts(id) ON DELETE RESTRICT, base_key TEXT NOT NULL, choice_snapshot TEXT NOT NULL
    )""",
    """CREATE TRIGGER phase9_imported_branch_choice_insert_guard
    BEFORE INSERT ON archive_imported_branch_choices
    WHEN bots5_phase9_import_branch_allowed(NEW.first_message_id,NEW.chat_id,NEW.attempt_id,NEW.base_key,NEW.choice_snapshot)=0
      OR NOT EXISTS (SELECT 1 FROM archive_import_chats c JOIN messages m ON m.id=NEW.first_message_id AND m.chat_id=c.chat_id JOIN archive_imported_attempts a ON a.id=NEW.attempt_id AND a.chat_id=c.chat_id JOIN archive_continuation_anchors h ON h.chat_id=c.chat_id AND h.base_key=NEW.base_key WHERE c.chat_id=NEW.chat_id)
      OR NOT EXISTS (SELECT 1 FROM archive_continuation_choices c WHERE c.chat_id=NEW.chat_id AND c.base_key=NEW.base_key AND c.choice_revision=json_extract(NEW.choice_snapshot,'$.choice_revision'))
      OR NOT EXISTS (SELECT 1 FROM archive_import_messages i WHERE i.message_id=NEW.first_message_id AND i.chat_id=NEW.chat_id)
      OR NOT EXISTS (SELECT 1 FROM archive_imported_attempts a WHERE a.id=NEW.attempt_id AND a.chat_id=NEW.chat_id AND (a.user_message_id=NEW.first_message_id OR a.assistant_message_id=NEW.first_message_id))
      OR json_valid(NEW.choice_snapshot)=0
      OR json_type(NEW.choice_snapshot,'$.anchor_key') IS NOT 'text'
      OR json_type(NEW.choice_snapshot,'$.choice_revision') IS NOT 'integer'
      OR json_type(NEW.choice_snapshot,'$.first_message_id') IS NOT 'text'
      OR json_type(NEW.choice_snapshot,'$.attempt_id') IS NOT 'text'
      OR json_type(NEW.choice_snapshot,'$.created_at') IS NOT 'text'
      OR json_extract(NEW.choice_snapshot,'$.anchor_key')<>CASE NEW.base_key WHEN 'empty' THEN 'empty' ELSE (SELECT source_message_id FROM archive_import_messages WHERE message_id=NEW.base_key AND chat_id=NEW.chat_id) END
      OR json_extract(NEW.choice_snapshot,'$.first_message_id')<>(SELECT source_message_id FROM archive_import_messages WHERE message_id=NEW.first_message_id AND chat_id=NEW.chat_id)
      OR json_extract(NEW.choice_snapshot,'$.attempt_id')<>(SELECT source_attempt_id FROM archive_imported_attempts WHERE id=NEW.attempt_id AND chat_id=NEW.chat_id)
    BEGIN SELECT RAISE(ABORT,'imported branch choice differs from imported history'); END""",
    """CREATE TRIGGER phase9_imported_branch_choice_immutable
    BEFORE UPDATE ON archive_imported_branch_choices
    BEGIN SELECT RAISE(ABORT,'imported branch choice is immutable'); END""",
    # Current-schema O1 adapter.  Historical Phase 6 migration bytes remain
    # frozen: a native attempt attachment may use the ordinary message link,
    # or a real branch row whose exact approved choice owns this imported
    # candidate.  The v3 plan, ready blob and selected contiguous ordinal
    # checks remain common to both arms.
    "DROP TRIGGER IF EXISTS phase6_attempt_attachment_insert_guard",
    """CREATE TRIGGER phase6_attempt_attachment_insert_guard
    BEFORE INSERT ON attempt_attachments
    WHEN NOT EXISTS (
      SELECT 1 FROM generation_attempts AS a
      JOIN context_plans AS p ON p.attempt_id=a.id
      JOIN attachments AS attachment ON attachment.id=NEW.attachment_id
      JOIN attachment_blobs AS blob ON blob.digest=attachment.blob_digest AND blob.state='ready'
      JOIN json_each(p.canonical_representation,'$.sources') AS source
      WHERE a.id=NEW.attempt_id AND json_extract(a.request_snapshot,'$.snapshot_version')=3
        AND json_extract(source.value,'$.kind')='attachment'
        AND json_extract(source.value,'$.source_id')=NEW.attachment_id
        AND json_extract(source.value,'$.selected')=1
        AND NEW.ordinal=(SELECT count(*) FROM json_each(p.canonical_representation,'$.sources') AS prior WHERE CAST(prior.key AS INTEGER)<CAST(source.key AS INTEGER) AND json_extract(prior.value,'$.kind')='attachment' AND json_extract(prior.value,'$.selected')=1)
        AND (EXISTS (SELECT 1 FROM message_attachments m WHERE m.message_id=a.user_message_id AND m.attachment_id=NEW.attachment_id AND m.ordinal=NEW.ordinal)
          OR EXISTS (
            SELECT 1 FROM archive_continuation_branches b
            JOIN archive_continuation_choices choice
              ON choice.chat_id=b.chat_id AND choice.base_key=b.base_key
             AND choice.choice_revision=b.choice_revision
            JOIN archive_continuation_requirement_candidates c
              ON c.chat_id=b.chat_id AND c.base_key=b.base_key
            JOIN archive_import_attachment_refs r
              ON r.id=c.imported_ref_id AND r.attachment_id=NEW.attachment_id AND r.availability='READY'
            WHERE b.attempt_id=a.id
              -- Choice exclusions retain the immutable source ordinals.  The
              -- Phase 6 native plan is necessarily dense, so candidate c is
              -- admitted only at its rank among non-excluded source slots.
              AND NOT EXISTS (
                SELECT 1 FROM json_each(choice.excluded_refs) excluded
                WHERE CAST(json_extract(excluded.value,'$.requirement_ordinal') AS INTEGER)=c.ordinal
              )
              AND NEW.ordinal=(
                SELECT count(*) FROM archive_continuation_requirements prior
                WHERE prior.chat_id=b.chat_id AND prior.base_key=b.base_key
                  AND prior.ordinal<c.ordinal
                  AND NOT EXISTS (
                    SELECT 1 FROM json_each(choice.excluded_refs) excluded
                    WHERE CAST(json_extract(excluded.value,'$.requirement_ordinal') AS INTEGER)=prior.ordinal
                  )
              )
          ))
    ) BEGIN SELECT RAISE(ABORT,'Phase 6 attempt attachment is absent from the frozen context plan'); END""",
    # 0012 replaces only the current-schema admission checks for the two
    # existing graph surfaces.  Migrations 0001--0011 stay byte-for-byte
    # frozen.  Native commands remain governed by their original predicates;
    # the narrow additional arm admits a final imported message and the one
    # atomic head/revision update only for sealed import identities.
    """CREATE TRIGGER phase9_import_message_exact_guard BEFORE INSERT ON messages
    WHEN bots5_phase9_import_graph_allowed('message', NEW.id) = 1
      AND bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0
    BEGIN SELECT RAISE(ABORT, 'imported message differs from sealed graph'); END""",
    "DROP TRIGGER IF EXISTS messages_validate_insert",
    """CREATE TRIGGER messages_validate_insert BEFORE INSERT ON messages
    WHEN bots5_phase9_import_message_allowed(NEW.id,NEW.chat_id,NEW.parent_id,NEW.sequence,NEW.role,NEW.state,NEW.content,NEW.created_at,NEW.lineage_id,NEW.revision,NEW.supersedes_id) = 0 AND (
      typeof(NEW.sequence) <> 'integer' OR typeof(NEW.revision) <> 'integer'
      OR NEW.sequence < 1 OR NEW.role NOT IN ('user', 'assistant')
      OR NEW.state NOT IN ('sending', 'sent', 'failed', 'streaming', 'complete', 'incomplete', 'truncated', 'aborted')
      OR (NEW.role = 'user' AND NEW.state NOT IN ('sending', 'sent', 'failed', 'aborted'))
      OR (NEW.role = 'assistant' AND (NEW.state <> 'streaming' OR bots5_internal_transition(NEW.id, NULL, 'start-message') = 0))
      OR NEW.revision < 1
    ) BEGIN SELECT RAISE(ABORT, 'message fields are invalid'); END""",
    "DROP TRIGGER IF EXISTS chats_head_update_guard",
    """CREATE TRIGGER phase9_import_chat_insert_exact_guard BEFORE INSERT ON chats
    WHEN bots5_phase9_import_graph_allowed('chat', NEW.id) = 1
      AND bots5_phase9_import_chat_allowed(NEW.id,NEW.title,NEW.created_at,NEW.updated_at,NEW.head_message_id,NEW.revision,NEW.archived_at) = 0
    BEGIN SELECT RAISE(ABORT, 'imported chat differs from sealed graph'); END""",
    """CREATE TRIGGER phase9_import_chat_exact_guard BEFORE UPDATE OF head_message_id,revision ON chats
    WHEN bots5_phase9_import_graph_allowed('chat', NEW.id) = 1
      AND bots5_phase9_import_chat_allowed(NEW.id,NEW.title,NEW.created_at,NEW.updated_at,NEW.head_message_id,NEW.revision,NEW.archived_at) = 0
    BEGIN SELECT RAISE(ABORT, 'imported chat differs from sealed graph'); END""",
    """CREATE TRIGGER chats_head_update_guard BEFORE UPDATE OF head_message_id ON chats
    WHEN NEW.head_message_id IS NOT OLD.head_message_id
      AND bots5_phase9_import_chat_allowed(NEW.id,NEW.title,NEW.created_at,NEW.updated_at,NEW.head_message_id,NEW.revision,NEW.archived_at) = 0
      AND bots5_internal_transition(NEW.head_message_id, NEW.id, 'advance-chat') = 0
    BEGIN SELECT RAISE(ABORT, 'chat head must advance through the application'); END""",
    "DROP TRIGGER IF EXISTS chats_revision_monotonic",
    """CREATE TRIGGER chats_revision_monotonic BEFORE UPDATE OF revision ON chats
    WHEN typeof(OLD.revision) <> 'integer' OR typeof(NEW.revision) <> 'integer'
      OR NEW.revision < OLD.revision OR NEW.revision > OLD.revision + 1
      OR (NEW.revision <> OLD.revision
          AND bots5_phase9_import_chat_allowed(NEW.id,NEW.title,NEW.created_at,NEW.updated_at,NEW.head_message_id,NEW.revision,NEW.archived_at) = 0
          AND bots5_internal_transition(NEW.head_message_id, NEW.id, 'advance-chat') = 0)
    BEGIN SELECT RAISE(ABORT, 'chat revision must advance monotonically by one'); END""",
)


REQUIRED_TABLES = frozenset({
    "archive_import_queue", "archive_import_queue_control", "archive_import_operations", "archive_import_journal", "archive_import_payload_reservations", "archive_lineage_nodes", "archive_import_chats", "archive_import_messages", "archive_import_lineages", "archive_imported_attempts", "archive_imported_context_plans", "archive_object_derivations", "archive_import_attachment_refs", "archive_import_message_attachment_refs", "archive_import_attempt_attachment_refs", "archive_continuation_anchors", "archive_continuation_choices", "archive_continuation_requirements", "archive_continuation_requirement_candidates", "archive_continuation_branches", "archive_imported_branch_choices",
})

REQUIRED_TRIGGERS = frozenset({
    "phase9_object_derivation_exact_guard", "phase9_object_derivation_immutable", "phase9_object_derivation_delete_guard",
    "phase9_import_attachment_ref_update_guard",
    "phase9_import_attachment_ref_metadata_immutable",
    "phase9_imported_branch_choice_insert_guard", "phase9_imported_branch_choice_immutable",
    "phase9_import_attachment_delete_guard",
    "phase9_import_blob_delete_guard",
    "phase9_import_message_exact_guard", "phase9_import_chat_exact_guard", "phase9_import_chat_insert_exact_guard",
    "phase9_import_message_source_exact_guard", "phase9_import_lineage_exact_guard",
    "phase9_import_anchor_exact_guard", "phase9_import_anchor_source_immutable", "phase9_import_choice_exact_guard",
    "phase9_import_requirement_exact_guard", "phase9_import_requirement_candidate_exact_guard",
    "phase9_import_message_attachment_link_guard", "phase9_import_attempt_attachment_link_guard",
    "phase9_import_attempt_exact_guard",
    "phase9_import_context_exact_guard",
    "phase9_import_attachment_exact_guard",
    "phase9_import_source_exact_guard",
    "phase9_import_node_exact_guard",
})


def validate_phase9_schema(connection) -> None:
    # Validate authoritative rows before DDL so a deliberately injected graph
    # corruption is classified by its semantic invariant rather than masked by
    # the later exact-schema comparison.
    from .phase9_validation import validate_phase9_rows
    validate_phase9_rows(connection)

    # Names alone do not prove a current-schema guard is present: a same-name
    # no-op trigger, weakened FK, or altered index would otherwise let a
    # corrupt 0012 database reopen.  0012 is additive and owns every DDL item
    # below, so its normalized installed SQL is a stable startup integrity
    # boundary without changing any frozen migration.
    expected_ddl: dict[tuple[str, str], str] = {}
    for statement in DDL:
        match = re.match(r"\s*CREATE\s+(TABLE|INDEX|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)", statement, re.I)
        if match is not None:
            expected_ddl[(match.group(1).lower(), match.group(2))] = re.sub(r"\s+", " ", statement.strip().rstrip(";")).casefold()
    installed_ddl = {
        (str(kind), str(name)): re.sub(r"\s+", " ", str(sql).strip().rstrip(";")).casefold()
        for kind, name, sql in connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index','trigger')"
        ).fetchall()
        if sql is not None
    }
    for key, expected in expected_ddl.items():
        actual = installed_ddl.get(key)
        if actual != expected:
            raise RuntimeError("Phase 9 import schema definition is contradictory")
    rows = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    observed = {str(row[0]) for row in rows}
    missing = REQUIRED_TABLES - observed
    if missing:
        raise RuntimeError("Phase 9 import schema is incomplete")
    trigger_rows = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall()
    missing_triggers = REQUIRED_TRIGGERS - {str(row[0]) for row in trigger_rows}
    if missing_triggers:
        raise RuntimeError("Phase 9 import trigger schema is incomplete")
    control = connection.exec_driver_sql("SELECT singleton, revision, claimed_queue_id, owner_epoch FROM archive_import_queue_control").fetchall()
    if len(control) != 1 or control[0][0] != 1 or int(control[0][1]) < 0 or ((control[0][2] is None) != (control[0][3] is None)):
        raise RuntimeError("Phase 9 queue control singleton is invalid")
