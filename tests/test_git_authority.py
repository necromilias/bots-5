"""Tests for Git authority boundaries and disposable-repository consequence tests."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from bots5.infrastructure.git_authority import (
    GitAuthorityManager,
    GitAuthorityLevel,
    GitOperationRequest,
    GitOperationState,
    GitAuthorityGrant,
    GitReceipt,
    GitAuthorityBindingScope,
    GitBroadAuthorityApproval,
    GitConsequenceClass,
    DisposableGitRepository,
    DisposableGitRemote,
    GitInspectAuthority,
    GitConsequentialAuthority,
    GitWorkspaceOwnership,
    disposable_git_repository,
    disposable_git_remote,
    git_workspace_isolation,
)
from bots5.infrastructure.process_execution import ProcessResourceLimits, ProcessExecutionRequest, BoundedProcessExecutor



class TestDisposableGitRepository:
    """Tests for disposable Git repository."""

    async def test_create_repository(self):
        """Test creating a disposable repository."""
        async with disposable_git_repository() as repo:
            assert repo.path.exists()
            assert repo.path.is_dir()
            assert (repo.path / ".git").exists()

            # Check initial commit
            result = await repo._run_git(["log", "--oneline", "-1"])
            assert result[0] == 0
            assert "Initial commit" in result[1]

    async def test_create_bare_repository(self):
        """Test creating a bare repository."""
        async with disposable_git_repository(bare=True) as repo:
            assert repo.path.exists()
            assert repo.is_bare()
            # Bare repo has no working tree
            assert not (repo.path / ".git").exists()  # Bare repo IS the .git dir

    async def test_repository_cleanup(self):
        """Test repository is cleaned up after context exit."""
        repo_path = None
        async with disposable_git_repository() as repo:
            repo_path = repo.path
            assert repo_path.exists()

        # After context exit, should be cleaned up
        assert not repo_path.exists()

    async def test_git_operations_in_repo(self):
        """Test running Git commands in disposable repo."""
        async with disposable_git_repository() as repo:
            # Create a file and commit
            test_file = repo.path / "test.txt"
            test_file.write_text("test content")

            result = await repo._run_git(["add", "test.txt"])
            assert result[0] == 0

            result = await repo._run_git(["commit", "-m", "Add test file"])
            assert result[0] == 0

            # Check log
            result = await repo._run_git(["log", "--oneline", "-1"])
            assert result[0] == 0
            assert "Add test file" in result[1]


class TestDisposableGitRemote:
    """Tests for disposable Git remote."""

    async def test_create_remote(self):
        """Test creating a disposable remote."""
        async with disposable_git_remote() as remote:
            assert remote.path.exists()
            assert remote.path.is_dir()
            # Bare repo
            assert (remote.path / "HEAD").exists()
            assert (remote.path / "objects").exists()
            assert (remote.path / "refs").exists()

    async def test_remote_url(self):
        """Test remote URL generation."""
        async with disposable_git_remote() as remote:
            url = remote.get_url()
            assert url.startswith("file://")
            assert str(remote.path) in url

    async def test_remote_cleanup(self):
        """Test remote is cleaned up after context exit."""
        remote_path = None
        async with disposable_git_remote() as remote:
            remote_path = remote.path
            assert remote_path.exists()

        assert not remote_path.exists()


class TestGitAuthorityManager:
    """Tests for Git authority manager."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    request_digest="unbound-grant-for-validation-",
    def test_issue_grant(self, manager, repo):
        """Test issuing an authority grant."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            ttl_seconds=3600.0,
            command=["status", "--porcelain=v2"],
        )

        assert grant.grant_id is not None
        assert grant.authority_level == GitAuthorityLevel.INSPECT
        assert grant.repository_path == repo.path
        assert grant.scope == "refs/heads/*"
        assert grant.approved_by == "operator"
        assert grant.expires_at > 0

    def test_validate_grant(self, manager, repo):
        """Test grant validation."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # Valid grant
        assert manager.validate_grant(grant.grant_id, GitAuthorityLevel.INSPECT, repo.path)

        # Wrong authority level
        assert not manager.validate_grant(grant.grant_id, GitAuthorityLevel.COMMIT, repo.path)

        # Wrong repository
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            other_repo = Path(tmpdir)
            assert not manager.validate_grant(grant.grant_id, GitAuthorityLevel.INSPECT, other_repo)

    def test_revoke_grant(self, manager, repo):
        """Test grant revocation."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        assert manager.validate_grant(grant.grant_id, GitAuthorityLevel.INSPECT, repo.path)
        assert manager.revoke_grant(grant.grant_id)
        assert not manager.validate_grant(grant.grant_id, GitAuthorityLevel.INSPECT, repo.path)

    async def test_execute_inspect_operation(self, manager, repo):
        """Test executing an inspect operation with grant."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        request = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
            required_grant_id=grant.grant_id,
        )

        result = await manager.execute_operation(request)

        assert result.state == GitOperationState.COMPLETED
        assert result.exit_code == 0
        assert result.grant_id == grant.grant_id

    async def test_execute_without_grant_rejected(self, manager, repo):
        """Test operation without valid grant is rejected."""
        request = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "test"],
            required_grant_id="invalid-grant",
        )

        result = await manager.execute_operation(request)

        assert result.state == GitOperationState.REJECTED
        assert result.error_message is not None

    async def test_consequential_operation_without_grant_id_rejected(self, manager, repo):
        """Test consequential operation with required_grant_id=None is rejected (fail-closed)."""
        request = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "test"],
            required_grant_id=None,  # Explicit None for consequential op
        )

        result = await manager.execute_operation(request)

        assert result.state == GitOperationState.REJECTED
        assert "no authority grant" in result.error_message.lower()

    async def test_execute_consequential_operation(self, manager, repo):
        """Test executing a consequential operation with grant."""
        # Stage grant - must match the actual command (after normalization)
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "new.txt"],
        )

        # Commit grant - must match the actual command (after normalization)
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["commit", "-m", "Add new file"],
        )

        # First stage a file
        test_file = repo.path / "new.txt"
        test_file.write_text("new content")

        stage_result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "new.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        assert stage_result.state == GitOperationState.COMPLETED

        # Then commit
        commit_result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "Add new file"],
            required_grant_id=commit_grant.grant_id,
        ))
        assert commit_result.state == GitOperationState.COMPLETED

        # Check receipt exists and has correct authority level
        receipt = manager.get_receipt(commit_result.operation_id)
        assert receipt is not None
        assert receipt.authority_level == GitAuthorityLevel.COMMIT


