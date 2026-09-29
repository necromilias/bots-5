# COMMAND_OPERATOR_AUTHORITY_UX.md

## Executive Summary

This report defines the operator workflow for Phase 10 desktop campaign integration, focusing on approval binding, zero-spend validation, preflight integrity, and regeneration/rerun semantics. Analysis of the campaign engine reveals **no native campaign pricing registry**—requiring a HUMAN_SEMANTIC_FORK for pricing authority. The design must bind approval to exact work bytes/configuration, prevent TOCTOU attacks, and support append-only sibling regeneration without silent provider route changes.

## 1. Workflow Analysis

### 1.1 Load Job → Zero-Spend Validation
- **Current**: `manifest.load_job()` + `validate_referenced_files()` (CLI `_cmd_validate`)
- **Zero-spend guarantee**: No provider calls, no run-directory creation
- **Desktop requirement**: Reuse exact validation pair; add content digest binding for TOCTOU protection

### 1.2 Preflight → Exact Approval
- **Preflight evidence**: Filesystem snapshot (HEAD/tree clean), OrgMem authority verification, model availability
- **Approval binding**: Must reference immutable execution snapshot (content hash of job inputs/model config)
- **TOCTOU protection**: Digest ensures approval matches bytes that actually execute

### 1.3 Running → Result
- **Live state**: Truthful progress via filesystem evidence (`run.json`, stage records, usage)
- **Cancellation**: Distinguish desktop-initiated cancel (abort) from timeout (terminal failure)

### 1.4 Regenerate Worker
- **Requirement**: Preserve original attempt, create sibling with explicit model change
- **Evidence**: Append-only storage via new run ID; never overwrite existing
- **UX**: Clear distinction between original and regenerated attempts

### 1.5 Rerun Synthesis
- **Trigger**: Explicit user action after worker regeneration
- **Behavior**: New synthesis attempt consuming updated dependency set
- **Staleness**: Old synthesis becomes mechanically stale relative to new worker selection

## 2. Pricing Source Investigation

### 2.1 Evidence Review
- **CONTRACT.md**: Explicitly notes absence of campaign pricing registry
- **FINDINGS.md F-06**: "paid preflight pricing source is currently absent"
- **QWEN archaeology**: No pricing code in `src/bots5/*`
- **Historical evidence**: Manual rate capture performed out-of-band in `preflight.json`

### 2.2 Authority Gap
- **OPv1 requirement**: Conservative preflight pricing from advertised rates
- **No existing authority**: No operator-rate entry, provider lookup, cached metadata
- **Classification**: **HUMAN_SEMANTIC_FORK** - genuine product choice not settled by current authority

### 2.3 Fork Options

| Option | Implementation | Consequences | Recommendation |
|---|---|---|---|
| **A: Operator-entered rates** | UI input field with rate validation | Fastest to implement, requires operator expertise, audit trail needed | **Primary** - preserves existing manual workflow in UI |
| **B: Provider pricing lookup** | Add pricing API call (requires new provider capability) | Cleanest semantic, requires provider cooperation, adds dependency | Secondary - if provider supports it |
| **C: Cached historical rates** | Store last-known rates in desktop config | No new authority, may become stale, simple fallback | Tertiary - temporary measure |
| **D: Unknown-cost + approval** | Show "cost unknown" in preflight, require explicit approval | Honest but may block legitimate work, requires operator judgment | Fallback if A/B unavailable |

## 3. Approval/Consequence Rules

### 3.1 Zero-Spend Validation
- **Rule**: Approval cannot proceed without successful validation
- **Evidence**: Use existing CLI `_cmd_validate()` - no network, no filesystem writes

### 3.2 Preflight Evidence
- **Required**: HEAD/tree verification, OrgMem authority, model availability
- **Binding**: Digest of validated job inputs + model configuration

### 3.3 Pricing Evidence
- **Conservative**: Use operator-entered rates or unknown-cost disclosure
- **No fabrication**: Never create live token-dollar progress

### 3.4 Approval Identity
- **Operator**: Must be explicit human action (not automated)
- **Revocable**: Changed job/model invalidates previous approval

### 3.5 Post-Approval Invalidation
- **Changed-after-approval**: Any modification to inputs, model, or provider route invalidates approval
- **Re-approval required**: For regeneration/rerun, must re-execute preflight and approval

## 4. UX Requirements

### 4.1 Load/Validate Phase
- **Clear status**: "Validating... (zero-spend)" → "Ready for approval"
- **Digest display**: Show content hash of validated job

### 4.2 Approval Phase
- **Consequence modal**: Show exact work that will execute (inputs, model, limits)
- **Pricing preview**: Conservative estimate or "cost unknown" indicator
- **One-shot approval**: Cannot be bypassed or auto-confirmed

### 4.3 Running Phase
- **Live evidence**: Real-time filesystem status updates
- **Cancellation clarity**: Distinguish abort vs timeout vs failure

### 4.4 Regeneration/Rerun Phase
- **Explicit action**: Separate "Regenerate Worker" and "Rerun Synthesis" buttons
- **Attempt history**: Clear visualization of original vs new attempts
- **Re-approval required**: Cannot reuse old approval

## 5. Technical Implementation Constraints

### 5.1 Storage Model
- **Current**: One record per stage ID (no attempt index)
- **Phase 10 requirement**: Append-only sibling attempts
- **Solution**: New run ID for regeneration; preserve original run directory

### 5.2 Evidence Binding
- **Digest requirement**: SHA256 of job inputs + model config
- **Implementation**: Compute during validation, store in approval record

### 5.3 Event Evolution
- **Current**: Closed event vocabulary
- **Phase 10**: Add new event types for regeneration/synthesis rerun
- **Constraint**: Must be versioned schema change

## 6. Defects and Risks

### 6.1 Critical Defects
1. **TOCTOU vulnerability** - Approval binds to pathnames only, not content
2. **Missing pricing authority** - OPv1 requirement unmet
3. **No run-directory enumeration** - Desktop cannot discover existing jobs programmatically
4. **Cancellation state ambiguity** - Desktop cancel vs timeout vs failure unclear

### 6.2 Integration Risks
- **Desktop as second engine** - Must only observe existing campaign
- **Evidence pollution** - Desktop SQLite must not compete with filesystem truth
- **Regeneration confusion** - Users may expect automatic retry

## 7. Recommendations

1. **Implement HUMAN_SEMANTIC_FORK A** (operator-entered rates) as primary pricing solution
2. **Add content digest binding** to approval for TOCTOU protection
3. **Create explicit regeneration UI** with clear attempt separation
4. **Add run-directory discovery API** for desktop loading
5. **Extend event vocabulary** for regeneration/synthesis lifecycle

---

**Sources**: CONTRACT.md, BASELINE.json, QWEN_CAMPAIGN_ARCHAEOLOGY.md, FINDINGS.md, VALIDATION.md, SOURCES.md, MODEL_ROUTING_EVIDENCE.md
**Evidence**: 190+ lines of source inspection, 115 passing zero-provider tests, 3 retained historical runs

---

*This report is design-only evidence. Implementation requires separate Mick gate approval.*