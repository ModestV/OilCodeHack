# Protocol frozen before this experiment, 2026-09-21

Source: imported observations.parquet, SHA recorded by the runner. Target is
lims.ht.2.Mg.Sulfur, individual nonnegative laboratory samples. Origins are
sample time minus 0/60/120/180 min; LIMS publication delay 240 min. Last lab
expires 48 h after sampling; calibration pairs use the last 10 published
samples within 45 days. No relabeling, target winsorization or 2026 tuning.

First fix causal plateau detection, per-horizon applicability and pre-2026
control-response fitting. Historical first readings of plateaus stay visible.
No claim that a plateau is a confirmed instrument failure.

Candidates fixed here: log Ridge alpha 10/100/1000/10000; Huber log and raw
regression (epsilon 1.35, alpha 1); nonnegative convex L1 blends in raw and log
units; Q21, PAK, previous lab and five-sample lab median baselines. All use the
same six analyzer/lab features. Missing features are imputed from training
only; baseline missing values fall back to local level then train median.
Convex weights sum to one: no negative sensor contribution or extrapolation
outside the filled sensor/level estimates. This is robust sensor fusion, not
a mechanistic hydrodesulfurization or causal control model.

Selection: fit labels published before 2024, choose minimum accepted MAE on
2024 (gate fitted before 2024). The incumbent is log Ridge chosen by 2024
log-MAE. Refit before 2025; the challenger may replace the incumbent only if
2025 accepted MAE improves by at least 2% and RMSE is at most 105% of incumbent.
Same rows/gates for every candidate; also report all-row and complete-sensor
comparisons. No changes to this rule after seeing results.

Final fit: labels published before 2026. Residual quantiles from accepted 2025
predictions of the chosen frozen family. Alarm threshold fixed at 0.30, not
optimized on calibration residuals. Report 2025 as selection/confirmation,
not an independent final test. 2026 is a retrospective audit already seen in
prior work, never a blind test. Report MAE/RMSE, point threshold recall/FPR,
probability Brier/AUC/recall/FPR, interval coverage and day-block paired MAE CI.
Compare frozen choice, incumbent and corrected Claude separately.

Acceptance also requires all 1012 audit origins to match prefix replay and
real runtime spot checks, plus synthetic future/availability regressions.
Changes to recommendation coverage or cetane freshness are not ML gains.
No production deployment or merge of unrelated process-chain code in this run.
