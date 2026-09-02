# Operating Procedure V2

This procedure covers zero-spend validation and deliberate execution of schema-v2 jobs using the
built-in local OpenAI-compatible provider. It does not authorize provider execution, endpoint changes,
campaign publication, Git mutation, or Organisational Memory updates.

## 1. Review the job

Confirm that the job uses `schema_version: 2`, has the required closed top-level `providers` object,
and declares only `openrouter` or `local_openai` at each stage. For every local stage, review:

- `providers.local_openai.base_url` as the intended HTTP/HTTPS API base;
- the fact that B.O.T.S. appends `/chat/completions`;
- the selected model name and output ceiling;
- whether authentication is required and, if so, the exact `api_key_env` name;
- the absence of URL userinfo, query strings, and fragments.

The provider is generic and does not discover endpoints or models. A local endpoint may be bound to a
loopback or otherwise operator-controlled interface, but endpoint reachability and service identity
remain operator responsibilities.

## 2. Validate without network

From the repository checkout:

```bash
env -u OPENROUTER_API_KEY bots5 validate JOB.json
```

Validation reads the manifest and declared UTF-8 inputs/prompts only. It makes no provider request,
does not resolve credentials, and creates no run directory. For a local-only job, `OPENROUTER_API_KEY`
is not needed. If authentication is configured, validation still does not require that local
environment variable; credential availability is checked when `run` constructs the selected provider.

## 3. Preflight credentials and cost

For each OpenRouter stage, make `OPENROUTER_API_KEY` available only in the execution environment. For
each authenticated local stage, make only its configured `api_key_env` available. Never put a token in
the manifest, endpoint URL, prompt, input, example, command transcript, or an artifact directory.

The local provider does not invent a cost. If valid provider cost telemetry is absent or invalid, the
stage cost remains unknown. The existing synthesis gate still compares only the known worker-cost
subtotal; unknown or partial cost does not itself block synthesis.

## 4. Execute intentionally

After human review and any required approval for the local service request:

```bash
bots5 run JOB.json
```

B.O.T.S. sends one non-streaming chat-completions request per reached stage, with the existing
`CompletionRequest` payload semantics. There are no retries, streaming responses, endpoint/model
discovery, provider fallback, tools, or stage reuse/resume.

Every worker and synthesis stage uses its declared provider. A missing provider mapping is a
programmatic error before run creation; the CLI constructs only providers actually declared by the
job. A local-only CLI run therefore does not require `OPENROUTER_API_KEY`.

## 5. Inspect durable evidence

Use the run ID printed by the command:

```bash
bots5 status RUN_ID --runs-dir PATH
bots5 inspect RUN_ID STAGE_ID --runs-dir PATH
```

These commands are disk-only. Review `run.json`, `job.resolved.json`, `events.jsonl`, `usage.json`,
stage JSON, stage text, and `result.md` as applicable. Confirm:

- the persisted provider ID matches the declared stage;
- completion is `complete` only for exact `finish_reason: "stop"`;
- incomplete output remains evidence but makes the overall run fail;
- local cost is unknown when valid cost telemetry was not returned;
- `job.resolved.json` contains no resolved secret value;
- provider errors contain no authorization value.

For `message.content: null`, preserve the shared normalization rules: missing or untrustworthy finish
reason is malformed; `stop` is an empty-model error; a trustworthy non-`stop` reason is a valid empty
incomplete result with telemetry preserved. Reasoning text and reasoning details are never output.

## 6. Human usefulness review

Normal completion and clean artifacts do not establish usefulness or authority. Separately record the
human decision about whether each output is fit for its stated low-stakes purpose, whether the local
service behavior was acceptable, and whether any finding needs independent verification.
