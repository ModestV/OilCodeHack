# Cost-sensitive exceedance policy — frozen before this run, 2026-09-21

Scope: probability of LIMS ht.2 sulphur >10 mg/kg at H0/60/120/180.
Not a batch/blend probability, causal control effect, or plant safety guarantee.
2024/2025/2026 have all been examined before: next-year evaluation is temporal
out-of-fit evidence, not a new blind holdout.

Input: causal raw feature columns and distinct lab targets from the corrected
anchored predictions cache, whose offline/runtime prefix parity was verified
in f8794e8. Do not train on its prediction/probability/OOF columns. Record cache,
source dataset (from parity provenance), script and protocol hashes.

Cost scenarios (assumptions, NOT plant estimates): C_FP=1, C_FN in
{1,2,5,10,20,50}; true alarms and correct non-alarms cost zero. Review/abstention,
batch size, money, alarm duration and operator capacity are not priced.
For calibrated probabilities the theoretical threshold is 1/(1+C_FN).

Logistic regression uses the existing six features, no class weights; C grid
{.01,.1,1,10}, chosen by Brier on accepted 2024 origins after pre-2024 fitting.
For each cost scenario choose the empirical alarm threshold on those same 2024
out-of-time probabilities, including always/never alarm; tie break toward lower
FN, then lower FP, then higher threshold. Selection cost is optimistic.

Freeze C and empirical threshold BEFORE any 2025/2026 scores. Fit the model on
labels published before each subsequent year. Evaluate frozen empirical and
theoretical thresholds, fixed .30 logistic, always alarm and never alarm.
Compare also Ridge100 residual probability using ONLY accepted OOF residuals
from the previous year (2024 for 2025; 2025 for 2026) with theoretical threshold.
This is the earlier annual residual comparator, not identical to active runtime.
For 2026 only, additionally evaluate the actual active corrected runtime alarm
and theoretical cost thresholds from its saved probabilities.

All policies use the SAME fold-local support gate; unsupported rows are manual
review, not negative predictions. Report excluded events and sample coverage.
Do not aggregate H0/H1/H2/H3 as independent samples of different events.

Report TP/FP/FN/TN, recall, precision, false-alarm rate, alarm share, cost per
100 accepted samples and monthly stability. Pairwise cost differences versus
residual/always/never: 2000 bootstraps of calendar 7-day blocks, seed 42;
also 28-day blocks as dependence sensitivity. Intervals are exploratory,
conditional on the frozen policy, and not adjusted for multiple comparisons.

Conservative eligibility for further validation: empirical logistic policy
must reduce 2025 cost by >=5% against EACH of residual/theoretical, always,
never, with upper 95% paired 7-day bootstrap bound <0 against each. Determine
this before computing 2026 audit. Never deploy automatically: actual costs,
review capacity, simultaneous uncertainty and independent data are unresolved.
No changes to runtime safety gates, freshness or active model in this study.
