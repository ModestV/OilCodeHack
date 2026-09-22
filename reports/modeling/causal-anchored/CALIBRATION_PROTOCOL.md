# Second experiment: calibration memory, 2026-09-21

After the frozen family comparison, no challenger passed the 2025 promotion
rule. Next hypothesis: analyzer calibration memory is more consequential than
the regressor. Freeze Ridge alpha=100 for all four horizons (the phase-one
incumbent). Compare anchor sample counts 3/5/10/20 and lab-level counts 3/5/10.
All candidates keep the 45-day maximum age, additive median lab-minus-analyzer
correction and same cleaning/publication contract. No changes to lab labels.

Choose ONE global (anchor, level) pair by mean MAE across four horizons in
2024, with training before 2024. Confirmation in 2025: require at least 2%
mean MAE improvement and no more than 5% mean RMSE degradation relative to
(10,5). Each comparison uses the intersection of candidate/incumbent support
gates fitted on earlier training data, and reports coverage separately. Refit
before 2026. This is another pre-2026 selection experiment, not a fresh blind
test. Inspect 2026 only after writing selection.json. Do not reselect after
that audit. Calibrate residuals from accepted 2025 predictions, fixed alarm .30.
