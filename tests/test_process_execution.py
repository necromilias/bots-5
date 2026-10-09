"""Tests for bounded process/code execution with timeout/cancellation/resource accounting."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from bots5.bootstrap.desktop import ControlPlaneState
from bots5.core.capabilities import (
    CapabilityAuthority,
    DirectoryScope,
    GrantRequest,
    PROCESS_RUN,
    WORKSPACE_WRITE,
    Subject,
)
from bots5.infrastructure.process_execution import (
    AuthorizationBoundaryError,
    BoundedProcessExecutor,
    CodeExecutionRequest,
    CodeExecutor,
    ProcessExecutionRequest,
    ProcessExecutionResult,
    ProcessReceipt,
    ProcessResourceLimits,
    ProcessState,
    ResourceLimitExceeded,
    bounded_workspace,
)


def _process_run_authority() -> CapabilityAuthority:
    """Return a fresh capability authority for process execution tests."""
    return CapabilityAuthority()


def _process_run_grant(authority: CapabilityAuthority, root: Path, *, effects: int = 10):
    """Return a process-run capability grant scoped to ``root``."""
    return authority.grant(
        GrantRequest(
            subject=Subject(kind="process", identity="test-process"),
            kind=PROCESS_RUN,
            scope=DirectoryScope(root=root),
            ttl_seconds=60.0,
            max_effects=effects,
        )
    )


def _workspace_write_grant(authority: CapabilityAuthority, root: Path, *, effects: int = 10):
    """Return a workspace-write capability grant scoped to ``root``.

    ``CodeExecutor`` authors code into a bounded workspace, so it consumes the
    ``workspace-write`` kind (the seam's ``effect()`` kind binding refuses a
    ``process-run`` grant for that effect).
    """
    return authority.grant(
        GrantRequest(
            subject=Subject(kind="code", identity="test-code"),
            kind=WORKSPACE_WRITE,
            scope=DirectoryScope(root=root),
            ttl_seconds=60.0,
            max_effects=effects,
        )
    )


class TestBoundedProcessExecutor:
    """Tests for BoundedProcessExecutor."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    async def test_simple_command_execution(self, executor, temp_dir):
        """Test basic command execution with a valid grant."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["echo", "hello world"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert result.exit_code == 0
        assert b"hello world" in result.stdout
        assert result.cpu_time_seconds is not None
        # Per-child peak memory is not measurable through portable POSIX rusage:
        # ru_maxrss is a high-water mark shared across every reaped child, so a
        # delta is not attributable to this one. The executor reports it as
        # unavailable rather than reporting false precision; memory bounds remain
        # enforced (see the resource-limit tests).
        assert result.peak_memory_bytes is None
        assert grant.remaining_effects == 9

    async def test_execute_without_grant_is_refused_and_spawns_nothing(self, executor, temp_dir):
        """A call without authority/grant is refused and must not spawn a process."""
        request = ProcessExecutionRequest(
            command=["sh", "-c", "echo 'should not run' > spawned.txt"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request)

        assert result.state == ProcessState.FAILED
        assert "NO_GRANT" in (result.error_message or "")
        assert not (temp_dir / "spawned.txt").exists()
        assert not executor._active_processes
        assert result.remote_outcome_unknown is False

    async def test_execute_with_invalid_grant_type_is_refused(self, executor, temp_dir):
        """Passing non-grant objects is refused before any subprocess starts."""
        request = ProcessExecutionRequest(
            command=["echo", "hello"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority="not-an-authority", grant="not-a-grant")

        assert result.state == ProcessState.FAILED
        assert "NO_GRANT" in (result.error_message or "")

    async def test_execute_with_foreign_grant_is_refused(self, executor, temp_dir):
        """A grant issued by a different authority is refused."""
        ca = _process_run_authority()
        other_ca = _process_run_authority()
        grant = _process_run_grant(other_ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["echo", "hello"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.FAILED
        assert "NO_GRANT" in (result.error_message or "")

    async def test_command_with_timeout(self, executor, temp_dir):
        """Test command that times out."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["sleep", "10"],
            working_directory=temp_dir,
            timeout_seconds=0.1,
            resource_limits=ProcessResourceLimits(max_cpu_seconds=1.0),
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state in (ProcessState.TIMEOUT, ProcessState.FAILED)
        assert result.error_message is not None

    async def test_command_failure(self, executor, temp_dir):
        """Test command that fails."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["false"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.FAILED
        assert result.exit_code != 0

    async def test_cancellation(self, executor, temp_dir):
        """Test process cancellation."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["sleep", "10"],
            working_directory=temp_dir,
            timeout_seconds=30.0,
        )

        # Start execution in background
        task = asyncio.create_task(executor.execute(request, authority=ca, grant=grant))

        # Wait deterministically for the child to be registered. A fixed sleep
        # is a race: under load the process may not have been spawned yet, so
        # poll with a bounded deadline instead of asserting immediately.
        process_id = None
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            for pid, proc in executor._active_processes.items():
                process_id = pid
                break
            if process_id is not None:
                break
            await asyncio.sleep(0.01)

        assert process_id is not None
        cancelled = await executor.cancel(process_id)
        assert cancelled

        # The task will raise CancelledError, but we can get the result from the executor
        try:
            await task
        except asyncio.CancelledError:
            pass

        result = executor.get_result(process_id)
        assert result is not None
        assert result.state == ProcessState.CANCELLED

    async def test_stdin_stdout_stderr(self, executor, temp_dir):
        """Test stdin/stdout/stderr handling."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["cat"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
            stdin_data=b"input data",
            capture_stdout=True,
            capture_stderr=True,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert result.stdout == b"input data"

    async def test_working_directory_isolation(self, executor, temp_dir):
        """Test working directory isolation."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        # Create a file in temp_dir
        (temp_dir / "test.txt").write_text("original")

        request = ProcessExecutionRequest(
            command=["sh", "-c", "echo 'modified' > test.txt && cat test.txt"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert b"modified" in result.stdout
        # Original file should be modified in the working directory
        assert (temp_dir / "test.txt").read_text().strip() == "modified"

    async def test_resource_limits_cpu(self, executor, temp_dir):
        """Test CPU time limit enforcement."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["python3", "-c", "while True: pass"],
            working_directory=temp_dir,
            timeout_seconds=30.0,
            resource_limits=ProcessResourceLimits(max_cpu_seconds=0.5),
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        # Should be killed due to CPU limit
        assert result.state in (ProcessState.TIMEOUT, ProcessState.FAILED)
        assert result.error_message is not None

    async def test_output_size_limit(self, executor, temp_dir):
        """Test output size limiting."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["python3", "-c", "import sys; sys.stdout.write('x' * 50000)"],
            working_directory=temp_dir,
            timeout_seconds=10.0,
            resource_limits=ProcessResourceLimits(max_output_bytes=1000),
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert len(result.stdout) <= 1100  # Limit + truncation message

    async def test_unknown_preserved_for_unexpected_error(self, executor, temp_dir):
        """An unexpected failure after spawn keeps UNKNOWN — never rewritten."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["echo", "ok"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )

        original_read_stream = executor._read_stream

        async def broken_read_stream(stream, chunks):
            raise RuntimeError("simulated unexpected stream failure")

        executor._read_stream = broken_read_stream
        try:
            result = await executor.execute(request, authority=ca, grant=grant)
        finally:
            executor._read_stream = original_read_stream

        assert result.state == ProcessState.UNKNOWN
        assert result.remote_outcome_unknown is True
        assert result.error_message is not None

    async def test_timeout_marks_remote_outcome_unknown(self, executor, temp_dir):
        """A local timeout does not prove the child had no remote effect."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["sleep", "10"],
            working_directory=temp_dir,
            timeout_seconds=0.1,
            resource_limits=ProcessResourceLimits(max_cpu_seconds=1.0),
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state is ProcessState.TIMEOUT
        assert result.remote_outcome_unknown is True

    async def test_cancellation_marks_remote_outcome_unknown(self, executor, temp_dir):
        """Cancellation after the child may have acted keeps the outcome unknown."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["sleep", "10"],
            working_directory=temp_dir,
            timeout_seconds=30.0,
        )

        task = asyncio.create_task(executor.execute(request, authority=ca, grant=grant))

        process_id = None
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            for pid in executor._active_processes:
                process_id = pid
                break
            if process_id is not None:
                break
            await asyncio.sleep(0.01)

        assert process_id is not None
        await executor.cancel(process_id)

        try:
            await task
        except asyncio.CancelledError:
            pass

        result = executor.get_result(process_id)
        assert result is not None
        assert result.state is ProcessState.CANCELLED
        assert result.remote_outcome_unknown is True

    async def test_completed_and_definite_failure_stay_known(self, executor, temp_dir):
        """Definite completion and definite failure remain remote-outcome-known."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)

        ok_request = ProcessExecutionRequest(
            command=["echo", "ok"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        ok_result = await executor.execute(ok_request, authority=ca, grant=grant)
        assert ok_result.state is ProcessState.COMPLETED
        assert ok_result.remote_outcome_unknown is False

        fail_request = ProcessExecutionRequest(
            command=["false"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        fail_result = await executor.execute(fail_request, authority=ca, grant=grant)
        assert fail_result.state is ProcessState.FAILED
        assert fail_result.remote_outcome_unknown is False

    async def test_direct_execute_raw_call_is_refused(self, executor, temp_dir):
        """The raw helper rejects direct calls and spawns no process."""
        request = ProcessExecutionRequest(
            command=["sh", "-c", "echo spawned > raw.txt"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        with pytest.raises(AuthorizationBoundaryError, match="authorization boundary"):
            await executor._execute_raw(request)

        assert not (temp_dir / "raw.txt").exists()
        assert not executor._active_processes


class TestUnknownOutcomeProjection:
    """Downstream receipt projections must not settle an unknown outcome."""

    def test_process_timeout_projection_is_unknown_not_settled(self):
        """A TIMEOUT result with remote_outcome_unknown=True projects UNKNOWN."""
        result = ProcessExecutionResult(
            process_id="p1",
            state=ProcessState.TIMEOUT,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            started_at=1.0,
            ended_at=2.0,
            cpu_time_seconds=None,
            peak_memory_bytes=None,
            error_message="timeout",
            remote_outcome_unknown=True,
        )
        receipt = ProcessReceipt(
            process_id="p1",
            request_digest="d1",
            result=result,
            settled_at=2.0,
            authority_grant_id="g1",
        )

        state = ControlPlaneState(capability_authority=CapabilityAuthority())
        state.observe_process_execution(receipt)

        projections = state.receipts()
        assert len(projections) == 1
        assert projections[0].state == "unknown"
        executions = state.executions()
        assert executions[0].state == "unknown"
        assert executions[0].provider_side_outcome_unknown is True

    def test_process_cancellation_projection_is_unknown_not_settled(self):
        """A CANCELLED result with remote_outcome_unknown=True projects UNKNOWN."""
        result = ProcessExecutionResult(
            process_id="p2",
            state=ProcessState.CANCELLED,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            started_at=1.0,
            ended_at=2.0,
            cpu_time_seconds=None,
            peak_memory_bytes=None,
            error_message="cancelled",
            remote_outcome_unknown=True,
        )
        receipt = ProcessReceipt(
            process_id="p2",
            request_digest="d2",
            result=result,
            settled_at=2.0,
            authority_grant_id="g2",
        )

        state = ControlPlaneState(capability_authority=CapabilityAuthority())
        state.observe_process_execution(receipt)

        assert state.receipts()[0].state == "unknown"
        assert state.executions()[0].state == "unknown"

    def test_completed_process_projection_remains_settled(self):
        """A definite completion still projects a SETTLED receipt."""
        result = ProcessExecutionResult(
            process_id="p3",
            state=ProcessState.COMPLETED,
            exit_code=0,
            stdout=b"ok",
            stderr=b"",
            started_at=1.0,
            ended_at=2.0,
            cpu_time_seconds=None,
            peak_memory_bytes=None,
            remote_outcome_unknown=False,
        )
        receipt = ProcessReceipt(
            process_id="p3",
            request_digest="d3",
            result=result,
            settled_at=2.0,
            authority_grant_id="g3",
        )

        state = ControlPlaneState(capability_authority=CapabilityAuthority())
        state.observe_process_execution(receipt)

        assert state.receipts()[0].state == "settled"
        assert state.executions()[0].state == "succeeded"


class TestCodeExecutor:
    """Tests for CodeExecutor."""

    @pytest.fixture
    def executor(self):
        # Create a fresh executor for each test to avoid state issues
        return CodeExecutor()

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    async def test_python_execution(self, executor, temp_dir):
        """Test Python code execution with a valid grant."""
        ca = _process_run_authority()
        grant = _workspace_write_grant(ca, temp_dir)
        request = CodeExecutionRequest(
            code="print('Hello from Python')\nresult = 2 + 2\nprint(f'Result: {result}')",
            language="python",
            working_directory=temp_dir,
            timeout_seconds=10.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert b"Hello from Python" in result.stdout
        assert b"Result: 4" in result.stdout

    async def test_bash_execution(self, executor, temp_dir):
        """Test Bash code execution."""
        ca = _process_run_authority()
        grant = _workspace_write_grant(ca, temp_dir)
        request = CodeExecutionRequest(
            code='echo "Hello from Bash"\nRESULT=$((3 * 3))\necho "Result: $RESULT"',
            language="bash",
            working_directory=temp_dir,
            timeout_seconds=10.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert b"Hello from Bash" in result.stdout
        assert b"Result: 9" in result.stdout

    async def test_javascript_execution(self, executor, temp_dir):
        """Test JavaScript/Node.js code execution."""
        ca = _process_run_authority()
        grant = _workspace_write_grant(ca, temp_dir)
        request = CodeExecutionRequest(
            code="console.log('Hello from JavaScript');\nconst result = 5 + 5;\nconsole.log(`Result: ${result}`);",
            language="javascript",
            working_directory=temp_dir,
            timeout_seconds=10.0,
            resource_limits=ProcessResourceLimits(max_memory_mb=256),
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert b"Hello from JavaScript" in result.stdout
        assert b"Result: 10" in result.stdout

    async def test_code_with_files(self, executor, temp_dir):
        """Test code execution with additional files."""
        ca = _process_run_authority()
        grant = _workspace_write_grant(ca, temp_dir)
        request = CodeExecutionRequest(
            code="import data\nprint(f'Data: {data.VALUE}')",
            language="python",
            working_directory=temp_dir,
            timeout_seconds=10.0,
            files={"data.py": "VALUE = 42\n"},
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.state == ProcessState.COMPLETED
        assert b"Data: 42" in result.stdout

    async def test_unsupported_language(self, executor, temp_dir):
        """Test unsupported language raises error."""
        ca = _process_run_authority()
        grant = _workspace_write_grant(ca, temp_dir)
        request = CodeExecutionRequest(
            code="print('hello')",
            language="unsupported",
            working_directory=temp_dir,
            timeout_seconds=10.0,
        )
        with pytest.raises(Exception):  # StateError
            await executor.execute(request, authority=ca, grant=grant)

    async def test_code_execute_without_grant_is_refused_and_writes_nothing(self, executor, temp_dir):
        """A code execution call without a grant must not write files or spawn a process."""
        request = CodeExecutionRequest(
            code="print('hello')",
            language="python",
            working_directory=temp_dir,
            timeout_seconds=10.0,
        )
        result = await executor.execute(request)

        assert result.state == ProcessState.FAILED
        assert "NO_GRANT" in (result.error_message or "")
        assert not (temp_dir / "script.py").exists()


class TestBoundedWorkspace:
    """Tests for bounded_workspace context manager."""

    async def test_workspace_creation_and_cleanup(self):
        """Test workspace is created and cleaned up."""
        with tempfile.TemporaryDirectory() as base_dir:
            base_path = Path(base_dir)
            workspace_path = None

            async with bounded_workspace(base_path, prefix="test-") as workspace:
                workspace_path = workspace
                assert workspace.exists()
                assert workspace.is_dir()
                # Create a file
                (workspace / "test.txt").write_text("content")
                assert (workspace / "test.txt").exists()

            # After context exit, workspace should be cleaned up
            assert not workspace_path.exists()

    async def test_workspace_no_cleanup(self):
        """Test workspace persists when cleanup=False."""
        with tempfile.TemporaryDirectory() as base_dir:
            base_path = Path(base_dir)
            workspace_path = None

            async with bounded_workspace(base_path, prefix="test-", cleanup=False) as workspace:
                workspace_path = workspace
                (workspace / "test.txt").write_text("content")

            # After context exit, workspace should still exist
            assert workspace_path.exists()
            assert (workspace_path / "test.txt").read_text() == "content"

            # Manual cleanup
            import shutil
            shutil.rmtree(workspace_path)


class TestProcessReceipt:
    """Tests for process execution receipts."""

    @pytest.fixture
    def executor(self):
        return BoundedProcessExecutor()

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    async def test_receipt_creation(self, executor, temp_dir):
        """Test receipt is created after execution."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["echo", "test"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)

        assert result.process_id in executor._receipts
        receipt = executor._receipts[result.process_id]
        assert receipt.process_id == result.process_id
        assert receipt.authority_grant_id == grant.grant_id
        assert receipt.settled_at > 0

    async def test_receipt_request_digest(self, executor, temp_dir):
        """Test receipt request digest is consistent."""
        ca = _process_run_authority()
        grant = _process_run_grant(ca, temp_dir)
        request = ProcessExecutionRequest(
            command=["echo", "test"],
            working_directory=temp_dir,
            timeout_seconds=5.0,
        )
        result = await executor.execute(request, authority=ca, grant=grant)
        receipt = executor.get_receipt(result.process_id)

        assert receipt.request_digest == executor._compute_request_digest(request)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
