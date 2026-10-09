from __future__ import annotations

import asyncio
import inspect
import os
import resource
import signal
import shlex
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from bots5.core.capabilities import (
    CapabilityAuthority,
    CapabilityDenied,
    CapabilityGrant,
    DenialReason,
    PROCESS_RUN,
    WORKSPACE_WRITE,
)
from bots5.core.errors import StateError
from bots5.core.execution import ExecutionManager
from bots5.domain.clock import Clock, SystemClock
from bots5.domain.ids import IdFactory, Uuid7Factory


class ProcessState(str, Enum):
    """State of a bounded process execution."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ResourceLimitExceeded(Exception):
    """Raised when a resource limit is exceeded during process execution."""
    pass


class AuthorizationBoundaryError(RuntimeError):
    """Raised when an internal authorization-bound helper is called outside its boundary."""
    pass


@dataclass(frozen=True, slots=True)
class ProcessResourceLimits:
    """Resource limits for bounded process execution."""
    max_cpu_seconds: float = 30.0
    max_memory_mb: int = 512
    max_virtual_memory_mb: int | None = None  # Separate virtual memory limit (for RLIMIT_AS)
    max_processes: int = 10
    max_file_descriptors: int = 256
    max_output_bytes: int = 10 * 1024 * 1024  # 10 MB


@dataclass(frozen=True, slots=True)
class ProcessExecutionRequest:
    """Request for bounded process execution."""
    command: list[str]
    working_directory: Path
    environment: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    resource_limits: ProcessResourceLimits = field(default_factory=ProcessResourceLimits)
    stdin_data: bytes | None = None
    capture_stdout: bool = True
    capture_stderr: bool = True


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    """Result of bounded process execution."""
    process_id: str
    state: ProcessState
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    started_at: float
    ended_at: float | None
    cpu_time_seconds: float | None
    peak_memory_bytes: int | None
    error_message: str | None = None
    remote_outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class ProcessReceipt:
    """Receipt for process execution settlement."""
    process_id: str
    request_digest: str
    result: ProcessExecutionResult
    settled_at: float
    authority_grant_id: str | None = None


class BoundedProcessExecutor:
    """
    Executes processes within bounded resource constraints with timeout,
    cancellation, and resource accounting. Each execution is isolated in
    its owned workspace with explicit resource limits.

    The public :meth:`execute` entry point is fail-closed: it requires an
    explicit capability grant issued by a :class:`CapabilityAuthority` and
    validates it through the shared capability seam before any subprocess is
    spawned.  The private :meth:`_execute_raw` helper performs the actual
    subprocess work and is only callable from authorization-owning consumers
    (this module's own :meth:`execute`/:meth:`_execute_code`, and the Git
    authority module, which performs its own grant validation).  Direct calls
    from any other caller are rejected at runtime so the method cannot be
    mistaken for an ambient spawn path.
    """

    def __init__(
        self,
        execution_manager: ExecutionManager | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._execution = execution_manager or ExecutionManager()
        self._clock = clock or SystemClock()
        self._ids = ids or Uuid7Factory()
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_results: dict[str, ProcessExecutionResult] = {}
        self._receipts: dict[str, ProcessReceipt] = {}
        self._cancelled_processes: set[str] = set()

    async def execute(
        self,
        request: ProcessExecutionRequest,
        *,
        authority: CapabilityAuthority | None = None,
        grant: CapabilityGrant | None = None,
    ) -> ProcessExecutionResult:
        """
        Execute a process within bounded constraints.

        Args:
            request: The execution request with command, limits, and workspace.
            authority: The capability authority that issued the grant.
            grant: A live capability grant authorizing this execution.

        Returns:
            ProcessExecutionResult with execution outcome and resource accounting.
            A missing, foreign, expired, exhausted, or out-of-scope grant is
            returned as a definite ``FAILED`` result (never ``UNKNOWN``) and no
            subprocess is spawned.
        """
        process_id = self._ids.new()
        started_at = self._clock.now().timestamp()

        # Structural authorization check before any subprocess work.
        if authority is None or grant is None:
            return self._denial_result(
                process_id,
                started_at,
                request,
                DenialReason.NO_GRANT,
                "Process execution requires an explicit authority and grant",
            )
        if not isinstance(authority, CapabilityAuthority) or not isinstance(
            grant, CapabilityGrant
        ):
            return self._denial_result(
                process_id,
                started_at,
                request,
                DenialReason.NO_GRANT,
                "Invalid authority or grant type",
            )

        try:
            # The capability seam authorizes, consumes one unit of budget, and
            # retains the effect as in-flight until the context exits.  Any
            # CapabilityDenied is raised before the subprocess starts, so no
            # process or filesystem side effect occurs.
            with authority.effect(grant, units=1, target=request.working_directory, kind=PROCESS_RUN):
                return await self._execute_raw(
                    request,
                    process_id=process_id,
                    started_at=started_at,
                    authority_grant_id=grant.grant_id,
                )
        except CapabilityDenied as exc:
            return self._denial_result(
                process_id, started_at, request, exc.reason, exc.message
            )

    def _denial_result(
        self,
        process_id: str,
        started_at: float,
        request: ProcessExecutionRequest,
        reason: DenialReason,
        message: str,
    ) -> ProcessExecutionResult:
        """Return a definite denial result and settle an in-memory receipt.

        A refusal is a definite denial: it records ``FAILED`` and never
        ``UNKNOWN``, because the effect (subprocess execution) was definitely
        prevented before it could start.
        """
        result = ProcessExecutionResult(
            process_id=process_id,
            state=ProcessState.FAILED,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            started_at=started_at,
            ended_at=self._clock.now().timestamp(),
            cpu_time_seconds=None,
            peak_memory_bytes=None,
            error_message=f"{reason.value}: {message}",
            remote_outcome_unknown=False,
        )
        self._settle(process_id, request, result, authority_grant_id=None)
        return result

    def _settle(
        self,
        process_id: str,
        request: ProcessExecutionRequest,
        result: ProcessExecutionResult,
        *,
        authority_grant_id: str | None = None,
    ) -> None:
        """Store the result and create a settlement receipt."""
        self._process_results[process_id] = result
        receipt = ProcessReceipt(
            process_id=process_id,
            request_digest=self._compute_request_digest(request),
            result=result,
            settled_at=self._clock.now().timestamp(),
            authority_grant_id=authority_grant_id,
        )
        self._receipts[process_id] = receipt

    async def _execute_raw(
        self,
        request: ProcessExecutionRequest,
        *,
        process_id: str | None = None,
        started_at: float | None = None,
        authority_grant_id: str | None = None,
    ) -> ProcessExecutionResult:
        """
        Low-level subprocess execution helper.

        This method performs the actual process spawn and accounting.  It does
        NOT perform capability authorization.  It is guarded at runtime: only
        callers from this module or from ``bots5.infrastructure.git_authority``
        (which performs its own grant validation) may invoke it.  All other
        callers receive :exc:`AuthorizationBoundaryError` before any subprocess
        is spawned.
        """
        if process_id is None:
            process_id = self._ids.new()
        if started_at is None:
            started_at = self._clock.now().timestamp()

        # Runtime enforcement: this helper performs no authorization of its
        # own, so it must only be invoked from consumers that own an
        # authorization boundary.  Reject direct calls from outside the two
        # known authorization-owning modules.
        frame = inspect.currentframe()
        caller_module = (
            frame.f_back.f_globals.get("__name__") if frame and frame.f_back else None
        )
        if caller_module not in {
            "bots5.infrastructure.process_execution",
            "bots5.infrastructure.git_authority",
        }:
            raise AuthorizationBoundaryError(
                "_execute_raw must be called from an authorization boundary; "
                "use the public execute() entry point or an authorized internal consumer"
            )

        # Prepare environment
        env = os.environ.copy()
        env.update(request.environment)

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        cpu_time = 0.0
        peak_memory = 0

        # RUSAGE_CHILDREN is CUMULATIVE across every child this process has ever
        # reaped, so using it directly charges this execution for all previous
        # ones (observed: a 0.1s-timeout `sleep` reported "CPU time limit
        # exceeded: 358.64s"). Snapshot the baseline here and always work with
        # the DELTA that is attributable to this child.
        try:
            _usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            _cpu_before = _usage_before.ru_utime + _usage_before.ru_stime
            _maxrss_before = _usage_before.ru_maxrss
        except Exception:
            _cpu_before = 0.0
            _maxrss_before = 0

        # Create process with resource limits
        # Use DEVNULL for stdin when no stdin_data is provided to prevent
        # child processes (e.g., Node.js) from waiting on inherited stdin.
        # This ensures processes exit when they finish writing output.
        stdin_mode = asyncio.subprocess.PIPE if request.stdin_data else subprocess.DEVNULL
        try:
            proc = await asyncio.create_subprocess_exec(
                *request.command,
                cwd=request.working_directory,
                env=env,
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE if request.capture_stdout else None,
                stderr=asyncio.subprocess.PIPE if request.capture_stderr else None,
                preexec_fn=self._setup_resource_limits(request.resource_limits),
            )
        except Exception as exc:
            result = ProcessExecutionResult(
                process_id=process_id,
                state=ProcessState.FAILED,
                exit_code=None,
                stdout=b"",
                stderr=b"",
                started_at=started_at,
                ended_at=self._clock.now().timestamp(),
                cpu_time_seconds=None,
                peak_memory_bytes=None,
                error_message=f"Failed to spawn process: {exc}",
                remote_outcome_unknown=False,
            )
            self._settle(process_id, request, result, authority_grant_id=authority_grant_id)
            return result

        self._active_processes[process_id] = proc

        try:
            # Handle stdin
            if request.stdin_data and proc.stdin:
                proc.stdin.write(request.stdin_data)
                await proc.stdin.drain()
                proc.stdin.close()

            # Read stdout/stderr concurrently so the child never blocks on a
            # full pipe, even for large outputs.
            stdout_task: asyncio.Task | None = None
            stderr_task: asyncio.Task | None = None
            if proc.stdout:
                stdout_task = asyncio.create_task(
                    self._read_stream(proc.stdout, stdout_chunks),
                    name=f"stdout-{process_id}",
                )
            if proc.stderr:
                stderr_task = asyncio.create_task(
                    self._read_stream(proc.stderr, stderr_chunks),
                    name=f"stderr-{process_id}",
                )
            stream_tasks = [t for t in (stdout_task, stderr_task) if t is not None]

            try:
                if request.timeout_seconds is None:
                    await proc.wait()
                else:
                    deadline = (
                        asyncio.get_running_loop().time() + request.timeout_seconds
                    )
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            proc.terminate()
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5.0)
                            except asyncio.TimeoutError:
                                proc.kill()
                                await proc.wait()
                            raise ResourceLimitExceeded(
                                f"Process timeout after {request.timeout_seconds}s"
                            )

                        try:
                            await asyncio.wait_for(
                                proc.wait(), timeout=min(0.1, max(0.0, remaining))
                            )
                            break
                        except asyncio.TimeoutError:
                            # Still running; check per-child resource usage.
                            try:
                                usage = resource.getrusage(resource.RUSAGE_CHILDREN)
                                # Deltas attributable to THIS child only.
                                cpu_time = max(
                                    0.0,
                                    (usage.ru_utime + usage.ru_stime) - _cpu_before,
                                )
                                peak_memory = max(
                                    0, (usage.ru_maxrss - _maxrss_before) * 1024
                                )  # Linux: KiB -> bytes
                                if cpu_time > request.resource_limits.max_cpu_seconds:
                                    proc.terminate()
                                    raise ResourceLimitExceeded(
                                        f"CPU time limit exceeded: {cpu_time:.2f}s"
                                    )
                                if (
                                    peak_memory
                                    > request.resource_limits.max_memory_mb * 1024 * 1024
                                ):
                                    proc.terminate()
                                    raise ResourceLimitExceeded(
                                        f"Memory limit exceeded: {peak_memory / (1024 * 1024):.2f} MB"
                                    )
                            except (ResourceLimitExceeded, ProcessLookupError):
                                raise
                            except Exception:
                                # Resource tracking failed, continue.
                                pass
                            continue
            finally:
                # Ensure stream readers have drained before we build the result.
                if stream_tasks:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*stream_tasks), timeout=2.0
                        )
                    except asyncio.TimeoutError:
                        for t in stream_tasks:
                            t.cancel()
                        await asyncio.gather(*stream_tasks, return_exceptions=True)

            exit_code = proc.returncode
            ended_at = self._clock.now().timestamp()

            was_cancelled = process_id in self._cancelled_processes
            if was_cancelled:
                self._cancelled_processes.discard(process_id)

            # Final resource accounting (delta attributable to THIS child;
            # RUSAGE_CHILDREN is cumulative across all reaped children).
            try:
                usage = resource.getrusage(resource.RUSAGE_CHILDREN)
                cpu_time = max(
                    0.0, (usage.ru_utime + usage.ru_stime) - _cpu_before
                )
                peak_memory = max(0, (usage.ru_maxrss - _maxrss_before) * 1024)
            except Exception:
                pass

            stdout_bytes = b"".join(stdout_chunks)
            stderr_bytes = b"".join(stderr_chunks)

            # Check output size limits
            if len(stdout_bytes) > request.resource_limits.max_output_bytes:
                stdout_bytes = (
                    stdout_bytes[: request.resource_limits.max_output_bytes]
                    + b"\n... [TRUNCATED: output limit exceeded]"
                )
            if len(stderr_bytes) > request.resource_limits.max_output_bytes:
                stderr_bytes = (
                    stderr_bytes[: request.resource_limits.max_output_bytes]
                    + b"\n... [TRUNCATED: output limit exceeded]"
                )

            if was_cancelled:
                state = ProcessState.CANCELLED
                error_message = "Process was cancelled"
            elif exit_code == 0:
                state = ProcessState.COMPLETED
                error_message = None
            else:
                state = ProcessState.FAILED
                error_message = f"Process exited with code {exit_code}"

            result = ProcessExecutionResult(
                process_id=process_id,
                state=state,
                exit_code=exit_code if not was_cancelled else None,
                stdout=stdout_bytes,
                stderr=stderr_bytes,
                started_at=started_at,
                ended_at=ended_at,
                cpu_time_seconds=cpu_time,
                # Portable POSIX rusage exposes only ru_maxrss, a HIGH-WATER MARK
                # across every child this process has ever reaped. A delta against
                # a baseline is therefore NOT a per-child peak: a child that stays
                # below the previous maximum measures 0, and the first child can be
                # credited with 60+ MB it never used. Rather than report false
                # precision, per-child peak memory is reported as unmeasurable.
                # Memory bounds are still ENFORCED (RLIMIT_DATA/RLIMIT_RSS and the
                # mid-flight check); only the measurement is unavailable.
                peak_memory_bytes=None,
                error_message=error_message,
                remote_outcome_unknown=was_cancelled,
            )

        except ResourceLimitExceeded as exc:
            ended_at = self._clock.now().timestamp()
            state = (
                ProcessState.TIMEOUT
                if "timeout" in str(exc).lower()
                else ProcessState.FAILED
            )
            result = ProcessExecutionResult(
                process_id=process_id,
                state=state,
                exit_code=None,
                stdout=b"".join(stdout_chunks),
                stderr=b"".join(stderr_chunks),
                started_at=started_at,
                ended_at=ended_at,
                cpu_time_seconds=cpu_time,
                # Portable POSIX rusage exposes only ru_maxrss, a HIGH-WATER MARK
                # across every child this process has ever reaped. A delta against
                # a baseline is therefore NOT a per-child peak: a child that stays
                # below the previous maximum measures 0, and the first child can be
                # credited with 60+ MB it never used. Rather than report false
                # precision, per-child peak memory is reported as unmeasurable.
                # Memory bounds are still ENFORCED (RLIMIT_DATA/RLIMIT_RSS and the
                # mid-flight check); only the measurement is unavailable.
                peak_memory_bytes=None,
                error_message=str(exc),
                # A process we terminated because it exceeded a CPU/memory bound,
                # or because it outlived its deadline, was killed MID-FLIGHT. That
                # proves nothing about whether it had already applied its effect,
                # so the outcome must be reported as unknown rather than as a
                # definite no-op. (Previously this was only True for the timeout
                # branch, so a CPU/memory kill claimed a definite outcome.)
                remote_outcome_unknown=True,
            )
        except asyncio.CancelledError:
            ended_at = self._clock.now().timestamp()
            proc.kill()
            await proc.wait()
            result = ProcessExecutionResult(
                process_id=process_id,
                state=ProcessState.CANCELLED,
                exit_code=None,
                stdout=b"".join(stdout_chunks),
                stderr=b"".join(stderr_chunks),
                started_at=started_at,
                ended_at=ended_at,
                cpu_time_seconds=cpu_time,
                # Portable POSIX rusage exposes only ru_maxrss, a HIGH-WATER MARK
                # across every child this process has ever reaped. A delta against
                # a baseline is therefore NOT a per-child peak: a child that stays
                # below the previous maximum measures 0, and the first child can be
                # credited with 60+ MB it never used. Rather than report false
                # precision, per-child peak memory is reported as unmeasurable.
                # Memory bounds are still ENFORCED (RLIMIT_DATA/RLIMIT_RSS and the
                # mid-flight check); only the measurement is unavailable.
                peak_memory_bytes=None,
                error_message="Process was cancelled",
                remote_outcome_unknown=True,
            )
            raise
        except Exception as exc:
            ended_at = self._clock.now().timestamp()
            result = ProcessExecutionResult(
                process_id=process_id,
                state=ProcessState.UNKNOWN,
                exit_code=None,
                stdout=b"".join(stdout_chunks),
                stderr=b"".join(stderr_chunks),
                started_at=started_at,
                ended_at=ended_at,
                cpu_time_seconds=cpu_time,
                # Portable POSIX rusage exposes only ru_maxrss, a HIGH-WATER MARK
                # across every child this process has ever reaped. A delta against
                # a baseline is therefore NOT a per-child peak: a child that stays
                # below the previous maximum measures 0, and the first child can be
                # credited with 60+ MB it never used. Rather than report false
                # precision, per-child peak memory is reported as unmeasurable.
                # Memory bounds are still ENFORCED (RLIMIT_DATA/RLIMIT_RSS and the
                # mid-flight check); only the measurement is unavailable.
                peak_memory_bytes=None,
                error_message=f"Execution error: {exc}",
                remote_outcome_unknown=True,
            )
        finally:
            self._active_processes.pop(process_id, None)
            self._settle(process_id, request, result, authority_grant_id=authority_grant_id)

        return result

    async def _read_stream(
        self, stream: asyncio.StreamReader | None, chunks: list[bytes]
    ) -> None:
        """Drain a stream into chunks until EOF."""
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(65536)
            except Exception:
                break
            if not chunk:
                break
            chunks.append(chunk)

    def _setup_resource_limits(self, limits: ProcessResourceLimits):
        """Create a preexec function to set resource limits."""
        def set_limits():
            try:
                # CPU time limit (seconds)
                resource.setrlimit(resource.RLIMIT_CPU, (int(limits.max_cpu_seconds), int(limits.max_cpu_seconds) + 1))
                # Memory limit - use RLIMIT_DATA for data segment, RLIMIT_RSS for resident set size
                # Avoid RLIMIT_AS (virtual memory) as it breaks V8/Node.js which needs large virtual address space
                memory_bytes = limits.max_memory_mb * 1024 * 1024
                try:
                    resource.setrlimit(resource.RLIMIT_DATA, (memory_bytes, memory_bytes))
                except Exception:
                    pass
                try:
                    resource.setrlimit(resource.RLIMIT_RSS, (memory_bytes, memory_bytes))
                except Exception:
                    pass
                # Optional virtual memory limit (only if explicitly set)
                if limits.max_virtual_memory_mb is not None:
                    vm_bytes = limits.max_virtual_memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (vm_bytes, vm_bytes))
                # Process/thread limit.
                #
                # RLIMIT_NPROC is NOT a per-child "how many children may I
                # spawn" budget: it is an absolute cap on the number of
                # processes/threads the REAL UID may have system-wide. Setting
                # it to a small absolute value therefore fails as soon as the
                # user's live process count approaches it, and the failure
                # surfaces inside the child as "cannot fork()/pthread_create:
                # Resource temporarily unavailable" (EAGAIN). That broke
                # legitimate work: `git commit`/`git push` forks
                # (unpack-objects) and Node/V8 spawns threads at startup, so
                # both intermittently died under concurrent test load.
                #
                # Apply it only when the caller asks for a value that is
                # meaningful relative to the user's CURRENT process count;
                # otherwise skip it (best-effort, same as the other limits).
                if limits.max_processes is not None:
                    try:
                        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_NPROC)
                    except (ValueError, OSError):
                        current_soft, current_hard = resource.RLIM_INFINITY, resource.RLIM_INFINITY
                    headroom = limits.max_processes
                    if current_soft != resource.RLIM_INFINITY:
                        headroom = current_soft + limits.max_processes
                    target = headroom
                    if current_hard != resource.RLIM_INFINITY:
                        target = min(target, current_hard)
                    if target > 0:
                        resource.setrlimit(resource.RLIMIT_NPROC, (target, target))
                # File descriptor limit
                resource.setrlimit(resource.RLIMIT_NOFILE, (limits.max_file_descriptors, limits.max_file_descriptors))
            except Exception:
                # Best effort; limits may not be available on all platforms
                pass
        return set_limits

    def _compute_request_digest(self, request: ProcessExecutionRequest) -> str:
        """Compute a digest of the request for receipt binding."""
        import hashlib
        content = f"{request.command}|{request.working_directory}|{request.timeout_seconds}|{request.resource_limits.max_cpu_seconds}|{request.resource_limits.max_memory_mb}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get_result(self, process_id: str) -> ProcessExecutionResult | None:
        """Get the result of a completed process."""
        return self._process_results.get(process_id)

    def get_receipt(self, process_id: str) -> ProcessReceipt | None:
        """Get the receipt for a completed process."""
        return self._receipts.get(process_id)

    async def cancel(self, process_id: str) -> bool:
        """Cancel a running process."""
        proc = self._active_processes.get(process_id)
        if proc is None:
            return False
        self._cancelled_processes.add(process_id)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return True

    async def shutdown(self) -> None:
        """Shutdown the executor, cancelling all active processes."""
        for proc in self._active_processes.values():
            proc.terminate()
        if self._active_processes:
            await asyncio.gather(
                *(proc.wait() for proc in self._active_processes.values()),
                return_exceptions=True,
            )
        self._active_processes.clear()


@asynccontextmanager
async def bounded_workspace(
    base_path: Path,
    *,
    prefix: str = "bots5-workspace-",
    cleanup: bool = True,
) -> AsyncIterator[Path]:
    """
    Create a bounded workspace directory for process execution.

    The workspace is a disposable directory tree that can be used for
    isolated process execution. It is cleaned up on exit unless cleanup=False.

    Args:
        base_path: Base directory for workspace creation.
        prefix: Prefix for the workspace directory name.
        cleanup: Whether to clean up the workspace on exit.

    Yields:
        Path to the created workspace directory.
    """
    workspace = Path(tempfile.mkdtemp(prefix=prefix, dir=base_path))
    try:
        yield workspace
    finally:
        if cleanup:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class CodeExecutionRequest:
    """Request for bounded code execution."""
    code: str
    language: str  # e.g., "python", "bash", "javascript"
    working_directory: Path
    timeout_seconds: float = 30.0
    resource_limits: ProcessResourceLimits = field(default_factory=ProcessResourceLimits)
    files: dict[str, str] = field(default_factory=dict)  # Additional files to write


class CodeExecutor:
    """
    Executes code snippets in a bounded workspace with timeout,
    cancellation, and resource accounting. Supports multiple languages
    through language-specific execution strategies.

    Like :class:`BoundedProcessExecutor`, the public :meth:`execute` entry
    point is fail-closed: it requires an explicit capability grant and
    validates it through the shared capability seam before any code is written
    to disk or any subprocess is spawned.
    """

    def __init__(
        self,
        process_executor: BoundedProcessExecutor | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._process_executor = process_executor or BoundedProcessExecutor(clock=clock, ids=ids)
        self._clock = clock or SystemClock()
        self._ids = ids or Uuid7Factory()

    async def execute(
        self,
        request: CodeExecutionRequest,
        *,
        authority: CapabilityAuthority | None = None,
        grant: CapabilityGrant | None = None,
    ) -> ProcessExecutionResult:
        """
        Execute code in a bounded workspace.

        Args:
            request: The code execution request.
            authority: The capability authority that issued the grant.
            grant: A live capability grant authorizing this execution.

        Returns:
            ProcessExecutionResult with execution outcome.  A missing or
            invalid grant is returned as a definite ``FAILED`` result and no
            code is written or executed.
        """
        process_id = self._ids.new()
        started_at = self._clock.now().timestamp()

        if authority is None or grant is None:
            return self._denial_result(
                process_id,
                started_at,
                DenialReason.NO_GRANT,
                "Code execution requires an explicit authority and grant",
            )
        if not isinstance(authority, CapabilityAuthority) or not isinstance(
            grant, CapabilityGrant
        ):
            return self._denial_result(
                process_id,
                started_at,
                DenialReason.NO_GRANT,
                "Invalid authority or grant type",
            )

        try:
            # Authorization happens before writing code files or spawning the
            # interpreter, so a refusal produces no disk or process side effect.
            with authority.effect(grant, units=1, target=request.working_directory, kind=WORKSPACE_WRITE):
                return await self._execute_code(
                    request,
                    process_id=process_id,
                    started_at=started_at,
                    authority_grant_id=grant.grant_id,
                )
        except CapabilityDenied as exc:
            return self._denial_result(
                process_id, started_at, exc.reason, exc.message
            )

    def _denial_result(
        self,
        process_id: str,
        started_at: float,
        reason: DenialReason,
        message: str,
    ) -> ProcessExecutionResult:
        """Return a definite denial result for code execution."""
        return ProcessExecutionResult(
            process_id=process_id,
            state=ProcessState.FAILED,
            exit_code=None,
            stdout=b"",
            stderr=b"",
            started_at=started_at,
            ended_at=self._clock.now().timestamp(),
            cpu_time_seconds=None,
            peak_memory_bytes=None,
            error_message=f"{reason.value}: {message}",
            remote_outcome_unknown=False,
        )

    async def _execute_code(
        self,
        request: CodeExecutionRequest,
        *,
        process_id: str,
        started_at: float,
        authority_grant_id: str | None = None,
    ) -> ProcessExecutionResult:
        """Write code files and dispatch to the process executor."""
        # Write code and additional files to workspace
        workspace = request.working_directory
        workspace.mkdir(parents=True, exist_ok=True)

        # Determine execution command based on language
        command = self._get_execution_command(request.language, request.code, workspace)

        # Write additional files
        for filename, content in request.files.items():
            file_path = workspace / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)

        # Adjust resource limits for languages that need more memory (e.g., Node.js/V8)
        resource_limits = request.resource_limits
        if request.language in ("javascript", "node"):
            # Node.js/V8 needs large virtual address space; don't restrict RLIMIT_DATA/RSS
            # Use a very high memory limit and no virtual memory limit
            resource_limits = ProcessResourceLimits(
                max_cpu_seconds=resource_limits.max_cpu_seconds,
                max_memory_mb=2048,  # 2GB for data/RSS
                max_virtual_memory_mb=None,  # No virtual memory limit
                max_processes=resource_limits.max_processes,
                max_file_descriptors=resource_limits.max_file_descriptors,
                max_output_bytes=resource_limits.max_output_bytes,
            )

        process_request = ProcessExecutionRequest(
            command=command,
            working_directory=workspace,
            timeout_seconds=request.timeout_seconds,
            resource_limits=resource_limits,
            capture_stdout=True,
            capture_stderr=True,
        )

        return await self._process_executor._execute_raw(
            process_request,
            process_id=process_id,
            started_at=started_at,
            authority_grant_id=authority_grant_id,
        )

    def _get_execution_command(self, language: str, code: str, workspace: Path) -> list[str]:
        """Get the execution command for a given language."""
        if language == "python":
            script_path = workspace / "script.py"
            script_path.write_text(code)
            return ["python3", str(script_path)]
        elif language == "bash":
            script_path = workspace / "script.sh"
            script_path.write_text(code)
            script_path.chmod(0o755)
            return ["bash", str(script_path)]
        elif language == "javascript" or language == "node":
            script_path = workspace / "script.js"
            script_path.write_text(code)
            return ["node", str(script_path)]
        elif language == "sh":
            script_path = workspace / "script.sh"
            script_path.write_text(code)
            script_path.chmod(0o755)
            return ["sh", str(script_path)]
        else:
            raise StateError(f"Unsupported language: {language}")

    def get_process_executor(self) -> BoundedProcessExecutor:
        """Get the underlying process executor."""
        return self._process_executor
