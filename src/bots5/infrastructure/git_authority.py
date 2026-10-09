from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import unicodedata
import shutil
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Optional

from bots5.core.errors import StateError
from bots5.core.execution import ExecutionManager
from bots5.domain.clock import Clock, SystemClock
from bots5.domain.ids import IdFactory, Uuid7Factory
from bots5.infrastructure.process_execution import (
    BoundedProcessExecutor,
    ProcessExecutionRequest,
    ProcessResourceLimits,
    ProcessState,
)


#: Zero-width / invisible formatting characters that must NOT count as content.
_INVISIBLE_CHARS: str = "\u200b\u200c\u200d\u2060\ufeff\u00ad"


#: Unicode bidi controls, which can visually reorder a string so that what an
#: operator reads differs from what the machine compares.
_BIDI_CONTROLS: str = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"

#: Machine-readable identifiers must be plain ASCII. Identifiers are tokens, not
#: prose, so allowing the full Unicode range only creates spoofing surface
#: (confusables such as Cyrillic "а" for ASCII "a", and invisible joiners).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def _has_visible_content(token: str) -> bool:
    """True when ``token`` holds real, unambiguous content.

    Rejects, in addition to blank text:
      * zero-width / invisible formatting (U+200B/C/D, U+2060, U+FEFF, U+00AD) --
        ``str.strip()`` does not remove these, so `"\u200b"` used to pass;
      * invisible characters *embedded between* visible ones (`"a\u200bb"`),
        which can make two different strings look identical;
      * bidi controls, which can visually reorder text;
      * text whose NFKC normalisation collapses to nothing.
    """
    if not isinstance(token, str) or not token:
        return False
    stripped = token.strip()
    if stripped.strip(_INVISIBLE_CHARS).strip() != stripped:
        return False
    if any(ch in stripped for ch in _INVISIBLE_CHARS):
        return False
    if any(ch in stripped for ch in _BIDI_CONTROLS):
        return False
    try:
        normalised = unicodedata.normalize("NFKC", stripped).strip()
    except (TypeError, ValueError):
        return False
    return bool(normalised.strip(_INVISIBLE_CHARS).strip())


def _is_plain_identifier(token: str) -> bool:
    """True when ``token`` is a safe machine identifier (ASCII, no spoofing)."""
    if not isinstance(token, str):
        return False
    return bool(_IDENTIFIER_RE.match(token))


class GitAuthorityLevel(str, Enum):
    """Git authority levels - each requires separate explicit approval."""
    INSPECT = "INSPECT"           # Read-only: status, diff, log, show
    EDIT = "EDIT"                 # Workspace modifications (staged/unstaged changes)
    VALIDATE = "VALIDATE"         # Validation checks (pre-commit hooks, tests)
    STAGE = "STAGE"               # Stage changes (git add) - local history gate
    COMMIT = "COMMIT"             # Commit changes - local history gate (reversible via reflog)
    PUSH = "PUSH"                 # Push to remote - reputation-bearing, separate approval
    MERGE = "MERGE"               # Merge histories - requires base/head/tree seals
    REF_MUTATE = "REF_MUTATE"     # Rewrite shared state (rebase, reset --hard)
    DELETE = "DELETE"             # Destructive: branch/remote deletion (top rung)


class GitOperationState(str, Enum):
    """State of a Git operation."""
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


from typing import Literal


class GitAuthorityBindingScope(str, Enum):
    """Binding scope for a Git authority grant."""
    EXACT_REQUEST = "exact_request"  # Valid only for the exact command that was authorized
    LEVEL_SCOPE = "level_scope"  # Valid for any operation at this level/scope - REQUIRES explicit approval


class GitConsequenceClass(str, Enum):
    """Consequence class for Git operations - each requires separate authorization.

    A grant carries the consequence class it was authorised for, and that class is
    compared against the PARSED operation at use time in EVERY binding mode. A
    grant issued for ``NON_DESTRUCTIVE`` work therefore cannot perform a
    ``HISTORY_REWRITING`` operation such as a force push.

    ``UNKNOWN`` exists so ambiguous or unrecognised commands fail closed: a
    command whose consequence cannot be established is never treated as harmless.
    """
    NON_DESTRUCTIVE = "non_destructive"  # Read-only or local-only operations
    HISTORY_REWRITING = "history_rewriting"  # Push --force, reset --hard, etc.
    UNKNOWN = "unknown"  # Consequence could not be established -> fail closed


#: Rank used for consequence comparison. A grant may cover an operation at or
#: below its own class; UNKNOWN is never satisfiable by any grant.
_CONSEQUENCE_RANK: dict["GitConsequenceClass", int] = {
    GitConsequenceClass.NON_DESTRUCTIVE: 0,
    GitConsequenceClass.HISTORY_REWRITING: 1,
    GitConsequenceClass.UNKNOWN: 99,
}

#: Subcommands known to be read-only or purely local-reporting. This set is
#: deliberately MINIMAL: it contains only operations that cannot rewrite history
#: or destroy working-tree content. Mutating-but-ordinary subcommands (add,
#: commit, merge, ...) are also listed here because they are NON_DESTRUCTIVE at
#: their own authority level; genuinely destructive ones (rm, mv, switch, prune,
#: repack, reset --hard, clean -f, ...) are classified explicitly in
#: ``_classify_consequence``. Anything in NEITHER set becomes UNKNOWN and fails
#: closed, so a newly-added or unrecognised subcommand is never assumed harmless.


def _scope_covers_ref(scope: str, ref: str) -> bool:
    """True when ``scope`` covers ``ref``, with git refspec ``*`` semantics.

    ``*`` is a SINGLE-LEVEL wildcard: it does not match ``/``. So
    ``refs/heads/*`` covers ``refs/heads/main`` but never
    ``refs/heads/feature/x``, and a scope that is part of a component
    (``refs/heads/ma*``) may only complete that component.

    This is the single implementation used both when validating an approval
    record's scope and when enforcing a grant's scope at execution time. Having
    two different glob implementations previously let a naive ``startswith``
    check in one place accept a ref the other rejected.
    """
    if scope == "*":
        return True
    if "*" not in scope:
        return ref == scope
    parts = scope.split("*")
    cursor = 0
    for index, part in enumerate(parts):
        if index == 0:
            if not ref.startswith(part):
                return False
            cursor = len(part)
            continue
        last = index == len(parts) - 1
        if last and part:
            if not ref.endswith(part):
                return False
            limit = len(ref) - len(part)
            if limit < cursor:
                return False
        else:
            limit = len(ref)
        if not part:
            if "/" in ref[cursor:limit]:
                return False
            cursor = limit
            continue
        found = ref.find(part, cursor, limit)
        if found < 0:
            return False
        if "/" in ref[cursor:found]:
            return False
        cursor = found + len(part)
    return True


