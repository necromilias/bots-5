# B.O.T.S. 5 Completion-Telemetry Live Canary

Date: 2026-08-28

Classification: **PASS** for the completion-telemetry objective. The single job correctly ended
`failed` with CLI exit `1` because one real provider response returned `finish_reason: "length"`;
the response was conservatively marked incomplete and synthesis was blocked.

## Sealed implementation and preflight

- Repository: `/home/mick/Documents/Codex/2026-08-28/files-pasted-by-the-user-implement/bots-5`
- Git top-level: identical to the repository path
- Branch: `v0.1-worker-boundary-hardening`
- HEAD and remote feature ref: `592c1e371ee39019bc2676bc63b6cc204153041e`
- HEAD parent: `f90ee5b05550a6189635764c1a624ff824261a84`
- Local and remote `main`: `ccbde6bf658192e0d29ef6eef29bae6c55c7bc79`, unchanged before and after the job
- Index/worktree: clean; zero untracked files before the provider call
- Pre-run tests: `env -u OPENROUTER_API_KEY .venv/bin/python -m pytest`, exit `0`, 58 passed in 0.19s
- Manifest validation: `env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/v0.1-live-conformance/canary-job.json`, exit `0`
- Credential: present and non-empty in the documented Fish login environment; value neither printed nor persisted
- Manifest SHA-256: `031af9e38a68cb9fce3eb988bdd261077e9ac8cbed507d514d1ed4969cbacdde`

The manifest declared OpenRouter, three parallel worker calls and at most one conditional synthesis
call. Models and output limits were two `openai/gpt-5.6-luna` workers at 650 tokens, one
`google/gemini-3.7-flash` worker at 650 tokens, and conditional Gemini synthesis at 900 tokens.
Every stage timeout was 90 seconds; the job timeout was 240 seconds; maximum worker parallelism was
3; and the known-cost pre-synthesis gate was USD 0.01.

Immediately before execution, OpenRouter's public model catalogue reported Luna rates of USD
0.0000002/input token and USD 0.0000012/output token, with the declared long-context override, and
Gemini rates of USD 0.000000375/input token and USD 0.000001875/output token. Bounding each possible
request by the advertised context window and configured output limit yielded a deliberately loose
four-request maximum plausible cost of USD `1.630577000`. The USD 0.01 manifest value is not a hard
whole-job cap.

## Single live execution

Exact command:

```text
fish -lc 'exec .venv/bin/bots5 run examples/v0.1-live-conformance/canary-job.json'
```

- Run ID: `bots5-v0.1-live-conformance-canary-20260828T125203Z-7214f83a`
- Persisted interval: `2026-08-28T12:52:03.517Z` through `2026-08-28T12:52:10.531Z`
- Command wall time: 7.104100546 seconds
- Exit: `1`
- Provider requests observed: 3; retries observed: 0; synthesis requests observed: 0
- Final run state: `failed`

| Stage | Returned model | State | Finish reason | Complete | Tokens P/C/R/T | Cost USD |
| --- | --- | --- | --- | --- | --- | ---: |
| extractor | `openai/gpt-5.6-luna` | succeeded | `stop` | true | 419/173/91/592 | 0.0002914 |
| analyst | `openai/gpt-5.6-luna` | succeeded | `stop` | true | 447/300/133/747 | 0.0004494 |
| adversary | `google/gemini-3.7-flash` | succeeded | `length` | false | 483/646/599/1129 | 0.001392375 |
| synthesis | not called | skipped | not available | not available | null/null/null/null | 0 |

Aggregate persisted usage was 1349 prompt, 1119 completion, 823 reasoning, and 2468 total tokens.
All provider costs were known; the exact aggregate was USD `0.002133175`. The aggregate and every
stage cost reproduce exactly from the provider token counts and preflight prices. No accounting
field changed or collapsed an unknown value to zero; synthesis alone records known zero because no
synthesis request was launched.

## Mechanical telemetry verification

- Each of the three completed provider responses persists its raw finish reason in both its stage
  JSON record and `run.json`.
- The persisted boolean is true exactly for the two `stop` responses and false for the live
  `length` response.
- `events.jsonl` contains exactly three `request_sent` events, one for each worker. It contains no
  synthesis start or request, then records `stage_skipped` and `synthesis_blocked` with
  `reason: dependency_incomplete` and `incomplete_dependencies: ["adversary"]`, followed by
  `run_failed`.
- `stages/synthesis.json`, the events, `run.json`, `bots5 status`, and CLI exit `1` agree on the
  failed/blocking outcome. No `result.md` or synthesis Markdown exists because synthesis never ran.
- Disk-only `bots5 status` exited `0` and displayed extractor/analyst as complete with `stop`,
  adversary as incomplete with `length`, synthesis as unavailable/skipped, and the exact aggregate
  cost. Disk-only `bots5 inspect` exited `0` for all four stages and reconstructed the same values.
- The live response therefore covers the non-`stop` path. It does not live-test an absent or
  malformed finish reason. The passing offline suite separately covers absent, malformed,
  `content_filter`, `refusal`, future, and other non-`stop` values, disk reconstruction, successful
  synthesis, and `dependency_incomplete` synthesis blocking.
- Exact credential-value scanning found zero matches in runtime and packaged evidence. Generic
  authorization, bearer, OpenRouter-key, and private-key scans also found zero matches.

## Post-run closure

- `env -u OPENROUTER_API_KEY .venv/bin/python -m pytest`: exit `0`, 58 passed in 0.18s
- `env -u OPENROUTER_API_KEY .venv/bin/bots5 validate examples/v0.1-live-conformance/canary-job.json`: exit `0`
- All 11 runtime files are byte-identical to their packaged evidence copies.
- Runtime payload composite SHA-256: `4e49ced6a5ff6c66d6816af7884c490b69775d7af8fda1087c94535aa456f101`
- Complete new evidence payload excluding this report composite SHA-256: `996ead1126e4d7b0706fb3840bd0b8e108613dea1476d745db5f09b8ebff138f`
- Prior canary evidence composite SHA-256 before and after: `6405691afdd92cc15a8b850afa3396eebaa37a4bdb4d091fd23bfa62a059a941`
- `run.json` SHA-256: `b733b3aa78152c8baa9aa0047a7e5a48e891658784e78307d8a40df94d3e60ac`
- `events.jsonl` SHA-256: `cb7a9218e9a95064b58d5815f84a4325eb3664d64ea0096a568ff06e374f022e`
- `usage.json` SHA-256: `a950f663d2a15f2178d7cd7613f081c591e3676d6a1010db5effab20d91445eb`
- Final tracked diff and index diff: empty. Every untracked path is under this new evidence directory.
- No commit, push, fetch, amend, rebase, retry, or remote-ref mutation was performed.
