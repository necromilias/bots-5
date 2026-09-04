# B.O.T.S. Linux v0.1 Draft 1 UI shell implementation and acceptance

Status: **IMPLEMENTED / OPERATOR-ACCEPTED AS GOOD ENOUGH FOR NOW**

This report records the bounded first-pass UI shell and its operator acceptance. It does not promote the
UI design into final or immutable authority.

## Authority and boundary

The implementation used the provisional `docs/LINUX_V0_1_UI_UX_DRAFT_1.md` together with the approved
Draft 1 adjudications: ordinary native Linux window chrome; one read-only model identity pill; visible but
honestly inert future affordances; plain canonical text/Markdown; a dismissible inspector; durable chat,
message, and generation state only; and real offscreen Qt/qasync validation. Blood Oath styling and controls
remain deferred.

The approved baseline is `main` / `HEAD` / local `origin/main` at
`f9f7ff4cf428d80e0064cf305a507fbe5dca9c68`. Phase 1, Phase 2, and Phase 3 remain closed. Phase 4 has not
begun. The candidate remains unstaged and uncommitted.

## Implemented shell

- A compact top bar sits inside the ordinary native Linux window frame and includes B.O.T.S. identity,
  one read-only model identity pill, and the accepted disabled/inert future controls.
- A collapsible left rail exposes chat navigation and new-chat access. When collapsed, its rounded-square
  controls form a compact vertical stack; when expanded, the existing horizontal arrangement returns.
- The central work surface contains the chat title, transcript, and minimal bottom composer.
- The composer sends with **Enter** and inserts a newline with **Shift+Enter**.
- **Send** starts normal or same-chat immutable Edit/Regenerate generation. **Stop** remains available while
  the active generation is running and cancellation persists the existing terminal state.
- **Copy**, **Edit**, and **Regenerate** use the existing core commands and preserve immutable revisions;
  cross-chat stale edit context is cleared and cannot dispatch against another chat.
- Tune, Settings, Branch, attachment, and tool affordances remain visible but disabled/inert with honest
  tooltips. The model pill is read-only and is not a model selector.
- The right-side inspector/details surface is read-only and dismissible.
- The visual direction is restrained graphite/near-black with blue accents. Rich Markdown rendering,
  syntax highlighting, and Blood Oath treatment are not part of this slice.

## Generation activity indication

While existing generation lifecycle state is active, the composer shows a persistent `● Generating…`
indicator. The currently streaming assistant message receives a restrained blue treatment and a matching
status label. The indicator and active treatment clear when the lifecycle reaches COMPLETE, INCOMPLETE,
FAILED, or ABORTED. No progress percentage, token rate, or provider status is invented.

## Deterministic evidence

- Focused Draft 1 tests: **10 passed**.
- Offscreen Qt/qasync UI subset: **14 passed**.
- Full deterministic suite: **254 passed, 1 skipped**; the skipped test is the explicitly opt-in local-Qwen
  acceptance.
- Repeated lifecycle checks: **35/35 passed**.
- `compileall`: **PASS**.
- `git diff --check`: **PASS**.
- `git fsck --full --no-dangling`: **PASS**.
- `OPENROUTER_API_KEY` and local-Qwen opt-in variables were unset for validation/audit subprocesses;
  ordinary socket blocking remained active. No provider, API, model, or network call occurred.

Fresh independent Sol/xhigh review returned **PASS**. Hands-on operator acceptance is recorded as:
**“good enough for now.”** No additional visual/theme polish is required before moving on; further UX changes
should be driven by later real use.

## Deliberate deferred UX findings

- Chat switching during active generation remains intentionally gated. This is observed operator friction
  and evidence for Phase 4 concurrency/workspace behavior; this pass does not add background generation,
  concurrent chat navigation, or attention state.
- Markdown remains plain text. Native rich Markdown rendering remains deferred.
- Collapsed-rail per-chat bubbles and activity indicators remain deferred. Phase 4 is the appropriate point
  to revisit them when background/concurrent chat state actually exists.

The provisional status of `LINUX_V0_1_UI_UX_DRAFT_1.md` is preserved. This report is an implementation and
acceptance record, not final UI design authority.