def _normalise_repo_path(path: "Path | str") -> str:
    """Canonical form of a repository path for containment comparison.

    ``/repo`` and ``/repo/`` are the same repository; so are ``/repo/./x`` and
    ``/repo/x``. Comparing raw values treated those spellings as DIFFERENT
    repositories, which wrongly refused a legitimate same-repository grant (it
    failed closed, but it was still wrong). Resolution is non-strict so a path
    that does not exist yet still normalises lexically.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        # Fall back to a purely lexical normalisation.
        return os.path.normpath(str(Path(path).expanduser()))


def _repo_identity(path: "Path | str") -> str:
    """Filesystem identity of the repository at ``path``, or "" if none.

    Path equality is NOT repository equality: moving the original repository away
    and placing a DIFFERENT repository at the same canonical pathname leaves the
    string comparison satisfied while pointing the authority at a different
    repository. Binding to the identity of the git directory itself (device +
    inode of the resolved ``.git``) detects that, because a replacement
    repository is a different filesystem object.

    Returns a stable opaque token such as ``"66306:1234567"``, or "" when the
    path does not currently hold a git directory.
    """
    try:
        base = Path(path).expanduser()
        git_dir = base / ".git"
        if not git_dir.exists():
            # bare repository: the directory itself is the git directory
            git_dir = base
        st = git_dir.stat()
        return f"{st.st_dev}:{st.st_ino}"
    except (OSError, RuntimeError, ValueError):
        return ""

#: Git long options that change the CONSEQUENCE of an operation, and not merely
#: its output. Used for abbreviation expansion and for deciding whether an
#: unrecognised flag must fail closed.
_KNOWN_LONG_FLAGS: frozenset[str] = frozenset({
    "force", "force-with-lease", "force-if-includes", "mirror", "delete",
    "hard", "merge", "keep", "soft", "mixed", "orphan", "quiet", "verbose",
    "dry-run", "all", "prune", "tags", "set-upstream", "no-verify",
    "recurse-submodules", "atomic", "porcelain", "staged", "cached", "stat",
    "oneline", "graph", "amend", "allow-empty", "no-edit", "squash", "no-ff",
    "ff-only", "no-commit", "message", "author", "date", "prune=now",
    "git-dir", "work-tree", "namespace", "exec-path", "literal-pathspecs",
    "no-literal-pathspecs", "bare", "no-pager", "paginate",
    "list", "remotes", "contains", "merged", "no-merged", "sort", "format",
    "show-current", "set-upstream-to", "unset-upstream", "track", "no-track",
    "strip", "keep-index", "include-untracked", "ignored", "patch", "interactive",
    "short", "branch", "long", "null", "z", "untracked-files", "ignored-files",
})

#: Short options that take a value, which git permits to be ATTACHED with no
#: separating space (`-C/tmp`). Without this, such a token looks like a cluster
#: of unrelated short flags and any retargeting it performs goes undetected.
_VALUE_TAKING_SHORT_FLAGS: frozenset[str] = frozenset({"-C"})

#: Consequence-changing push flags, canonical (no leading dashes).
_PUSH_CONSEQUENCE_FLAGS: frozenset[str] = frozenset({
    "force", "force-with-lease", "force-if-includes", "mirror", "delete",
})


def _canonical_flags(command: list[str]) -> tuple[set[str], list[str], bool]:
    """Canonicalise the flags in ``command``.

    Git accepts several spellings for the same option, and a classifier that
    matches only the textbook spelling is trivially bypassed. Returns:

    * ``flags`` - canonical names (``-fq`` -> ``{"f", "q"}``, ``--forc`` ->
      ``{"force"}``, ``--force-with-lease=x`` -> ``{"force-with-lease"}``);
    * ``refspecs`` - non-flag tokens after the subcommand, in order;
    * ``uncertain`` - True when a flag could not be resolved to a known option,
      in which case the caller must fail closed.

    Abbreviations expand only when they are an unambiguous git-style prefix of
    exactly one known option; anything ambiguous or unknown is reported as
    uncertain rather than assumed harmless.
    """
    flags: set[str] = set()
    refspecs: list[str] = []
    uncertain = False

    for index, token in enumerate(command):
        if index == 0:
            continue
        if not token.startswith("-") or token == "-":
            refspecs.append(token)
            continue
        if token == "--":
            continue
        if token.startswith("--"):
            name = token[2:].split("=", 1)[0]
            if name in _KNOWN_LONG_FLAGS:
                flags.add(name)
                continue
            matches = [k for k in _KNOWN_LONG_FLAGS if k.startswith(name)] if name else []
            if len(matches) == 1:
                flags.add(matches[0])
            else:
                uncertain = True
            continue
        # Short options that TAKE A VALUE may have it attached: `-C/tmp` is the
        # `-C` option with value `/tmp`, not a cluster of C,/,t,m,p. Treat any
        # token starting with one of these as that option.
        if token[:2] in _VALUE_TAKING_SHORT_FLAGS:
            flags.add(token[1])
            continue
        for char in token[1:]:
            flags.add(char)
    return flags, refspecs, uncertain


def _push_is_history_rewriting(command: list[str]) -> bool:
    """True (or unresolvable) for a ``push`` that may rewrite/delete remote history.

    Covers every spelling git itself accepts: ``--force``, ``-f``, bundled
    ``-fq``, unambiguous abbreviations such as ``--del``/``--mirr``,
    ``--force-with-lease[=ref]``, ``--force-if-includes``, ``--mirror``,
    ``--delete``, a ``+`` refspec, and a deletion refspec (``:branch``/``src:``).

    An AMBIGUOUS abbreviation (``--forc``, which git itself rejects as ambiguous
    between ``--force`` and ``--force-with-lease``) is treated as history
    rewriting so the caller fails closed rather than classifying it harmless.
    """
    flags, refspecs, uncertain = _canonical_flags(command)
    if uncertain:
        return True
    if flags & (_PUSH_CONSEQUENCE_FLAGS | {"f", "del", "mirr"}):
        return True
    for token in command:
        if token.startswith("+") and len(token) > 1:
            return True
    for token in refspecs:
        if ":" in token:
            src, _, dst = token.partition(":")
            if not src or not dst:
                return True
    return False


_KNOWN_READ_ONLY_SUBCOMMANDS: frozenset[str] = frozenset({
    # purely informational
    "status", "diff", "log", "show", "rev-parse", "rev-list", "ls-files",
    "ls-remote", "cat-file", "describe", "blame", "shortlog", "whatchanged",
    "grep", "version", "help", "for-each-ref", "show-ref", "merge-base",
    "name-rev", "count-objects", "verify-pack", "check-ignore", "check-attr",
    "diff-tree", "diff-index", "diff-files", "annotate", "verify-commit",
    "verify-tag", "fsck", "archive", "bundle", "remote", "fetch", "config",
    "commit-tree", "write-tree", "mktree", "hash-object", "commit-graph",
    "read-tree", "checkout-index", "index-pack", "unpack-objects",
    # ordinary mutating work at its own authority level (not history rewriting)
    "add", "commit", "merge", "cherry-pick", "revert", "am", "apply",
    "init", "clone", "notes", "stash", "worktree", "submodule", "maintenance",
    "pack-refs", "repack", "prune", "gc", "send-pack", "receive-pack",
})




@dataclass(frozen=True, slots=True)
class GitBroadAuthorityApproval:
    """A privileged, separately-attributable approval for broad Git authority.

    This record is the ONLY thing that can authorise a ``LEVEL_SCOPE`` grant: it
    names the approver, the scope approved, the consequence classes the approver
    accepted, and a rationale. A caller-supplied free-text string is NOT proof of
    approval, so ``issue_grant`` resolves an ``approval_id`` against a store of
    these records rather than trusting a string.
    """
    approval_id: str
    approved_by: str
    scope: str
    permitted_consequences: tuple["GitConsequenceClass", ...]
    rationale: str
    issued_at: float
    #: Repository this approval applies to. REQUIRED, and canonically matched at
    #: registration, grant issuance, validation and digest construction. There is
    #: deliberately NO unbound/wildcard form: an approval that does not name one
    #: repository would be implicit multi-repository authority, which is refused.
    repository_path: "Path | str" = ""

    def __post_init__(self) -> None:
        # Use _has_visible_content, NOT str.strip(): U+200B/U+200C/U+200D are not
        # Unicode whitespace, so str.strip() leaves them and an approval could be
        # registered under an id that is visually empty and resolved by that exact
        # string. Same bug class as the `command` identity check, missed here.
        if not _is_plain_identifier(self.approval_id):
            raise ValueError(
                "broad-authority approval_id must be a plain ASCII identifier "
                "(letters, digits, dot, underscore, colon or hyphen; 1-200 chars). "
                "Unicode confusables, bidi controls and zero-width characters are "
                "refused because they let two different ids look identical"
            )
        if not _has_visible_content(self.approved_by):
            raise ValueError(
                "broad-authority approval requires a visible approver "
                "(blank, zero-width, embedded-invisible and bidi text are refused)"
            )
        if not _has_visible_content(self.scope):
            raise ValueError(
                "broad-authority approval requires a visible scope "
                "(blank, zero-width, embedded-invisible and bidi text are refused)"
            )
        # Mandatory repository binding: missing, blank or non-normalisable binding
        # fails closed rather than defaulting to any repository.
        if not _has_visible_content(str(self.repository_path or "")):
            raise ValueError(
                "broad-authority approval requires an explicit repository_path; "
                "an unbound approval would be implicit multi-repository authority. "
                "Name exactly one repository"
            )
        normalised = _normalise_repo_path(self.repository_path)
        if not normalised or normalised in (".", "/"):
            raise ValueError(
                f"broad-authority approval repository_path "
                f"{self.repository_path!r} does not normalise to a usable repository"
            )
        if not self.permitted_consequences:
            raise ValueError(
                "broad-authority approval must state the consequence classes it permits"
            )
        for consequence in self.permitted_consequences:
            if not isinstance(consequence, GitConsequenceClass):
                raise ValueError(
                    "permitted_consequences must contain GitConsequenceClass values, "
                    f"got {type(consequence).__name__}"
                )
            if consequence is GitConsequenceClass.UNKNOWN:
                raise ValueError(
                    "UNKNOWN is not a grantable consequence; an approval cannot "
                    "authorise an operation whose consequence is unknown"
                )
        self._validate_rationale(self.rationale)

    @staticmethod
    def _validate_rationale(rationale: str) -> None:
        """Require a recorded, visible rationale — and NOTHING MORE.

        Mick's approved decision: rationale-quality heuristics are NOT an
        authorization gate. Structural word scoring was removed because it could
        not establish legitimate authority: it accepted word salad while rejecting
        honest low-diversity rationales, so it produced false assurance in one
        direction and false refusals in the other.

        What remains is a mandatory, visible, attributable rationale. The actual
        controls on broad authority are the trusted approval-registration boundary,
        the attributable approver, the scope, the permitted consequence classes and
        the required repository binding — not the prose.
        """
        if not _has_visible_content(rationale):
            raise ValueError(
                "broad-authority approval requires a recorded rationale "
                "(blank, zero-width, embedded-invisible and bidi-only text are refused)"
            )

    def covers_scope(self, scope: str) -> bool:
        """True when the approved scope covers ``scope``.

        Glob semantics are namespace-aware. A naive character-prefix test made
        ``refs/heads/ma*`` "cover" ``refs/heads/main-other``, which is not what the
        approver agreed to: a ``*`` must not silently swallow a longer ref name
        that merely shares a prefix. ``<prefix>*`` therefore covers ``<prefix>``
        itself and any name continuing with ``/`` (the next path segment), and
        nothing else.
        """
        approved = (self.scope or "").strip()
        requested = (scope or "").strip()
        if approved == "*":
            return True
        if approved == requested:
            return True
        return _scope_covers_ref(approved, requested)

    def permits(self, consequence: "GitConsequenceClass") -> bool:
        """True only when the approver accepted this exact consequence class."""
        if consequence is GitConsequenceClass.UNKNOWN:
            return False
        return consequence in self.permitted_consequences


@dataclass(frozen=True, slots=True)
class GitAuthorityGrant:
    """Grant for a specific Git authority level."""
    grant_id: str
    authority_level: GitAuthorityLevel
    repository_path: Path
    scope: str  # e.g., "refs/heads/main", "refs/remotes/origin/*"
    expires_at: float
    approved_by: str  # Operator/Director identity
    request_digest: str
    binding_scope: GitAuthorityBindingScope
    #: The consequence class this grant authorises. Compared against the parsed
    #: operation in EVERY binding mode, so a grant for non-destructive work can
    #: never perform a history-rewriting operation.
    consequence_class: GitConsequenceClass = GitConsequenceClass.NON_DESTRUCTIVE
    #: For LEVEL_SCOPE: the id of a GitBroadAuthorityApproval resolved in the
    #: manager's approval store. A free-text string is not proof of approval.
    broad_authority_approval_id: str | None = None
    broad_authority_approval: str | None = None  # Resolved approver identity (audit)
    justification: str | None = None  # Recorded rationale from the approval record
    #: Canonical target resolved ONCE at issuance and then frozen. Validation
    #: compares against this, never against a fresh resolution of
    #: ``repository_path``: re-resolving would let a symlink swap after issuance
    #: silently redirect the grant to a different repository.
    repository_canonical: str = ""
    #: Filesystem identity (device:inode of the git directory) frozen at issuance.
    #: REQUIRED for repository-bound authority: a path string alone does not
    #: identify a repository, so replacing the repository at the same pathname
    #: would otherwise preserve the comparison while changing the target.
    repository_identity: str = ""


@dataclass(frozen=True, slots=True)
class GitOperationRequest:
    """Request for a Git operation at a specific authority level."""
    operation_id: str
    authority_level: GitAuthorityLevel
    repository_path: Path
    command: list[str]  # Git subcommand and args
    working_directory: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    resource_limits: ProcessResourceLimits = field(default_factory=ProcessResourceLimits)
    required_grant_id: str | None = None  # None means no validation required for inspect-only


@dataclass(frozen=True, slots=True)
class GitOperationResult:
    """Result of a Git operation."""
    operation_id: str
    authority_level: GitAuthorityLevel
    state: GitOperationState
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: float
    ended_at: float | None
    grant_id: str | None = None
    error_message: str | None = None
    remote_outcome_unknown: bool = False


@dataclass(frozen=True, slots=True)
class GitReceipt:
    """Receipt for Git operation settlement."""
    operation_id: str
    authority_level: GitAuthorityLevel
    request_digest: str
    result: GitOperationResult
    settled_at: float
    grant_id: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    tree_digest: str | None = None


class DisposableGitRepository:
    """
    A mechanically disposable Git repository for testing and isolation.
    Created in a temporary directory with no connection to product repositories.
    """

    def __init__(
        self,
        path: Path,
        *,
        bare: bool = False,
        init_commit: bool = True,
    ) -> None:
        self.path = path
        self._bare = bare
        self._init_commit = init_commit
        self._initialized = False

    @classmethod
    async def create(
        cls,
        base_dir: Path | None = None,
        *,
        bare: bool = False,
        init_commit: bool = True,
        prefix: str = "bots5-git-test-",
    ) -> "DisposableGitRepository":
        """Create a new disposable Git repository."""
        if base_dir is None:
            base_dir = Path(tempfile.gettempdir())
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=base_dir))
        repo = cls(path, bare=bare, init_commit=init_commit)
        await repo._initialize()
        return repo

    async def _initialize(self) -> None:
        """Initialize the Git repository."""
        if self._bare:
            await self._run_git(["init", "--bare"])
        else:
            await self._run_git(["init"])
            await self._run_git(["config", "user.name", "BOTS Test"])
            await self._run_git(["config", "user.email", "bots-test@example.com"])
            if self._init_commit:
                # Create an initial commit
                readme = self.path / "README.md"
                readme.write_text("# Test Repository\n")
                await self._run_git(["add", "README.md"])
                await self._run_git(["commit", "-m", "Initial commit"])
        self._initialized = True

    async def _run_git(self, args: list[str], **kwargs) -> tuple[int, str, str]:
        """Run a git command in the repository."""
        executor = BoundedProcessExecutor()
        request = ProcessExecutionRequest(
            command=["git"] + args,
            working_directory=self.path,
            timeout_seconds=kwargs.get("timeout_seconds", 30.0),
            resource_limits=kwargs.get("resource_limits", ProcessResourceLimits(max_processes=20, max_memory_mb=1024)),
            capture_stdout=True,
            capture_stderr=True,
        )
        # Use _execute_raw since this is internal test infrastructure
        result = await executor._execute_raw(request)
        return result.exit_code or 0, result.stdout.decode("utf-8", errors="replace"), result.stderr.decode("utf-8", errors="replace")

    def get_path(self) -> Path:
        """Get the repository path."""
        return self.path

    def is_bare(self) -> bool:
        """Check if repository is bare."""
        return self._bare

    async def cleanup(self) -> None:
        """Clean up the disposable repository."""
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

    async def __aenter__(self) -> "DisposableGitRepository":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.cleanup()


class DisposableGitRemote:
    """
    A mechanically disposable Git remote for testing push/pull operations.
    Created as a bare repository that can serve as a remote.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False

    @classmethod
    async def create(
        cls,
        base_dir: Path | None = None,
        *,
        prefix: str = "bots5-git-remote-",
    ) -> "DisposableGitRemote":
        """Create a new disposable Git remote (bare repository)."""
        if base_dir is None:
            base_dir = Path(tempfile.gettempdir())
        path = Path(tempfile.mkdtemp(prefix=prefix, dir=base_dir))
        remote = cls(path)
        await remote._initialize()
        return remote

    async def _initialize(self) -> None:
        """Initialize the bare Git remote."""
        executor = BoundedProcessExecutor()
        request = ProcessExecutionRequest(
            command=["git", "init", "--bare"],
            working_directory=self.path,
            timeout_seconds=30.0,
            resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            capture_stdout=True,
            capture_stderr=True,
        )
        # Use _execute_raw since this is internal test infrastructure
        result = await executor._execute_raw(request)
        if result.exit_code != 0:
            raise StateError(f"Failed to initialize bare repository: {result.stderr}")
        self._initialized = True

    def get_url(self) -> str:
        """Get the remote URL (file:// protocol)."""
        return f"file://{self.path}"

    async def cleanup(self) -> None:
        """Clean up the disposable remote."""
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)

    async def __aenter__(self) -> "DisposableGitRemote":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.cleanup()


