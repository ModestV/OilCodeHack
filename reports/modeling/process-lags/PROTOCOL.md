# Additional pre-2026 diagnostic, fixed before execution

Question: were independent forecast horizons, process lags and published feed
LIMS omitted from the earlier H3 challenger study? This is a retrospective
validation diagnostic, not a new blind test or a production selection run.
Previously inspected 2025 validation cannot establish independent generalization.
No 2026 measurements or labels enter fitting, comparison or selection.

Use imported observations with timestamp < 2026-01-01, finite nonnegative sulfur,
and exclude invalid/conflict/suspect/flatline input telemetry. Targets retain
finite nonnegative observations except invalid/conflict. Label publication is
sample+4h. Train labels published before 2025; validation origins in 2025 and
labels published before 2026. Horizons fixed to 0,60,120,180 minutes; H0 is a
nowcast, not a forecast. Current telemetry tolerance 30min; rolling means 1/6h.

Families, fixed Ridge alpha=3000 on log1p(target), train-only imputation/scaling:
1. HT tags T6,T11,P13,P8,F9,F14,F17,F25,Q20,Q21 + previous available output LIMS.
2. Same + separate PAK series, whose provenance is distinct from Q21.
3. Same + values at origin-60/-120/-180min (each own 30min tolerance).
4. Same + published feed LIMS, age, reciprocal absolute temperature and pressure
   drop/pressure ratio. Feed Mass.Sulfur wt% converts to mg/kg by 10000; exclude
   feed/output laboratory baselines older than 48h by sample age. The shape
   features are process-informed regressors, not a fitted kinetic mechanism.
   Unknown F15/F26 unit scales are not repaired or used for LHSV/density.

Compare each family and output-LIMS persistence on exactly the same validation
origins with at least 80% current HT tags available and fresh published output
LIMS. Report raw validation metrics too, coverage, exceedance recall and FPR.
This subset is a diagnostic availability mask, not the production OOD gate.
Do not optimize masks, thresholds, alphas or families after observing results.
Save predictions and source/code/protocol hashes. No runtime artifact replacement.
