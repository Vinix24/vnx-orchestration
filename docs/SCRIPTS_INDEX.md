# SCRIPTS_INDEX

Inventory of script files under `.claude/vnx-system/scripts` (extensions containing `.sh` or `.py`, excluding `__pycache__` and `.pyc`).
Evidence is based on ripgrep matches in `.claude/vnx-system/scripts`, `.claude/vnx-system/docs`, `.claude/vnx-system/configs`, `.claude/vnx-system/bin`, `.claude/vnx-system/dashboard`, `.claude/vnx-system/tests`, `.claude/terminals`, and `.claude/settings*.json`, plus a repo-wide safety scan and Python import analysis.

## Active
- `build_t0_quality_digest.py`
  Evidence: `.claude/vnx-system/docs/operations/MONITORING_GUIDE.md:370`, `.claude/vnx-system/docs/operations/MONITORING_GUIDE.md:927`, `.claude/vnx-system/docs/operations/MONITORING_GUIDE.md:989`
- `build_t0_tags_digest.py`
  Evidence: `.claude/vnx-system/scripts/repair_index.py:132`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1119`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1150`
- `cached_intelligence.py`
  Evidence: `.claude/vnx-system/scripts/cached_intelligence.py:532`, `.claude/vnx-system/docs/core/technical/INTELLIGENCE_SYSTEM.md:791`, `.claude/vnx-system/docs/core/technical/INTELLIGENCE_SYSTEM.md:804`
- `check_intelligence_health.py`
  Evidence: `.claude/vnx-system/tests/test_cli_json_output.py:47`, `.claude/vnx-system/docs/operations/INTELLIGENCE_DAEMON.md:129`, `.claude/vnx-system/docs/operations/INTELLIGENCE_DAEMON.md:133`
- `claude_auth_check.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:102`, `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:186`, `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:251`
- `claude_auth_check_v2.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:103`, `./VNX_HYBRID_FINAL copy.sh:21`, `./VNX_HYBRID_FINAL copy.sh:22`
- `cli_output.py`
  Evidence: `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PHASE_H_PR_PROMPTS.md:51`, `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PROJECT_PLAN_VNX_HARDENING.md:23`, `./AGENT_TEAMS/VNX_PhaseH_Promps.md:23`
- `code_quality_scanner.py`
  Evidence: `.claude/vnx-system/docs/operations/INTELLIGENCE_DAEMON.md:220`, `.claude/vnx-system/scripts/intelligence_daemon.py:214`, `.claude/vnx-system/scripts/code_snippet_extractor.py:423`
- `code_snippet_extractor.py`
  Evidence: `.claude/vnx-system/docs/operations/INTELLIGENCE_DAEMON.md:221`, `.claude/vnx-system/scripts/intelligence_daemon.py:215`, `.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md:550`
- `doc_section_extractor.py`
  Evidence: `.claude/vnx-system/scripts/intelligence_daemon.py:282`, `.claude/vnx-system/docs/core/technical/INTELLIGENCE_SYSTEM.md`, `.claude/vnx-system/tests/test_doc_section_extractor.py`
  Purpose: Extracts markdown documentation sections into FTS5 code_snippets table with language="markdown". Configured via VNX_DOCS_DIRS env var.
- `cost_tracker.py`
  Evidence: `.claude/vnx-system/bin/vnx:383`, `.claude/vnx-system/docs/operations/COST_TRACKING_GUIDE.md:11`, `.claude/vnx-system/docs/operations/COST_TRACKING_GUIDE.md:85`
- `daily_log_rotation.sh`
  Evidence: `.claude/vnx-system/scripts/setup_daily_cleanup_cron.sh:8`, `.claude/vnx-system/scripts/setup_daily_cleanup_cron.sh:17`, `.claude/vnx-system/scripts/setup_daily_cleanup_cron.sh:30`
- `dispatch_ack_watcher.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:50`, `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:27`, `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:272`
- `dispatch_lifecycle_tracker.py`
  Evidence: `.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md:303`, `.claude/vnx-system/scripts/notify_lifecycle_tracker.py`