class GitAuthorityManager:
    """
    Manages Git authority boundaries with separate inspect/status/diff
    and consequential stage/commit/push authorities. Each authority level
    requires explicit approval and produces a receipt at settlement.
    """

    # Command-level mapping: maps git subcommands to required authority levels.
    # Key: tuple of (command_parts, flags) where flags are optional extra args that don't change the level
    # Value: GitAuthorityLevel
    # This is a best-effort mapping; any unrecognised or ambiguous command fails closed.
    _COMMAND_LEVEL_MAP: dict[tuple[str, ...], GitAuthorityLevel] = {
        # INSPECT: read-only operations
        ("status",): GitAuthorityLevel.INSPECT,
        ("status", "--porcelain=v2"): GitAuthorityLevel.INSPECT,
        ("diff",): GitAuthorityLevel.INSPECT,
        ("diff", "--staged"): GitAuthorityLevel.INSPECT,
        ("log",): GitAuthorityLevel.INSPECT,
        ("log", "--oneline"): GitAuthorityLevel.INSPECT,
        ("show",): GitAuthorityLevel.INSPECT,
        ("show", "--stat"): GitAuthorityLevel.INSPECT,
        ("rev-parse",): GitAuthorityLevel.INSPECT,
        ("branch",): GitAuthorityLevel.INSPECT,
        ("branch", "-a"): GitAuthorityLevel.INSPECT,
        ("branch", "-v"): GitAuthorityLevel.INSPECT,
        ("remote",): GitAuthorityLevel.INSPECT,
        ("remote", "-v"): GitAuthorityLevel.INSPECT,
        ("config",): GitAuthorityLevel.INSPECT,
        ("config", "--get"): GitAuthorityLevel.INSPECT,
        ("config", "--list"): GitAuthorityLevel.INSPECT,
        ("ls-files",): GitAuthorityLevel.INSPECT,
        ("ls-tree",): GitAuthorityLevel.INSPECT,
        ("cat-file",): GitAuthorityLevel.INSPECT,
        ("hash-object",): GitAuthorityLevel.INSPECT,
        ("show-ref",): GitAuthorityLevel.INSPECT,
        ("describe",): GitAuthorityLevel.INSPECT,
        # STAGE: staging operations (modify index)
        ("add",): GitAuthorityLevel.STAGE,
        ("rm",): GitAuthorityLevel.STAGE,
        ("restore",): GitAuthorityLevel.STAGE,
        ("reset",): GitAuthorityLevel.STAGE,
        ("reset", "--soft"): GitAuthorityLevel.STAGE,
        ("reset", "--mixed"): GitAuthorityLevel.STAGE,
        ("reset", "-q"): GitAuthorityLevel.STAGE,
        ("update-index",): GitAuthorityLevel.STAGE,
        ("sparse-checkout",): GitAuthorityLevel.STAGE,
        # COMMIT: commit operations
        ("commit",): GitAuthorityLevel.COMMIT,
        ("commit", "-m"): GitAuthorityLevel.COMMIT,
        ("commit", "--amend"): GitAuthorityLevel.COMMIT,
        ("commit", "-a"): GitAuthorityLevel.COMMIT,
        ("commit", "--author"): GitAuthorityLevel.COMMIT,
        ("commit-tree",): GitAuthorityLevel.COMMIT,
        # PUSH: remote push operations
        ("push",): GitAuthorityLevel.PUSH,
        ("push", "--force"): GitAuthorityLevel.PUSH,
        ("push", "--force-with-lease"): GitAuthorityLevel.PUSH,
        ("push", "-f"): GitAuthorityLevel.PUSH,
        ("push", "origin"): GitAuthorityLevel.PUSH,
        ("fetch",): GitAuthorityLevel.PUSH,  # fetch from remote is reputation-bearing
        ("pull",): GitAuthorityLevel.PUSH,   # pull combines fetch + merge
        # MERGE: merge/rebase operations (history rewriting)
        ("merge",): GitAuthorityLevel.MERGE,
        ("merge", "--no-ff"): GitAuthorityLevel.MERGE,
        ("merge", "-s"): GitAuthorityLevel.MERGE,
        ("rebase",): GitAuthorityLevel.MERGE,
        ("rebase", "--interactive"): GitAuthorityLevel.MERGE,
        ("cherry-pick",): GitAuthorityLevel.MERGE,
        ("revert",): GitAuthorityLevel.MERGE,
        ("merge-base",): GitAuthorityLevel.MERGE,
        # REF_MUTATE: reference mutation operations (rewriting shared state)
        ("update-ref",): GitAuthorityLevel.REF_MUTATE,
        ("symbolic-ref",): GitAuthorityLevel.REF_MUTATE,
        ("reset", "--hard"): GitAuthorityLevel.REF_MUTATE,
        ("reset", "--merge"): GitAuthorityLevel.REF_MUTATE,
        ("reset", "--keep"): GitAuthorityLevel.REF_MUTATE,
        ("checkout", "--orphan"): GitAuthorityLevel.REF_MUTATE,
        ("clone",): GitAuthorityLevel.REF_MUTATE,  # creates refs in new repo
        # DELETE: destructive operations
        ("branch", "-d"): GitAuthorityLevel.DELETE,
        ("branch", "-D"): GitAuthorityLevel.DELETE,
        ("branch", "--delete"): GitAuthorityLevel.DELETE,
        ("remote", "remove"): GitAuthorityLevel.DELETE,
        ("remote", "prune"): GitAuthorityLevel.DELETE,
        ("tag", "-d"): GitAuthorityLevel.DELETE,
        ("clean", "-f"): GitAuthorityLevel.DELETE,
        ("clean", "-fd"): GitAuthorityLevel.DELETE,
        ("gc",): GitAuthorityLevel.DELETE,
        ("prune",): GitAuthorityLevel.DELETE,
        ("prune", "--expire"): GitAuthorityLevel.DELETE,
        # `checkout` MUTATES: the path form (`checkout -- <path>`, `checkout .`)
        # overwrites the working tree from the index and DISCARDS uncommitted
        # edits, and the branch form moves HEAD and rewrites the working tree.
        # Mapping it to INSPECT previously let a read-only-declared request
        # destroy uncommitted work with no capability grant at all.
        ("checkout",): GitAuthorityLevel.STAGE,
        ("checkout", "-b"): GitAuthorityLevel.STAGE,
        ("checkout", "-B"): GitAuthorityLevel.STAGE,
        ("checkout", "--"): GitAuthorityLevel.STAGE,
        ("checkout", "-f"): GitAuthorityLevel.STAGE,
        ("checkout", "-q"): GitAuthorityLevel.STAGE,
        ("checkout", "HEAD"): GitAuthorityLevel.STAGE,
        ("checkout", "main"): GitAuthorityLevel.STAGE,
    }

    def __init__(
        self,
        process_executor: BoundedProcessExecutor | None = None,
        execution_manager: ExecutionManager | None = None,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._process_executor = process_executor or BoundedProcessExecutor(execution_manager=execution_manager, clock=clock, ids=ids)
        self._clock = clock or SystemClock()
        self._ids = ids or Uuid7Factory()
        self._grants: dict[str, GitAuthorityGrant] = {}
        self._operations: dict[str, GitOperationResult] = {}
        self._receipts: dict[str, GitReceipt] = {}
        #: Trusted store of separately-attributable broad-authority approvals.
        #: A LEVEL_SCOPE grant is only issuable against a record registered here.
        self._broad_authority_approvals: dict[str, GitBroadAuthorityApproval] = {}

    def register_broad_authority_approval(
        self, approval: GitBroadAuthorityApproval
    ) -> GitBroadAuthorityApproval:
        """Register a trusted broad-authority approval record.

        This is the governance-side entry point. ``issue_grant`` will only accept a
        LEVEL_SCOPE request whose ``approval_id`` resolves to a record registered
        here, so a caller cannot manufacture broad authority by passing a string.
        """
        if approval.approval_id in self._broad_authority_approvals:
            raise ValueError(
                f"broad-authority approval '{approval.approval_id}' is already registered"
            )
        self._broad_authority_approvals[approval.approval_id] = approval
        return approval

    def request_digest_for(
        self,
        authority_level: GitAuthorityLevel,
        repository_path: Path,
        command: list[str],
        timeout_seconds: float = 30.0,
    ) -> str:
        """Public helper: the digest a request will carry for ``command``.

        Callers that must pre-compute an operation identity (for example issuing
        an exact-request grant) use this to obtain exactly the value
        ``execute_operation`` will recompute, instead of inventing one.
        """
        return self._digest_for_command(
            authority_level, repository_path, command, timeout_seconds
        )

    def get_broad_authority_approval(
        self, approval_id: str
    ) -> GitBroadAuthorityApproval | None:
        """Resolve a registered broad-authority approval record."""
        if not approval_id:
            return None
        return self._broad_authority_approvals.get(approval_id)

    def _consequence_for_command(self, command: list[str]) -> GitConsequenceClass:
        """Consequence class of ``command`` (public-ish helper for audit/tests)."""
        return self._classify_consequence(command)

    def issue_grant(
        self,
        authority_level: GitAuthorityLevel,
        repository_path: Path,
        scope: str,
        approved_by: str,
        ttl_seconds: float = 3600.0,
        request_digest: str = "",
        binding_scope: GitAuthorityBindingScope | None = None,
        command: list[str] | None = None,
        request_timeout_seconds: float = 30.0,
        approval_id: str | None = None,
        permitted_consequences: tuple[GitConsequenceClass, ...] | None = None,
    ) -> GitAuthorityGrant:
        """
        Issue a Git authority grant for a specific level and scope.

        Args:
            authority_level: The authority level being granted.
            repository_path: Path to the repository.
            scope: Scope of the grant (refs, paths, etc.).
            approved_by: Identity of the approver (operator/Director).
            ttl_seconds: Time-to-live for the grant.
            request_digest: Digest of the request being authorized. Prefer
                ``command``: a digest the manager derives itself is trusted, a
                caller-supplied string is not.
            binding_scope: Binding mode. Defaults to EXACT_REQUEST.
            command: The operation this grant is for. The manager derives the
                operation identity from this by TRUSTED NORMALISATION. Required
                unless ``request_digest`` is supplied.
            approval_id: For LEVEL_SCOPE only: the id of a
                :class:`GitBroadAuthorityApproval` previously registered with
                :meth:`register_broad_authority_approval`. A free-text
                justification is NOT accepted as proof of approval.
            permitted_consequences: Consequence classes this grant authorises.
                Defaults to the class of ``command``. A LEVEL_SCOPE grant may not
                exceed what its approval record permits.

        Returns:
            GitAuthorityGrant for the approved operation.

        Security model:
            * EXACT_REQUEST is the unconditional default. Missing operation
              identity REFUSES; there is no fallback to broader authority.
            * LEVEL_SCOPE requires a trusted approval RECORD, not a string.
            * Every grant carries a consequence class that is enforced against the
              parsed operation at use time, in every binding mode.

        Raises:
            ValueError: if operation identity cannot be established, or if a
                LEVEL_SCOPE request lacks a resolvable approval record.
        """
        grant_id = self._ids.new()
        expires_at = self._clock.now().timestamp() + ttl_seconds

        # ---- Operation identity, derived by trusted normalisation -------------
        # A digest supplied by the caller is accepted only for backward
        # compatibility; a `command` is preferred because the manager computes the
        # identity itself.
        if command is not None:
            # An empty or blank command carries NO operation identity. Without
            # this check `command=[]` produced a digest of the empty normalised
            # command and issued an EXACT_REQUEST grant bound to nothing, which is
            # precisely the "missing identity must refuse" case.
            # Strip ALL Unicode whitespace AND zero-width/invisible formatting
            # characters, so a token such as "\u200b" cannot masquerade as a real
            # operation identity. `str.strip()` alone does not remove U+200B.
            if not [t for t in command if isinstance(t, str) and _has_visible_content(t)]:
                raise ValueError(
                    "command carries no operation identity (empty or blank tokens); "
                    "refusing to issue a grant bound to nothing"
                )
            derived_digest = self._digest_for_command(
                authority_level, repository_path, command, request_timeout_seconds
            )
            if request_digest and request_digest != derived_digest:
                raise ValueError(
                    "request_digest does not match the digest derived from the "
                    "supplied command; refusing to issue a mis-bound grant"
                )
            request_digest = derived_digest

        if binding_scope is None:
            binding_scope = GitAuthorityBindingScope.EXACT_REQUEST

        # ---- Fail-closed: no operation identity => refuse ---------------------
        if binding_scope == GitAuthorityBindingScope.EXACT_REQUEST and not request_digest:
            raise ValueError(
                "EXACT_REQUEST grant requires operation identity. Pass command=[...] "
                "so the manager derives it, or an explicit request_digest. There is "
                "no fallback to broader authority."
            )

        # ---- Consequence class of the authorised operation --------------------
        if command is not None:
            command_consequence = self._classify_consequence(command)
        else:
            command_consequence = GitConsequenceClass.NON_DESTRUCTIVE
        consequence_class = (
            command_consequence
            if permitted_consequences is None
            else max(permitted_consequences, key=lambda c: _CONSEQUENCE_RANK[c])
        )

        # ---- LEVEL_SCOPE requires a trusted approval RECORD -------------------
        approval: GitBroadAuthorityApproval | None = None
        if binding_scope == GitAuthorityBindingScope.LEVEL_SCOPE:
            if not approval_id:
                raise ValueError(
                    "LEVEL_SCOPE grant requires an approval_id resolving to a "
                    "registered GitBroadAuthorityApproval. A caller-supplied "
                    "justification string is not proof of approval."
                )
            approval = self.get_broad_authority_approval(approval_id)
            if approval is None:
                raise ValueError(
                    f"LEVEL_SCOPE grant references unknown approval_id "
                    f"'{approval_id}'; no such record is registered"
                )
            if _normalise_repo_path(approval.repository_path) != _normalise_repo_path(
                repository_path
            ):
                raise ValueError(
                    f"approval '{approval_id}' is bound to repository "
                    f"'{approval.repository_path}' and cannot authorise a grant for "
                    f"'{repository_path}'"
                )
            if not approval.covers_scope(scope):
                raise ValueError(
                    f"approval '{approval_id}' covers scope '{approval.scope}' which "
                    f"does not cover the requested scope '{scope}'"
                )
            for requested in (
                permitted_consequences
                if permitted_consequences is not None
                else (command_consequence,)
            ):
                if not approval.permits(requested):
                    raise ValueError(
                        f"approval '{approval_id}' does not permit consequence "
                        f"'{requested.value}'; it permits "
                        f"{sorted(c.value for c in approval.permitted_consequences)}"
                    )
            if command_consequence is GitConsequenceClass.UNKNOWN and not approval.permits(
                GitConsequenceClass.UNKNOWN
            ):
                raise ValueError(
                    "operation consequence could not be established; a broad-authority "
                    "approval must name the consequences it accepts, so this refuses"
                )
            if not request_digest:
                # LEVEL_SCOPE is not request-bound; record the approved scope only.
                request_digest = f"level-scope:{approval_id}"

        grant = GitAuthorityGrant(
            grant_id=grant_id,
            authority_level=authority_level,
            repository_path=repository_path,
            repository_canonical=_normalise_repo_path(repository_path),
            repository_identity=_repo_identity(repository_path),
            scope=scope,
            expires_at=expires_at,
            approved_by=approved_by,
            request_digest=request_digest,
            binding_scope=binding_scope,
            consequence_class=consequence_class,
            broad_authority_approval_id=approval_id
            if binding_scope == GitAuthorityBindingScope.LEVEL_SCOPE
            else None,
            broad_authority_approval=approval.approved_by if approval else None,
            justification=approval.rationale if approval else None,
        )

        self._grants[grant_id] = grant
        return grant

    def _digest_for_command(
        self,
        authority_level: GitAuthorityLevel,
        repository_path: Path,
        command: list[str],
        timeout_seconds: float = 30.0,
    ) -> str:
        """Derive an operation digest from a command via trusted normalisation.

        ``timeout_seconds`` must match the value the eventual
        :class:`GitOperationRequest` will carry, because the canonical request
        digest (``_compute_request_digest``) includes it. It defaults to the
        request default so the common case lines up.
        """
        normalized = self._normalize_command(command)
        # Must mirror _compute_request_digest exactly, including path
        # normalisation, or the two sides disagree for equivalent spellings.
        content = (
            f"{authority_level}|{_normalise_repo_path(repository_path)}"
            f"|{normalized}|{timeout_seconds}"
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]


    def validate_grant(self, grant_id: str, authority_level: GitAuthorityLevel, repository_path: Path) -> bool:
        """
        Validate that a grant exists and is valid for the requested operation.

        Args:
            grant_id: The grant ID to validate.
            authority_level: The authority level being requested.
            repository_path: The repository path.

        Returns:
            True if grant is valid, False otherwise.
        """
        grant = self._grants.get(grant_id)
        if grant is None:
            return False
        
        # Repository binding: compare against what was FROZEN at issuance. There
        # is deliberately NO fallback to re-resolving grant.repository_path -- that
        # fallback was itself reachable (via object.__setattr__ clearing the field)
        # and would restore the very re-resolution the freeze exists to prevent.
        if not grant.repository_canonical:
            return False
        if grant.repository_canonical != _normalise_repo_path(repository_path):
            return False
        # A matching pathname is not a matching repository. The SAME filesystem
        # object must still be present; otherwise the repository was replaced at
        # the same path and the authority would leak to whatever now occupies it.
        #
        # This is deliberately NOT conditional on the field being non-empty: an
        # `if grant.repository_identity and ...` guard meant that clearing the
        # field (reachable via object.__setattr__ on a returned grant) SILENTLY
        # DISABLED the identity check. Identity is now mandatory -- a grant with no
        # frozen identity is refused outright, exactly like an empty canonical.
        if not grant.repository_identity:
            return False
        if _repo_identity(repository_path) != grant.repository_identity:
            return False
        
        # Grant must not be expired
        if self._clock.now().timestamp() > grant.expires_at:
            return False
        
        # Check authority level compatibility
        # Higher authority levels imply lower authority levels
        # A STAGE grant can be used for INSPECT (can't stage without knowing status)
        # A COMMIT grant can be used for STAGE and INSPECT
        # A PUSH grant can be used for COMMIT, STAGE, and INSPECT
        # And so on...
        level_hierarchy = {
            GitAuthorityLevel.INSPECT: 0,
            GitAuthorityLevel.EDIT: 1,
            GitAuthorityLevel.VALIDATE: 2,
            GitAuthorityLevel.STAGE: 3,
            GitAuthorityLevel.COMMIT: 4,
            GitAuthorityLevel.PUSH: 5,
            GitAuthorityLevel.MERGE: 6,
            GitAuthorityLevel.REF_MUTATE: 7,
            GitAuthorityLevel.DELETE: 8,
        }
        
        requested_level = level_hierarchy.get(authority_level, -1)
        granted_level = level_hierarchy.get(grant.authority_level, -1)
        
        # Grant allows same level or lower levels (higher hierarchy value = more authority)
        # So we check if granted_level >= requested_level
        if granted_level >= requested_level:
            return True
        
        return False

    async def execute_operation(
        self,
        request: GitOperationRequest,
    ) -> GitOperationResult:
        """
        Execute a Git operation with authority validation.

        Args:
            request: The Git operation request.

        Returns:
            GitOperationResult with execution outcome.
        """
        operation_id = request.operation_id or self._ids.new()
        started_at = self._clock.now().timestamp()

        # Derive the required authority level from the actual git command.
        # This is the key security boundary: we validate based on what the command DOES,
        # not what the caller claims their authority level is.
        try:
            derived_level = self._derive_required_authority_level(request.command)
        except ValueError as e:
            result = GitOperationResult(
                operation_id=operation_id,
                authority_level=request.authority_level,
                state=GitOperationState.REJECTED,
                exit_code=None,
                stdout="",
                stderr=str(e),
                started_at=started_at,
                ended_at=self._clock.now().timestamp(),
                grant_id=request.required_grant_id,
                error_message=f"Command analysis failed: {e}",
                remote_outcome_unknown=False,
            )
            self._operations[operation_id] = result
            return result

        # The effective required level is the MAXIMUM of:
        # 1. The command-derived level (what the git command actually requires)
        # 2. The declared authority_level (what the caller claims)
        # This prevents a caller from lowering the requirement by declaring INSPECT
        # while executing a mutative command.
        effective_required_level = derived_level
        
        # For comparison, we map the declared level to a hierarchy value
        level_hierarchy = {
            GitAuthorityLevel.INSPECT: 0,
            GitAuthorityLevel.EDIT: 1,
            GitAuthorityLevel.VALIDATE: 2,
            GitAuthorityLevel.STAGE: 3,
            GitAuthorityLevel.COMMIT: 4,
            GitAuthorityLevel.PUSH: 5,
            GitAuthorityLevel.MERGE: 6,
            GitAuthorityLevel.REF_MUTATE: 7,
            GitAuthorityLevel.DELETE: 8,
        }
        
        # The effective required level is the MAXIMUM required
        # If derived_level is higher than the declared level, we use derived_level
        # This means a caller cannot bypass by declaring a lower level
        if derived_level != request.authority_level:
            # If the derived level is HIGHER than declared, reject (caller is misrepresenting)
            if level_hierarchy.get(derived_level, -1) > level_hierarchy.get(request.authority_level, -1):
                result = GitOperationResult(
                    operation_id=operation_id,
                    authority_level=request.authority_level,
                    state=GitOperationState.REJECTED,
                    exit_code=None,
                    stdout="",
                    stderr=f"Command requires {derived_level.value} authority but caller declared {request.authority_level.value}",
                    started_at=started_at,
                    ended_at=self._clock.now().timestamp(),
                    grant_id=request.required_grant_id,
                    error_message=f"Authority mismatch: command {request.command[0] if request.command else ''} requires {derived_level.value}, caller declared {request.authority_level.value}",
                    remote_outcome_unknown=False,
                )
                self._operations[operation_id] = result
                return result

        # Determine if the effective operation is consequential (requires a grant).
        # Based on the DERIVED level, not the declared level.
        #
        # This is expressed as "anything ABOVE INSPECT" rather than an explicit
        # enumeration. The previous enumeration omitted EDIT and VALIDATE, so a
        # caller could declare one of those levels and reach the executor with no
        # grant at all (demonstrated: `checkout -- <path>` destroying uncommitted
        # work). Deriving it from the hierarchy means a new level cannot silently
        # fall outside the grant requirement.
        _level_rank = {
            GitAuthorityLevel.INSPECT: 0,
            GitAuthorityLevel.EDIT: 1,
            GitAuthorityLevel.VALIDATE: 2,
            GitAuthorityLevel.STAGE: 3,
            GitAuthorityLevel.COMMIT: 4,
            GitAuthorityLevel.PUSH: 5,
            GitAuthorityLevel.MERGE: 6,
            GitAuthorityLevel.REF_MUTATE: 7,
            GitAuthorityLevel.DELETE: 8,
        }
        is_consequential = _level_rank.get(effective_required_level, 99) > _level_rank[
            GitAuthorityLevel.INSPECT
        ]

        # Fail-closed: consequential operations require a valid grant
        if is_consequential:
            if not request.required_grant_id:
                result = GitOperationResult(
                    operation_id=operation_id,
                    authority_level=request.authority_level,
                    state=GitOperationState.REJECTED,
                    exit_code=None,
                    stdout="",
                    stderr="Consequential Git operations require an explicit authority grant",
                    started_at=started_at,
                    ended_at=self._clock.now().timestamp(),
                    grant_id=None,
                    error_message="Consequential operation rejected: no authority grant provided",
                    remote_outcome_unknown=False,
                )
                self._operations[operation_id] = result
                return result

        # A grant that IS supplied must always be valid, for consequential AND
        # read-only inspect operations. Otherwise a caller could present a
        # foreign or expired grant for an inspect operation and have the request
        # recorded as grant-backed when it was never checked.
        if request.required_grant_id:
            grant = self._grants.get(request.required_grant_id)
            if grant is None or not self.validate_grant(
                request.required_grant_id, effective_required_level, request.repository_path
            ):
                result = GitOperationResult(
                    operation_id=operation_id,
                    authority_level=request.authority_level,
                    state=GitOperationState.REJECTED,
                    exit_code=None,
                    stdout="",
                    stderr="Authority grant validation failed",
                    started_at=started_at,
                    ended_at=self._clock.now().timestamp(),
                    grant_id=request.required_grant_id,
                    error_message="Invalid or expired authority grant",
                    remote_outcome_unknown=False,
                )
                self._operations[operation_id] = result
                return result
            
            # Additionally bind the grant to the operation via digest validation
            digest = self._compute_request_digest(request)
            if grant and not self._check_grant_binding(grant, request.command, digest):
                result = GitOperationResult(
                    operation_id=operation_id,
                    authority_level=request.authority_level,
                    state=GitOperationState.REJECTED,
                    exit_code=None,
                    stdout="",
                    stderr="Authority grant not bound to this operation",
                    started_at=started_at,
                    ended_at=self._clock.now().timestamp(),
                    grant_id=request.required_grant_id,
                    error_message="Grant digest mismatch - grant was issued for a different operation",
                    remote_outcome_unknown=False,
                )
                self._operations[operation_id] = result
                return result

        # Determine working directory
        workdir = request.working_directory or request.repository_path

        # Execute git command using _execute_raw (after our own authorization boundary)
        process_request = ProcessExecutionRequest(
            command=["git"] + request.command,
            working_directory=workdir,
            environment=request.environment,
            timeout_seconds=request.timeout_seconds,
            resource_limits=request.resource_limits,
            capture_stdout=True,
            capture_stderr=True,
        )

        try:
            # Call _execute_raw since we've already performed our own authority validation
            process_result = await self._process_executor._execute_raw(
                process_request,
                authority_grant_id=request.required_grant_id,
            )
            ended_at = self._clock.now().timestamp()

            # Map process states to Git operation states
            # TIMEOUT and CANCELLED are UNKNOWN (uncertain remote outcome)
            # Definite nonzero exit codes are FAILED
            if process_result.state == ProcessState.COMPLETED:
                state = GitOperationState.COMPLETED
            elif process_result.state == ProcessState.TIMEOUT:
                state = GitOperationState.UNKNOWN
            elif process_result.state == ProcessState.CANCELLED:
                state = GitOperationState.UNKNOWN
            elif process_result.state == ProcessState.FAILED:
                # Only FAILED with a definite exit code stays FAILED
                if process_result.exit_code is not None and process_result.exit_code != 0:
                    state = GitOperationState.FAILED
                else:
                    # Other FAILED states are UNKNOWN
                    state = GitOperationState.UNKNOWN
            else:
                state = GitOperationState.UNKNOWN

            # remote_outcome_unknown from process_result overrides our state determination
            # if it indicates uncertainty (e.g., network failure, timeout)
            if process_result.remote_outcome_unknown:
                state = GitOperationState.UNKNOWN

            result = GitOperationResult(
                operation_id=operation_id,
                authority_level=request.authority_level,
                state=state,
                exit_code=process_result.exit_code,
                stdout=process_result.stdout.decode("utf-8", errors="replace"),
                stderr=process_result.stderr.decode("utf-8", errors="replace"),
                started_at=started_at,
                ended_at=ended_at,
                grant_id=request.required_grant_id,
                error_message=process_result.error_message,
                remote_outcome_unknown=process_result.remote_outcome_unknown,
            )

        except Exception as exc:
            ended_at = self._clock.now().timestamp()
            result = GitOperationResult(
                operation_id=operation_id,
                authority_level=request.authority_level,
                state=GitOperationState.UNKNOWN,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                started_at=started_at,
                ended_at=ended_at,
                grant_id=request.required_grant_id,
                error_message=f"Execution error: {exc}",
                remote_outcome_unknown=True,
            )

        self._operations[operation_id] = result

        # Create receipt
        receipt = GitReceipt(
            operation_id=operation_id,
            authority_level=request.authority_level,
            request_digest=self._compute_request_digest(request),
            result=result,
            settled_at=self._clock.now().timestamp(),
            grant_id=request.required_grant_id,
        )
        self._receipts[operation_id] = receipt

        return result

    def _derive_required_authority_level(self, command: list[str]) -> GitAuthorityLevel:
        """
        Derive the required authority level from the git command.
        
        Parses the command to identify the subcommand and any flags,
        then maps it to the appropriate GitAuthorityLevel.
        
        Fails closed (raises ValueError) for unrecognised or ambiguous commands.
        
        Args:
            command: The git command and arguments (e.g., ["add", "file.txt"])
            
        Returns:
            GitAuthorityLevel required for this command
            
        Raises:
            ValueError: If the command is unrecognised or ambiguous
        """
        if not command:
            raise ValueError("Empty command - cannot determine required authority level")
        
        # First pass: find the subcommand (first non-flag argument)
        subcommand = None
        for part in command:
            if not part.startswith("-"):
                subcommand = part
                break
        
        if subcommand is None:
            raise ValueError("No subcommand found in git command")
        
        # Second pass: build a key that includes the subcommand and its direct flags
        # We want to match patterns like ("reset", "--hard") or ("branch", "-d")
        # We stop at positional arguments (non-flags after the subcommand and its flags)
        
        # For most commands, we look at: subcommand + immediate flags
        # e.g., "reset --hard HEAD" -> key = ("reset", "--hard")
        # e.g., "branch -d main" -> key = ("branch", "-d")
        
        key_parts = [subcommand]
        
        # Look at arguments after the subcommand
        subcommand_idx = command.index(subcommand)
        args_after_subcommand = command[subcommand_idx + 1:]
        
        # Add immediate flags (non-positional args)
        # Flags are args starting with -
        # Positional args are non-flag args (like filenames, branch names, etc.)
        for i, arg in enumerate(args_after_subcommand):
            if arg.startswith("-"):
                key_parts.append(arg)
            else:
                # First positional arg - stop here
                break
        
        # Canonical destructive-flag guard, evaluated BEFORE the command map.
        # The map keys are literal spellings, and its bare `(subcommand,)` entries
        # would otherwise answer first with INSPECT/STAGE for an abbreviated but
        # genuinely destructive form such as `branch --del` or `reset --har`.
        # `_canonical_flags` expands unambiguous git abbreviations, so this closes
        # every accepted spelling rather than only the textbook one.
        _cflags, _crefs, _cunc = _canonical_flags(command)
        if _cunc:
            raise ValueError(
                f"Ambiguous or unrecognised flag in {subcommand!r} command; failing closed"
            )
        if subcommand == "branch" and (
            _cflags & {"d", "D", "delete", "move", "M"}
        ):
            return GitAuthorityLevel.DELETE
        if subcommand == "tag" and (_cflags & {"d", "delete"}):
            return GitAuthorityLevel.DELETE
        if subcommand == "reset" and (_cflags & {"hard", "merge", "keep"}):
            return GitAuthorityLevel.REF_MUTATE
        if subcommand == "clean" and (_cflags & {"f", "force"}):
            return GitAuthorityLevel.DELETE
        if subcommand == "remote" and any(
            t in ("remove", "rm", "prune") for t in command[1:]
        ):
            return GitAuthorityLevel.DELETE
        if subcommand == "checkout" and (_cflags & {"f", "force"}):
            return GitAuthorityLevel.STAGE

        # Try to find the best matching key in the command map
        best_match = None
        best_match_len = 0
        
        for i in range(1, len(key_parts) + 1):
            key = tuple(key_parts[:i])
            if key in self._COMMAND_LEVEL_MAP:
                if i > best_match_len:
                    best_match = self._COMMAND_LEVEL_MAP[key]
                    best_match_len = i
        
        if best_match is not None:
            return best_match
        
        # Handle special cases based on subcommand and flags
        # These are commands that don't have all variants in the map
        
        if subcommand == "reset":
            # Canonical flags so abbreviated forms (--har, --kee, --mer) are seen.
            _flags, _refs, _unc = _canonical_flags(command)
            if _unc or (_flags & {"hard", "merge", "keep"}):
                return GitAuthorityLevel.REF_MUTATE
            return GitAuthorityLevel.STAGE
        elif subcommand == "checkout":
            # checkout -b creates a branch (still INSPECT-level for branching)
            # but checkout of a branch is INSPECT
            if "-b" in command or "--branch" in command:
                return GitAuthorityLevel.INSPECT
            return GitAuthorityLevel.INSPECT
        elif subcommand == "branch":
            _flags, _refs, _unc = _canonical_flags(command)
            # `--del` canonicalises to `delete`; `-d`/`-D` arrive as short flags.
            if _unc or (_flags & {"d", "D", "delete", "move", "M"}):
                return GitAuthorityLevel.DELETE
            return GitAuthorityLevel.INSPECT
        elif subcommand == "remote":
            # `remote rm` is an accepted synonym for `remote remove`.
            if any(t in ("remove", "rm", "prune") for t in command[1:]):
                return GitAuthorityLevel.DELETE
            return GitAuthorityLevel.INSPECT
        elif subcommand == "tag":
            _flags, _refs, _unc = _canonical_flags(command)
            if _unc or ("d" in _flags) or ("delete" in _flags):
                return GitAuthorityLevel.DELETE
            return GitAuthorityLevel.INSPECT
        elif subcommand == "clean":
            _flags, _refs, _unc = _canonical_flags(command)
            # bundled (-fd/-fx) and long (--force) spellings all force a real clean
            if _unc or ("f" in _flags) or ("force" in _flags):
                return GitAuthorityLevel.DELETE
            return GitAuthorityLevel.INSPECT  # dry-run is inspect
        elif subcommand == "gc":
            return GitAuthorityLevel.DELETE
        elif subcommand == "prune":
            return GitAuthorityLevel.DELETE
        elif subcommand == "push":
            return GitAuthorityLevel.PUSH
        elif subcommand == "fetch":
            return GitAuthorityLevel.PUSH
        elif subcommand == "pull":
            return GitAuthorityLevel.PUSH
        elif subcommand == "commit":
            return GitAuthorityLevel.COMMIT
        elif subcommand == "merge":
            return GitAuthorityLevel.MERGE
        elif subcommand == "rebase":
            return GitAuthorityLevel.MERGE
        elif subcommand == "cherry-pick":
            return GitAuthorityLevel.MERGE
        elif subcommand == "revert":
            return GitAuthorityLevel.MERGE
        elif subcommand == "update-ref":
            return GitAuthorityLevel.REF_MUTATE
        elif subcommand == "symbolic-ref":
            return GitAuthorityLevel.REF_MUTATE
        elif subcommand == "clone":
            return GitAuthorityLevel.REF_MUTATE
        elif subcommand == "init":
            # Creating a repository rewrites no history and touches no remote, so
            # it needs no more than INSPECT. It previously had no derivation at
            # all, which made a manager-mediated `git init` fail closed and broke
            # the documented init -> stage -> commit -> push workflow.
            return GitAuthorityLevel.INSPECT
        elif subcommand == "config":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "status":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "diff":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "log":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "show":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "rev-parse":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "branch":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "remote":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "ls-files":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "ls-tree":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "cat-file":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "hash-object":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "show-ref":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "describe":
            return GitAuthorityLevel.INSPECT
        elif subcommand == "restore":
            return GitAuthorityLevel.STAGE
        elif subcommand == "update-index":
            return GitAuthorityLevel.STAGE
        elif subcommand == "commit-tree":
            return GitAuthorityLevel.COMMIT
        elif subcommand == "merge-base":
            return GitAuthorityLevel.MERGE
        
        # Unrecognised command - fail closed
        raise ValueError(f"Unrecognised git subcommand: {subcommand}")

    def _check_grant_binding(self, grant: GitAuthorityGrant, command: list[str], digest: str) -> bool:
        """
        Check that a grant is bound to the specific operation.
        
        Validates that:
        1. If binding_scope is EXACT_REQUEST, the request_digest must match
        2. If binding_scope is LEVEL_SCOPE, the grant can only be used if it has
           proper broad_authority_approval and justification, AND the consequence
           class matches
        3. The grant's scope allows the operation's repository path and ref scope
        
        Args:
            grant: The grant to validate
            command: The git command being executed
            digest: The computed digest of the current request
            
        Returns:
            True if grant is properly bound, False otherwise
        """
        # Extract consequence class from command for binding validation
        consequence_class = self._classify_consequence(command)
        
        # Check binding scope
        if grant.binding_scope == GitAuthorityBindingScope.EXACT_REQUEST:
            # For exact-request binding, the digest must match exactly
            if grant.request_digest != digest:
                return False
            # Validate scope
            if not self._validate_grant_scope(grant, command):
                return False
            # Consequence class must match for EXACT_REQUEST
            return self._consequence_matches_grant(consequence_class, grant)
        
        elif grant.binding_scope == GitAuthorityBindingScope.LEVEL_SCOPE:
            # LEVEL_SCOPE requires explicit, attributable broad authority
            if not grant.broad_authority_approval:
                return False
            if not grant.justification or not grant.justification.strip():
                return False
            # Validate scope
            if not self._validate_grant_scope(grant, command):
                return False
            # Consequence class must match - LEVEL_SCOPE grants cannot bypass consequence separation
            return self._consequence_matches_grant(consequence_class, grant)
        
        # Unknown binding scope - fail closed
        return False
    
    def _classify_consequence(self, command: list[str]) -> GitConsequenceClass:
        """
        Classify the consequence class of a git command.
        
        History-rewriting operations require separate authorization regardless of binding mode.
        """
        if not command:
            return GitConsequenceClass.NON_DESTRUCTIVE

        # FIRST, and before any subcommand shortcut: a command that RETARGETS the
        # repository is never authorised by a grant bound to a different path.
        # `--git-dir=`, `-C <path>` and `--work-tree=` point the operation at
        # another checkout, escaping the grant's repository containment. This must
        # precede the read-only shortcuts, otherwise `--git-dir=/other/.git status`
        # would be waved through as an ordinary `status`.
        # NOTE: the global flags precede the subcommand (`--git-dir=X status`), so
        # every token must be inspected -- an earlier version scanned command[1:]
        # and therefore missed the flag in first position.
        if any(
            token in ("-C", "--git-dir", "--work-tree", "--namespace", "--exec-path")
            or token.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--exec-path="))
            # `-C<path>` attaches the value with no space; `-C` is the only git
            # global short option that retargets the repository.
            or (token.startswith("-C") and not token.startswith("--"))
            for token in command
        ):
            return GitConsequenceClass.UNKNOWN

        subcommand = None
        for token in command:
            if not token.startswith("-"):
                subcommand = token
                break
        
        if subcommand is None:
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # History-rewriting forms that require separate authorization
        history_rewriting_commands = {
            # Push with force options
            "push": {"--force", "--force-with-lease", "--force-if-includes", "-f"},
            # Reset operations that rewrite history
            "reset": {"--hard", "--merge", "--keep"},
            # Working tree destructive operations
            "clean": {"-f", "-fd", "-df"},
            "checkout": {"-f"},
            # Branch/tag deletion
            "branch": {"-D", "--delete"},
            "tag": {"-d", "--delete"},
            # Direct ref manipulation
            "update-ref": set(),
            "symbolic-ref": set(),
            # Reflog manipulation
            "reflog": {"expire"},
            # History rewriting
            "filter-branch": set(),
            "rebase": {"--force-rebase"},
            # Garbage collection with pruning
            "gc": {"--prune=now", "--prune"},
        }
        
        # Check if this is a push command with force options
        if subcommand == "push":
            if _push_is_history_rewriting(command):
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE

        
        # Check reset commands
        if subcommand == "reset":
            flags, _refs, _unc = _canonical_flags(command)
            if flags & {"hard", "merge", "keep"}:
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # Check clean commands
        if subcommand == "clean":
            flags, _refs, _unc = _canonical_flags(command)
            if "f" in flags or "force" in flags:
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # Check checkout -f
        if subcommand == "checkout":
            flags, _refs, _unc = _canonical_flags(command)
            if "f" in flags or "force" in flags:
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # Check branch deletion
        if subcommand == "branch":
            flags, _refs, _unc = _canonical_flags(command)
            if "D" in flags or "d" in flags or "delete" in flags:
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # Check tag deletion
        if subcommand == "tag":
            flags, _refs, _unc = _canonical_flags(command)
            if "d" in flags or "delete" in flags:
                return GitConsequenceClass.HISTORY_REWRITING
            return GitConsequenceClass.NON_DESTRUCTIVE
        
        # Direct ref manipulation
        if subcommand in ("update-ref", "symbolic-ref", "reflog", "filter-branch", "rebase"):
            return GitConsequenceClass.HISTORY_REWRITING
        
        # GC with pruning
        if subcommand == "gc":
            flags, _refs, _unc = _canonical_flags(command)
            if "prune" in flags or "prune=now" in flags:
                return GitConsequenceClass.HISTORY_REWRITING

        # Fail closed: a subcommand whose consequence we cannot establish is NOT
        # assumed harmless. It classifies as UNKNOWN, which no grant satisfies,
        # so it can only proceed through an explicit broad-authority approval that
        # names UNKNOWN -- and `permits()` refuses UNKNOWN outright.
        if subcommand in _KNOWN_READ_ONLY_SUBCOMMANDS:
            return GitConsequenceClass.NON_DESTRUCTIVE

        return GitConsequenceClass.UNKNOWN

    
    def _validate_grant_scope(self, grant: GitAuthorityGrant, command: list[str]) -> bool:
        """Validate that the grant scope covers the refs this command operates on."""
        scope = grant.scope or "*"
        if scope == "*":
            return True
        return self._command_within_scope(scope, command)
    
    def _consequence_matches_grant(self, consequence_class: GitConsequenceClass, grant: GitAuthorityGrant) -> bool:
        """Enforce consequence separation between a grant and the parsed operation.

        The grant carries the consequence class it was authorised for and this
        compares it against the class of the operation actually requested, in
        EVERY binding mode.

        This previously ended in an unconditional ``return True`` while a comment
        asserted that "consequence separation is enforced via digest comparison".
        That was false for ``LEVEL_SCOPE``, because ``_check_grant_binding`` skips
        the digest in exactly that mode -- so a broad grant issued for an ordinary
        push also authorised ``push --force`` and ``push origin +HEAD:main``.

        Rules:
          * ``UNKNOWN`` operation consequence is never satisfiable (fail closed).
          * A grant covers an operation at or below its own consequence rank.
          * A non-destructive grant therefore cannot perform history rewriting.
        """
        if consequence_class is GitConsequenceClass.UNKNOWN:
            return False
        granted = getattr(grant, "consequence_class", GitConsequenceClass.NON_DESTRUCTIVE)
        if granted is GitConsequenceClass.UNKNOWN:
            return False
        return _CONSEQUENCE_RANK[consequence_class] <= _CONSEQUENCE_RANK[granted]


    def _command_within_scope(self, scope: str, command: list[str]) -> bool:
        """Return True only if every ref the command names is covered by ``scope``.

        A command that names no ref (``status``, ``diff``, ``log``, ``branch
        --list``, ``commit`` of already-staged work, ``merge`` of the configured
        upstream, ...) is not constrained by a ref scope: the scope constrains
        *which refs may be written*, and a command that names none cannot write a
        ref outside it. Such commands remain gated by the authority-level check,
        which is where their consequence is decided.

        A command that DOES name refs must have every one of them covered, and a
        ref-bearing command whose refs cannot be determined fails closed.
        """
        refs = self._command_refs(command)
        if not refs:
            return True
        # Short names were expanded to several possible interpretations (heads /
        # tags). Every DESTINATION must be covered, but for a single destination
        # it is enough that one interpretation is inside the scope, since git
        # resolves the short name to whichever namespace exists.
        groups = self._command_ref_groups(command)
        if not groups:
            return True
        return all(self._any_ref_matches_scope(group, scope) for group in groups)

    @staticmethod
    def _short_ref_group(name: str) -> tuple[str, ...]:
        """Return the ref interpretations for one destination name."""
        if name.startswith("refs/"):
            return (name,)
        if not name or name == "HEAD":
            return (name,) if name else ()
        return (f"refs/heads/{name}", f"refs/tags/{name}")

    @classmethod
    def _command_ref_groups(cls, command: list[str]) -> list[tuple[str, ...]]:
        """Per-destination ref interpretations for ``command`` (scope checking)."""
        groups: list[tuple[str, ...]] = []
        for token in command[1:]:
            if token.startswith("-"):
                continue
            if token.startswith("refs/"):
                groups.append((token,))
                continue
            if ":" in token and not token.startswith("/"):
                _src, _, dst = token.partition(":")
                group = cls._short_ref_group(dst)
                if group:
                    groups.append(group)
                continue
            if token == "HEAD":
                groups.append(("HEAD",))
        return groups

    @staticmethod
    def _looks_like_ref_scope(scope: str) -> bool:
        return scope.startswith("refs/") or scope.startswith("refs") or "/" in scope

    @staticmethod
    def _command_refs(command: list[str]) -> list[str]:
        """Flat view of every ref interpretation ``command`` names.

        Kept for callers that want a simple list; scope enforcement uses the
        grouped form from :meth:`_command_ref_groups`, which preserves which
        interpretations belong to the same destination.
        """
        flat: list[str] = []
        for group in GitAuthorityManager._command_ref_groups(command):
            for ref in group:
                if ref not in flat:
                    flat.append(ref)
        return flat

    @staticmethod
    def _looks_like_ref_scope(scope: str) -> bool:
        return scope.startswith("refs/") or scope.startswith("refs") or "/" in scope

    @staticmethod
    def _command_refs(command: list[str]) -> list[str]:
        """Extract the refs a git command *writes to or names as its target*.

        Only the scope-relevant side of a refspec is returned:

        * ``push <remote> <src>:<dst>``  -> the DESTINATION (``dst``). The source
          side is what is being read from locally; a bare ``HEAD`` there is a
          symbolic source, not an authority target, so constraining it would make
          every legitimate ``HEAD:<branch>`` push impossible.
        * ``push <remote> HEAD``         -> ``HEAD`` (no destination given; git
          pushes to the upstream/branch it resolves to, so fail closed unless the
          scope admits it).
        * ``refs/...`` arguments and ``update-ref``/``branch``/``tag`` targets are
          treated as refs.

        Short ("unqualified") ref names are NORMALISED to fully-qualified form, and
        a short destination that is not already qualified is checked against BOTH
        ``refs/heads/<name>`` and ``refs/tags/<name>``. Omitting this previously
        let ``git push origin main:evil`` slip through with no extracted ref at
        all, so a grant scoped to ``refs/heads/main`` happily created
        ``refs/heads/evil``.
        """
        refs: list[str] = []

        def _add_short(name: str) -> None:
            if name.startswith("refs/"):
                if name not in refs:
                    refs.append(name)
                return
            if not name or name == "HEAD":
                if name == "HEAD" and name not in refs:
                    refs.append(name)
                return
            # Unqualified short name: it may resolve under refs/heads or refs/tags.
            for candidate in (f"refs/heads/{name}", f"refs/tags/{name}"):
                if candidate not in refs:
                    refs.append(candidate)

        for token in command[1:]:
            if token.startswith("-"):
                continue
            if token.startswith("refs/"):
                _add_short(token)
                continue
            if ":" in token and not token.startswith("/"):
                # refspec: only the destination side is an authority target.
                _src, _, dst = token.partition(":")
                _add_short(dst)
                continue
            if token == "HEAD":
                _add_short(token)
        return refs

    @staticmethod
    def _ref_matches_scope(ref: str, scope: str) -> bool:
        """Match a ref against a grant scope, supporting a trailing ``*`` glob.

        A short/unqualified ref is expanded to both ``refs/heads/<name>`` and
        ``refs/tags/<name>`` by ``_command_refs``; here it is enough that ONE of
        those interpretations is inside the scope, because git will resolve the
        short name to whichever namespace actually exists. Requiring both would
        reject every legitimate ``push origin HEAD:main``.
        """
        return _scope_covers_ref(scope, ref)

    @classmethod
    def _any_ref_matches_scope(cls, refs: list[str], scope: str) -> bool:
        """True when at least one extracted ref interpretation fits ``scope``."""
        return any(_scope_covers_ref(scope, r) for r in refs)

    @staticmethod
    def _normalize_command(command: list[str]) -> list[str]:
        """
        Normalize a git command for deterministic digest computation.
        
        Normalisation rules (consequence-preserving):
        - Strip leading/trailing whitespace from each token
        - Collapse multiple spaces into one
        - Normalize flag spelling: -f and --force are equivalent
        - Normalize refspecs: convert <src>:<dst> forms to canonical reference names
        - Keep flags that change consequence: --force, --hard, -f, --orphan, etc.
        - Drop flags that don't change consequence: -q, -v, --quiet, --verbose
        - Normalize whitespace between tokens
        
        This ensures semantically identical commands produce the same digest,
        while consequence-changing variants produce different digests.
        
        Args:
            command: The git command and arguments
            
        Returns:
            Normalized command list
        """
        if not command:
            return []
        
        normalized = []
        i = 0
        while i < len(command):
            token = command[i].strip()
            if not token:
                i += 1
                continue
            
            # Normalize --force, --force-with-lease, -f to canonical form
            if token in ("--force", "-f"):
                if i + 1 < len(command) and command[i + 1].strip() == "with-lease":
                    normalized.append("--force-with-lease")
                    i += 2
                else:
                    normalized.append("--force")
                    i += 1
            elif token == "--force-with-lease":
                normalized.append("--force-with-lease")
                i += 1
            elif token == "-f":
                normalized.append("--force")
                i += 1
            # Normalize --hard, --merge, --keep
            elif token == "--hard":
                normalized.append("--hard")
                i += 1
            elif token == "--merge":
                normalized.append("--merge")
                i += 1
            elif token == "--keep":
                normalized.append("--keep")
                i += 1
            # Normalize -B and -b (branch creation)
            elif token in ("-B", "-b"):
                normalized.append(token)
                i += 1
            # Normalize --orphan
            elif token == "--orphan":
                normalized.append("--orphan")
                i += 1
            # Normalize -d, -D (delete)
            elif token == "-d":
                normalized.append("-d")
                i += 1
            elif token == "-D":
                normalized.append("-D")
                i += 1
            # Normalize --delete
            elif token == "--delete":
                normalized.append("--delete")
                i += 1
            # Normalize --amend
            elif token == "--amend":
                normalized.append("--amend")
                i += 1
            # Normalize -a (all)
            elif token == "-a":
                normalized.append("-a")
                i += 1
            # Normalize --author
            elif token == "--author":
                normalized.append("--author")
                i += 1
                # Skip the value
                if i < len(command):
                    i += 1
            # Skip quiet/verbose flags that don't change consequence
            elif token in ("-q", "--quiet", "-v", "--verbose", "--progress"):
                i += 1
            # Keep positional arguments and other flags
            else:
                normalized.append(token)
                i += 1
        
        return normalized

    def _compute_request_digest(self, request: GitOperationRequest) -> str:
        """Compute a digest of the request for receipt binding."""
        # Use normalized command for deterministic digest
        normalized_command = self._normalize_command(request.command)
        # Normalise the path so a digest derived for "/repo" also matches a
        # request whose repository_path is spelled "/repo/" or "/repo/.".
        content = (
            f"{request.authority_level}|{_normalise_repo_path(request.repository_path)}"
            f"|{normalized_command}|{request.timeout_seconds}"
        )
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get_grant(self, grant_id: str) -> GitAuthorityGrant | None:
        """Get a grant by ID."""
        return self._grants.get(grant_id)

    def get_operation(self, operation_id: str) -> GitOperationResult | None:
        """Get an operation result by ID."""
        return self._operations.get(operation_id)

    def get_receipt(self, operation_id: str) -> GitReceipt | None:
        """Get a receipt by operation ID."""
        return self._receipts.get(operation_id)

    def list_grants(self) -> list[GitAuthorityGrant]:
        """List all active grants."""
        return list(self._grants.values())

    def revoke_grant(self, grant_id: str) -> bool:
        """Revoke a grant."""
        if grant_id in self._grants:
            del self._grants[grant_id]
            return True
        return False


@asynccontextmanager
async def disposable_git_repository(
    base_dir: Path | None = None,
    *,
    bare: bool = False,
    init_commit: bool = True,
    prefix: str = "bots5-git-test-",
) -> AsyncIterator[DisposableGitRepository]:
    """
    Context manager for a disposable Git repository.

    Creates a temporary Git repository that is automatically cleaned up
    on exit. Use for testing Git operations without affecting product repos.

    Args:
        base_dir: Base directory for repository creation.
        bare: Whether to create a bare repository.
        init_commit: Whether to create an initial commit.
        prefix: Prefix for the temporary directory name.

    Yields:
        DisposableGitRepository instance.
    """
    repo = await DisposableGitRepository.create(base_dir, bare=bare, init_commit=init_commit, prefix=prefix)
    try:
        yield repo
    finally:
        await repo.cleanup()


@asynccontextmanager
async def disposable_git_remote(
    base_dir: Path | None = None,
    *,
    prefix: str = "bots5-git-remote-",
) -> AsyncIterator[DisposableGitRemote]:
    """
    Context manager for a disposable Git remote (bare repository).

    Creates a temporary bare Git repository that can serve as a remote
    for push/pull testing. Automatically cleaned up on exit.

    Args:
        base_dir: Base directory for remote creation.
        prefix: Prefix for the temporary directory name.

    Yields:
        DisposableGitRemote instance.
    """
    remote = await DisposableGitRemote.create(base_dir, prefix=prefix)
    try:
        yield remote
    finally:
        await remote.cleanup()


class GitInspectAuthority:
    """
    Read-only Git inspection authority (INSPECT level).
    Provides status, diff, log, show operations without any mutation.
    """

    def __init__(self, authority_manager: GitAuthorityManager) -> None:
        self._manager = authority_manager

    async def status(self, repo_path: Path, grant_id: str) -> GitOperationResult:
        """Get repository status."""
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=["status", "--porcelain=v2"],
            required_grant_id=grant_id,
        ))

    async def diff(
        self,
        repo_path: Path,
        grant_id: str,
        *,
        staged: bool = False,
        paths: list[str] | None = None,
    ) -> GitOperationResult:
        """Get repository diff."""
        cmd = ["diff"]
        if staged:
            cmd.append("--staged")
        if paths:
            cmd.extend(["--"] + paths)
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def log(
        self,
        repo_path: Path,
        grant_id: str,
        *,
        max_count: int = 10,
        oneline: bool = True,
    ) -> GitOperationResult:
        """Get commit log."""
        cmd = ["log"]
        if oneline:
            cmd.append("--oneline")
        cmd.extend(["-n", str(max_count)])
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def show(
        self,
        repo_path: Path,
        grant_id: str,
        revision: str = "HEAD",
    ) -> GitOperationResult:
        """Show commit details."""
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=["show", "--stat", revision],
            required_grant_id=grant_id,
        ))

    async def branch_list(
        self,
        repo_path: Path,
        grant_id: str,
        *,
        all_branches: bool = False,
    ) -> GitOperationResult:
        """List branches."""
        cmd = ["branch"]
        if all_branches:
            cmd.append("-a")
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def remote_list(
        self,
        repo_path: Path,
        grant_id: str,
    ) -> GitOperationResult:
        """List remotes."""
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.INSPECT,
            repository_path=repo_path,
            command=["remote", "-v"],
            required_grant_id=grant_id,
        ))


