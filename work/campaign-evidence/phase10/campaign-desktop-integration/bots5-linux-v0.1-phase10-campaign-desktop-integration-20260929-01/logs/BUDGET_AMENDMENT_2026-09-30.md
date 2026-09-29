# Budget amendment — Mick, 2026-09-30 (verbatim)

Persisted verbatim as human authority for launches 33 and 34. `parcel-v2/BUDGET.json` is
immutable and is **not** modified; this amendment supersedes its ceilings for this campaign only.

> Increase the campaign total child-launch ceiling from 32 to 34.
> Increase the MiMo-V2.6 Flash ceiling from 4 to 6.
> MiMo launch #5 is authorized exclusively as a fresh final implementation oracle against exact
> sealed candidate v4 02fbcce4….
> It must independently verify, at minimum:
> the bridge_factory is actually invoked and the dock is operational, not merely constructible;
> the new widget tests instantiate and exercise the real dock path that previously escaped validation;
> the recovered lifecycle test file contains the complete intended prior contract coverage plus the
> three new tests, with no test loss or weakened assertions;
> the 1495-test T4 evidence is correctly attributable to exact candidate v4;
> D-10/D-11/D-12 are inspected in their exact recorded form and classified for acceptance;
> no tracked drift occurred after candidate sealing.
> If launch #5 returns PASS/PASS_WITH_LIMITATIONS with no candidate-changing defect, STOP FOR MICK
> at the same pre-commit boundary.
> If launch #5 finds an in-fence repairable defect, fix it under existing authority, run the
> required focused/broader validation, reseal, then MiMo launch #6 is authorized exclusively as the
> fresh oracle for that repaired candidate.
> If launch #6 finds another candidate-changing defect, or either oracle discovers a
> semantic/fence problem, STOP FOR MICK. No further launch expansion is implied.
> No staging, commit, push, ref mutation, OrgMem mutation, dependency change, or scope expansion is
> authorized.

## Effect on accounting

| Item | Before | After |
|---|---|---|
| Global child-launch ceiling | 32 (exhausted) | 34 |
| MiMo V2.6 Flash ceiling | 4 (exhausted) | 6 |
| Launches available | 0 | 2, both reserved exclusively for the implementation oracle |

Neither authorization permits any other route, role or scope. No launch may occur except #5 and,
only if #5 finds an in-fence repairable defect, #6.

## Second amendment — Mick, 2026-09-30 (verbatim request and confirmed terms)

Request (verbatim): "you can have 2 more GLM implementation workers."

Confirmed with Mick before spending, because launch accounting was the one place this campaign
already recorded a breach:

- Global child-launch ceiling: **34 to 36**.
- MiMo V2.6 Flash launch #6 remains reserved for the oracle against the repaired candidate.
- GLM 5.3 Flash family allowance: **12 used, raised to 14**; these two are the **last** GLM
  launches authorized. The earlier 12-versus-8 overage is folded into this explicit amended
  ceiling rather than treated as a second, separate breach.
- The two GLM launches are **implementation** workers only (repair and test authoring), not
  oracles.

Resulting arithmetic: 33 used (32 + MiMo oracle #5) + 2 GLM implementation + 1 reserved MiMo
oracle #6 = 36 of 36. No other route, role or scope is expanded.
