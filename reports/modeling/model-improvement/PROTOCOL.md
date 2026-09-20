# Protocol fixed before fitting challengers

Base code: d8afb0b. Existing 2026 test has already been inspected; results on it
are a retrospective audit, not a newly untouched blind test. No candidate or
hyperparameter will be chosen using 2026 labels. Do not replace the production
artifact simply because one challenger scores better on that year.

Training/selection: training labels published before 2025-01-01; validation
origins in 2025 with labels published before 2026-01-01. Fit imputation/scaling
only on training. Features end at prediction origin; target origin+3h; LIMS
sample+4h<=origin. Add expanding-window checks within pre-2026 history to assess
whether a selected improvement is stable. Report all rows, common eligible rows,
coverage, MAE/RMSE, recall/precision/FPR and average precision for alarms.

Families: frozen incumbent; simple past-LIMS median/persistence; robust linear
and log-residual regressions; histogram boosting with small trees (7/15 leaves,
30/60 minimum leaf, 100/200 iterations, no random early-stopping split);
ExtraTrees with regularized leaves; constrained pseudo-first-order kinetic
surrogate and kinetic+statistical residual hybrids. Fixed seeds.

Additional information: published LIMS history/age, current and rolling PAK
(excluding invalid/conflicting/flatline/suspect readings), HT-only features,
recent changes and physically meaningful ratios. No future PAK or target
interpolation. Compare changes on matched rows, not by silently dropping hard
examples. Preserve sulfur extremes in evaluation.

Kinetic surrogate: S_out = S_in * exp(-k_eff * exposure), with Arrhenius
temperature factor, pressure/gas ratio factors and inverse volumetric flow.
Unknown catalyst volume is absorbed into fitted k_eff. P13 is total pressure,
not known H2 partial pressure; F25/F15 is only fresh-gas/oil, not total H2/oil.
Inlet T6 is not mean bed temperature. Therefore this is a physically inspired,
constrained surrogate, not a validated reactor model. All constants are fitted
only on train; no borrowed plant-specific coefficients.

Regression selection: lowest validation MAE on the common production-supported
rows; report full-coverage behavior too. Alarm selection independent: threshold
maximizes validation recall at FPR<=10%; break ties by precision then higher
threshold. Also report higher-sensitivity FPR<=20% as an explicit tradeoff,
never change the sulfur specification of 10 mg/kg. Test thresholds remain fixed.

Sources for the structure, not plant constants:
- Petras et al., 2025, https://pubmed.ncbi.nlm.nih.gov/40508478/ (Arrhenius,
  temperature, LHSV, gas/oil and H2 pressure; DOI10.3390/ma18112481).
- https://doi.org/10.1016/j.fuel.2017.07.092 (diesel hydrotreating kinetic model).
- https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.HistGradientBoostingRegressor.html

Acceptance: compare before/after without weakening gates or using test data for
selection. If evidence does not support deployment, save reproducible results
and say so. Model family choice alone is not an improvement.

## Follow-up protocol, after the first audit

The first frozen winner did not improve accepted-row 2026 MAE. This follow-up
was designed after seeing that outcome, so it is exploratory and cannot restore
a blind test. Freeze these additions before running them: (1) delayed online
median forecast-error correction with windows 7/30 days, strengths 0.5/1;
(2) monthly HGB refits using all published history or the last 365 days,
plus matching training-median baselines; (3) locally anchored kinetics.
For (3), infer conversion from the last published lab and feed sulfur at that
sample, then propagate relative current severity with Ea=0/30/60/90 kJ/mol,
pressure exponent=0/0.5/1 and inverse flow. This does not establish transport
lag, hydrogen purity or causality; current severity is assumed to persist.
All hyperparameters and the winner still use only 2025 MAE on the frozen mask.
Monthly refits may use earlier 2026 labels only after publication; report them
as a prequential adaptive policy, not a frozen-model test. Do not tune after
the second audit. Record both successful and unsuccessful results.

Expanding-fold reporting clarification: refit the unchanged applicability
policy on each fold's training set and compare the same accepted rows. Retain
the all-row denominator too. Nonfinite predictions make full-denominator MAE
unavailable and are counted explicitly; do not delete those rows or cap errors.