class TestGitInspectAuthority:
    """Tests for Git inspect authority (read-only)."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    def inspect(self, manager):
        return GitInspectAuthority(manager)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    async def test_status(self, inspect, repo, manager):
        """Test git status."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        result = await inspect.status(repo.path, grant.grant_id)
        assert result.state == GitOperationState.COMPLETED

    async def test_diff(self, inspect, repo, manager):
        """Test git diff."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["diff"],
        )

        # Modify a file
        (repo.path / "README.md").write_text("# Modified\n")

        result = await inspect.diff(repo.path, grant.grant_id)
        assert result.state == GitOperationState.COMPLETED
        assert "Modified" in result.stdout or "modified" in result.stdout

    async def test_log(self, inspect, repo, manager):
        """Test git log."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["log", "--oneline", "-n", "5"],
        )

        result = await inspect.log(repo.path, grant.grant_id, max_count=5)
        assert result.state == GitOperationState.COMPLETED
        assert "Initial commit" in result.stdout

    async def test_branch_list(self, inspect, repo, manager):
        """Test git branch list."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["branch"],
        )

        result = await inspect.branch_list(repo.path, grant.grant_id)
        assert result.state == GitOperationState.COMPLETED
        assert "main" in result.stdout or "master" in result.stdout


class TestGitConsequentialAuthority:
    """Tests for Git consequential authorities."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    def consequential(self, manager):
        return GitConsequentialAuthority(manager)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    async def test_stage_and_commit(self, consequential, repo, manager):
        """Test stage and commit as separate authorities."""
        # Stage grant - must match exact command
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "staged.txt"],
        )

        # Commit grant - must match exact command
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["commit", "-m", "Add staged file"],
        )

        # Create and stage file
        (repo.path / "staged.txt").write_text("staged content")
        stage_result = await consequential.stage(repo.path, stage_grant.grant_id, ["staged.txt"])
        assert stage_result.state == GitOperationState.COMPLETED

        # Commit
        commit_result = await consequential.commit(repo.path, commit_grant.grant_id, "Add staged file")
        assert commit_result.state == GitOperationState.COMPLETED

        # Verify commit exists - use INSPECT grant for log operation
        inspect = GitInspectAuthority(manager)
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["log", "--oneline", "-n", "5"],
        )
        log_result = await inspect.log(repo.path, inspect_grant.grant_id, max_count=5)
        assert "Add staged file" in log_result.stdout

    async def test_push_to_remote(self, consequential, repo, manager):
        """Test push to disposable remote."""
        async with disposable_git_remote() as remote:
            # Add remote - no grant needed for INSPECT-level command
            add_remote_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                command=["remote", "add", "origin", remote.get_url()],
            ))
            assert add_remote_result.state == GitOperationState.COMPLETED

            # Create a commit before pushing
            test_file = repo.path / "test.txt"
            test_file.write_text("test content")

            # Stage grant - exact command match
            stage_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["add", "test.txt"],
            )
            # Commit grant - exact command match
            commit_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["commit", "-m", "Test commit"],
            )

            stage_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["add", "test.txt"],
                required_grant_id=stage_grant.grant_id,
            ))
            assert stage_result.state == GitOperationState.COMPLETED

            commit_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                command=["commit", "-m", "Test commit"],
                required_grant_id=commit_grant.grant_id,
            ))
            assert commit_result.state == GitOperationState.COMPLETED

            # Push grant - exact command match
            push_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "main"],
            )

            push_result = await consequential.push(repo.path, push_grant.grant_id, "origin", "main")
            assert push_result.state == GitOperationState.COMPLETED

    async def test_merge(self, consequential, repo, manager):
        """Test merge operation."""
        # STAGE grant for the branch checkout
        checkout_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["checkout", "-b", "feature"],
        )
        # Create a feature branch
        branch_result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["checkout", "-b", "feature"],
            required_grant_id=checkout_grant.grant_id,
        ))
        assert branch_result.state == GitOperationState.COMPLETED

        # Make a commit on feature - need STAGE and COMMIT grants
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/feature",
            approved_by="operator",
            command=["add", "feature.txt"],
        )
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/feature",
            approved_by="operator",
            command=["commit", "-m", "Feature commit"],
        )

        (repo.path / "feature.txt").write_text("feature")
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "feature.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "Feature commit"],
            required_grant_id=commit_grant.grant_id,
        ))

        # Go back to main - needs STAGE grant (checkout is STAGE level)
        checkout_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["checkout", "main"],
        )
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["checkout", "main"],
            required_grant_id=checkout_grant.grant_id,
        ))

        # Merge grant (separate authority!) - exact command
        merge_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.MERGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["merge", "feature"],
        )

        merge_result = await consequential.merge(repo.path, merge_grant.grant_id, "feature")
        assert merge_result.state == GitOperationState.COMPLETED


