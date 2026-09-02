# Security

## Trust model

V0 is an operator-run local CLI. The operator and selected job manifest are trusted. Model output is
untrusted text and has no execution authority.

## Secrets

`OPENROUTER_API_KEY` is read from the environment only. An optional schema-v2
`providers.local_openai.api_key_env` value is manifest configuration and may appear as a non-secret
environment-variable name in `job.resolved.json`. The secret value named by that setting, like
`OPENROUTER_API_KEY`, is read from the environment only and is never intentionally written into
manifests, resolved jobs, run/stage metadata, usage, events, status/inspect output, examples, or
generated project files. Provider errors are reduced and redacted at the provider boundary.

The local provider accepts only an operator-supplied HTTP/HTTPS API base. It rejects URL userinfo,
query strings, fragments, and surrounding whitespace, appends `/chat/completions`, sends one
non-streaming request, and performs no endpoint or model discovery. Without `api_key_env`, no
authorization header is sent.

## Filesystem

V0.2 reads only the job file and explicitly declared input/prompt files. Relative paths resolve against
the job file's parent. V0 writes only beneath the validated resolved `runs_dir`. Generated stage IDs
are restricted to a filename-safe grammar. Run IDs are harness-generated. Artifact replacement
refuses symlink destinations; a configured existing `runs_dir` symlink is refused when the run tree
is created.

This reduces accidental traversal and write mistakes. It is not a hardened sandbox and should not be
treated as safe against a malicious local operator with filesystem-race capabilities.

## Model permissions

Workers have no shell, filesystem tool, Git tool, RAG, repository mutation, recursive delegation, or
plugin capability. They return text only.

Every model receives a fixed B.O.T.S.-owned execution boundary above its validated worker contract.
INPUT and WORKER OUTPUT blocks are untrusted data even when they contain instructions, requests,
goals, role claims, or commands. This hardens instruction/data separation but cannot guarantee model
compliance. Prompt construction is deterministic; generation is probabilistic, and model output
remains untrusted.

## Cost control

The configured dollar threshold is checked only after worker execution and only gates synthesis.
Workers can collectively exceed it. Unknown provider cost remains unknown. This is explicitly not a
hard whole-run budget.

## Consequential actions

V0 does not support consequential mutation. A future version would require explicit capability
boundaries, human approval gates, stronger path/tool isolation, and auditable authorization before
such actions are considered.