- `dispatcher_v8_minimal.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:46`, `.claude/terminals/T-MANAGER/DISPATCHER_V8_FIX_REPORT.md:9`, `.claude/terminals/T-MANAGER/DISPATCHER_V8_FIX_REPORT.md:33`
- `extract_open_items.py`
  Evidence: `.claude/vnx-system/docs/orchestration/OPEN_ITEMS_WORKFLOW.md:116`, `.claude/terminals/T-MANAGER/REPORT_UPDATE_CHANGELOG.md:20`, `.claude/terminals/T-MANAGER/REPORT_UPDATE_CHANGELOG.md:34`
- `gather_intelligence.py`
  Evidence: `.claude/vnx-system/tests/test_cli_json_output.py:33`, `.claude/vnx-system/tests/test_cli_json_output.py:55`, `.claude/terminals/T-MANAGER/20260128-TRACK-2B-CREATION-SUMMARY.md:60`
- `generate_lean_receipt.sh`
  Evidence: `.claude/vnx-system/scripts/generate_lean_receipt.sh:3`, `.claude/vnx-system/docs/implementation/P1_MIGRATION_REPORT.md:39`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1059`
- `generate_t0_brief.sh`
  Evidence: `.claude/vnx-system/scripts/unified_state_manager_v2.py:61`, `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:34`, `.claude/vnx-system/docs/operations/STATE_MANAGEMENT_OVERVIEW.md:91`
- `generate_t0_recommendations.py`
  Evidence: `.claude/terminals/T-MANAGER/PR_QUEUE_READINESS_CHECK.md:19`, `.claude/terminals/T-MANAGER/PR_QUEUE_READINESS_CHECK.md:135`, `.claude/terminals/T-MANAGER/PR_QUEUE_READINESS_CHECK.md:200`
- `generate_valid_dashboard.sh`
  Evidence: `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:19`, `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:43`, `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:55`
- `heartbeat_ack_monitor.py`
  Evidence: `.claude/terminals/T-MANAGER/ARCHITECTURE_UPDATE_TASKS.md:192`, `.claude/terminals/T-MANAGER/ARCHITECTURE_UPDATE_TASKS.md:231`, `.claude/terminals/T-MANAGER/VNX_DOCUMENTATION_CONSOLIDATION_COMPLETE.md:208`, `.claude/vnx-system/scripts/dispatch_ack_watcher.sh:20`
- `heartbeat_ack_monitor_daemon.py`
  Evidence: `.claude/vnx-system/scripts/heartbeat_ack_monitor_daemon.py:8`
- `intelligence_ack.sh`
  Evidence: `.claude/vnx-system/scripts/pretooluse_dispatch_check.sh:32`
- `intelligence_daemon.py`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:51`, `.claude/vnx-system/dashboard/serve_dashboard.py:65`, `.claude/vnx-system/docs/operations/MONITORING_GUIDE.md:932`
- `intelligence_daemon_monitor.py`
  Evidence: `.claude/vnx-system/scripts/generate_valid_dashboard.sh:317`, `.claude/vnx-system/scripts/generate_valid_dashboard.sh:318`
- `intelligence_queries.py`
  Evidence: `.claude/vnx-system/tests/test_cli_json_output.py:40`, `.claude/terminals/T-MANAGER/20260128-PR8-INTELLIGENCE-HOOKS-ADDED.md:45`, `.claude/terminals/T-MANAGER/20260128-PR8-INTELLIGENCE-HOOKS-ADDED.md:104`
- `intelligence_refresh.sh`
  Evidence: `.claude/vnx-system/configs/t0_hooks_enforced.json:35`, `.claude/vnx-system/docs/implementation/P1_MIGRATION_REPORT.md:92`, `.claude/vnx-system/docs/operations/RECEIPT_PROCESSING_FLOW.md:35`
- `learning_loop.py`
  Evidence: `.claude/vnx-system/scripts/learning_loop.py:583`, `.claude/vnx-system/docs/core/technical/INTELLIGENCE_SYSTEM.md:665`, `.claude/vnx-system/docs/core/technical/INTELLIGENCE_SYSTEM.md:741`
- `lib/vnx_paths.py`
  Evidence: `.claude/vnx-system/docs/implementation/P1_MIGRATION_REPORT.md:70`, `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PHASE_P_PR_PROMPTS.md:63`, `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PROJECT_PLAN_VNX_PACKAGING.md:24`
- `lib/vnx_paths.sh`
  Evidence: `.claude/vnx-system/bin/vnx:10`, `.claude/vnx-system/bin/vnx:12`, `.claude/vnx-system/scripts/vnx_doctor.sh:7`