class TestGitWorkspaceOwnership:
    """Tests for Git workspace ownership and isolation."""

    @pytest.fixture
    def ownership(self):
        return GitWorkspaceOwnership()

    def test_acquire_lease(self, ownership):
        """Test acquiring exclusive lease."""
        lease = ownership.acquire_lease("workspace-1", "worker-1", ttl_seconds=3600.0)

        assert lease["workspace_id"] == "workspace-1"
        assert lease["owner_id"] == "worker-1"
        assert lease["epoch"] == 1
        assert ownership.is_leased("workspace-1")

    def test_exclusive_lease(self, ownership):
        """Test lease is exclusive."""
        ownership.acquire_lease("workspace-1", "worker-1", ttl_seconds=3600.0)

        with pytest.raises(Exception):  # StateError
            ownership.acquire_lease("workspace-1", "worker-2", ttl_seconds=3600.0)

    def test_release_lease(self, ownership):
        """Test releasing lease."""
        ownership.acquire_lease("workspace-1", "worker-1", ttl_seconds=3600.0)
        assert ownership.is_leased("workspace-1")

        assert ownership.release_lease("workspace-1", "worker-1")
        assert not ownership.is_leased("workspace-1")

    def test_release_lease_wrong_owner(self, ownership):
        """Test releasing lease by wrong owner fails."""
        ownership.acquire_lease("workspace-1", "worker-1", ttl_seconds=3600.0)
        assert not ownership.release_lease("workspace-1", "worker-2")
        assert ownership.is_leased("workspace-1")

    def test_renew_lease(self, ownership):
        """Test renewing lease."""
        ownership.acquire_lease("workspace-1", "worker-1", ttl_seconds=1.0)
        lease = ownership.get_lease("workspace-1")
        original_expiry = lease["expires_at"]

        # Wait a bit and renew
        import time
        time.sleep(0.1)
        renewed = ownership.renew_lease("workspace-1", "worker-1", ttl_seconds=3600.0)
        assert renewed is not None
        assert renewed["expires_at"] > original_expiry
        assert renewed["epoch"] == 2

    async def test_workspace_isolation_disposable_snapshot(self, ownership):
        """Test workspace isolation with disposable snapshot."""
        with tempfile.TemporaryDirectory() as base_dir:
            base_path = Path(base_dir)

            # Create source repo
            async with disposable_git_repository(base_path) as source_repo:
                (source_repo.path / "source.txt").write_text("source content")
                await source_repo._run_git(["add", "source.txt"])
                await source_repo._run_git(["commit", "-m", "Source commit"])

                # Create isolated workspace using linked_worktree since source_repo has refs
                async with git_workspace_isolation(
                    base_path,
                    "workspace-1",
                    ownership,
                    "worker-1",
                    snapshot_kind="linked_worktree",
                    source_repo=source_repo.path,
                ) as workspace:
                    assert workspace.exists()
                    # Should have the source file
                    assert (workspace / "source.txt").exists()
                    assert (workspace / "source.txt").read_text() == "source content"

                    # Should be a worktree (has .git pointing to main repo)
                    gitfile = workspace / ".git"
                    assert gitfile.exists()
                    assert gitfile.is_file()

                # After context, workspace should be cleaned up
                assert not workspace.exists()

    async def test_workspace_isolation_fresh(self, ownership):
        """Test fresh workspace creation without source (uses fresh git init)."""
        with tempfile.TemporaryDirectory() as base_dir:
            base_path = Path(base_dir)

            async with git_workspace_isolation(
                base_path,
                "workspace-fresh",
                ownership,
                "worker-1",
                snapshot_kind="fresh",
                source_repo=None,
            ) as workspace:
                assert workspace.exists()
                assert (workspace / ".git").exists()

                # Should be empty repo
                executor = BoundedProcessExecutor()
                result = await executor.execute(
                    ProcessExecutionRequest(
                        command=["git", "status", "--porcelain"],
                        working_directory=workspace,
                        timeout_seconds=5.0,
                    )
                )
                assert result.stdout.strip() == b""


