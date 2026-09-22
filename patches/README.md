# AIDE compatibility

These fixes target the external AIDE execution environment. They are not harness
edits and are not applied automatically by the RSI loop.

- `metric_guard_skip_fold_counts.patch`: fixes metric parsing so the fold count in
  `Mean AUC score (5-fold CV): 0.62` is not mistaken for the score; includes a regression test.
- `metric_guard_scripts_only.patch`: the runtime-script portion of the same fix.
- `apply_agent_decision_fix.py`: fixes prompt construction that otherwise causes
  agent decisions to fail and fall back to rule selection.

Apply compatible fixes to a dedicated execution overlay and set `RSI_OVERLAY_PATH`
to that overlay. Changing only a host-side script does not update the installed AIDE
package inside an overlay. Check patch applicability against your execution checkout.

`tools/build_agent_fix_overlay.sh` is a CRC helper: it requires an existing
`overlays/v2/agent_fixes_v2.overlay` with the metric fix and copies it before applying
the decision fix. Neither overlay is distributed here. It is not an environment installer.
Use `tools/smoke_overlay.py --help` for the associated offline/import smoke-check interface.
