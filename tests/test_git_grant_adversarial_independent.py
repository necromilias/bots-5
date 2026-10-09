"""Independent adversarial suite for fail-closed Git grant security.

This file attacks three properties of the Git authority manager:

  P1 - Fail-closed issuance: missing operation identity must refuse.
  P2 - Broad authority must be backed by a registered approval record.
  P3 - Consequence separation must be enforced in every binding mode,
       including against smuggled/abbreviated/bundled flags.

The tests use only disposable repositories.  Any test that documents a
currently-failing security violation is marked ``xfail(strict=True)`` so the
suite remains green while the defect is recorded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import sys
import subprocess
import shutil
import os

import pytest

from bots5.infrastructure.git_authority import (

    GitAuthorityBindingScope,
    GitAuthorityLevel,
    GitAuthorityManager,
    GitBroadAuthorityApproval,
    GitConsequenceClass,
    GitOperationRequest,
    GitOperationState,
    DisposableGitRepository,
    disposable_git_repository,
    disposable_git_remote,
)

_approval_repo = Path("/tmp/bots5-approval-repo")

SAFE = GitConsequenceClass.NON_DESTRUCTIVE
FORCE = GitConsequenceClass.HISTORY_REWRITING


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _approval(
    approval_id: str,
    scope: str,
    permitted: tuple[GitConsequenceClass, ...] | GitConsequenceClass,
    rationale: str = "operator approved broad authority for this campaign run",
    approved_by: str = "mick",
    issued_at: float = 0.0,
    repository_path=None,
) -> GitBroadAuthorityApproval:
    if isinstance(permitted, GitConsequenceClass):
        permitted = (permitted,)
    return GitBroadAuthorityApproval(
        approval_id=approval_id,
        approved_by=approved_by,
        scope=scope,
        permitted_consequences=permitted,
        rationale=rationale,
        issued_at=issued_at,
        repository_path=repository_path or _approval_repo,
    )


async def _commit(repo: DisposableGitRepository, filename: str, content: str, message: str) -> None:
    (repo.path / filename).write_text(content)
    await repo._run_git(["add", filename])
    await repo._run_git(["commit", "-m", message])


async def _make_diverged_for_force_push(repo: DisposableGitRepository, remote) -> None:
    """Create a local/remote pair where a plain push would be rejected."""
    await repo._run_git(["remote", "add", "origin", remote.get_url()])
    await _commit(repo, "base.txt", "base", "base")
    await repo._run_git(["push", "-u", "origin", "main"])

    # Push a conflicting commit from a second clone so remote is ahead.
    other = await DisposableGitRepository.create()
    try:
        await other._run_git(["clone", remote.get_url(), "."])
        await other._run_git(["config", "user.name", "Other"])
        await other._run_git(["config", "user.email", "other@example.com"])
        await _commit(other, "remote.txt", "remote", "remote commit")
        await other._run_git(["push", "origin", "main"])
    finally:
        await other.cleanup()

    # Diverge locally.
    await repo._run_git(["reset", "--hard", "HEAD~1"])
    await _commit(repo, "local.txt", "local", "local commit")


async def _remote_head(remote) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "rev-parse", "HEAD",
        cwd=remote.path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0
    return stdout.decode("utf-8", errors="replace").strip()


async def _local_head(repo: DisposableGitRepository) -> str:
    code, out, _ = await repo._run_git(["rev-parse", "HEAD"])
    assert code == 0
    return out.strip()


# -----------------------------------------------------------------------------
# P1 - Fail-closed issuance
# -----------------------------------------------------------------------------
class TestP1FailClosedIssuance:
    """Missing operation identity must refuse; no fallback to broad authority."""

    def test_omit_command_and_digest_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="requires operation identity"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
            )

    def test_empty_digest_with_no_command_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="requires operation identity"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                request_digest="",
            )

    def test_whitespace_digest_with_no_command_accepted_but_useless(self):
        """A whitespace-only digest is non-empty, so it is treated as identity.

        This is a degenerate identity: the grant cannot match any real request.
        It is recorded here rather than treated as a bypass.
        """
        manager = GitAuthorityManager()
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=Path("/tmp/repo"),
            scope="refs/heads/main",
            approved_by="operator",
            request_digest="   ",
        )
        assert grant.binding_scope is GitAuthorityBindingScope.EXACT_REQUEST
        assert grant.request_digest == "   "

    def test_digest_disagreeing_with_command_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="does not match"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "main"],
                request_digest="0000000000000000",
            )

    def test_empty_command_list_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="empty or blank"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                command=[],
            )

    def test_whitespace_command_tokens_refuse(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="empty or blank"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                command=["", "  "],
            )

    async def test_timeout_seconds_change_breaks_digest(self):
        """A grant bound at one timeout must not authorise the same command at another."""
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                scope="refs/heads/*",
                approved_by="operator",
                command=["status", "--porcelain=v2"],
                request_timeout_seconds=30.0,
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo.path,
                    command=["status", "--porcelain=v2"],
                    required_grant_id=grant.grant_id,
                    timeout_seconds=31.0,
                )
            )
            assert result.state is GitOperationState.REJECTED
            assert "not bound" in result.error_message.lower() or "digest" in result.error_message.lower()

    def test_none_binding_scope_defaults_to_exact_request(self):
        manager = GitAuthorityManager()
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=Path("/tmp/repo"),
            scope="refs/heads/*",
            approved_by="operator",
            command=["status"],
            binding_scope=None,
        )
        assert grant.binding_scope is GitAuthorityBindingScope.EXACT_REQUEST


# -----------------------------------------------------------------------------
# P2 - Broad authority must be genuinely approved
# -----------------------------------------------------------------------------
class TestP2BroadAuthorityApproval:
    """LEVEL_SCOPE requires a registered GitBroadAuthorityApproval record."""

    def test_unregistered_approval_id_refuses(self):
        manager = GitAuthorityManager()
        with pytest.raises(ValueError, match="unknown approval_id"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="not-registered",
            )

    def test_reuse_approval_for_wider_scope_refuses(self):
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/main", SAFE, repository_path=Path("/tmp/repo"))
        )
        with pytest.raises(ValueError, match="does not cover"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/*",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
            )

    def test_widen_permitted_consequences_refuses(self):
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/main", SAFE, repository_path=Path("/tmp/repo"))
        )
        with pytest.raises(ValueError, match="does not permit consequence"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                permitted_consequences=(FORCE,),
            )

    # --- rationale handling, AFTER Mick's approved decision -------------------
    # Rationale-quality heuristics are no longer an authorization gate. They were
    # removed because they could not establish legitimate authority: they accepted
    # word salad while rejecting honest low-diversity rationales. What is enforced
    # is a mandatory, VISIBLE rationale; the real controls on broad authority are
    # the trusted registration boundary, the attributable approver, the scope, the
    # permitted consequence classes and the mandatory repository binding.

    def test_arbitrary_rationale_text_is_accepted_as_recorded(self):
        """Low-content prose is NOT an authorization gate any more."""
        for text in ("aaaa aaaa aaaa aaaa", "aaaaaaaaaaaaaaaa", "foo foo foo foo foo"):
            repository_path=Path("/tmp/repo"),
            approval = _approval("ap", "refs/heads/main", SAFE, rationale=text)
            assert approval.rationale == text

    def test_blank_and_invisible_rationales_still_refused(self):
        """The rationale must exist and be visible -- that part is mandatory."""
        for text in ("", "   ", "\u200b", "\u200c\u200d", "\ufeff", "\u202e"):
            with pytest.raises(ValueError, match="recorded rationale"):
                repository_path=Path("/tmp/repo"),
                _approval("ap", "refs/heads/main", SAFE, rationale=text)

    def test_rationale_is_recorded_for_audit(self):
        """Provenance survives: the rationale is carried onto the grant verbatim."""
        manager = GitAuthorityManager()
        text = "released by the on-call engineer to unblock the hotfix branch"
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/main", SAFE, rationale=text, repository_path=Path("/tmp/repo"))
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=Path("/tmp/repo"),
            scope="refs/heads/main",
            approved_by="mick",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
            command=["push", "origin", "main"],
        )
        assert grant.justification == text

    def test_re_register_same_approval_id_refuses(self):
        manager = GitAuthorityManager()
        repository_path=Path("/tmp/repo"),
        approval = _approval("ap", "refs/heads/main", SAFE, repository_path=Path("/tmp/repo"))
        manager.register_broad_authority_approval(approval)
        with pytest.raises(ValueError, match="already registered"):
            repository_path=Path("/tmp/repo"),
            manager.register_broad_authority_approval(approval)

    def test_scope_glob_does_not_cross_a_namespace_level(self):
        """A single-level ``*`` must not swallow a deeper ref.

        Git refspec ``*`` does not match ``/``, so an approval for
        ``refs/heads/*`` covers direct branch names but NOT a nested ref such as
        ``refs/heads/feature/x``. This is the property that matters: without it a
        namespace-wide approval would silently extend to every sub-namespace.
        """
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/*", SAFE, repository_path=Path("/tmp/repo"))
        )
        with pytest.raises(ValueError, match="does not cover"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=Path("/tmp/repo"),
                scope="refs/heads/feature/x",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
            )

    def test_scope_glob_covers_exact_prefix_match(self):
        """Glob ``refs/heads/ma*`` covers ``refs/heads/ma`` (zero trailing chars).

        This is the documented namespace-aware behaviour.
        """
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/ma*", SAFE, repository_path=Path("/tmp/repo"))
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=Path("/tmp/repo"),
            scope="refs/heads/ma",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
        )
        assert grant is not None


# -----------------------------------------------------------------------------
# P3 - Consequence separation
# -----------------------------------------------------------------------------
class TestP3ConsequenceClassification:
    """The classifier must recognise destructive forms."""

    @pytest.mark.parametrize(
        "command",
        [
            ["push", "--force", "origin", "main"],
            ["push", "-f", "origin", "main"],
            ["push", "--force-with-lease", "origin", "main"],
            ["push", "--force-if-includes", "origin", "main"],
            ["push", "origin", "+HEAD:main"],
            ["push", "--mirror", "origin"],
            ["push", "--delete", "origin", "main"],
            ["push", "origin", ":main"],
            ["reset", "--hard", "HEAD~1"],
            ["reset", "--merge", "HEAD~1"],
            ["reset", "--keep", "HEAD~1"],
            ["clean", "-f"],
            ["clean", "-fd"],
            ["checkout", "-f"],
            ["branch", "-D", "topic"],
            ["tag", "-d", "v1"],
            ["update-ref", "refs/heads/x", "HEAD"],
            ["reflog", "expire", "--expire=now", "--all"],
            ["filter-branch", "--force", "--", "--all"],
            ["gc", "--prune=now"],
        ],
    )
    def test_history_rewriting_forms(self, command):
        assert GitAuthorityManager()._classify_consequence(command) is FORCE

    @pytest.mark.parametrize(
        "command",
        [
            ["status"],
            ["diff"],
            ["log"],
            ["push", "origin", "HEAD:main"],
        ],
    )
    def test_non_destructive_forms(self, command):
        assert GitAuthorityManager()._classify_consequence(command) is SAFE


class TestP3ExactRequestConsequenceSeparation:
    """Consequence separation in EXACT_REQUEST binding mode."""

    async def test_plain_push_grant_refuses_force_push(self):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await repo._run_git(["remote", "add", "origin", remote.get_url()])
            await _commit(repo, "x.txt", "x", "x")
            await repo._run_git(["push", "-u", "origin", "main"])

            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "main"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "--force", "origin", "main"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_reset_hard_grant_refuses_reset_merge(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            await _commit(repo, "x.txt", "x", "x")
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.REF_MUTATE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["reset", "--hard", "HEAD~1"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.REF_MUTATE,
                    repository_path=repo.path,
                    command=["reset", "--merge", "HEAD~1"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_clean_fd_grant_refuses_plain_clean(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            (repo.path / "a.log").write_text("log")
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.DELETE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["clean", "-fd"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.DELETE,
                    repository_path=repo.path,
                    command=["clean"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED


class TestP3LevelScopeConsequenceSeparation:
    """Consequence separation in LEVEL_SCOPE binding mode."""

    async def test_safe_level_scope_refuses_force_push(self):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await repo._run_git(["remote", "add", "origin", remote.get_url()])
            await _commit(repo, "x.txt", "x", "x")
            await repo._run_git(["push", "-u", "origin", "main"])

            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/main", SAFE, repository_path=repo.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["push", "origin", "main"],
            )
            assert grant.consequence_class is SAFE
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "--force", "origin", "main"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_safe_level_scope_refuses_reset_hard(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            await _commit(repo, "x.txt", "x", "x")
            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/main", SAFE, repository_path=repo.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.REF_MUTATE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["reset", "--soft", "HEAD~1"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.REF_MUTATE,
                    repository_path=repo.path,
                    command=["reset", "--hard", "HEAD~1"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED


# -----------------------------------------------------------------------------
# Smuggling / bypass attempts
# -----------------------------------------------------------------------------
class TestSmuggledFlagClassification:
    """Destructive spellings must be classified as HISTORY_REWRITING."""

    @pytest.mark.parametrize(
        "command",
        [
            ["push", "-fq", "origin", "main"],
            ["push", "--del", "origin", "main"],
            ["push", "--mirr", "origin"],
            ["push", "--force-with-lease=main", "origin", "main"],
            ["checkout", "--force", "tracked.txt"],
            ["clean", "-fx"],
            ["reset", "--h", "HEAD~1"],
        ],
    )
    def test_smuggled_forms_are_history_rewriting(self, command):
        assert GitAuthorityManager()._classify_consequence(command) is FORCE


class TestSmuggledFlagReuseExactRequest:
    """An EXACT_REQUEST safe grant must not be reused for a destructive spelling."""

    @pytest.mark.parametrize(
        "smuggled",
        [
            ["push", "-fq", "origin", "main"],
            ["push", "origin", "--del", "main"],
            ["push", "--mirr", "origin"],
            ["push", "--force-with-lease=main", "origin", "main"],
        ],
    )
    async def test_safe_push_grant_refuses_smuggled_push(self, smuggled):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await repo._run_git(["remote", "add", "origin", remote.get_url()])
            await _commit(repo, "x.txt", "x", "x")
            await repo._run_git(["push", "-u", "origin", "main"])

            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "main"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=smuggled,
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_safe_stage_grant_refuses_checkout_force(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            await _commit(repo, "tracked.txt", "base", "base")
            (repo.path / "tracked.txt").write_text("UNCOMMITTED")
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["checkout", "tracked.txt"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.STAGE,
                    repository_path=repo.path,
                    command=["checkout", "--force", "tracked.txt"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED
            assert (repo.path / "tracked.txt").read_text() == "UNCOMMITTED"

    async def test_safe_delete_grant_refuses_clean_fx(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            (repo.path / ".gitignore").write_text("*.log\n")
            (repo.path / "temp.log").write_text("temp")
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.DELETE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["clean", "-f"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.DELETE,
                    repository_path=repo.path,
                    command=["clean", "-fx"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED
            assert (repo.path / "temp.log").exists()

    async def test_safe_ref_mutate_grant_refuses_reset_h(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            await _commit(repo, "x.txt", "x", "x")
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.REF_MUTATE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["reset", "--soft", "HEAD~1"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.REF_MUTATE,
                    repository_path=repo.path,
                    command=["reset", "--h", "HEAD~1"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED


class TestSmuggledFlagReuseLevelScope:
    """A LEVEL_SCOPE safe grant must not authorise a destructive spelling."""

    @pytest.mark.parametrize(
        "smuggled",
        [
            ["push", "-fq", "origin", "main"],
            ["push", "origin", "--del", "main"],
            ["push", "--mirr", "origin"],
            ["push", "--force-with-lease=main", "origin", "main"],
        ],
    )
    async def test_safe_level_scope_refuses_smuggled_push(self, smuggled):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _make_diverged_for_force_push(repo, remote)
            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/main", SAFE, repository_path=repo.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["push", "origin", "main"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=smuggled,
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED


class TestPathPrefixEscapes:
    """Global git options that change the target repository bypass path checks."""

    async def test_minus_c_path_is_rejected_as_unrecognised(self):
        """``-C <path>`` is parsed as ``subcommand=path`` and rejected.

        The property holds, but only because the parser fails closed on an
        unrecognised subcommand rather than because the path is validated.
        """
        async with disposable_git_repository() as repo_a, disposable_git_repository() as repo_b:
            manager = GitAuthorityManager()
            (repo_a.path / "a.txt").write_text("A")
            (repo_b.path / "b.txt").write_text("B")
            await repo_a._run_git(["add", "a.txt"])
            await repo_a._run_git(["commit", "-m", "a"])
            await repo_b._run_git(["add", "b.txt"])
            await repo_b._run_git(["commit", "-m", "b"])

            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/*", SAFE, repository_path=repo_a.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo_a.path,
                scope="refs/heads/*",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["status"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo_a.path,
                    command=["-C", str(repo_b.path), "status"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_git_dir_space_form_is_rejected_as_unrecognised(self):
        """``--git-dir <path>`` (two tokens) is parsed as ``subcommand=path``."""
        async with disposable_git_repository() as repo_a, disposable_git_repository() as repo_b:
            manager = GitAuthorityManager()
            (repo_a.path / "a.txt").write_text("A")
            (repo_b.path / "b.txt").write_text("B")
            await repo_a._run_git(["add", "a.txt"])
            await repo_a._run_git(["commit", "-m", "a"])
            await repo_b._run_git(["add", "b.txt"])
            await repo_b._run_git(["commit", "-m", "b"])

            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/*", SAFE, repository_path=repo_a.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo_a.path,
                scope="refs/heads/*",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["status"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo_a.path,
                    command=["--git-dir", str(repo_b.path / ".git"), "status"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED

    async def test_git_dir_equals_changes_target_repository(self):
        async with disposable_git_repository() as repo_a, disposable_git_repository() as repo_b:
            manager = GitAuthorityManager()
            (repo_a.path / "a.txt").write_text("A")
            (repo_b.path / "b.txt").write_text("B")
            await repo_a._run_git(["add", "a.txt"])
            await repo_a._run_git(["commit", "-m", "a"])
            await repo_b._run_git(["add", "b.txt"])
            await repo_b._run_git(["commit", "-m", "b"])

            manager.register_broad_authority_approval(
                _approval("ap", "refs/heads/*", SAFE, repository_path=repo_a.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo_a.path,
                scope="refs/heads/*",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap",
                command=["status"],
            )
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo_a.path,
                    command=[
                        "--git-dir=" + str(repo_b.path / ".git"),
                        "--work-tree=" + str(repo_b.path),
                        "status",
                    ],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.REJECTED


# -----------------------------------------------------------------------------
# Controls: legitimate operations must still work
# -----------------------------------------------------------------------------
class TestControlsStillWork:
    """Consequence separation must not break authorised legitimate use."""

    async def test_inspect_grant_allows_status_diff_log(self):
        async with disposable_git_repository() as repo:
            manager = GitAuthorityManager()
            for cmd in (["status"], ["diff"], ["log", "--oneline", "-1"]):
                grant = manager.issue_grant(
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo.path,
                    scope="refs/heads/*",
                    approved_by="operator",
                    command=cmd,
                )
                result = await manager.execute_operation(
                    GitOperationRequest(
                        operation_id=manager._ids.new(),
                        authority_level=GitAuthorityLevel.INSPECT,
                        repository_path=repo.path,
                        command=cmd,
                        required_grant_id=grant.grant_id,
                    )
                )
                assert result.state is GitOperationState.COMPLETED, (cmd, result.stderr)

    async def test_history_rewriting_grant_allows_force_push_exact_request(self):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _make_diverged_for_force_push(repo, remote)
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "--force", "origin", "main"],
                permitted_consequences=(FORCE,),
            )
            assert grant.consequence_class is FORCE
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "--force", "origin", "main"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.COMPLETED
            assert await _remote_head(remote) == await _local_head(repo)

    async def test_history_rewriting_grant_allows_force_push_level_scope(self):
        async with disposable_git_repository() as repo, disposable_git_remote() as remote:
            manager = GitAuthorityManager()
            await _make_diverged_for_force_push(repo, remote)
            manager.register_broad_authority_approval(
                _approval("ap-force", "refs/heads/main", (SAFE, FORCE), repository_path=repo.path)
            )
            grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="ap-force",
                command=["push", "origin", "main"],
                permitted_consequences=(FORCE,),
            )
            assert grant.consequence_class is FORCE
            result = await manager.execute_operation(
                GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "--force", "origin", "main"],
                    required_grant_id=grant.grant_id,
                )
            )
            assert result.state is GitOperationState.COMPLETED
            assert await _remote_head(remote) == await _local_head(repo)


class TestSymlinkSwapCannotRedirectAGrant:
    """Oracle-G blocker: a symlink swap after issuance must not retarget a grant.

    A LEVEL_SCOPE grant bound to a symlink path used to be re-resolved at
    validation time, so replacing the symlink with one pointing at a different
    repository let the operation run against that repository: cross-repository
    authority leakage. The canonical target is now frozen at issuance and
    compared, so the swap is refused.
    """

    async def test_symlink_swap_is_refused(self, tmp_path):
        repo_a = tmp_path / "repoA"
        repo_b = tmp_path / "repoB"
        for repo in (repo_a, repo_b):
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo_b / "b_only_marker.txt").write_text("B")

        link = tmp_path / "link"
        os.symlink(repo_a, link, target_is_directory=True)

        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/*", SAFE, repository_path=link)
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=link,
            scope="refs/heads/*",
            approved_by="mick",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
        )
        # The canonical target is frozen at issuance...
        assert grant.repository_canonical == str(repo_a.resolve())

        # ...so retargeting the symlink afterwards must not move the grant.
        os.unlink(link)
        os.symlink(repo_b, link, target_is_directory=True)

        result = await manager.execute_operation(
            GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=link,
                command=["status", "--porcelain=v2"],
                required_grant_id=grant.grant_id,
            )
        )
        assert result.state is GitOperationState.REJECTED, (
            "a symlink swap redirected a LEVEL_SCOPE grant to another repository"
        )
        assert "b_only_marker" not in (result.stdout or ""), (
            "the operation ran against the swapped-in repository"
        )


class TestRepositoryIdentityBinding:
    """Oracle-H blocker: path equality is not repository equality.

    Freezing the canonical PATH still authorised a replacement repository placed at
    the same pathname, because the string comparison stayed satisfied. Grants now
    also freeze the filesystem identity (device:inode of the git directory) and
    require the same object to still be present.
    """

    async def test_repository_replaced_at_same_path_is_refused(self, tmp_path):
        repo_a = tmp_path / "repoA"
        repo_b = tmp_path / "repoB"
        for repo in (repo_a, repo_b):
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo_b / "b_only_marker.txt").write_text("B")

        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/*", SAFE, repository_path=repo_a)
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_a,
            scope="refs/heads/*",
            approved_by="mick",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
        )
        assert grant.repository_identity, "issuance must freeze the repository identity"

        # Replace the repository at the SAME pathname.
        shutil.move(str(repo_a), str(tmp_path / "repoA_moved"))
        shutil.move(str(repo_b), str(repo_a))

        result = await manager.execute_operation(
            GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo_a,
                command=["status", "--porcelain=v2"],
                required_grant_id=grant.grant_id,
            )
        )
        assert result.state is GitOperationState.REJECTED, (
            "a repository replaced at the same pathname was authorised"
        )
        assert "b_only_marker" not in (result.stdout or "")

    async def test_cleared_canonical_does_not_fall_back_to_re_resolution(self, tmp_path):
        """Clearing the frozen field must refuse, not re-resolve the path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        manager = GitAuthorityManager()
        manager.register_broad_authority_approval(
            _approval("ap", "refs/heads/*", SAFE, repository_path=repo)
        )
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo,
            scope="refs/heads/*",
            approved_by="mick",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="ap",
        )
        object.__setattr__(grant, "repository_canonical", "")
        assert manager.validate_grant(grant.grant_id, GitAuthorityLevel.INSPECT, repo) is False