- `list_valid_skills.sh`
  Evidence: `.claude/vnx-system/scripts/list_valid_skills.sh:3`, `.claude/vnx-system/scripts/list_valid_skills.sh:40`
- `log_quality_event.py`
  Evidence: `.claude/vnx-system/docs/testing/QUALITY_ASSURANCE_SYSTEM.md:28`, `.claude/vnx-system/docs/testing/QUALITY_REVIEWER_WORKFLOW.md:137`, `.claude/vnx-system/docs/testing/QUALITY_REVIEWER_WORKFLOW.md:157`
- `notify_dispatch.py`
  Evidence: `.claude/vnx-system/scripts/heartbeat_ack_monitor_daemon.py:59`, `.claude/vnx-system/scripts/dispatcher_v8_minimal.sh:635`, `.claude/vnx-system/scripts/notify_dispatch.py:4`
- `notify_lifecycle_tracker.py`
  Evidence: `.claude/vnx-system/scripts/notify_lifecycle_tracker.py:57`
- `open_items_manager.py`
  Evidence: `.claude/terminals/T0/CLAUDE.md:29`, `.claude/terminals/T0/CLAUDE.md:58`, `.claude/terminals/T0/CLAUDE.md:175`
- `pane_config.sh`
  Evidence: `.claude/vnx-system/scripts/pane_manager_v2.sh:3`, `.claude/vnx-system/scripts/receipt_notifier.sh:55`, `.claude/vnx-system/scripts/dispatcher_v8_minimal.sh:280`
- `pane_manager_v2.sh`
  Evidence: `.claude/vnx-system/scripts/pane_manager_v2.sh:2`, `.claude/vnx-system/scripts/receipt_processor_v4.sh:19`, `.claude/vnx-system/docs/operations/RECEIPT_PIPELINE.md:160`
- `popup_editor.sh`
  Evidence: `.claude/vnx-system/scripts/queue_ui_enhanced.sh:121`, `.claude/vnx-system/scripts/queue_ui_enhanced.sh:122`, `./github_merge/04_VNX_SYSTEM_DEPENDENCIES.md:21`
- `pr_queue_manager.py`
  Evidence: `.claude/terminals/T0/CLAUDE.md:11`, `.claude/terminals/T0/CLAUDE.md:12`, `.claude/terminals/T0/CLAUDE.md:29`
- `pretooluse_dispatch_check.sh`
  Evidence: `.claude/vnx-system/configs/t0_hooks_enforced.json:25`, `.claude/vnx-system/docs/TEMPLATE_VALIDATION.md:6`
- `quality_dashboard_integration.py`
  Evidence: `.claude/vnx-system/scripts/quality_metrics_updater.sh:22`, `.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md:568`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1334`
- `quality_db_init.py`
  Evidence: `.claude/vnx-system/scripts/code_quality_scanner.py:607`, `.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md:565`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1331`
- `quality_metrics_updater.sh`
  Evidence: `.claude/vnx-system/docs/roadmap/implementation/01_IMPLEMENTATION_ROADMAP.md:569`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1335`
- `query_quality_intelligence.py`
  Evidence: `.claude/vnx-system/docs/testing/QUALITY_ASSURANCE_SYSTEM.md:29`, `.claude/vnx-system/scripts/query_quality_intelligence.py:9`, `.claude/vnx-system/scripts/query_quality_intelligence.py:12`
- `query_t0_brief.sh`
  Evidence: `.claude/vnx-system/docs/orchestration/T0_QUERY_BEST_PRACTICES.md:34`, `.claude/vnx-system/docs/orchestration/T0_QUERY_BEST_PRACTICES.md:40`, `.claude/vnx-system/docs/orchestration/T0_QUERY_BEST_PRACTICES.md:41`
- `queue_popup_watcher.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:47`, `.claude/terminals/T-MANAGER/PR_QUEUE_READINESS_CHECK.md:226`, `.claude/vnx-system/docs/operations/STATE_MANAGEMENT_OVERVIEW.md:87`
- `queue_ui_enhanced.sh`
  Evidence: `.claude/vnx-system/docs/orchestration/CONFLICT_GATE_SPEC.md:48`, `.claude/vnx-system/docs/orchestration/CONFLICT_GATE_SPEC.md:427`, `.claude/vnx-system/scripts/queue_popup_watcher.sh:22`
- `receipt_notifier.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:53`, `.claude/terminals/T-MANAGER/ARCHITECTURE_UPDATE_TASKS.md:20`, `.claude/terminals/T-MANAGER/ARCHITECTURE_UPDATE_TASKS.md:48`
- `receipt_processor_lean_update.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:60`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1058`
- `receipt_processor_v4.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:48`, `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_SUMMARY.md:41`, `.claude/terminals/T-MANAGER/ARCHITECTURE_UPDATE_TASKS.md:17`
- `recommendations_engine_daemon.sh`
  Evidence: `.claude/vnx-system/scripts/vnx_supervisor_simple.sh:232`, `.claude/vnx-system/scripts/vnx_supervisor_simple.sh:255`, `.claude/vnx-system/scripts/vnx_supervisor_simple.sh:356`
