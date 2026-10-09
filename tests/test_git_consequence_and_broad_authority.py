"""Regression suite for fail-closed Git binding, trusted broad authority and
consequence separation.

Mick's four requirements for this wave:
  1. Fail-closed issuance: missing request identity must refuse.
  2. Genuine broad-grant approval: a trusted approval RECORD, not a caller string.
  3. Enforced consequence classes: grant consequence compared against the parsed
     operation in EVERY binding mode; unknown/ambiguous consequences fail closed.
  4. These tests must demonstrate that unauthorised broad grants and
     cross-consequence substitutions are REJECTED -- including when security
     parameters are omitted or deliberately manipulated.

They are written to fail against the previous behaviour, in which
``_consequence_matches_grant`` ended in an unconditional ``return True`` and
``issue_grant`` defaulted to LEVEL_SCOPE when no digest was supplied.
"""


import pytest
from pathlib import Path

from bots5.infrastructure.git_authority import (

    GitAuthorityBindingScope,
    GitAuthorityLevel,
    GitAuthorityManager,
    GitBroadAuthorityApproval,
    GitConsequenceClass,
    GitOperationRequest,
    GitOperationState,
    disposable_git_remote,
    disposable_git_repository,
)

_approval_repo = Path("/tmp/bots5-approval-repo")  # rebound per test by _bind_repo

SAFE = GitConsequenceClass.NON_DESTRUCTIVE
FORCE = GitConsequenceClass.HISTORY_REWRITING


def _approval(
    approval_id: str,
    scope: str,
    permitted,
    rationale: str = "operator approved broad authority for this campaign run",
    repository_path=None,
):
    # A bare GitConsequenceClass is a str enum, so tuple() on it would yield the
    # characters (or nothing) rather than one element. Normalise first.
    if isinstance(permitted, GitConsequenceClass):
        permitted = (permitted,)
    return GitBroadAuthorityApproval(
        approval_id=approval_id,
        approved_by="mick",
        scope=scope,
        permitted_consequences=tuple(permitted),
        rationale=rationale,
        issued_at=0.0,
        repository_path=repository_path or _approval_repo,
    )


class TestFailClosedIssuance:
    """Requirement 1: missing identity refuses; no fallback to broad authority."""

    def test_missing_identity_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/main",
                approved_by="operator",
            )

    def test_default_binding_is_exact_request_not_level_scope(self):
        manager = GitAuthorityManager()
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path="/tmp/repo",
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "HEAD:main"],
        )
        assert grant.binding_scope is GitAuthorityBindingScope.EXACT_REQUEST
        assert grant.binding_scope is not GitAuthorityBindingScope.LEVEL_SCOPE

    def test_caller_digest_must_match_derived_identity(self):
        """A caller cannot install a digest that disagrees with the command."""
        manager = GitAuthorityManager()
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "HEAD:main"],
                request_digest="0000000000000000",
            )


class TestTrustedBroadAuthority:
    """Requirement 2: a string is not proof; only a registered record is."""

    def test_level_scope_without_approval_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            )

    def test_level_scope_with_unregistered_approval_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="not-a-registered-approval",
            )

    def test_approval_scope_must_cover_requested_scope(self):
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(_approval("ap", "refs/heads/main", [SAFE]))
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/elsewhere",
                approved_by="mick",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
            )

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "\u200b", "\u200c", "\ufeff", "\u202e", "\u200b\u200c"],
    )
    def test_approval_record_rejects_blank_or_invisible_rationale(self, bad):
        with pytest.raises(ValueError):
            GitBroadAuthorityApproval(
                approval_id="ap",
                approved_by="mick",
                scope="refs/heads/main",
                permitted_consequences=(SAFE,),
                rationale=bad,
                issued_at=0.0,
            repository_path=_approval_repo,
            )

    @pytest.mark.parametrize("text", ["general", "placeholder", "asdf", "x", "short"])
    def test_arbitrary_rationale_text_is_accepted_after_heuristics_removed(self, text):
        """Rationale word-scoring is NOT an authorization gate (Mick's decision).

        It could not establish legitimate authority: it accepted word salad while
        rejecting honest low-diversity rationales. Only visibility is enforced.
        """
        approval = _approval("ap", "refs/heads/main", SAFE, rationale=text)
        assert approval.rationale == text

    def test_approval_record_requires_attributable_approver(self):
        with pytest.raises(ValueError):
            GitBroadAuthorityApproval(
                approval_id="ap",
                approved_by="   ",
                scope="refs/heads/main",
                permitted_consequences=(SAFE,),
                rationale="operator approved this scope for the campaign run",
                issued_at=0.0,
            repository_path=_approval_repo,
            )

    def test_permitted_cannot_exceed_approval(self):
        """A grant may not claim a consequence its approval record excludes."""
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(_approval("ap", "refs/heads/main", [SAFE]))
        with pytest.raises(ValueError):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path="/tmp/repo",
                scope="refs/heads/main",
                approved_by="mick",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                permitted_consequences=(FORCE,),
            )

    def test_registered_approval_issues_and_records_provenance(self):
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/main", [SAFE], rationale="ordinary pushes for campaign X",
                     repository_path="/tmp/repo")
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path="/tmp/repo",
            scope="refs/heads/main",
            approved_by="mick",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
        )
        assert grant.broad_authority_approval_id == "ap"
        assert grant.broad_authority_approval == "mick"
        assert grant.justification == "ordinary pushes for campaign X"
        assert grant.binding_scope is GitAuthorityBindingScope.LEVEL_SCOPE


