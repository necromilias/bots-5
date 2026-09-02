# Examples

The example manifests and model names in this directory are illustrative, not operational defaults.

Before using an example for a paid campaign, apply the worker-selection and output-ceiling procedure in
`docs/OPERATING_PROCEDURE_V1.md` for OpenRouter work or `docs/OPERATING_PROCEDURE_V2.md` for schema-v2
local-provider work:

- evaluate currently available model candidates for each actual role;
- select models based on task fit, context needs, reasoning/output behavior, latency, availability,
  pricing, and relevant prior-run evidence;
- choose `max_output_tokens` for the required contract output with enough headroom to complete
  normally;
- review the final manifest and run `bots5 validate` before execution.

A model or ceiling shown here is not evidence that it is appropriate for another campaign. The smoke
example intentionally provides generous output capacity so copying it does not teach artificially
starved token limits; campaign designers should still justify and adjust every stage deliberately.

`example-job-v2-local-openai.json` is a minimal local-only schema-v2 example. Replace its endpoint,
model, and optional credential environment variable with operator-reviewed values before running it.