- `report_miner.py`
  Evidence: `.claude/vnx-system/docs/core/technical/REPORT_LIFECYCLE.md:43`, `.claude/vnx-system/docs/core/technical/REPORT_LIFECYCLE.md:124`, `.claude/vnx-system/docs/core/technical/REPORT_LIFECYCLE.md:127`
- `report_parser.py`
  Evidence: `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:33`, `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:66`, `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:98`
- `report_watcher.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:52`, `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:32`, `.claude/vnx-system/docs/architecture/MASTER_STRATEGY_VNX_HYBRID.md:65`
- `report_watcher_shadow.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:132`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:1673`, `.claude/vnx-system/docs/core/00_VNX_ARCHITECTURE.md:586`
- `session_gc.py`
  Evidence: `.claude/vnx-system/docs/RETENTION_POLICY.md:7`, `.claude/vnx-system/docs/RETENTION_POLICY.md:35`, `.claude/vnx-system/docs/RETENTION_POLICY.md:40`
- `sessionstart_t0_intelligence.sh`
  Evidence: `.claude/vnx-system/configs/t0_hooks_enforced.json:13`
- `singleton_enforcer.sh`
  Evidence: `.claude/vnx-system/scripts/receipt_processor_v4.sh:15`, `.claude/vnx-system/docs/operations/MONITORING_GUIDE.md:693`, `.claude/vnx-system/scripts/receipt_notifier.sh:27`
- `smart_tap_v7_json_translator.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:45`, `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:44`, `.claude/vnx-system/docs/operations/STATE_MANAGEMENT_OVERVIEW.md:86`
- `state_integrity.py`
  Evidence: `.claude/vnx-system/scripts/verify_state_integrity.sh:10`, `.claude/vnx-system/scripts/verify_state_integrity.sh:13`, `.claude/vnx-system/docs/orchestration/PROGRESS_STATE_SPEC.md:162`
- `sync_progress_state_from_receipts.py`
  Evidence: `.claude/vnx-system/docs/orchestration/PROGRESS_TRACKING_USER_GUIDE.md:91`, `.claude/vnx-system/docs/orchestration/PROGRESS_STATE_SPEC.md:124`, `.claude/vnx-system/docs/orchestration/PROGRESS_STATE_SPEC.md:171`
- `t0_intelligence_aggregator.py`
  Evidence: `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_REPORT.md:68`, `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_REPORT.md:133`, `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_REPORT.md:224`
- `t0_query.py`
  Evidence: `.claude/vnx-system/docs/core/00_VNX_ARCHITECTURE.md:589`, `./claudedocs/00_VNX_ARCHITECTURE.txt:537`
- `tag_intelligence.py`
  Evidence: `.claude/terminals/T-MANAGER/20260127-SMART-CONTEXT-INTELLIGENCE-INVESTIGATION.md:1070`, `.claude/vnx-system/scripts/tag_intelligence.py:497`, `.claude/vnx-system/docs/roadmap/implementation/PROJECT_STATUS.md:679`
- `test_complete_v2_flow.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:119`
- `test_pr_dispatch_integration.py`
  Evidence: `.claude/terminals/T-MANAGER/PR_2_5_CHANGELOG.md:27`, `.claude/terminals/T-MANAGER/PR_2_5_CHANGELOG.md:91`, `.claude/terminals/T-MANAGER/PR_2_5_DEMO.md:146`
- `test_recommendation_flow.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:122`
- `test_v2_gate_progression.sh`
  Evidence: `.claude/vnx-system/scripts/test_v2_gate_progression.sh:2`, `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:120`, `.claude/vnx-system/docs/roadmap/implementation/MANAGER_BLOCK_V2_IMPLEMENTATION_STATUS.md:95`
- `test_v2_metadata_flow.sh`
  Evidence: `.claude/vnx-system/scripts/SCRIPT_CLEANUP_REPORT.md:121`
- `test_week_1_skills.py`
  Evidence: `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PHASE_1_PR_PROMPTS.md:228`, `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PHASE_1_PR_PROMPTS.md:369`, `.claude/vnx-system/docs/roadmap/unified_roadmap/ultimate_vnxsystem/PHASE_1_PR_PROMPTS.md:388`
- `unified_state_manager_v2.py`
  Evidence: `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:46`, `.claude/terminals/T-MANAGER/VNX_DOCUMENTATION_CONSOLIDATION_COMPLETE.md:207`, `.claude/terminals/T-MANAGER/PHASE_2-3_COMPLETION_SUMMARY.md:141`
- `update_progress_state.py`
  Evidence: `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_SUMMARY.md:54`, `.claude/terminals/T-MANAGER/ARCHITECTURE_VERIFICATION_SUMMARY.md:76`, `.claude/vnx-system/scripts/test_v2_gate_progression.sh:33`
- `userpromptsubmit_intelligence_inject.sh`
  Evidence: `.claude/vnx-system/docs/implementation/P1_MIGRATION_REPORT.md:79`, `.claude/vnx-system/docs/implementation/P1_MIGRATION_REPORT.md:89`, `.claude/vnx-system/scripts/test_complete_v2_flow.sh:147`
- `userpromptsubmit_intelligence_inject_v5.sh`
  Evidence: `.claude/terminals/T0/settings.json:91`, `./.claude/terminals/T0/settings.json:91`
- `validate_feature_plan.py`
  Evidence: `.claude/vnx-system/scripts/validate_feature_plan.py:247`, `./.claude/skills/vnx-manager/@vnx-manager.md:46`
- `validate_report.py`
  Evidence: `.claude/vnx-system/docs/TEMPLATE_VALIDATION.md:5`, `.claude/vnx-system/scripts/validate_report.py:7`, `.claude/vnx-system/scripts/validate_report.py:254`
- `validate_skill.py`
  Evidence: `.claude/terminals/T0/CLAUDE.md:215`, `.claude/terminals/T0/CLAUDE.md:218`, `.claude/terminals/T0/CLAUDE.md:221`
- `validate_template_tokens.py`
  Evidence: `.claude/vnx-system/docs/TEMPLATE_VALIDATION.md:3`, `.claude/vnx-system/docs/TEMPLATE_VALIDATION.md:11`, `.claude/vnx-system/docs/TEMPLATE_VALIDATION.md:12`
- `verify_completion.py`
  Evidence: `.claude/terminals/T3/CLAUDE.md:73`, `.claude/terminals/T3/CLAUDE.md:78`, `.claude/vnx-system/docs/testing/QUALITY_ASSURANCE_SYSTEM.md:16`
- `verify_state_integrity.sh`
  Evidence: `.claude/vnx-system/docs/orchestration/PROGRESS_STATE_SPEC.md:157`, `.claude/vnx-system/docs/orchestration/PR_QUEUE_WORKFLOW.md:508`, `.claude/vnx-system/docs/orchestration/PR_QUEUE_WORKFLOW.md:515`
- `vnx_doctor.sh`
  Evidence: `.claude/vnx-system/bin/vnx:149`, `.claude/vnx-system/scripts/vnx_doctor.sh:22`, `.claude/vnx-system/scripts/vnx_doctor.sh:34`
- `vnx_package_check.sh`
  Evidence: `.claude/vnx-system/bin/vnx:162`
- `vnx_worktree_setup.sh`
  Evidence: `README.md:165-167`, `plans/AUTONOMOUS_EXECUTION_PLAN.md`
  Purpose: Git worktree lifecycle for feature plans — create, remove, list. One worktree per feature plan, shared by all agents.
- `vnx_supervisor_simple.sh`
  Evidence: `.claude/vnx-system/dashboard/serve_dashboard.py:49`, `.claude/terminals/T-MANAGER/VNX_HYBRID_FINAL_UPDATE_REPORT.md:45`, `.claude/vnx-system/docs/operations/34_RECEIPT_TROUBLESHOOTING.md:60`

- `conversation_analyzer.py`
  Evidence: `.claude/vnx-system/scripts/conversation_analyzer.py`
  Purpose: Nightly session mining pipeline — parse JSONL logs, detect patterns, deep analysis, store to session_analytics. Extracts session_model from JSONL for model-based analytics.
- `conversation_analyzer_nightly.sh`
  Evidence: `.claude/vnx-system/scripts/conversation_analyzer_nightly.sh`
  Purpose: Nightly 3-phase pipeline runner: (1) conversation_analyzer → (2) generate_t0_session_brief → (3) generate_suggested_edits.
- `generate_t0_session_brief.py`
  Evidence: `.claude/vnx-system/scripts/generate_t0_session_brief.py`
  Purpose: Generate model-based session performance brief (`t0_session_brief.json`) for T0 dispatch intelligence. Auto, read-only.
- `generate_suggested_edits.py`
  Evidence: `.claude/vnx-system/scripts/generate_suggested_edits.py`
  Purpose: Generate human-in-the-loop suggested edits (`pending_edits.json`) for MEMORY, rules, CLAUDE.md, and skill templates.
- `apply_suggested_edits.py`
  Evidence: `.claude/vnx-system/scripts/apply_suggested_edits.py`, `.claude/vnx-system/bin/vnx` (suggest subcommand)
  Purpose: CLI for reviewing, accepting, rejecting, and applying suggested system tuning edits. Invoked via `vnx suggest`.
- `send_digest_email.py`
  Evidence: `.claude/vnx-system/scripts/send_digest_email.py`, `.claude/vnx-system/scripts/conversation_analyzer_nightly.sh` (Phase 4)
  Purpose: Opt-in email digest sender. Reads t0_session_brief.json and pending_edits.json, formats digest, sends via SMTP. Requires VNX_DIGEST_EMAIL + VNX_SMTP_PASS env vars.

## Legacy
- `ack_register.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:117`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:73`, `.claude/vnx-system/docs/archive/04_CHANGELOG.md:31`
- `archive/deprecated-versions/fix_browser_pool_auth.py`
  Evidence: `.claude/vnx-system/scripts/archive/deprecated-versions/README.md:21`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:38`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:57`