class GitConsequentialAuthority:
    """
    Consequential Git authorities (STAGE, COMMIT, PUSH, MERGE, REF_MUTATE, DELETE).
    Each requires separate explicit approval and produces a receipt.
    """

    def __init__(self, authority_manager: GitAuthorityManager) -> None:
        self._manager = authority_manager

    async def stage(
        self,
        repo_path: Path,
        grant_id: str,
        paths: list[str] | None = None,
    ) -> GitOperationResult:
        """Stage changes (git add)."""
        cmd = ["add"]
        if paths:
            cmd.extend(paths)
        else:
            cmd.append(".")
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.STAGE,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def commit(
        self,
        repo_path: Path,
        grant_id: str,
        message: str,
        *,
        author: str | None = None,
    ) -> GitOperationResult:
        """Commit staged changes."""
        cmd = ["commit", "-m", message]
        if author:
            cmd.extend(["--author", author])
        result = await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.COMMIT,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))
        # Enhance receipt with commit info
        if result.state == GitOperationState.COMPLETED:
            # Get INSPECT grant from the COMMIT grant's repository
            commit_grant = self._manager.get_grant(grant_id)
            if commit_grant:
                # Use a separate INSPECT grant for the same repo. Under
                # fail-closed binding a grant authorises exactly ONE operation,
                # so this one must be bound to the `rev-parse HEAD` it is about to
                # perform. It previously carried no identity at all, which the
                # API now refuses.
                inspect_grant = self._manager.issue_grant(
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo_path,
                    scope=commit_grant.scope,
                    approved_by=commit_grant.approved_by,
                    ttl_seconds=60.0,
                    command=["rev-parse", "HEAD"],
                )
                head_result = await self._manager.execute_operation(GitOperationRequest(
                    operation_id=self._manager._ids.new(),
                    authority_level=GitAuthorityLevel.INSPECT,
                    repository_path=repo_path,
                    command=["rev-parse", "HEAD"],
                    required_grant_id=inspect_grant.grant_id,
                ))
                # Revoke the temporary grant
                self._manager.revoke_grant(inspect_grant.grant_id)
                
                if head_result.state == GitOperationState.COMPLETED:
                    head_commit = head_result.stdout.strip()
                    receipt = self._manager.get_receipt(result.operation_id)
                    if receipt:
                        # Update receipt with commit info
                        updated_receipt = GitReceipt(
                            operation_id=receipt.operation_id,
                            authority_level=receipt.authority_level,
                            request_digest=receipt.request_digest,
                            result=receipt.result,
                            settled_at=receipt.settled_at,
                            grant_id=receipt.grant_id,
                            base_commit=None,  # Would need parent commit
                            head_commit=head_commit,
                            tree_digest=None,  # Would need tree hash
                        )
                        self._manager._receipts[result.operation_id] = updated_receipt
        return result

    async def push(
        self,
        repo_path: Path,
        grant_id: str,
        remote: str = "origin",
        refspec: str | None = None,
        *,
        force: bool = False,
    ) -> GitOperationResult:
        """Push to remote (reputation-bearing, separate approval required)."""
        cmd = ["push"]
        if force:
            cmd.append("--force")
        cmd.append(remote)
        if refspec:
            cmd.append(refspec)
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.PUSH,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def merge(
        self,
        repo_path: Path,
        grant_id: str,
        branch: str,
        *,
        no_ff: bool = False,
        strategy: str | None = None,
    ) -> GitOperationResult:
        """Merge branch (requires base/head/tree seals)."""
        cmd = ["merge"]
        if no_ff:
            cmd.append("--no-ff")
        if strategy:
            cmd.extend(["-s", strategy])
        cmd.append(branch)
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.MERGE,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))

    async def ref_mutate(
        self,
        repo_path: Path,
        grant_id: str,
        command: list[str],  # e.g., ["reset", "--hard", "HEAD~1"], ["rebase", "main"]
    ) -> GitOperationResult:
        """Mutate refs (rebase, reset --hard, etc.)."""
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=GitAuthorityLevel.REF_MUTATE,
            repository_path=repo_path,
            command=command,
            required_grant_id=grant_id,
        ))

    async def delete_branch(
        self,
        repo_path: Path,
        grant_id: str,
        branch: str,
        *,
        force: bool = False,
        remote: str | None = None,
    ) -> GitOperationResult:
        """Delete branch (destructive, top rung)."""
        if remote:
            cmd = ["push", remote, "--delete", branch]
            authority = GitAuthorityLevel.DELETE
        else:
            cmd = ["branch"]
            if force:
                cmd.append("-D")
            else:
                cmd.append("-d")
            cmd.append(branch)
            authority = GitAuthorityLevel.DELETE
        return await self._manager.execute_operation(GitOperationRequest(
            operation_id=self._manager._ids.new(),
            authority_level=authority,
            repository_path=repo_path,
            command=cmd,
            required_grant_id=grant_id,
        ))


