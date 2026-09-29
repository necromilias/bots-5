# Phase 10 model-routing evidence

Date assessed: 2026-09-29.

## Weighting rule

B.O.T.S. Operating Procedure v1 says observed campaign behavior overrides prior
model-selection rationale when a route repeatedly fails mechanically or proves
operationally unsuitable. Creator benchmarks are useful secondary evidence but are
not directly comparable across harnesses, context sizes, effort settings or task
distributions.

## Observed B.O.T.S. campaign evidence

### DeepSeek V4.1 Flash — retained supervisor
Observed:
- retained and coherently supervised the approximately 26-hour Slice D campaign;
- preserved human gates, continuation state, worker evidence and repair routing;
- continued effectively through the Slice E design/implementation campaign;
- low cost relative to wall-clock duration.

Decision: retain as supervisor/orchestrator, not independent reviewer.

### Qwen3.8 Flash — archaeology and cross-family engineering
Observed:
- Slice D archaeology produced a broad, useful reconstruction of the live recovery
  surfaces and dependencies;
- handled large repository context well.

Decision: primary archaeology; later fresh broad implementation falsifier and
repo-grounded cross-family diagnosis. It is not the primary writer.

### Gemini 3.8 Flash — architecture
Observed:
- strong architecture output in Slice D/E design campaigns;
- benefits from fresh independent falsification rather than reviewing its own design.

Decision: primary Phase 10 architecture specialist. GPT-6 Luna, not Gemini, performs
the fresh design falsification.

### MiniMax M2.7 — failure forensics
Observed:
- Slice D failure-forensics report enumerated dozens of crash/failure classes and
  surfaced concrete defects used by the supervisor.

Decision: lifecycle, concurrency, partial-run, provider-uncertainty and shutdown
forensics; targeted implementation review where material.

### Step 3.7 Flash — validation/proof architecture
Observed:
- strong validation planning in Slice D;
- Slice E proof-sufficiency review found four real issues, including a supposedly
  required test selector that actually collected zero tests.

Decision: validation architecture and targeted post-implementation proof review.

### GLM 5.3 Flash — bounded implementation
Observed:
- effective primary implementation/repair family across Slice D/E;
- one broad Slice D feasibility request returned null twice, while a narrower bounded
  request succeeded.

Decision: feasibility/fence after the other design specialists; primary writer after
the Mick gate. Keep tasks bounded and explicit.

### GPT-6 Luna — fresh falsification / difficult diagnosis
Observed:
- Slice E design falsification found seven real design findings;
- its repair verification caught an incomplete F-05 repair.

Decision: fresh independent design falsifier; difficult cross-family diagnosis if
implementation evidence conflicts or a defect class recurs.

### MiMo V2.6 Flash — final oracle
Observed:
- repeatedly found real design/implementation issues missed by other families;
- in Slice E it caught the vacuous nonexistent `phase9` pytest-marker plan plus three
  additional design defects;
- in Slice D it found consequential recovery defects after other reviewers passed.

Decision: final design oracle and final implementation oracle. Preserve fresh context.

### Solar Pro 4 — excluded from Phase 10
Observed:
- Slice E post-seal review entered a pathological loop trying to obtain test counts,
  issuing roughly dozens of pytest invocations and violating an explicit serial-test
  constraint by launching multiple pytest commands in a turn.

Creator evidence is respectable, but OPv1 explicitly gives observed campaign behavior
priority.

Decision: **no Phase 10 role; max launches 0.**

### Command A+ — cautious design-only trial
Observed B.O.T.S. evidence: none yet.

Decision: one low-risk read-only design specialist for operator consequence/approval
UX. It does not own architecture, implementation or final review. This gives us useful
new evidence without placing an unproven family on the critical implementation path.

### Qwen3 Coder Next — mechanical helper only
Observed B.O.T.S. evidence: limited.

Decision: mechanically specified Qt/test/refactor work after semantics are sealed.
Not product/authority design, not final review.

### Jamba Large 1.7 — optional final reviewer only
Observed B.O.T.S. engineering evidence: none.
Mick restriction: if Jamba is used, it may only be the final reviewer.

Decision: optional, max one launch, final implementation reviewer only, normally
unused. MiMo may follow solely as the distinct final oracle.

### Mistral Small 2603 — no role
No campaign evidence demonstrating a Phase 10 niche strong enough to displace a proven
family.

Decision: no role.

## Current creator-published evidence

