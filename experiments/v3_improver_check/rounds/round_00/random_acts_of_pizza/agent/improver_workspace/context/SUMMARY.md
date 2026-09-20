# Round evidence (overview; full detail in runs.json)

- run 20260920_012456_gpt4o_4h_nohint_j1459606 (harness H0): official score 0.64061, delivery_ok=True, nodes=500
    * validation_mirage: reward -0.108 rate [0.108, 0.174] (54/500 nodes): 54/500 nodes flagged by the largest pattern (10.8%; at most 17.4% if the 6 pattern(s) do not overlap) from M2_fit_before_split, M7_leaky_field, S8_tune_on_inval
    * fallback_to_runnable: reward -0.25: 1 finding(s) from S6_nn_abandoned; worst severity info. 430 个节点尝试过 NN (179 个报错), 最终提交无 NN — fallback-to-runnable 候选 (histo 96px / volcano flatten 模式)

Aggregate reward vector (negative = detected failure mode):
- extraction_distortion: 0.000
- fallback_to_runnable: -0.250
- simplified_error_attribution: 0.000
- submission_sanity: 0.000
- validation_argmax_anchoring: 0.000
- validation_mirage: -0.108

Node-level rates are INTERVALS (never take the midpoint):
- validation_mirage: [0.108, 0.174] over 1 run(s)