- `archive/deprecated-versions/fix_duplicate_processes.sh`
  Evidence: `.claude/vnx-system/scripts/archive/deprecated-versions/README.md:22`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:39`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:58`
- `archive/deprecated-versions/heartbeat_ack_monitor.py.bak`
  Evidence: `.claude/vnx-system/scripts/archive/deprecated-versions/README.md:32`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:40`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:63`
- `archive/deprecated-versions/unified_state_manager_v2.py.ORIGINAL`
  Evidence: `.claude/vnx-system/scripts/archive/deprecated-versions/README.md:13`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:41`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:52`
- `archive/deprecated-versions/unified_state_manager_v2_CURSOR_FIX.py`
  Evidence: `.claude/vnx-system/scripts/archive/deprecated-versions/README.md:12`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:42`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PHASE_8_SCRIPT_CLEANUP_COMPLETE.md:51`
- `archived/dashboard_data_generator.sh`
  Evidence: `.claude/vnx-system/scripts/archived/dashboard_server.py:106`, `.claude/vnx-system/scripts/archived/start_dashboard.sh:19`, `.claude/vnx-system/scripts/archived/start_dashboard.sh:21`
- `archived/dashboard_server.py`
  Evidence: `.claude/vnx-system/scripts/archived/start_dashboard.sh:30`, `.claude/vnx-system/docs/archive/04_CHANGELOG.md:344`