class TestGitCommandAuthorityDerivation:
    """Tests for command-based authority derivation (security fix for bots5-linux-v0.2-control-plane)."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    async def test_inspect_grant_cannot_stage_via_add_command(self, manager, repo):
        """Test that INSPECT grant + 'add' command is REJECTED (command-derived authority)."""
        # Issue only INSPECT grant
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # Try to execute 'git add' with INSPECT grant
        # This should be REJECTED because 'add' derives STAGE level, not INSPECT
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,  # Declared INSPECT
            repository_path=repo.path,
            command=["add", "marker.txt"],  # But command is 'add' which is STAGE
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED
        assert "authority mismatch" in result.error_message.lower() or "requires" in result.error_message.lower()

    async def test_inspect_grant_cannot_commit(self, manager, repo):
        """Test that INSPECT grant + 'commit' command is REJECTED."""
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["commit", "-m", "test"],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED
        assert "authority mismatch" in result.error_message.lower()

    async def test_inspect_grant_cannot_push(self, manager, repo):
        """Test that INSPECT grant + 'push' command is REJECTED."""
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["push", "origin", "main"],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED
        assert "authority mismatch" in result.error_message.lower()

    async def test_valid_inspect_command_with_inspect_grant_succeeds(self, manager, repo):
        """Test that a genuine INSPECT command with valid INSPECT grant still succeeds."""
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # This should succeed: INSPECT command + INSPECT grant
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.COMPLETED

    async def test_valid_stage_command_with_stage_grant_succeeds(self, manager, repo):
        """Test that a legitimate STAGE grant still executes 'add' command."""
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "new.txt"],
        )

        # Create a file to stage
        (repo.path / "new.txt").write_text("new content")

        # This should succeed: STAGE command + STAGE grant
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "new.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        assert result.state == GitOperationState.COMPLETED

    async def test_valid_commit_command_with_commit_grant_succeeds(self, manager, repo):
        """Test that a legitimate COMMIT grant still executes 'commit' command."""
        # First stage something
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "file.txt"],
        )
        (repo.path / "file.txt").write_text("content")
        stage_result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "file.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        assert stage_result.state == GitOperationState.COMPLETED

        # Then commit with COMMIT grant
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["commit", "-m", "test commit"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "test commit"],
            required_grant_id=commit_grant.grant_id,
        ))
        assert result.state == GitOperationState.COMPLETED

    async def test_valid_push_command_with_push_grant_succeeds(self, manager, repo):
        """Test that a legitimate PUSH grant still executes 'push' command."""
        async with disposable_git_remote() as remote:
            # Add remote
            inspect_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                scope="refs/heads/*",
                approved_by="operator",
                command=["status", "--porcelain=v2"],
            )
            await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                command=["remote", "add", "origin", remote.get_url()],
            ))

            # Create and commit a file
            stage_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["add"],
            )
            commit_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["commit"],
            )

            (repo.path / "push_test.txt").write_text("content")
            await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["add", "push_test.txt"],
                required_grant_id=stage_grant.grant_id,
            ))
            await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                command=["commit", "-m", "Push test"],
                required_grant_id=commit_grant.grant_id,
            ))

            # Push with PUSH grant
            # Bind to the EXACT command GitConsequentialAuthority.push() builds
            # for remote="origin", refspec="main".
            push_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                command=["push", "origin", "main"],
            )

            result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                command=["push", "origin", "main"],
                required_grant_id=push_grant.grant_id,
            ))
            assert result.state == GitOperationState.COMPLETED

    async def test_unrecognised_command_is_refused(self, manager, repo):
        """Test that an unrecognised git subcommand is refused (fail-closed)."""
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # Use a non-existent or unrecognised subcommand
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["unknown-subcommand", "--flag"],
            required_grant_id=grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED
        assert "unrecognised" in result.error_message.lower() or "command analysis failed" in result.error_message.lower()

    async def test_reset_hard_requires_ref_mutate_not_stage(self, manager, repo):
        """Test that 'reset --hard' requires REF_MUTATE level, not STAGE/INSPECT."""
        # Try with INSPECT grant - should fail
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["reset", "--hard", "HEAD~1"],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED

        # Try with STAGE grant - should fail (STAGE < REF_MUTATE)
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["reset", "--hard", "HEAD~1"],
            required_grant_id=stage_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED

    async def test_branch_delete_requires_delete_level(self, manager, repo):
        """Test that 'branch -d' requires DELETE level."""
        # Create a branch first
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add"],
        )
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["commit"],
        )

        # Create feature branch
        (repo.path / "temp.txt").write_text("temp")
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "temp.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "temp"],
            required_grant_id=commit_grant.grant_id,
        ))

        # Try to delete branch with INSPECT grant - should fail
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["branch", "-d", "main"],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert result.state == GitOperationState.REJECTED

    async def test_grant_scope_validation(self, manager, repo):
        """Test that grant scope is validated against the operation."""
        # Create two repos
        async with disposable_git_repository(prefix="bots5-test-repo1-") as repo1:
            async with disposable_git_repository(prefix="bots5-test-repo2-") as repo2:
                grant = manager.issue_grant(
                    authority_level=GitAuthorityLevel.STAGE,
                    repository_path=repo1.path,
                    scope="refs/heads/main",
                    approved_by="operator",
                    command=["status", "--porcelain=v2"],
                )

                # Grant is for repo1, should fail on repo2
                result = await manager.execute_operation(GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.STAGE,
                    repository_path=repo2.path,  # Wrong repo
                    command=["add", "file.txt"],
                    required_grant_id=grant.grant_id,
                ))
                assert result.state == GitOperationState.REJECTED
                # Grant validation fails when repository path doesn't match
                assert "invalid" in result.error_message.lower() or "expired" in result.error_message.lower()

    async def test_grant_digest_binding(self, manager, repo):
        """Test that grant with request_digest binding is enforced."""
        # Issue a grant with a specific request digest that matches the command
        request = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
        )
        digest = manager._compute_request_digest(request)
        
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            request_digest=digest,
            command=["status", "--porcelain=v2"],
        )

        # Try to use it for a different operation (different digest)
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "different.txt"],  # Different from what grant was issued for
            required_grant_id=grant.grant_id,
        ))
        # Grant digest binding should fail since the digest doesn't match
        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()


class TestGitAuthorityIntegration:
    """Integration tests for Git authority boundaries."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.mark.parametrize(
        "declared_level",
        [GitAuthorityLevel.INSPECT, GitAuthorityLevel.EDIT, GitAuthorityLevel.VALIDATE],
    )
    async def test_working_tree_checkout_requires_a_grant(
        self, manager, declared_level
    ):
        """Regression: `git checkout -- <path>` destroys uncommitted work.

        It was previously mapped to INSPECT ("just updates working tree") and
        EDIT/VALIDATE were absent from the set of levels requiring a grant, so a
        read-only-declared request could overwrite a modified tracked file with
        NO capability grant at all. It must now be rejected and the working tree
        left untouched.
        """
        async with disposable_git_repository() as repo:
            target = repo.path / "tracked.txt"
            target.write_text("base")
            for config_command in (["config", "user.email", "t@example.invalid"],
                                   ["config", "user.name", "t"]):
                await manager.execute_operation(GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo.path,
                    command=config_command,
                ))
            stage_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/*",
                approved_by="operator",
                command=["add", "-A"],
            )
            await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["add", "-A"],
                required_grant_id=stage_grant.grant_id,
            ))
            commit_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="refs/heads/*",
                approved_by="operator",
                command=["commit", "-m", "base"],
            )
            await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                command=["commit", "-m", "base"],
                required_grant_id=commit_grant.grant_id,
            ))
            target.write_text("UNCOMMITTED EDIT")

            # No grant: the destructive checkout must be refused and the edit kept.
            refused = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=declared_level,
                repository_path=repo.path,
                command=["checkout", "--", "tracked.txt"],
            ))
            assert refused.state == GitOperationState.REJECTED, (
                f"checkout declared as {declared_level} must not run without a grant"
            )
            assert target.read_text() == "UNCOMMITTED EDIT", (
                "a refused checkout must not modify the working tree"
            )

            # With the proper (STAGE) grant the same operation is legitimate.
            legitimate = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/*",
                approved_by="operator",
                command=["checkout", "--", "tracked.txt"],
            )
            completed = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["checkout", "--", "tracked.txt"],
                required_grant_id=legitimate.grant_id,
            ))
            assert completed.state == GitOperationState.COMPLETED
            assert target.read_text() == "base"

    async def test_authority_ladder_enforcement(self, manager):
        """Test that authority ladder is enforced: inspect < edit < validate < stage < commit < push."""
        async with disposable_git_repository() as repo:
            # Create a file to work with
            (repo.path / "test.txt").write_text("content")

            # INSPECT grant only
            inspect_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["status", "--porcelain=v2"],
            )

            # Try to commit with only INSPECT grant - should be rejected
            commit_request = GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                command=["commit", "-m", "test"],
                required_grant_id=inspect_grant.grant_id,
            )
            result = await manager.execute_operation(commit_request)
            assert result.state == GitOperationState.REJECTED

            # Now with proper STAGE grant
            stage_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["add", "test.txt"],
            )
            stage_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["add", "test.txt"],
                required_grant_id=stage_grant.grant_id,
            ))
            assert stage_result.state == GitOperationState.COMPLETED

            # COMMIT grant separate from STAGE
            commit_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["commit", "-m", "test commit"],
            )
            commit_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                command=["commit", "-m", "test commit"],
                required_grant_id=commit_grant.grant_id,
            ))
            assert commit_result.state == GitOperationState.COMPLETED

            # PUSH requires separate PUSH grant
            async with disposable_git_remote() as remote:
                # Add remote - needs INSPECT grant
                remote_grant = manager.issue_grant(
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo.path,
                    scope="refs/heads/*",
                    approved_by="operator",
                    command=["remote", "add", "origin", remote.get_url()],
                )
                await manager.execute_operation(GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo.path,
                    command=["remote", "add", "origin", remote.get_url()],
                ))

                push_request = GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "origin", "main"],
                    required_grant_id=commit_grant.grant_id,  # Wrong grant level
                )
                push_result = await manager.execute_operation(push_request)
                assert push_result.state == GitOperationState.REJECTED

                # With proper PUSH grant
                push_grant = manager.issue_grant(
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    scope="*",
                    approved_by="operator",
                    command=["push", "origin", "main"],
                )
                push_result = await manager.execute_operation(GitOperationRequest(
                    operation_id=manager._ids.new(),
                    authority_level=GitAuthorityLevel.PUSH,
                    repository_path=repo.path,
                    command=["push", "origin", "main"],
                    required_grant_id=push_grant.grant_id,
                ))
                assert push_result.state == GitOperationState.COMPLETED

    async def test_receipt_chain(self, manager):
        """Test that receipts form a chain for audit trail."""
        async with disposable_git_repository() as repo:
            (repo.path / "file1.txt").write_text("content1")

            grants = {}
            # Stage grant for specific add command
            grants[GitAuthorityLevel.STAGE] = manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["add", "file1.txt"],
            )
            # Commit grant for specific commit command
            grants[GitAuthorityLevel.COMMIT] = manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["commit", "-m", "Add file1"],
            )

            from bots5.infrastructure.git_authority import GitConsequentialAuthority
            consequential = GitConsequentialAuthority(manager)

            # Stage
            stage_result = await consequential.stage(
                repo.path,
                grants[GitAuthorityLevel.STAGE].grant_id,
                ["file1.txt"],
            )
            stage_receipt = manager.get_receipt(stage_result.operation_id)
            assert stage_receipt is not None
            assert stage_receipt.authority_level == GitAuthorityLevel.STAGE

            # Commit
            commit_result = await consequential.commit(
                repo.path,
                grants[GitAuthorityLevel.COMMIT].grant_id,
                "Add file1",
            )
            commit_receipt = manager.get_receipt(commit_result.operation_id)
            assert commit_receipt is not None
            assert commit_receipt.authority_level == GitAuthorityLevel.COMMIT
            assert commit_receipt.head_commit is not None

            # Each receipt is independent but linked by repository state
            assert stage_receipt.operation_id != commit_receipt.operation_id

    async def test_inspect_operation_without_grant(self, manager):
        """Test that inspect operations work without required_grant_id."""
        async with disposable_git_repository() as repo:
            result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                command=["status", "--porcelain=v2"],
                required_grant_id=None,  # Inspect can work without grant
            ))
            # Inspect operations should succeed without grant
            assert result.state == GitOperationState.COMPLETED

    async def test_inspect_grant_limited_to_inspect_operations(self, manager):
        """Test that INSPECT grant cannot be used for consequential operations."""
        async with disposable_git_repository() as repo:
            # Issue INSPECT grant only
            inspect_grant = manager.issue_grant(
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                scope="*",
                approved_by="operator",
                command=["status", "--porcelain=v2"],
            )

            # Try to STAGE with INSPECT grant - should be rejected
            stage_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                command=["add", "."],
                required_grant_id=inspect_grant.grant_id,
            ))
            assert stage_result.state == GitOperationState.REJECTED

            # But INSPECT works with INSPECT grant
            inspect_result = await manager.execute_operation(GitOperationRequest(
                operation_id=manager._ids.new(),
                authority_level=GitAuthorityLevel.INSPECT,
                repository_path=repo.path,
                command=["status", "--porcelain=v2"],
                required_grant_id=inspect_grant.grant_id,
            ))
            assert inspect_result.state == GitOperationState.COMPLETED


class TestGitAuthorityBindingRemediation:
    """
    Adversarial regression tests for Git operation/consequence binding fix.
    
    These tests would FAIL against the current code and PASS after the fix.
    They verify that:
    1. Level/scope-wide grants cannot be reused for different commands
    2. Exact-request grants cannot be reused for modified commands
    3. Normalisation stability: semantically identical commands map to same digest
    4. Existing default-open behavior still holds for inspect operations
    """

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    # === Adversarial Test 1: Exact-request grant cannot be reused for different command ===
    async def test_exact_request_grant_cannot_reuse_for_different_command(self, manager, repo):
        """
        Test that an exact-request grant (binding_scope=EXACT_REQUEST) CANNOT be
        presented for a different command than it was issued for.
        
        This is the SECURITY FIX: previously, a grant with a request_digest was
        bound heuristically based on whether the digest looked "generic". Now
        EXACT_REQUEST binding is explicit and enforced.
        """
        # First, get the digest for "push origin main"
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
        )
        digest1 = manager._compute_request_digest(request1)

        # Issue an exact-request grant for push origin main (command and digest must match)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        # Try to use it for "push --force origin main" (different digest)
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))
        # Should be REJECTED because EXACT_REQUEST binding requires exact digest match
        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_level_scope_grant_can_reuse_for_any_command_at_same_level_scope(self, manager, repo):
        """
        Test that a level/scope-wide grant (binding_scope=LEVEL_SCOPE) CAN be
        presented for ANY command at the same level and matching scope.
        
        LEVEL_SCOPE means: "authorize any operation at this authority level on
        refs within this scope". The request_digest is metadata only, not binding.
        """
        # Register a broad authority approval first
        approval = GitBroadAuthorityApproval(
            approval_id="level-scope-approval",
            approved_by="director",
            scope="refs/heads/main",
            permitted_consequences=(GitConsequenceClass.NON_DESTRUCTIVE,),
            rationale="batch processing operations for campaign",
            issued_at=0.0,
        repository_path=repo.path,
        )
        manager.register_broad_authority_approval(approval)

        # Issue a level/scope-wide grant (binding_scope=LEVEL_SCOPE)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="level-scope-approval",
        )

        # Use it for 'add file1.txt'
        (repo.path / "file1.txt").write_text("content")
        result1 = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "file1.txt"],
            required_grant_id=grant.grant_id,
        ))
        assert result1.state == GitOperationState.COMPLETED

        # Reuse the SAME grant for 'add file2.txt'
        (repo.path / "file2.txt").write_text("content")
        result2 = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "file2.txt"],
            required_grant_id=grant.grant_id,
        ))
        # With LEVEL_SCOPE, the same grant should work for different add commands
        # as long as they're within the scope
        assert result2.state == GitOperationState.COMPLETED

    async def test_level_scope_grant_reuse_for_same_command_allowed(self, manager, repo):
        """
        Test that a level/scope-wide grant CAN be reused for the same command.
        
        With binding_scope=LEVEL_SCOPE, the grant should work for any operation
        at the same level/scope that matches the scope. The key is that the
        command must be semantically equivalent (after normalization).
        
        Note: We use 'log' (INSPECT) to avoid needing a real remote.
        The test verifies that the same LEVEL_SCOPE grant can be reused.
        """
        # Register a broad authority approval first
        approval = GitBroadAuthorityApproval(
            approval_id="level-scope-approval-inspect",
            approved_by="director",
            scope="refs/heads/main",
            permitted_consequences=(GitConsequenceClass.NON_DESTRUCTIVE,),
            rationale="batch processing operations for campaign",
            issued_at=0.0,
        repository_path=repo.path,
        )
        manager.register_broad_authority_approval(approval)

        # Issue a level/scope-wide grant for INSPECT
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="level-scope-approval-inspect",
        )

        # Try to use it for the same command (normalized) - log with max-count
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["log", "--oneline", "-1"],
            required_grant_id=grant.grant_id,
        ))
        # With LEVEL_SCOPE, this should work because the scope matches
        # and no exact-request binding is enforced
        assert result.state == GitOperationState.COMPLETED

    # === Adversarial Test 2: Exact-request grant for modified command ===
    async def test_exact_request_grant_cannot_reuse_for_modified_command(self, manager, repo):
        """
        Test that an exact-request grant cannot be presented for a modified command
        (added --force, changed refspec, changed pathspec).
        """
        # First, get a digest for "push origin main"
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
        )
        digest1 = manager._compute_request_digest(request1)

        # Issue an exact-request grant for push origin main (command must match digest)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        # Try to use it for "push --force origin main" (different digest)
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))
        # Should be REJECTED because --force changes the digest
        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_exact_request_grant_reuse_for_same_command_allowed(self, manager, repo):
        """
        Test that the exact same request (after normalization) can be re-presented.
        
        Note: We use 'status' instead of 'push' to avoid needing a real remote.
        The grant binding test is the focus, not the actual git command success.
        """
        # First, get a digest for "status --porcelain=v2"
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
        )
        digest1 = manager._compute_request_digest(request1)

        # Issue an exact-request grant (command matches the digest)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # Re-present the exact same request (should work)
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
            required_grant_id=grant.grant_id,
        ))
        assert result.state == GitOperationState.COMPLETED

    # === Adversarial Test 3: Normalisation stability ===
    async def test_normalisation_stability_semantically_identical_commands(self, manager, repo):
        """
        Test that semantically identical command spellings map to the same digest.
        
        Examples:
        - "push origin main" == "push origin main" (same)
        - "push --force origin main" == "push --force origin main" (same)
        - "push -f origin main" normalizes to "push --force origin main" (same digest)
        """
        # Test -f normalizes to --force
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "-f", "origin", "main"],
        )
        digest1 = manager._compute_request_digest(request1)

        request2 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
        )
        digest2 = manager._compute_request_digest(request2)

        assert digest1 == digest2, "Semantically identical commands must have same digest"

    async def test_normalisation_consequence_changing_variants_different_digest(self, manager, repo):
        """
        Test that consequence-changing variants do NOT map to the same digest.
        
        Examples:
        - "push origin main" != "push --force origin main"
        - "add file.txt" != "add -f file.txt" (if -f changes behavior)
        """
        # push vs push --force should have different digests
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
        )
        digest1 = manager._compute_request_digest(request1)

        request2 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
        )
        digest2 = manager._compute_request_digest(request2)

        assert digest1 != digest2, "Consequence-changing variants must have different digests"

    # === Adversarial Test 4: Existing default behavior still holds ===
    async def test_inspect_operations_still_work_without_grant(self, manager, repo):
        """
        Test that inspect operations still work without required_grant_id.
        
        This is an existing guarantee that must not be weakened.
        """
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            command=["status", "--porcelain=v2"],
            required_grant_id=None,  # Inspect can work without grant
        ))
        assert result.state == GitOperationState.COMPLETED

    async def test_inspect_grant_limited_to_inspect_operations(self, manager, repo):
        """
        Test that INSPECT grant cannot be used for consequential operations.
        
        This is an existing guarantee that must not be weakened.
        """
        inspect_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo.path,
            scope="*",
            approved_by="operator",
            command=["status", "--porcelain=v2"],
        )

        # Try to STAGE with INSPECT grant - should be rejected
        stage_result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "."],
            required_grant_id=inspect_grant.grant_id,
        ))
        assert stage_result.state == GitOperationState.REJECTED



class TestGitAuthorityBindingRemediationQ1B:
    """
    Regression tests for Q1B remediation:
    1. Fail-closed defaults - EXACT_REQUEST is unconditional default
    2. Explicit attributable broad authority - LEVEL_SCOPE requires approval_id
    3. Consequence separation - history-rewriting operations require separate authorization
    
    These tests verify the security fixes are in place.
    """

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def manager(self, executor):
        return GitAuthorityManager(process_executor=executor)

    @pytest.fixture
    async def repo(self):
        async with disposable_git_repository() as repo:
            yield repo

    # ===== Test 1: Fail-closed defaults =====

    async def test_default_issuance_omitting_digest_raises(self, manager, repo):
        """
        Test 1a: Omitting binding info and no derivable identity RAISES rather than
        granting broad authority (EXACT_REQUEST is unconditional default).
        """
        with pytest.raises(ValueError, match="requires operation identity"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.PUSH,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
            )

    async def test_default_issuance_with_digest_yields_exact_request(self, manager, repo):
        """
        Test 1b: Omitting binding_scope but providing a command yields EXACT_REQUEST.
        """
        request = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
        )
        digest = manager._compute_request_digest(request)

        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        assert grant.binding_scope == GitAuthorityBindingScope.EXACT_REQUEST
        assert grant.request_digest == digest

    async def test_default_issuance_no_level_scope_fallback(self, manager, repo):
        """
        Test 1c: No fallback to LEVEL_SCOPE when digest is missing.
        Previously: missing digest -> LEVEL_SCOPE (broad authority)
        Now: missing digest -> ValueError (fail-closed)
        """
        with pytest.raises(ValueError, match="requires operation identity"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.COMMIT,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
            )

    # ===== Test 2: Explicit attributable broad authority =====

    async def test_intentional_broad_issuance_succeeds(self, manager, repo):
        """
        Test 2a: LEVEL_SCOPE with proper approval_id succeeds and is recorded.
        """
        # Register a broad authority approval first
        approval = GitBroadAuthorityApproval(
            approval_id="level-scope-approval",
            approved_by="director",
            scope="refs/heads/main",
            permitted_consequences=(GitConsequenceClass.NON_DESTRUCTIVE,),
            rationale="Batch processing operation requiring multiple staging operations",
            issued_at=0.0,
        repository_path=repo.path,
        )
        manager.register_broad_authority_approval(approval)

        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="level-scope-approval",
            command=["add", "test.txt"],
        )

        assert grant.binding_scope == GitAuthorityBindingScope.LEVEL_SCOPE
        assert grant.broad_authority_approval_id == "level-scope-approval"
        assert grant.broad_authority_approval == "director"
        assert grant.justification == "Batch processing operation requiring multiple staging operations"

    async def test_unauthorized_broad_no_approver_refused(self, manager, repo):
        """
        Test 2b: LEVEL_SCOPE with unregistered approval_id is REFUSED.
        """
        with pytest.raises(ValueError, match="unknown approval_id"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="not-a-registered-approval",
            )

    async def test_unauthorized_broad_empty_approval_id_refused(self, manager, repo):
        """
        Test 2c: LEVEL_SCOPE with empty approval_id is REFUSED.
        """
        with pytest.raises(ValueError, match="LEVEL_SCOPE grant requires an approval_id resolving to a registered GitBroadAuthorityApproval"):
            manager.issue_grant(
                authority_level=GitAuthorityLevel.STAGE,
                repository_path=repo.path,
                scope="refs/heads/main",
                approved_by="operator",
                binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
                approval_id="",
            )

    async def test_grant_recorded_with_broad_authority(self, manager, repo):
        """
        Test 2d: LEVEL_SCOPE grant records the approval_id and approver.
        """
        approval = GitBroadAuthorityApproval(
            approval_id="level-scope-approval-2",
            approved_by="director",
            scope="refs/heads/main",
            permitted_consequences=(GitConsequenceClass.NON_DESTRUCTIVE,),
            rationale="batch processing requires staging several files in sequence",
            issued_at=0.0,
        repository_path=repo.path,
        )
        manager.register_broad_authority_approval(approval)

        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="level-scope-approval-2",
            command=["add", "test.txt"],
        )

        # Check grant is recorded
        recorded = manager.get_grant(grant.grant_id)
        assert recorded is not None
        assert recorded.broad_authority_approval_id == "level-scope-approval-2"
        assert recorded.broad_authority_approval == "director"
        assert recorded.justification == "batch processing requires staging several files in sequence"

    # ===== Test 3: Consequence separation =====

    async def test_plain_push_grant_cannot_authorize_force_push_exact_request(self, manager, repo):
        """
        Test 3a: EXACT_REQUEST grant for plain push cannot authorize --force push.
        """
        # Issue EXACT_REQUEST grant for plain push (command must match)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        # Try to use it for force push (different digest)
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_plain_push_grant_cannot_authorize_f_short_force_exact_request(self, manager, repo):
        """
        Test 3b: EXACT_REQUEST grant for plain push cannot authorize -f (short force).
        -f normalizes to --force, which is a different consequence class.
        """
        # Issue EXACT_REQUEST grant for plain push (command must match)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        # Try to use it for -f push
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "-f", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_plain_push_grant_cannot_authorize_force_with_lease_exact_request(self, manager, repo):
        """
        Test 3c: EXACT_REQUEST grant for plain push cannot authorize --force-with-lease.
        """
        # Issue EXACT_REQUEST grant for plain push (command must match)
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "main"],
        )

        # Try to use it for --force-with-lease push
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force-with-lease", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_plus_refspec_grant_cannot_authorize_plain_push_exact_request(self, manager, repo):
        """
        Test 3d: EXACT_REQUEST grant for +refspec cannot authorize plain push.
        """
        # Issue EXACT_REQUEST grant for +refspec push
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "origin", "+HEAD:main"],
        )

        # Try to use it for plain push
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED
        assert "digest mismatch" in result.error_message.lower() or "not bound" in result.error_message.lower()

    async def test_force_push_grant_cannot_authorize_plain_push_exact_request(self, manager, repo):
        """
        Test 3e: EXACT_REQUEST grant for --force push CANNOT authorize plain push.
        Consequence-changing variants must not collapse.
        """
        # Issue EXACT_REQUEST grant for --force push
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["push", "--force", "origin", "main"],
        )

        # Try to use it for plain push - should FAIL because consequences differ
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED

    async def test_reset_hard_grant_cannot_authorize_reset_merge_exact_request(self, manager, repo):
        """
        Test 3f: Different consequence-changing operations must have separate grants.
        reset --hard grant cannot authorize reset --merge.
        """
        # Create a commit first to make reset valid
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "test.txt"],
        )
        commit_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["commit", "-m", "test"],
        )

        # Stage and commit something
        (repo.path / "test.txt").write_text("content")
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "test.txt"],
            required_grant_id=stage_grant.grant_id,
        ))
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo.path,
            command=["commit", "-m", "test"],
            required_grant_id=commit_grant.grant_id,
        ))

        # Issue EXACT_REQUEST grant for reset --hard
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.REF_MUTATE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["reset", "--hard", "HEAD~1"],
        )

        # Try to use it for reset --merge - should FAIL
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.REF_MUTATE,
            repository_path=repo.path,
            command=["reset", "--merge", "HEAD~1"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED

    async def test_clean_f_grant_cannot_authorize_plain_clean_exact_request(self, manager, repo):
        """
        Test 3g: Destructive working-tree operations must have separate grants.
        clean -f grant cannot authorize plain clean.
        """
        # First need a stage grant to create files
        stage_grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "temp.log"],
        )

        # Create some ignored files to clean
        (repo.path / ".gitignore").write_text("*.log\n")
        (repo.path / "temp.log").write_text("temp")
        await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "temp.log"],
            required_grant_id=stage_grant.grant_id,
        ))

        # Issue EXACT_REQUEST grant for clean -f
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.DELETE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["clean", "-f"],
        )

        # Try to use it for plain clean (dry-run) - should FAIL
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.DELETE,
            repository_path=repo.path,
            command=["clean"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.REJECTED

    async def test_level_scope_grant_cannot_bypass_consequence_class(self, manager, repo):
        """
        Test 3h: LEVEL_SCOPE grant with proper approval_id CANNOT bypass consequence
        separation - a plain push grant cannot authorize --force push.
        """
        # Register a broad authority approval first
        approval = GitBroadAuthorityApproval(
            approval_id="level-scope-approval-push",
            approved_by="director",
            scope="refs/heads/main",
            permitted_consequences=(GitConsequenceClass.NON_DESTRUCTIVE,),
            rationale="ordinary non-destructive pushes to the release branch",
            issued_at=0.0,
        repository_path=repo.path,
        )
        manager.register_broad_authority_approval(approval)

        # Issue LEVEL_SCOPE grant for plain push
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            binding_scope=GitAuthorityBindingScope.LEVEL_SCOPE,
            approval_id="level-scope-approval-push",
            command=["push", "origin", "main"],
        )

        # The digest for --force push is different from plain push
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
            required_grant_id=grant.grant_id,
        ))

        # The result should be REJECTED because consequence classes differ
        assert result.state == GitOperationState.REJECTED

    # ===== Test 4: Existing passing tests still work =====

    async def test_grant_reuse_for_same_command_still_works(self, manager, repo):
        """
        Test 4: An exact grant can still authorise the same command (keep existing test green).
        """
        grant = manager.issue_grant(
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            scope="refs/heads/main",
            approved_by="operator",
            command=["add", "file.txt"],
        )

        # Create the file first
        (repo.path / "file.txt").write_text("content")

        # Same command should succeed
        result = await manager.execute_operation(GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo.path,
            command=["add", "file.txt"],
            required_grant_id=grant.grant_id,
        ))

        assert result.state == GitOperationState.COMPLETED

    async def test_normalisation_f_yields_same_digest_as_force(self, manager, repo):
        """
        Test 4b: -f normalizes to --force, so both produce same digest.
        """
        request1 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "-f", "origin", "main"],
        )
        digest1 = manager._compute_request_digest(request1)

        request2 = GitOperationRequest(
            operation_id=manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo.path,
            command=["push", "--force", "origin", "main"],
        )
        digest2 = manager._compute_request_digest(request2)

        assert digest1 == digest2, "Semantically identical commands must have same digest"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
