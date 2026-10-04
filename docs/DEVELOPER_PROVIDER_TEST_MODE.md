# Temporary developer provider smoke mode

This launch-only path is authorized for manual UI/provider/model testing in the
`phase11/ui-polish` worktree. It is **INEXACT / PROVIDER TEST MODE**, not Phase 6
compliant and not a production provider solution. Do not use its success as
Phase 6 acceptance evidence.

From this worktree, launch with the explicit `--developer-provider-test-mode`
flag. The exact supported-runtime command on Mick's current host is:

```bash
cd /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1-ui-polish
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src BOTS5_ROOTED_VFS_LIBRARY=/home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1/build/native/libbots5_rooted_sqlite_vfs.so /home/mick/Documents/Codex/2026-09-03/bots-5-linux-v0.1-phase1/.venv314/bin/python -m bots5.bootstrap.desktop --data-root /tmp/bots5-ui-polish-test --developer-provider-test-mode
```

This uses Mick's existing `/tmp/bots5-ui-polish-test` installation and the
configured-provider router. The original developer amendment used migration head
`0019_phase11_generation_settings` without adding a migration. The separately authorized production provider-managed contract now adds0020.
The default XDG root was not used or migrated.
The historical `--backend fake` default names that router's bootstrap; it does
not restrict the model selector to fake models. Do not add `--backend
local_openai`. An explicit `--data-root /absolute/path` remains available for
an existing separate test installation. Do not launch a second process against
an installation already held by another desktop session.

Every window has a permanent amber warning banner and the title marker
`INEXACT / PROVIDER TEST MODE`. Select an existing OpenRouter connection/model
through the usual settings/model selector, then send a trivial short message.
The existing credential, availability, request capability, output-limit, and
provider payload gates still apply. The mode does not override or manufacture
capabilities. The provider-specific max-output wire alias now survives manual support overrides.

Only the current message is sent by this smoke path. Deterministic context
budgeting, history selection, and attachment guarantees are unavailable.
Attachment staging and explicit context planning reject; Attach is disabled.
Keep requests short: this mode does not establish an exact pre-dispatch context
budget. Conversation messages remain durable, but prior turns are not included
in subsequent smoke requests.

The mode uses the existing non-Phase-6 configured preparation path. Attempts
retain its existing v2 evidence (or existing v4 generation-settings evidence
when extended controls are used); no v3 ContextPlan, exact accounting adapter,
or Phase 6 compliance claim is created. Existing Phase 6 evidence is untouched.
No mode flag is stored in provider settings or the database. Closing the
process and launching without the flag restores normal production admission: OpenRouter uses provider-managed context, while registered exact paths remain exact and generic providers remain fail-closed. See [the production contract](PROVIDER_MANAGED_CONTEXT_ARCHIVE_V3.md).

There is no environment-variable activation, persisted toggle, automatic
fallback, migration, or new dependency. Automated tests use synthetic
credentials and mocked streams; they explicitly distinguish developer smoke
success from production accounting. Remove this temporary mode in a separately
authorized follow-up when it is no longer needed.