class TestConsequenceClassification:
    """Requirement 3: parsing, including ambiguous forms failing closed."""

    @pytest.mark.parametrize(
        "command",
        [
            ["push", "--force", "origin", "HEAD:main"],
            ["push", "-f", "origin", "HEAD:main"],
            ["push", "--force-with-lease", "origin", "HEAD:main"],
            ["push", "--force-if-includes", "origin", "HEAD:main"],
            ["push", "origin", "+HEAD:main"],
            ["push", "--mirror", "origin"],
            ["push", "--delete", "origin", "main"],
            ["push", "origin", ":main"],
            ["reset", "--hard"],
            ["clean", "-f"],
            ["checkout", "-f"],
            ["branch", "-D", "topic"],
            ["tag", "-d", "v1"],
            ["update-ref", "refs/heads/x", "HEAD"],
        ],
    )
    def test_history_rewriting_forms_classified(self, command):
        assert manager_classify(command) is FORCE

    @pytest.mark.parametrize(
        "command",
        [["status"], ["diff"], ["log"], ["push", "origin", "HEAD:main"], ["rev-parse", "HEAD"]],
    )
    def test_ordinary_forms_are_non_destructive(self, command):
        assert manager_classify(command) is SAFE

    @pytest.mark.parametrize(
        "command", [["rm", "file"], ["mv", "a", "b"], ["switch", "main"], ["totally-made-up"]]
    )
    def test_unknown_consequences_fail_closed(self, command):
        assert manager_classify(command) is GitConsequenceClass.UNKNOWN


def manager_classify(command):
    return GitAuthorityManager()._classify_consequence(command)


class TestCrossConsequenceSubstitution:
    """Requirement 3+4: the actual enforcement, in both binding modes."""

    @pytest.mark.parametrize(
        "forbidden",
        [
            ["push", "--force", "origin", "HEAD:main"],
            ["push", "-f", "origin", "HEAD:main"],
            ["push", "--force-with-lease", "origin", "HEAD:main"],
            ["push", "origin", "+HEAD:main"],
            ["push", "--mirror", "origin"],
            ["push", "--delete", "origin", "main"],
        ],
    )
    async def test_level_scope_safe_grant_cannot_substitute_consequence(self, forbidden):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _add_remote(manager, repo, remote)
            repository_path=repo.path,
            manager.register_broad_authority_approval(_approval("ap", "refs/heads/main", [SAFE], repository_path=repo.path))
            grant = manager.issue_grant(
                repository_path=repo.path,
                authority_level=GitAuthorityLevel.PUSH,
                scope="refs/heads/main",
                approved_by="mick",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["push", "origin", "HEAD:main"],
            )
            assert grant.consequence_class is SAFE
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    command=forbidden,
                    required_grant_id=grant.grant_id,
                repository_path=repo.path,
                )
            )
            assert result.state is GitOperationState.REJECTED, (
                f"a non-destructive LEVEL_SCOPE grant authorised {forbidden}"
            )

    @pytest.mark.parametrize(
        "forbidden",
        [
            ["push", "--force", "origin", "HEAD:main"],
            ["push", "origin", "+HEAD:main"],
            ["push", "--delete", "origin", "main"],
        ],
    )
    async def test_exact_request_grant_cannot_substitute_consequence(self, forbidden):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _add_remote(manager, repo, remote)
            grant = manager.issue_grant(
                repository_path=repo.path,
                authority_level=GitAuthorityLevel.PUSH,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "HEAD:main"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    command=forbidden,
                    required_grant_id=grant.grant_id,
                repository_path=repo.path,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_explicitly_approved_history_rewriting_is_permitted(self):
        """The control: separating consequences must not forbid legitimate force."""
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _add_remote(manager, repo, remote)
            manager.register_broad_authority_approval(
                _approval("ap-force", "refs/heads/main", [SAFE, FORCE], repository_path=repo.path)
            )
            grant = manager.issue_grant(
                repository_path=repo.path,
                authority_level=GitAuthorityLevel.PUSH,
                scope="refs/heads/main",
                approved_by="mick",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap-force",
                command=["push", "origin", "HEAD:main"],
                permitted_consequences=(FORCE,),
            )
            assert grant.consequence_class is FORCE
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    command=["push", "--force", "origin", "HEAD:main"],
                    required_grant_id=grant.grant_id,
                repository_path=repo.path,
                )
            )
            assert result.state is GitOperationState.COMPLETED

    async def test_grant_reuse_for_other_command_still_refused(self):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _add_remote(manager, repo, remote)
            grant = manager.issue_grant(
                repository_path=repo.path,
                authority_level=GitAuthorityLevel.PUSH,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "HEAD:main"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    command=["push", "origin", "HEAD:other"],
                    required_grant_id=grant.grant_id,
                repository_path=repo.path,
                )
            )
            assert result.state is GitOperationState.REJECTED


async def _add_remote(manager, repo, remote):
    await manager.execute_operation(
        GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            command=["remote", "add", "origin", remote.get_url()],
        repository_path=repo.path,
        )
    )