- `archived/gates_controller.sh`
  Evidence: `.claude/vnx-system/docs/archive/04_CHANGELOG.md:460`
- `archived/heartbeat_monitor.sh`
  Evidence: `.claude/vnx-system/scripts/archived/process_dashboard.sh:42`, `.claude/vnx-system/docs/archive/07_SYSTEM_UPGRADES.md:64`, `.claude/vnx-system/docs/archive/07_SYSTEM_UPGRADES.md:146`
- `archived/heartbeat_monitor_simple.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/dashboard_server_enhanced.py:29`, `.claude/vnx-system/docs/archive/ORCHESTRATION_FILE_INDEX.yaml:80`, `.claude/vnx-system/docs/archive/PERMISSION_CONFIGURATION_ANALYSIS.md:146`
- `archived/orchestration_manager.sh`
  Evidence: `.claude/vnx-system/scripts/archived/process_dashboard.sh:43`, `.claude/vnx-system/docs/archive/ORCHESTRATION_FILE_INDEX.yaml:90`, `.claude/vnx-system/docs/archive/ORCHESTRATION_FILE_INDEX.yaml:235`
- `archived/process_dashboard.sh`
  Evidence: `.claude/vnx-system/docs/archive/README_legacy.md:54`, `.claude/vnx-system/docs/archive/README_legacy.md:62`, `.claude/vnx-system/docs/archive/README_legacy.md:141`
