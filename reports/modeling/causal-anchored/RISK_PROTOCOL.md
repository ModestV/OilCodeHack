# Separate exceedance classifier, 2026-09-21

Motivation: a model minimizing error of typical sulphur concentration can
miss rare exceedances. Test a separate logistic model for laboratory S>10,
using the same six causal log-analyzer/lab features, no class weighting or
resampling. Train-only median imputation/standardization. Fixed C grid:
0.01, 0.1, 1, 10. Select by Brier on accepted 2024 origins after fitting
pre-2024. Confirm the single chosen model on 2025 after fitting pre-2025.

Comparators for 2025: pre-2025 lab event prevalence; Ridge100 residual risk
calibrated exclusively on accepted 2024 OOF residuals. Same 2025 support
gate for all. Promotion needs >=2% lower Brier than both comparators and AUC
at least that of residual risk. Freeze decision before 2026 audit. Final fit
uses pre-2026 labels; residual comparator uses 2025 calibration. Fixed alarm
threshold 0.30. No inference that observational event probability remains
valid after changing controls. This classifier is a diagnostic experiment;
it must not silently replace the scenario's conditional residual model.
