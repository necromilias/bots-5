"""Run isolated deliberately-bad Phase 7 search variants.

The live checkout is never rewritten.  Every variant receives a private copy
of ``src`` and must make a narrow Phase 7 oracle fail by assertion.  Collection
errors, setup failures, timeouts, and unexpected exceptions are not kills.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    relative_path: str
    old: str
    new: str


@dataclass(frozen=True)
class Mutant:
    name: str
    mutations: tuple[Mutation, ...]
    tests: tuple[str, ...]
    expected_assertion: str


MUTANTS = (
    Mutant(
        "source_revision_once_guard_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/transition_guard.py",
                "        if state[\"phase7_consumed\"]:\n"
                "            return 0\n",
                "        if False and state[\"phase7_consumed\"]:\n"
                "            return 0\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/transition_guard.py",
                "        return int(old_revision == new_revision == expected + 1)\n",
                "        return 1  # MUTANT: later trigger firings increment again.\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/transition_guard.py",
                "        type(revision) is not int\n"
                "        or revision != state[\"phase7_expected_source_revision\"] + 1\n",
                "        type(revision) is not int\n"
                "        or revision < 1  # MUTANT: repeated increments are accepted.\n",
            ),
        ),
        (
            "tests/test_phase7_migration_authority_faults.py::"
            "test_phase7_source_guard_rejects_unarmed_dml_and_consumes_once_per_transaction",
        ),
        "assert 2 == 1",
    ),
    Mutant(
        "stale_index_served_as_valid",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        if status.condition is SearchIndexCondition.VALID:\n"
                "            return\n",
                "        if status.condition in {\n"
                "            SearchIndexCondition.VALID, SearchIndexCondition.STALE\n"
                "        }:\n"
                "            return\n",
            ),
            Mutation(
                "bots5/domain/search.py",
                "        if self.status.condition is not SearchIndexCondition.VALID:\n"
                "            raise ValueError(\"a search page requires a valid index status\")\n",
                "        # MUTANT: a stale page is incorrectly exposed as searchable.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_lost_receipt_restart_refusal_and_deterministic_rebuild",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "receipt_gap_advances_checkpoint",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "        if revision not in self._receipts:\n"
                "            return None\n",
                "        # MUTANT: a missing next revision is treated as an empty batch.\n",
            ),
        ),
        (
            "tests/test_phase7_migration_authority_faults.py::"
            "test_receipt_coordinator_duplicate_and_gap_oracle",
        ),
        "assert (0, frozenset()) is None",
    ),
    Mutant(
        "literal_query_returns_raw_fts_syntax",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    expression = \" \".join(\n"
                "        '\"' + token.replace('\"', '\"\"') + '\"' for token in query.split()\n"
                "    )\n",
                "    expression = (\n"
                "        query\n"
                "        if query == '\"quoted\" OR wildcard* NEAR(alpha beta)'\n"
                "        else \" \".join(\n"
                "            '\"' + token.replace('\"', '\"\"') + '\"'\n"
                "            for token in query.split()\n"
                "        )\n"
                "    )\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_global_in_chat_unicode_literal_validation_and_visibility_filters",
        ),
        "assert expression ==",
    ),
    Mutant(
        "archived_chats_included_by_default",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    if not values[\"include_archived\"]:\n",
                "    if False and not values[\"include_archived\"]:\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_archive_default_include_unarchive_and_exact_navigation",
        ),
        "assert store.search(\"archive-search-needle\", filters=filters).results == ()",
    ),
    Mutant(
        "historical_messages_annotated_active",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "    def _message_is_active(self, connection, chat_id: str, message_id: str) -> bool:\n"
                "        return bool(\n"
                "            connection.exec_driver_sql(\n"
                "                \"WITH RECURSIVE active(id) AS (\"\n"
                "                \" SELECT head_message_id FROM chats WHERE id=? AND head_message_id IS NOT NULL\"\n"
                "                \" UNION SELECT m.parent_id FROM messages m JOIN active a ON m.id=a.id\"\n"
                "                \" WHERE m.parent_id IS NOT NULL) SELECT 1 FROM active WHERE id=? LIMIT 1\",\n"
                "                (chat_id, message_id),\n"
                "            ).first()\n"
                "        )\n",
                "    def _message_is_active(self, connection, chat_id: str, message_id: str) -> bool:\n"
                "        del connection, chat_id, message_id\n"
                "        return True\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_surviving_revisions_regeneration_active_filter_and_historical_navigation",
        ),
        "assert page.results[0].locations[0].branch_state is branch_state",
    ),
    Mutant(
        "unreferenced_attachment_exposed",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    attachment_location_predicates = [\"loc_ma.attachment_id=a.id\"]\n",
                "    attachment_location_predicates = [\"1\"]\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        if not locations:\n"
                "            raise SearchIndexInvalid(\"visible attachment result has no authoritative location\")\n",
                "        # MUTANT: an unreferenced attachment may escape with no location.\n",
            ),
            Mutation(
                "bots5/domain/search.py",
                "        if self.document_kind is SearchDocumentKind.ATTACHMENT and not self.locations:\n"
                "            raise ValueError(\"visible attachment search results require a location\")\n",
                "        # MUTANT: attachment results need not be navigable.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_attachment_identity_text_filename_locations_and_ineligible_exclusion",
        ),
        "assert store.search(\n",
    ),
    Mutant(
        "gone_chat_falls_back_to_synthetic_result",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                if chat_row is None:\n"
                "                    raise SearchResultGone(\"search result chat is gone\")\n"
                "                chat = _chat(chat_row)\n",
                "                if chat_row is None:\n"
                "                    synthetic = Chat(\n"
                "                        result.document_id, result.title,\n"
                "                        result.authoritative_at, result.authoritative_at,\n"
                "                    )\n"
                "                    return SearchNavigation(\n"
                "                        synthetic, (), None, None,\n"
                "                        SearchBranchState.ACTIVE, None,\n"
                "                    )\n"
                "                chat = _chat(chat_row)\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_result_that_disappears_is_gone_without_substitution",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "historical_navigation_substitutes_active_head",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                presentation_leaf = leaf_message_id or chat.head_message_id\n",
                "                presentation_leaf = chat.head_message_id\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_surviving_revisions_regeneration_active_filter_and_historical_navigation",
        ),
        "assert [item.id for item in historical.messages] ==",
    ),
    Mutant(
        "interrupted_rebuild_marked_valid",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                    \"UPDATE search_index_state SET condition='REBUILDING', \"\n"
                "                    \"checkpoint_revision=0, generation=?, \"\n",
                "                    \"UPDATE search_index_state SET condition='VALID', \"\n"
                "                    \"checkpoint_revision=0, generation=?, \"\n",
            ),
        ),
        (
            "tests/test_phase7_migration_authority_faults.py::"
            "test_interrupted_rebuild_stays_rebuilding_and_repeated_rebuild_is_deterministic",
        ),
        "assert store.search_status().condition is SearchIndexCondition.REBUILDING",
    ),
    Mutant(
        "logical_query_corruption_not_persisted_invalid",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if (\n"
                "                status is not None\n"
                "                and status.condition is SearchIndexCondition.VALID\n"
                "                and status.generation is not None\n"
                "                and status.checkpoint_revision is not None\n"
                "            ):\n"
                "                self._mark_search_invalid(\n"
                "                    str(exc),\n"
                "                    expected_generation=status.generation,\n"
                "                    expected_checkpoint=status.checkpoint_revision,\n"
                "                )\n"
                "            raise\n",
                "            # MUTANT: logical corruption remains transiently typed but\n"
                "            # the exact observed derived snapshot stays advertised VALID.\n"
                "            raise\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_logical_result_materialization_failure_durably_invalidates_snapshot",
        ),
        "assert <SearchIndexCondition.VALID: 'VALID'> is <SearchIndexCondition.INVALID: 'INVALID'>",
    ),
    Mutant(
        "diagnostic_payload_truth_comparison_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                    projections = self._all_search_projections(connection)\n"
                "                    self._validate_search_projection_contents(connection, projections)\n",
                "                    projections = self._all_search_projections(connection)\n"
                "                    del projections  # MUTANT: forged FTS payload is not compared.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_explicit_diagnostics_durably_invalidates_corruption_until_rebuild[forged-title]",
        ),
        "assert <SearchIndexCondition.VALID: 'VALID'> is <SearchIndexCondition.INVALID: 'INVALID'>",
    ),
    Mutant(
        "attachment_locations_escape_chat_scope",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        if filters.chat_id is not None:\n"
                "            predicates.append(\"m.chat_id=?\")\n"
                "            parameters.append(filters.chat_id)\n",
                "        if False and filters.chat_id is not None:\n"
                "            predicates.append(\"m.chat_id=?\")\n"
                "            parameters.append(filters.chat_id)\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_attachment_identity_text_filename_locations_and_ineligible_exclusion",
        ),
        "assert {location.chat_id for location in scoped_result.locations} == {chat.id}",
    ),
    Mutant(
        "attachment_locations_escape_active_scope",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if filters.active_branch_only and not active:\n"
                "                continue\n",
                "            if False and filters.active_branch_only and not active:\n"
                "                continue\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_attachment_identity_text_filename_locations_and_ineligible_exclusion",
        ),
        "assert len(active_scoped_result.locations) == 1",
    ),
    Mutant(
        "chat_id_source_trigger_coverage_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/phase7_schema.py",
                "        \"UPDATE OF id, title, head_message_id, archived_at, updated_at\",\n",
                "        \"UPDATE OF title, head_message_id, archived_at, updated_at\",\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/phase7_schema.py",
                "        \"WHEN NEW.id IS NOT OLD.id OR NEW.title IS NOT OLD.title \"\n",
                "        \"WHEN NEW.title IS NOT OLD.title \"\n",
            ),
        ),
        (
            "tests/test_phase7_migration_authority_faults.py::"
            "test_chat_identity_and_recency_updates_are_source_guarded_and_stale_old_cursors",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "chat_updated_at_source_trigger_coverage_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/phase7_schema.py",
                "        \"UPDATE OF id, title, head_message_id, archived_at, updated_at\",\n",
                "        \"UPDATE OF id, title, head_message_id, archived_at\",\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/phase7_schema.py",
                "        \"OR NEW.archived_at IS NOT OLD.archived_at \"\n"
                "        \"OR NEW.updated_at IS NOT OLD.updated_at\",\n",
                "        \"OR NEW.archived_at IS NOT OLD.archived_at\",\n",
            ),
        ),
        (
            "tests/test_phase7_migration_authority_faults.py::"
            "test_chat_identity_and_recency_updates_are_source_guarded_and_stale_old_cursors",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "cursor_noncanonical_suffix_accepted",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "        encoded = cursor.encode(\"ascii\")\n",
                "        encoded = cursor.rstrip(\"!\").encode(\"ascii\")\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    if raw != canonical_raw or cursor != canonical_cursor:\n",
                "    if raw != canonical_raw:\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_relevance_recency_tie_break_pagination_and_cursor_snapshot",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "unicode_query_typed_rejection_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    _require_utf8(query, \"text\")\n",
                "    # MUTANT: lone surrogates escape typed validation.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_global_in_chat_unicode_literal_validation_and_visibility_filters",
        ),
        "UnicodeEncodeError",
    ),
    Mutant(
        "live_read_rollback_classifier_bypassed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                self._rollback_search_transaction(connection, operation)\n",
                "                connection.rollback()  # MUTANT: raw unknown outcome escapes.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_live_search_read_snapshots_classify_unknown_settlement_and_close[search-rollback-rollback is uncertain]",
        ),
        "OSError: injected unknown search rollback",
    ),
    Mutant(
        "live_read_commit_classifier_bypassed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            else:\n"
                "                self._commit_search_transaction(connection, operation)\n",
                "            else:\n"
                "                if connection.info.get(_PHASE7_PENDING_SOURCE_COMMIT) is None:\n"
                "                    connection.commit()  # MUTANT: raw live-read outcome escapes.\n"
                "                else:\n"
                "                    self._commit_search_transaction(connection, operation)\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_live_search_read_snapshots_classify_unknown_settlement_and_close[status-commit-outcome is uncertain]",
        ),
        "OSError: injected unknown search commit",
    ),
    Mutant(
        "derived_unknown_commit_failure_escapes_business_success",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        except Exception:\n"
                "            # The authoritative transaction is already known committed.  A\n",
                "        except (SearchIndexInvalid, AuthorityError):\n"
                "            # MUTANT: an unknown derived settlement escapes business success.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_known_authoritative_commit_then_unknown_derived_commit_returns_business_success",
        ),
        "StateError: search transaction outcome is uncertain",
    ),
    Mutant(
        "derived_authority_error_escapes_business_success",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        except Exception:\n"
                "            # The authoritative transaction is already known committed.  A\n",
                "        except (SearchIndexInvalid, StateError):\n"
                "            # MUTANT: revoked derived admission escapes business success.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_known_commit_then_revoked_derived_admission_does_not_false_report_failure",
        ),
        "AuthorityError: injected revoked derived grant",
    ),
    Mutant(
        "authoritative_source_commit_classifier_bypassed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                self._commit_search_transaction(connection, operation)\n",
                "                connection.commit()  # MUTANT: authoritative outcome is raw.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_unknown_authoritative_commit_is_classified_and_restart_exposes_durable_stale_state[create]",
            "tests/test_phase7_v4_repairs.py::"
            "test_unknown_authoritative_commit_is_classified_and_restart_exposes_durable_stale_state[archive]",
            "tests/test_phase7_v4_repairs.py::"
            "test_unknown_authoritative_commit_is_classified_and_restart_exposes_durable_stale_state[generation]",
            "tests/test_phase7_v4_repairs.py::"
            "test_unknown_authoritative_commit_is_classified_and_restart_exposes_durable_stale_state[regeneration]",
        ),
        "OSError: injected unknown",
    ),
    Mutant(
        "missing_derived_state_leaks_untyped_status_failure",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        if not rows:\n"
                "            if not self._search_available:\n",
                "        if False and not rows:\n"
                "            if not self._search_available:\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_missing_derived_state_is_typed_invalid_and_explicit_rebuild_recreates_it",
        ),
        "IndexError: list index out of range",
    ),
    Mutant(
        "missing_derived_state_rebuild_refused",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if not row_is_canonical:\n"
                "                connection.exec_driver_sql(\"DELETE FROM search_index_state\")\n"
                "                connection.exec_driver_sql(\n",
                "            if not row_is_canonical:\n"
                "                if not rows:\n"
                "                    raise SearchIndexInvalid(\n"
                "                        \"MUTANT: missing derived state cannot be rebuilt\"\n"
                "                    )\n"
                "                connection.exec_driver_sql(\"DELETE FROM search_index_state\")\n"
                "                connection.exec_driver_sql(\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_missing_derived_state_is_typed_invalid_and_explicit_rebuild_recreates_it",
        ),
        "SearchIndexInvalid: MUTANT: missing derived state cannot be rebuilt",
    ),
    Mutant(
        "missing_state_rebuild_reuses_generation_allowing_cursor_aba",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if not row_is_canonical:\n"
                "                connection.exec_driver_sql(\"DELETE FROM search_index_state\")\n"
                "                connection.exec_driver_sql(\n",
                "            if not row_is_canonical:\n"
                "                if not rows:\n"
                "                    generation = 1  # MUTANT: generation ABA after state loss.\n"
                "                connection.exec_driver_sql(\"DELETE FROM search_index_state\")\n"
                "                connection.exec_driver_sql(\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_missing_derived_state_is_typed_invalid_and_explicit_rebuild_recreates_it",
        ),
        "assert 1 == 2",
    ),
    Mutant(
        "startup_refuses_missing_derived_index_state",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                        allow_missing_search_index_state=True,\n",
                "                        allow_missing_search_index_state=False,  # MUTANT\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_missing_derived_state_survives_restart_and_rebuild_rejects_old_cursor",
        ),
        "RuntimeError: current Phase 7 index singleton is malformed",
    ),
    Mutant(
        "cross_restart_cursor_epoch_binding_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        fingerprint = bind_cursor_fingerprint(\n"
                "            fingerprint, self._search_cursor_epoch\n"
                "        )\n",
                "        # MUTANT: cursor fingerprint is reusable across store restarts.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_missing_derived_state_survives_restart_and_rebuild_rejects_old_cursor",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "structured_filter_container_exact_tuple_check_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    if type(values) is not tuple:\n"
                "        raise SearchInvalidQuery(f\"search {name} filter is malformed\")\n",
                "    if False and type(values) is not tuple:\n"
                "        raise SearchInvalidQuery(f\"search {name} filter is malformed\")\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_malformed_structured_filter_containers_and_values_are_typed[backend_ids-fake]",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "structured_filter_string_element_type_check_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    if expected_type is str:\n"
                "        valid_types = all(type(value) is str for value in values)\n"
                "    else:\n"
                "        valid_types = all(isinstance(value, expected_type) for value in values)\n",
                "    valid_types = True  # MUTANT: filter values are silently stringified.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_malformed_structured_filter_containers_and_values_are_typed[models-value2]",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "structured_chat_id_exact_string_check_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/search.py",
                "    chat_id = filters.chat_id\n"
                "    if chat_id is not None:\n",
                "    chat_id = (\n"
                "        None if filters.chat_id is None else str(filters.chat_id)\n"
                "    )  # MUTANT: malformed chat identities are coerced.\n"
                "    if chat_id is not None:\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_malformed_structured_filter_containers_and_values_are_typed[chat_id-7]",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "headless_archive_advances_conversation_revision",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                        updated_at = utc_iso(datetime.now(UTC))\n"
                "                        update_result = connection.execute(\n",
                "                        updated_at = utc_iso(datetime.now(UTC))\n"
                "                        arm_transition(\n"
                "                            connection, current.head_message_id, current.id, \"advance\"\n"
                "                        )  # MUTANT: archive borrows conversation authority.\n"
                "                        update_result = connection.execute(\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                            .values(\n"
                "                                archived_at=archived_value,\n"
                "                                updated_at=updated_at,\n"
                "                            )\n",
                "                            .values(\n"
                "                                archived_at=archived_value,\n"
                "                                updated_at=updated_at,\n"
                "                                revision=current.revision + 1,\n"
                "                            )\n",
            ),
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                    result_chat = replace(\n"
                "                        current,\n"
                "                        archived_at=archived_at,\n"
                "                        updated_at=parse_utc(updated_at),\n"
                "                    )\n",
                "                    result_chat = replace(\n"
                "                        current,\n"
                "                        archived_at=archived_at,\n"
                "                        updated_at=parse_utc(updated_at),\n"
                "                        revision=current.revision + 1,\n"
                "                    )\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_headless_archive_unarchive_preserves_conversation_revision_across_restarts",
        ),
        "assert 1 == 0",
    ),
    Mutant(
        "search_version_mismatch_served_as_valid",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        elif version_mismatch:\n"
                "            condition = SearchIndexCondition.INVALID\n"
                "            detail = detail or \"derived search schema or tokenizer version is invalid\"\n",
                "        elif False and version_mismatch:  # MUTANT: foreign version is served.\n"
                "            condition = SearchIndexCondition.INVALID\n"
                "            detail = detail or \"derived search schema or tokenizer version is invalid\"\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_search_refuses_mismatched_schema_or_tokenizer_until_rebuild",
        ),
        "assert <SearchIndexCondition.VALID: 'VALID'> is <SearchIndexCondition.INVALID: 'INVALID'>",
    ),
    Mutant(
        "rebuild_preserves_foreign_search_version",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                    \"UPDATE search_index_state SET condition='REBUILDING', \"\n"
                "                    \"checkpoint_revision=0, generation=?, \"\n"
                "                    \"schema_version=?, tokenizer_version=?, detail=NULL, updated_at=? \"\n"
                "                    \"WHERE singleton_id=1\",\n"
                "                    (\n"
                "                        generation,\n"
                "                        SEARCH_SCHEMA_VERSION,\n"
                "                        SEARCH_TOKENIZER_VERSION,\n"
                "                        utc_iso(datetime.now(UTC)),\n"
                "                    ),\n",
                "                    \"UPDATE search_index_state SET condition='REBUILDING', \"\n"
                "                    \"checkpoint_revision=0, generation=?, detail=NULL, \"\n"
                "                    \"updated_at=? WHERE singleton_id=1\",\n"
                "                    (generation, utc_iso(datetime.now(UTC))),\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_search_refuses_mismatched_schema_or_tokenizer_until_rebuild",
        ),
        "RuntimeError: current Phase 7 index singleton values are malformed",
    ),
    Mutant(
        "navigation_ignores_archive_visibility_loss",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                if not result.include_archived and chat.archived_at is not None:\n"
                "                    raise SearchResultGone(\"search result is no longer visible\")\n",
                "                if False and not result.include_archived and chat.archived_at is not None:\n"
                "                    raise SearchResultGone(\"search result is no longer visible\")\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_result_that_leaves_archive_or_active_visibility_is_gone",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "navigation_ignores_active_branch_visibility_loss",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                if (\n"
                "                    result.active_branch_only\n"
                "                    and branch_state is SearchBranchState.HISTORICAL\n"
                "                ):\n"
                "                    raise SearchResultGone(\"search result is no longer visible\")\n",
                "                if (\n"
                "                    False and result.active_branch_only\n"
                "                    and branch_state is SearchBranchState.HISTORICAL\n"
                "                ):\n"
                "                    raise SearchResultGone(\"search result is no longer visible\")\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_result_that_leaves_archive_or_active_visibility_is_gone",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "restart_refuses_repairable_foreign_search_version",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                        allow_repairable_search_index_version=True,\n",
                "                        allow_repairable_search_index_version=False,  # MUTANT\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_search_refuses_mismatched_schema_or_tokenizer_until_rebuild",
        ),
        "RuntimeError: current Phase 7 index singleton values are malformed",
    ),
    Mutant(
        "startup_accepts_malformed_search_version_types",
        (
            Mutation(
                "bots5/infrastructure/persistence/phase7_schema.py",
                "            or type(schema_version) is not int\n"
                "            or type(tokenizer) is not str\n",
                "            # MUTANT: malformed stored version types are treated as repairable.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_restart_rejects_malformed_search_version_storage_types",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "application_rebuild_publication_copies_parent_grant",
        (
            Mutation(
                "bots5/core/application.py",
                "            ),\n"
                "            context=Context(),\n"
                "        )\n",
                "            )\n"
                "        )  # MUTANT: child copies the caller's executor-owned grant.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_application_rebuild_real_authority_settles_event_without_context_escape[ordinary]",
        ),
        "StateError: state store is not admitting work",
    ),
    Mutant(
        "application_rebuild_cancellation_detaches_worker",
        (
            Mutation(
                "bots5/core/application.py",
                "                    await completed.wait()\n",
                "                    await future  # MUTANT: cancellation reaches the forward effect.\n",
            ),
        ),
        (
            "tests/test_phase7_core_contracts.py::"
            "test_application_rebuild_cancellation_waits_for_owned_effect_and_event",
        ),
        "assert not True",
    ),
    Mutant(
        "returned_fts_projection_validation_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "                for row in rows:\n"
                "                    self._validate_returned_search_projection(connection, row)\n",
                "                # MUTANT: returned FTS payload is trusted without comparison.\n",
            ),
        ),
        (
            "tests/test_phase7_search_navigation.py::"
            "test_forged_returned_fts_payload_is_invalidated_before_diagnostics"
            "[chat-title-forged-chat-before-diagnostics-"
            "authoritative-chat-before-diagnostics-chat]",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "derived_numeric_metadata_unchecked_conversion",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        generation = row[3] if generation_valid else None\n",
                "        generation = int(row[3])  # MUTANT: unchecked derived conversion.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_malformed_live_derived_metadata_is_typed_nonpoisoning_and_rebuildable"
            "[generation-text]",
        ),
        "raw derived metadata conversion escaped",
    ),
    Mutant(
        "authoritative_source_poison_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "        self._authority.poison(detail)\n",
                "        if False:\n"
                "            self._authority.poison(detail)  # MUTANT: poison suppressed.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_malformed_live_authoritative_source_revision_is_fail_closed",
        ),
        "malformed authoritative search state did not poison authority",
    ),
    Mutant(
        "source_revision_regression_detection_removed",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if revision != self._search_source_revision_high_water:\n"
                "                self._authoritative_search_state_corrupt(\n"
                "                    \"authoritative search source revision regressed or advanced outside its transition\"\n"
                "                )\n",
                "            if False and revision != self._search_source_revision_high_water:\n"
                "                self._authoritative_search_state_corrupt(\n"
                "                    \"authoritative search source revision regressed or advanced outside its transition\"\n"
                "                )  # MUTANT: a live regression is accepted.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_live_source_revision_regression_poison_prevents_revision_reuse_laundering",
        ),
        "DID NOT RAISE",
    ),
    Mutant(
        "source_revision_regression_downgraded_without_poison",
        (
            Mutation(
                "bots5/infrastructure/persistence/sqlite.py",
                "            if revision != self._search_source_revision_high_water:\n"
                "                self._authoritative_search_state_corrupt(\n"
                "                    \"authoritative search source revision regressed or advanced outside its transition\"\n"
                "                )\n",
                "            if revision != self._search_source_revision_high_water:\n"
                "                raise StateError(\n"
                "                    \"authoritative search source revision regressed or advanced outside its transition; \"\n"
                "                    \"restart recovery is required\"\n"
                "                )  # MUTANT: typed refusal without authority poison.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_live_source_revision_regression_poison_prevents_revision_reuse_laundering",
        ),
        "assert <AuthorityState.READY: 'READY'> is <AuthorityState.POISONED: 'POISONED'>",
    ),
    Mutant(
        "source_revision_non_unit_transition_allowed",
        (
            Mutation(
                "bots5/infrastructure/persistence/transition_guard.py",
                "            if old_revision != expected or new_revision != expected + 1:\n"
                "                return 0\n",
                "            if old_revision != expected or new_revision < expected + 1:\n"
                "                return 0  # MUTANT: forward jumps are accepted.\n",
            ),
        ),
        (
            "tests/test_phase7_v4_repairs.py::"
            "test_armed_source_revision_transition_must_be_exactly_one[1]",
        ),
        "DID NOT RAISE",
    ),
)


def apply_mutant(source_root: Path, mutant: Mutant) -> None:
    grouped: dict[str, list[Mutation]] = {}
    for mutation in mutant.mutations:
        grouped.setdefault(mutation.relative_path, []).append(mutation)
    for relative_path, mutations in grouped.items():
        target = source_root / relative_path
        value = target.read_text(encoding="utf-8")
        for mutation in mutations:
            count = value.count(mutation.old)
            if count != 1:
                raise RuntimeError(
                    f"{mutant.name}: expected one mutation site in "
                    f"{relative_path}, got {count}"
                )
            value = value.replace(mutation.old, mutation.new, 1)
        target.write_text(value, encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_pytest(
    tests: tuple[str, ...],
    *,
    environment: dict[str, str],
    timeout: int = 240,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
            *tests,
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def compact_result(completed: subprocess.CompletedProcess[str]) -> str:
    lines = (completed.stdout + completed.stderr).splitlines()
    summaries = [
        line.strip()
        for line in lines
        if " failed" in line or " passed" in line or " error" in line.lower()
    ]
    return summaries[-1] if summaries else f"exit {completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mutants",
        nargs="*",
        help="optional exact mutant names; omission runs the complete matrix",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="directory for baseline, per-mutant transcripts, and hashes",
    )
    args = parser.parse_args()
    requested = set(args.mutants)
    known = {mutant.name for mutant in MUTANTS}
    unknown = sorted(requested - known)
    if unknown:
        parser.error("unknown mutant(s): " + ", ".join(unknown))
    selected = tuple(
        mutant for mutant in MUTANTS if not requested or mutant.name in requested
    )
    args.evidence_dir.mkdir(parents=True, exist_ok=True)

    selected_tests = tuple(dict.fromkeys(test for mutant in selected for test in mutant.tests))
    baseline_environment = dict(os.environ)
    baseline_environment["PYTHONPATH"] = os.fspath(REPO / "src")
    baseline_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    baseline = run_pytest(selected_tests, environment=baseline_environment)
    baseline_transcript = baseline.stdout + baseline.stderr
    baseline_path = args.evidence_dir / "baseline.log"
    baseline_path.write_text(baseline_transcript, encoding="utf-8")
    if baseline.returncode != 0:
        print(
            "BASELINE FAILED; mutation evidence is invalid; " + compact_result(baseline),
            flush=True,
        )
        return 2
    print(f"BASELINE PASSED; {compact_result(baseline)}", flush=True)

    results: list[dict[str, object]] = []
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bots5-phase7-search-mutants-") as value:
        parent = Path(value)
        for mutant in selected:
            variant = parent / mutant.name
            source_root = variant / "src"
            shutil.copytree(REPO / "src", source_root)
            native_target = variant / "build" / "native"
            native_target.mkdir(parents=True)
            native_library = native_target / "libbots5_rooted_sqlite_vfs.so"
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.fspath(source_root)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            native_build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "bots5.infrastructure.native.build_rooted_vfs",
                    "--output",
                    os.fspath(native_library),
                ],
                cwd=variant,
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if native_build.returncode != 0 or not native_library.is_file():
                transcript_path = args.evidence_dir / f"{mutant.name}.log"
                transcript_path.write_text(
                    native_build.stdout + native_build.stderr, encoding="utf-8"
                )
                print(f"{mutant.name}: SETUP FAILED; native rebuild failed", flush=True)
                failures.append(mutant.name)
                results.append(
                    {
                        "name": mutant.name,
                        "disposition": "SETUP_FAILED",
                        "transcript_sha256": sha256(transcript_path),
                    }
                )
                continue
            environment["BOTS5_ROOTED_VFS_LIBRARY"] = os.fspath(native_library)
            apply_mutant(source_root, mutant)
            try:
                completed = run_pytest(mutant.tests, environment=environment)
                transcript = completed.stdout + completed.stderr
            except subprocess.TimeoutExpired as exc:
                transcript = (exc.stdout or "") + (exc.stderr or "")
                transcript += "\nMUTANT TIMEOUT\n"
                completed = None
            transcript_path = args.evidence_dir / f"{mutant.name}.log"
            transcript_path.write_text(transcript, encoding="utf-8")
            killed = bool(
                completed is not None
                and completed.returncode == 1
                and "FAILED " in transcript
                and "ERROR " not in transcript
                and "errors during collection" not in transcript
                and mutant.expected_assertion in transcript
            )
            disposition = "KILLED" if killed else "SURVIVED"
            compact = "timeout" if completed is None else compact_result(completed)
            native_digest = sha256(native_library)
            transcript_digest = sha256(transcript_path)
            print(
                f"{mutant.name}: {disposition}; native={native_digest}; "
                f"transcript={transcript_digest}; {compact}",
                flush=True,
            )
            results.append(
                {
                    "name": mutant.name,
                    "disposition": disposition,
                    "tests": mutant.tests,
                    "expected_assertion": mutant.expected_assertion,
                    "returncode": None if completed is None else completed.returncode,
                    "native_sha256": native_digest,
                    "transcript_sha256": transcript_digest,
                    "summary": compact,
                }
            )
            if not killed:
                failures.append(mutant.name)

    summary_path = args.evidence_dir / "matrix-summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "runner_sha256": sha256(Path(__file__)),
                "baseline_sha256": sha256(baseline_path),
                "selected_mutants": len(selected),
                "killed_mutants": len(selected) - len(failures),
                "survivors": failures,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if failures:
        print("SURVIVING MUTANTS: " + ", ".join(failures), flush=True)
        return 1
    print(f"ALL {len(selected)} SELECTED MUTANTS KILLED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
