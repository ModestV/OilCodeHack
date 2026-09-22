# Cetane/T95 temporal audit — fixed before model scores, 2026-09-21

Targets: distinct imported lims.ht.2.CetaneNumber and lims.ht.2.95%.T.
Audit raw workbook headers/counts/duplicates against imported records.
Do not merge other sample points into the labels or forward-fill labels as samples.
Horizon 0 and 180 minutes separately. Labs become available at sample+4h.
Inputs: last published value, last three published target median, same-point
D15/T50/T90 and input-point T95; controls T6/F9/P13/F2 available by origin.
Published laboratory predictors must have sample age <=48h, controls <=30min.
Research persistence of cetane may use <=60-day previous samples; report this
as a stale hypothesis, NOT permission to change the runtime's 48h freshness.
T95 persistence requires <=48h. Retain target outcomes, including unusual ones.
Lab predictor sanity ranges: density 700–1000 kg/m3, temperatures 100–450 C;
flag them as research guards, not plant limits. Controls use existing flags.

Fixed candidates: previous, median3; standardized Ridge alpha 1/10/100 on
complete rows (no target-derived imputation). Cetane predictors: previous,
D15,T50,T90. T95: previous, feed T95, product T90,T6,F9,P13.
Additional cetane diagnostics: D976 two-variable calculated index (density
converted kg/m3 to g/ml, log10 T50 in C) and index plus median training offset.
Index uses only past published inputs for prediction. A separate exact-sample
index comparison is retrospective analytical compatibility, not forecasting.
No D4737 because T10 is absent. No claim of standard conformity or additive response.
T95 expert formula: evaluate BOTH explicitly hypothetical mappings of
LIMS Pipeline T95 to latest published ht.1 and ht.2, never silently resolve it.

Fit before 2024, select by MAE on 2024 on a shared complete cohort across all
candidates; freeze choice before 2025/2026 scoring. Fit before each next year
using only labels published before its start. Report all-candidate native
coverage and shared-cohort performance, and stale/fresh previous-target strata.
Minimum 20 common selection observations and 30 common confirmation observations
for promotion consideration. Need >=5% lower MAE than both previous/median3,
RMSE <=1.05 times each baseline, and upper paired 7-day bootstrap MAE difference
95% bound <0 against each (2000 replicates, seed42). Do not tune on 2025/2026.

On 2026 only, calibrate one-sided nominal90% residual bound using common 2025
predictions of the selected policy (ceil((n+1)*.9) order statistic); refuse a
bound if fewer than30 calibration samples. Cetane lower>=51 or T95 upper<=360
is a diagnostic predicted-compliance flag, NOT runtime authorization. Report
actual violations among flags and coverage. No exchangeability guarantee.
Report 2026 inference-input coverage also on frozen sulfur origins; there are
no same-time target labels at most such origins, so this is not accuracy.

All previous years have been examined; this is retrospective audit, not a fresh
blind holdout. No runtime, safety-gate, freshness, model or UI changes in this study.
Record source, cache/script/protocol/module hashes and timestamp evidence.
