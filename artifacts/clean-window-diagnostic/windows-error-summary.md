# Windows clean-shadow test errors

Command:

```text
python -m unittest tests.test_clean_window_shadow tests.test_day_handoff tests.test_generate_day_handoff -v
```

Result: `53 passed, 0 failed, 13 errors, 0 skipped` (exit 1).

| # | Test | Error | Phase | Test body |
|---:|---|---|---|---|
| 1 | `setUpClass (tests.test_clean_window_shadow.CleanWindowShadowGatewayTests)` | `PermissionError [WinError 5]` creating `/opt/workspace/tools` | class setup | Not executed for 6 class tests |
| 2 | `CleanWindowShadowSessionTests.test_ttl_purge_closes_session_resources` | `PermissionError [WinError 5]` creating `/tmp` | test body | Partially executed |
| 3 | `DailyCandidateShadowTests.test_clean_profile_unchanged` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 4 | `DailyCandidateShadowTests.test_daily_candidate_reinjects_on_cold_respawn` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 5 | `DailyCandidateShadowTests.test_daily_candidate_requires_valid_file_path` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 6 | `DailyCandidateShadowTests.test_raw_day_handoff_text_not_accepted` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 7 | `DailyCandidateShadowTests.test_send_turn_failure_does_not_commit_state_snapshot` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 8 | `DayHandoffSecurePathTests.test_read_does_not_follow_symlink_replacement` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 9 | `DayHandoffSecurePathTests.test_refuse_overwrite_existing_file` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 10 | `DayHandoffSecurePathTests.test_reject_parent_directory_symlink` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 11 | `DayHandoffSecurePathTests.test_reject_path_outside_shadow_dir` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 12 | `DayHandoffSecurePathTests.test_reject_symlink` | `AttributeError: os.geteuid` | instance setup | Not executed |
| 13 | `DayHandoffSecurePathTests.test_write_uses_secure_directory_and_permissions` | `AttributeError: os.geteuid` | instance setup | Not executed |

The first row is one unittest error record that prevents six test bodies from running.
Those bodies are:

- `CleanWindowShadowGatewayTests.test_gateway_endpoints_disabled_by_default`
- `CleanWindowShadowGatewayTests.test_gateway_requires_bearer_token`
- `CleanWindowShadowGatewayTests.test_gateway_close_allowed_when_disabled_with_auth`
- `CleanWindowShadowGatewayTests.test_gateway_start_when_enabled`
- `CleanWindowShadowGatewayTests.test_formal_chat_path_unchanged`
- `CleanWindowShadowGatewayTests.test_no_chat_messages_write_from_shadow_turn`