- `archived/restart_with_singleton.sh`
  Evidence: `None found`
- `archived/singleton_manager.sh`
  Evidence: `.claude/vnx-system/scripts/archived/restart_with_singleton.sh:8`, `.claude/vnx-system/docs/archive/04_CHANGELOG.md:162`
- `archived/smart_tap_enhanced.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-12-operations/02_OPERATIONS_GUIDE.md:518`, `.claude/vnx-system/docs/archive/04_CHANGELOG.md:234`, `.claude/vnx-system/docs/archive/2026-01-26-consolidation/51_TROUBLESHOOTING.md:163`
- `archived/smart_tap_multi_block.sh`
  Evidence: `None found`
- `archived/smart_tap_multi_with_editor.sh`
  Evidence: `None found`
- `archived/smart_tap_with_editor.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/dashboard_server_enhanced.py:25`, `.claude/vnx-system/scripts/archived/orchestration_manager.sh:185`, `.claude/vnx-system/scripts/archived/smart_tap_multi_with_editor.sh:4`
- `archived/start_dashboard.sh`
  Evidence: `None found`
- `archived/vnx_launch.sh`
  Evidence: `None found`
- `archived/vnx_supervisor.sh`
  Evidence: `.claude/vnx-system/scripts/archived/vnx_launch.sh:99`, `.claude/vnx-system/scripts/archived/vnx_launch.sh:102`, `.claude/vnx-system/scripts/archived/vnx_launch.sh:124`
- `archived_20250929/dashboard_server_enhanced.py`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:40`, `.claude/vnx-system/docs/archive/2026-01-12-operations/03_TROUBLESHOOTING_GUIDE.md:296`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:85`
- `archived_20250929/dispatch_ack_watcher_deprecated.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:33`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:82`
- `archived_20250929/dispatcher_v5_cognition_aware.sh.DEPRECATED`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:13`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:7`
- `archived_20250929/dispatcher_v6_ack_timeout.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:17`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:78`
- `archived_20250929/log_based_ack_monitor.py`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:44`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:86`
- `archived_20250929/pane_config.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/smart_tap_with_editor_multi.sh.deprecated:42`, `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:67`, `.claude/vnx-system/scripts/archived_20250929/dispatcher_v5_cognition_aware.sh.DEPRECATED:59`
- `archived_20250929/receipt_based_status.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:52`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:88`
- `archived_20250929/report_parser_json.py`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:48`, `.claude/vnx-system/scripts/archived_20250929/report_parser_json.py:17`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:87`
- `archived_20250929/setup_tmux_keybindings.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:71`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:94`
- `archived_20250929/singleton_enforcer.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/smart_tap_with_editor_multi.sh.deprecated:13`, `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:75`, `.claude/vnx-system/scripts/archived_20250929/dispatcher_v5_cognition_aware.sh.DEPRECATED:13`
- `archived_20250929/smart_tap_with_editor_multi.sh.deprecated`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:21`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:79`
- `archived_20250929/state_manager.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:29`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:81`, `.claude/vnx-system/docs/archive/2026-01-07-phase1b/35_STATE_MONITORING_SYSTEM.md:42`
- `archived_20250929/state_monitor_integration.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:56`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:89`, `.claude/vnx-system/docs/archive/2026-02/development/20_DEVELOPMENT_LEARNINGS.md:181`
- `archived_20250929/tag_search.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:82`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:98`
- `archived_20250929/terminal_state_monitor.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:60`, `.claude/vnx-system/scripts/archived_20250929/state_monitor_integration.sh:13`, `.claude/vnx-system/scripts/archived_20250929/state_monitor_integration.sh:177`
- `archived_20250929/unified_state_manager.py`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:25`, `.claude/vnx-system/docs/archive/VNX_ORCHESTRATION_CLEANUP_REPORT.md:80`
- `archived_migrations/backfill_receipts.py`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-08-cleanup/CODEX_CLEANUP_VERIFICATION_REPORT_20260106.md:55`, `.claude/vnx-system/docs/archive/2026-01-08-cleanup/CLEANUP_COMPARISON_CODEX_VS_TMANAGER_20260106.md:55`, `.claude/vnx-system/docs/archive/2026-01-08-cleanup/VNX_CLEANUP_PLAN_20260106.md:69`
- `archived_migrations/migrate_receipts.py`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-19-consolidation/PROJECT_STATUS.md.v8.3.0-outdated:393`, `.claude/vnx-system/docs/archive/2026-01-19-consolidation/60_PROJECT_STATUS.md.promoted:655`, `.claude/vnx-system/docs/archive/RECEIPT_MIGRATION_COMPLETE.md:114`
- `archived_migrations/migrate_shadow_receipts.py`
  Evidence: `.claude/vnx-system/docs/archive/SHADOW_RECEIPT_MIGRATION_COMPLETE.md:166`, `.claude/vnx-system/docs/archive/SHADOW_RECEIPT_MIGRATION_COMPLETE.md:167`, `.claude/vnx-system/docs/archive/2026-01-08-cleanup/CODEX_CLEANUP_VERIFICATION_REPORT_20260106.md:55`
