# Remote run instructions

1. Extract this pack into the B.O.T.S. checkout or another retained campaign-evidence
   location where the supervisor can read it without changing tracked product files.
2. Launch only `prompts/SUPERVISOR_LAUNCH.md`.
3. The supervisor must first verify `PACK_MANIFEST.json`.
4. The local checkout must resolve exactly to:
   - HEAD `0756904481ae884bb9e864e8e1e11fc4a27a72ff`
   - parent `7c2fe400d1f78e2c4398f3bb2461e8dceff5c6f7`
   - tree `7fe879035a01a87339dfe6319a435fa0e84c94bd`
5. Verify `origin/main` and tracked/index state. Preserve existing `work/**` and any
   unrelated untracked material. Do not reset/rebase/clean over drift.
6. Resolve OrgMem authority at
   `cc0c3c80348b1d798def65988102e0d8ad966730`.
7. Run the design campaign only. No tracked product mutation is authorized by parcel-v1.
8. Stop for Mick after the final design oracle on an exact design seal.
9. If Mick accepts the design, persist his exact adjudication, create a successor
   campaign record and immutable parcel-v2, lint it, and only then start implementation.

Mick may intervene conversationally. The campaign must never require him to operate
the Forge filesystem.

Configured API credentials may be used by the orchestration harness to reach the
authorized worker models, but secret values must never be printed, persisted in the
pack, copied into prompts, or placed in evidence.