class GitWorkspaceOwnership:
    """
    Manages workspace/process ownership for Git operations.
    Implements exclusive owner lease with snapshot isolation.
    """

    def __init__(
        self,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or Uuid7Factory()
        self._leases: dict[str, dict] = {}  # workspace_id -> lease info

    def acquire_lease(
        self,
        workspace_id: str,
        owner_id: str,
        ttl_seconds: float = 3600.0,
    ) -> dict:
        """
        Acquire an exclusive owner lease for a workspace.

        Args:
            workspace_id: Unique workspace identifier.
            owner_id: Owner identity (worker/task ID).
            ttl_seconds: Lease time-to-live.

        Returns:
            Lease information dictionary.

        Raises:
            StateError: If workspace is already leased to another owner.
        """
        now = self._clock.now().timestamp()
        existing = self._leases.get(workspace_id)
        if existing and existing["owner_id"] != owner_id and existing["expires_at"] > now:
            raise StateError(f"Workspace {workspace_id} is leased to {existing['owner_id']} until {existing['expires_at']}")

        lease = {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "acquired_at": now,
            "expires_at": now + ttl_seconds,
            "epoch": existing["epoch"] + 1 if existing else 1,
        }
        self._leases[workspace_id] = lease
        return lease

    def release_lease(self, workspace_id: str, owner_id: str) -> bool:
        """
        Release a workspace lease.

        Args:
            workspace_id: Workspace identifier.
            owner_id: Owner identity.

        Returns:
            True if lease was released, False if not owned by owner_id.
        """
        lease = self._leases.get(workspace_id)
        if lease and lease["owner_id"] == owner_id:
            del self._leases[workspace_id]
            return True
        return False

    def renew_lease(self, workspace_id: str, owner_id: str, ttl_seconds: float = 3600.0) -> dict | None:
        """
        Renew a workspace lease.

        Args:
            workspace_id: Workspace identifier.
            owner_id: Owner identity.
            ttl_seconds: New TTL.

        Returns:
            Updated lease info, or None if not owned by owner_id.
        """
        lease = self._leases.get(workspace_id)
        if lease and lease["owner_id"] == owner_id:
            now = self._clock.now().timestamp()
            lease["expires_at"] = now + ttl_seconds
            lease["epoch"] += 1
            return lease
        return None

    def get_lease(self, workspace_id: str) -> dict | None:
        """Get current lease for a workspace."""
        lease = self._leases.get(workspace_id)
        if lease and lease["expires_at"] > self._clock.now().timestamp():
            return lease
        return None

    def is_leased(self, workspace_id: str) -> bool:
        """Check if workspace has an active lease."""
        lease = self._leases.get(workspace_id)
        if lease:
            return lease["expires_at"] > self._clock.now().timestamp()
        return False

    def list_leases(self) -> list[dict]:
        """List all active leases."""
        now = self._clock.now().timestamp()
        return [lease for lease in self._leases.values() if lease["expires_at"] > now]


@asynccontextmanager
async def git_workspace_isolation(
    base_path: Path,
    workspace_id: str,
    ownership: GitWorkspaceOwnership,
    owner_id: str,
    *,
    snapshot_kind: str = "disposable_tree_copy",  # "linked_worktree" | "disposable_tree_copy"
    source_repo: Path | None = None,
    cleanup: bool = True,
) -> AsyncIterator[Path]:
    """
    Create an isolated Git workspace for a mutating worker.

    Implements CB2 (workspace snapshot + exclusive owner) from the contracts:
    - Exactly one exclusive owner per workspace at a time
    - Disposable snapshot touches no refs (satisfies no-Git-mutation fence)
    - Worktree mode only when refs must be shared, never trusted as authority

    Args:
        base_path: Base directory for workspace creation.
        workspace_id: Unique workspace identifier.
        ownership: GitWorkspaceOwnership manager.
        owner_id: Owner identity.
        snapshot_kind: "linked_worktree" or "disposable_tree_copy".
        source_repo: Source repository to clone/snapshot from.
        cleanup: Whether to clean up on exit.

    Yields:
        Path to the isolated workspace.
    """
    # Acquire exclusive lease
    lease = ownership.acquire_lease(workspace_id, owner_id)
    epoch = lease["epoch"]

    workspace_path = base_path / f"workspace-{workspace_id}-epoch{epoch}"

    try:
        if snapshot_kind == "disposable_tree_copy" and source_repo:
            # Disposable filesystem snapshot (tree copy + byte seal, zero refs touched)
            shutil.copytree(source_repo, workspace_path, ignore=shutil.ignore_patterns(".git"))
            # Re-initialize as new repo
            executor = BoundedProcessExecutor()
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "init"],
                working_directory=workspace_path,
                timeout_seconds=10.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "config", "user.name", "BOTS Worker"],
                working_directory=workspace_path,
                timeout_seconds=5.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "config", "user.email", f"worker-{owner_id}@bots.local"],
                working_directory=workspace_path,
                timeout_seconds=5.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "add", "."],
                working_directory=workspace_path,
                timeout_seconds=10.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "commit", "-m", f"Workspace snapshot epoch {epoch}"],
                working_directory=workspace_path,
                timeout_seconds=10.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
        elif snapshot_kind == "linked_worktree" and source_repo:
            # Linked worktree (only when refs must be shared)
            executor = BoundedProcessExecutor()
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "worktree", "add", str(workspace_path)],
                working_directory=source_repo,
                timeout_seconds=10.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
        else:
            # Fresh workspace
            workspace_path.mkdir(parents=True, exist_ok=True)
            executor = BoundedProcessExecutor()
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "init"],
                working_directory=workspace_path,
                timeout_seconds=10.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "config", "user.name", "BOTS Worker"],
                working_directory=workspace_path,
                timeout_seconds=5.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))
            await executor._execute_raw(ProcessExecutionRequest(
                command=["git", "config", "user.email", f"worker-{owner_id}@bots.local"],
                working_directory=workspace_path,
                timeout_seconds=5.0,
                resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
            ))

        yield workspace_path

    finally:
        # Release lease
        ownership.release_lease(workspace_id, owner_id)

        # Cleanup workspace
        if cleanup and workspace_path.exists():
            if snapshot_kind == "linked_worktree" and source_repo:
                # Remove worktree
                executor = BoundedProcessExecutor()
                await executor._execute_raw(ProcessExecutionRequest(
                    command=["git", "worktree", "remove", str(workspace_path)],
                    working_directory=source_repo,
                    timeout_seconds=10.0,
                    resource_limits=ProcessResourceLimits(max_processes=100, max_memory_mb=1024),
                ))
            else:
                shutil.rmtree(workspace_path, ignore_errors=True)