These claims inform routing but do not override observed B.O.T.S. behavior.

### DeepSeek V4.1 Flash
Official DeepSeek change log reports:
- Terminal-Bench 2.1: 90.6
- DeepSWE v1.1: 74.2
- NL2Repo-Bench: 65.4
and describes V4.1 Flash as the current high-throughput Flash model.

Sources:
- https://api-docs.deepseek.com/updates/
- https://www.deepseek.com/en/news/deepseek-v4-1-flash/

### Gemini 3.8 Flash
Google describes Gemini 3.8 Flash as engineered for long-horizon software
engineering and autonomous agents. Google reports:
- DeepSWE v1.1: 73.7%
- Terminal-Bench 2.1: 89.4%
- 1,048,576-token input limit.

Sources:
- https://deepmind.google/models/gemini/
- https://deepmind.google/models/gemini/flash/
- https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash

### Qwen3.8 Flash Next
Official Qwen model card reports:
- DeepSWE 1.1: 58.7
- SWE-bench Pro: 62.5
- SWE-bench Multilingual: 81.0
- NL2Repo-Bench: 48.1
- CoWorkBench: 73.9
- native 262,144 context, extensible to 1,000,000.

Source:
- https://huggingface.co/Qwen/Qwen3.8-Flash-Next

Runtime note: the configured route is `qwen/qwen3.8-flash`; preflight must verify the
actual selectable provider route/version instead of silently assuming an alias is the
same build as this model card.

### GLM 5.3 Flash
Official model-card evaluation metadata reports:
- Terminal-Bench 2.1: 84.3
- DeepSWE v1.1: 63.4.

Source:
- https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/.eval_results/GLM-5.3-Flash.yaml

### MiniMax M2.7
MiniMax reports:
- SWE-Pro: 56.22%
- VIBE-Pro: 55.6%
- Terminal Bench 2: 57.0
- NL2Repo: 39.8
and explicitly highlights live-production debugging/troubleshooting.

Source:
- https://www.minimax.io/news/minimax-m27-en

### Step 3.7 Flash
Official StepFun model-card material reports:
- SWE-Bench PRO: 56.3
- Terminal-Bench 2.1: 59.5
and describes it as built for live engineering tasks.

Source:
- https://huggingface.co/stepfun-ai/Step-3.7-Flash

### MiMo V2.6 Flash
Xiaomi states V2.6 RL tasks include software engineering and vulnerability
reproduction. Its published RL data reports improvements including SWE-bench
Verified 61.1→66.2 and Terminal Bench 2.1 37.1→52.8 for the cited training line.

Source:
- https://mimo.mi.com/docs/en-US/news/latest/v2-6

### GPT-6 Luna
OpenAI describes GPT-6 Luna as the efficient focused/high-volume GPT-6 model and
documents:
- 1,050,000-token context;
- 128,000 max output;
- reasoning effort through `max`.

Source:
- https://developers.openai.com/api/docs/models/gpt-6-luna

Internal Slice E falsification evidence, rather than a creator benchmark, is the main
reason it receives the independent-falsifier role.

### Solar Pro 4
Upstage reports:
- Terminal-Bench 2.1: 57.0
- SWE-Bench Verified (OpenHands): 70.6
- 512K context / up to 128K output
and targets long complex agent workloads.

Source:
- https://www.upstage.ai/blog/en/solar-pro-4

Despite those published claims, the B.O.T.S. Slice E review behavior removes Solar
from this campaign's critical path.

### Command A+
Cohere calls Command A+ the strongest agentic model in the Command family and reports
Terminal-Bench Hard reaching 25% in its launch material. Current docs list 128K
context and 64K max output.

Sources:
- https://cohere.com/blog/command-a-plus
- https://docs.cohere.com/docs/command-a-plus

### Qwen3 Coder Next
Qwen describes it as an open-weight model designed specifically for coding agents,
with long-horizon reasoning, complex tool use, execution-failure recovery and 256K
context.

Source:
- https://huggingface.co/Qwen/Qwen3-Coder-Next

### Jamba 1.7
AI21's public material emphasizes grounding/factuality and 256K long context rather
than coding-agent leadership.

Source:
- https://www.ai21.com/blog/grounding-bedrock-enterprise-ai/

## Routing conclusion

The campaign intentionally optimizes **independence plus observed reliability**, not
benchmark leaderboard coverage. More available models do not enlarge the worker or
repair budget.