- `archived_phase1b/dispatcher_v7_compilation.sh`
  Evidence: `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:14`, `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:18`, `.claude/vnx-system/scripts/archived_20250929/ARCHIVE_MANIFEST.md:96`
- `archived_setup/machine_setup.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-06-cleanup/REMOTE_ACCESS_QUICK_REFERENCE.md:10`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/REMOTE_ACCESS_QUICK_REFERENCE.md:111`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/REMOTE_ACCESS_SETUP.md:382`
- `archived_setup/machine_setup_fixed.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-08-cleanup/CLEANUP_COMPARISON_CODEX_VS_TMANAGER_20260106.md:58`, `.claude/vnx-system/docs/archive/2026-01-08-cleanup/VNX_CLEANUP_PLAN_20260106.md:73`
- `archived_setup/machine_setup.sh`
  Evidence: `.claude/vnx-system/scripts/archived_setup/machine_setup_fixed.sh:285`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/REMOTE_ACCESS_QUICK_REFERENCE.md:16`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/REMOTE_ACCESS_QUICK_REFERENCE.md:117`
- `cleanup_auto.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:24`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:57`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:145`
- `cleanup_massive_logs.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-08-cleanup/VNX_CLEANUP_PLAN_20260106.md:51`
- `intelligence_summary.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-11-reorganization/T0_INTELLIGENCE_ENFORCEMENT_LIGHTWEIGHT.md:134`, `.claude/vnx-system/docs/archive/2026-01-11-reorganization/T0_INTELLIGENCE_ENFORCEMENT_LIGHTWEIGHT.md:144`, `.claude/vnx-system/docs/archive/2026-01-11-reorganization/T0_INTELLIGENCE_ENFORCEMENT_LIGHTWEIGHT.md:236`
- `repair_index.py`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-12-roadmap/INTELLIGENCE_DIGEST_DELIVERY.md:22`, `.claude/vnx-system/docs/archive/2026-01-12-roadmap/INTELLIGENCE_DIGEST_DELIVERY.md:86`, `.claude/vnx-system/docs/archive/2026-01-12-roadmap/INTELLIGENCE_DIGEST_DELIVERY.md:98`
- `send_single_receipt_to_t0.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-08-cleanup/VNX_STATUS_REPORT_20260106.md:14`
- `setup_daily_cleanup_cron.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:50`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:62`, `.claude/vnx-system/docs/archive/2026-01-06-cleanup/LOG_CLEANUP_SUMMARY.md:148`
- `test_pr_recommendation_integration.py`
  Evidence: `.claude/vnx-system/docs/archive/2026-02/roadmap/PR_2_3_CHANGELOG.md:38`, `.claude/vnx-system/docs/archive/2026-02/roadmap/PR_2_3_CHANGELOG.md:59`
- `update_pane_mapping.sh`
  Evidence: `.claude/vnx-system/docs/archive/2026-01-08-cleanup/VNX_CLEANUP_PLAN_20260106.md:78`

## Unused
None.
