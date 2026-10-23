# NETOPS-CHECKLIST — container-spec §12 → implementation → test

For the NetOps reviewer: every blocking item from §12 of the FROZEN
container spec (2026-10-22), plus role-mode conditions R1–R10/R9a and
key-mode K1–K4, mapped to where it lives and what proves it. Line
numbers are against this commit; "test" means a pytest in `tests/`
runnable via `make test` (all 120 pass; see VERIFICATION.md). Everything
here is template/stub-level proof — nothing has run on AWS.

## §12 blocking items 1–13

| # | Item | Implementation | Test |
|---|---|---|---|
| 1 | Private route table local-only; no IGW/NAT/IPv6/peering from guest subnet | `stacks/network_stack.py:57` (`nat_gateways=0`), `:65` (`PRIVATE_ISOLATED`); no IPv6/peering/TGW/VPN/endpoint constructs anywhere in `stacks/` | `test_gherkin_scenarios.py::test_private_subnet_has_no_default_route`; `test_netops_deltas.py::test_fixed_ips_and_cidrs` |
| 2 | §4.2 DNS control (option **a**) + assertion + three `dig` checks | `stacks/network_stack.py:54` (`enable_dns_support=False`), `:75-83` (DHCP options 1.1.1.1/9.9.9.9); `scripts/guest/smoke-isolation.sh:127-130` (`dig @10.99.0.2 / @169.254.169.253 / @1.1.1.1`, all must fail, all three vantages) | `test_netops_deltas.py::test_vpc_dns_support_disabled`, `::test_dhcp_options_external_resolvers`, `::test_proxy_sg_dns_egress_only_to_two_resolvers` |
| 3 | Fixed IPs consistent; no bare truncated-proxy-IP literal (the proxy IP minus its final digit) anywhere | `stacks/constants.py` (single source: .10/.70/CIDRs); `scripts/proxy/squid.conf:11`; `scripts/proxy/litellm-config.template.yaml` bind; `scripts/guest/guest-bootstrap.sh:30-31` (`NO_PROXY=10.99.0.10`); `scripts/lib/common.sh` | `test_netops_deltas.py::test_fixed_ips_and_cidrs`, `::test_no_bare_gateway_prefix_in_templates`, `::test_no_bare_gateway_prefix_in_scripts_and_source` (regex `10\.99\.0\.1(?![0-9])` over templates AND the whole source tree) |
| 4 | SG matrix per §7; no world ingress; default SG restricted | `stacks/network_stack.py:88-147` (eic/eval/proxy SGs, `allow_all_outbound=False`); default SG: `restrict_default_security_group=False` at `:56` (R9 — no Lambda) with `scripts/setup.sh:174-186` (`step_default_sg` revokes ingress+egress) — **recorded deviation**, README "Deviations" | `test_gherkin_scenarios.py::test_eval_sg_egress_only_proxy_3128_4000`, `::test_no_sg_ingress_from_world`; `test_scripts.py::test_setup_dry_run_prints_full_plan` (revoke step present) |
| 5 | Guest: no profile, IMDSv2 hop 1, encrypted root, cloud-init.disabled before IMDS-off, static operator pubkey (C7), no secrets in user-data | `stacks/eval_stack.py:96-114` (no `iam_instance_profile`, hop 1, 80 GB encrypted), `:84-86` (authorized_keys from `operatorSshPublicKey` context — `stacks/config.py:153-160` refuses private keys); ordering in `scripts/setup.sh:162-172` (`step_harden`: cloud-init.disabled THEN `modify-instance-metadata-options --http-endpoint disabled`, guest only) | `test_gherkin_scenarios.py::test_eval_instance_has_no_instance_profile`, `::test_eval_instance_imdsv2_hop1_encrypted_root`, `::test_no_secret_in_parameters_outputs_or_userdata`; `test_config.py::test_private_key_in_ssh_context_refused` |
| 6 | R1/R2/R4/R5/R6/R7/R9 (role) or K1–K4 (key); both modes synthesize | see R/K tables below | `test_gherkin_scenarios.py::test_all_four_stacks_synthesize` (parametrized role+key); `make synth` builds both |
| 7 | Squid: CONNECT :443 only; :80 only apt in setup; §4.3 deny set BEFORE allows; binds 10.99.0.10; allowlists byte-frozen | `scripts/proxy/squid.conf:11` (bind), `:32-50` (IP-literal + RFC1918/link-local/multicast/v6 deny set), `:53-59+` (all denies precede allows; `deny CONNECT !SSL_443`); lists in `scripts/proxy/allow-*.txt`, swapped by phase via `h2-egress:56-66` | `test_allowlists.py` (all 5: setup CONNECT frozen 9 hosts, http80 frozen 2 hosts, run+closed EMPTY, no contamination names) |
| 8 | h2-egress gates squid AND :4000 per §4.3 table; boot=closed via systemd ordering; logs changes; unreachable from guest | `scripts/proxy/h2-egress:8-11` (phase table), `:94-103` (nftables :4000 gate, open only in `run`), `:105` (logs each change); boot default: `scripts/proxy/proxy-bootstrap.sh:66` (nft boot rule) + `:155-165` (`h2-egress-boot.service`, `Before=squid.service litellm.service`); guest can't reach it (no :22/ssh path guest→proxy in SG matrix) | `test_scripts.py::test_run_arm_dry_run` (phase transitions in disarm); shellcheck; live gate behavior = **deploy-time** (smoke-isolation.sh per phase) |
| 9 | run-arm.sh: snapshot BEFORE key; tmpfs 0600; EXIT/ERR trap → closed + wiped + gateway key deleted | `scripts/run-arm.sh:107-122` (snapshot `pre-run-N` + wait), `:125-126` (`trap disarm EXIT/ERR` set before `/key/generate` at `:132-137`), `:141-147` (tmpfs `/run/h2/keys.env` 0600 eval), `:78-86` (`disarm`: h2-egress closed → `shred -u` → `/key/delete`) | `test_scripts.py::test_run_arm_dry_run` (asserts `out.index("pre-run-1") < out.index("/key/generate")`, `/key/delete`, keys.env, h2-egress closed), `::test_run_arm_requires_run_id` |
| 10 | Budget: 50/80/100% notifications; 100% action targets correct principal both modes; exec role scoped by `iam:PolicyARN`; no Service-only filter | `stacks/guardrails_stack.py:113-136` (ACTUAL 50/80/100 + FORECASTED 100), `:62-100` (action role trust `budgets.amazonaws.com`, `ArnEquals iam:PolicyARN` at `:70`, Roles vs Users per mode at `:81-100`), `:104` (optional tag filter; no Service filter) | `test_gherkin_scenarios.py::test_budget_has_50_80_100_actual_and_100_forecast`, `::test_budget_action_attaches_deny_policy_at_100`, `::test_budget_action_targets_role_or_user`; `test_netops_deltas.py::test_budget_not_filtered_to_bedrock_service`, `::test_budget_tag_filter_optional`, `::test_budget_default_3500_comes_from_cdk_json`, `::test_budget_override_500` |
| 11 | Teardown: deny-policy detach, K4 check, `--delete-replaced-root-volume`, orphan volumes; dry-run read-only | `scripts/teardown.sh:69-88` (detach role/user), `:90-100` (K4), §9-step-8 order per header `:7-19`, orphan snapshots/volumes/log group after destroy; `scripts/restore-snapshot.sh:55-58` (`create-replace-root-volume-task --delete-replaced-root-volume`) | `test_scripts.py::test_teardown_dry_run_read_only` (mutating stub verbs exit 97), `::test_teardown_dry_run_lists_deletions`, `::test_teardown_k4_aborts_on_leftover_access_key`, `::test_teardown_k4_passes_with_no_keys`, `::test_restore_snapshot_dry_run` |
| 12 | `setup.sh --dry-run` zero AWS calls; setup aborts on smoke failure — both tested | `scripts/setup.sh` (`run()` in `scripts/lib/common.sh` prints instead of executing; `resolve_account_operator` makes no sts call in dry-run); smoke abort in `step_smoke_closed` (`scripts/setup.sh:228`) | `test_scripts.py::test_setup_dry_run_zero_aws_calls` (poisoned stubs exit 97 if touched), `::test_setup_dry_run_does_not_write_state`, `::test_setup_aborts_on_smoke_failure` (later steps not marked done), `::test_setup_typed_confirmation_mismatch_aborts` |
| 13 | Supply chain: Node sha256 pinned fail-closed; LiteLLM pinned/hash-locked/non-prerelease/not 1.82.7-8; uv pinned; no unpinned `curl \| sh` | `scripts/guest/guest-bootstrap.sh:16` (NODE_SHA256), `:72-77` (`sha256sum -c` fails closed); `scripts/proxy/litellm-requirements.lock` (1.106.2, 144 pkgs, hashes) installed `--require-hashes` at `proxy-bootstrap.sh:106`; `dev/regen-litellm-lock.sh:10-16` (refuses 1.82.7/1.82.8); uv 0.13.5 hashes `guest-bootstrap.sh:25-26` | Review + VERIFICATION.md §1.7 (checked against live upstream 2026-10-22); shellcheck sweep |

## Role-mode conditions R1–R10

| ID | Implementation | Test / proof |
|---|---|---|
| R1 | One `AWS::IAM::InstanceProfile`, proxy only: `stacks/proxy_stack.py:95-96`; eval has none (`stacks/eval_stack.py:100` comment, no property) | `test_netops_deltas.py::test_iam_inventory_role_mode` (EXACTLY 3 roles/0 users), `::test_proxy_has_instance_profile_role_mode_only`; `test_gherkin_scenarios.py::test_eval_instance_has_no_instance_profile` |
| R2 | Invoke-only actions, resource-scoped, no managed policies: `stacks/bedrock_policy.py:23-24` (the only two actions) | `test_netops_deltas.py::test_gateway_allows_only_invoke_actions`; IAM5 NOT acknowledged for any Allow (nag audit in `test_every_suppression_has_nontrivial_reason`) |
| R3 | CRIS shapes per §6.4: `stacks/bedrock_policy.py:32-98` (in-region RequestedRegion pin; profile ARN; per-destination FMs under `bedrock:InferenceProfileArn`; global region-less FM with `RequestedRegion=unspecified`) | `test_netops_deltas.py::test_cris_statement_shapes`; `test_config.py::test_cris_profile_needs_destination_regions`. Live AccessDenied behavior = **deploy-time Phase 0** |
| R4 | LiteLLM native venv, uid `litellm`, pinned `--require-hashes`, never 1.82.7/8, no Docker: `proxy-bootstrap.sh:42,102-106`; version rationale in README/VERIFICATION §1.7; lock regen documented in `dev/regen-litellm-lock.sh` | Review; lock file hashes present; refusal list tested by reading `regen-litellm-lock.sh:16` |
| R5 | Proxy IMDSv2 hop 1: `stacks/proxy_stack.py:130-133`; owner-match nft rule (root+litellm only): `proxy-bootstrap.sh:69-76` | `test_netops_deltas.py::test_proxy_imdsv2_hop1`; owner-match = review + deploy-time smoke (`sudo -u proxy curl IMDS` fails) |
| R6 | LiteLLM hardening: `scripts/proxy/litellm-config.template.yaml` — `store_model_in_db: false` (:83), `ui_access_mode: admin_only` (:87), `store_prompts_in_spend_logs: false`, no client-side credentials, bind 10.99.0.10:4000 (`proxy-bootstrap.sh:143`); master key root-only tmpfs-adjacent file (`:110-115`), Postgres 127.0.0.1 (`:83-100`). **Caveat:** `DISABLE_ADMIN_UI` env unconfirmed in 1.106.2 source (VERIFICATION §1.7) | Config review; `test_gherkin_scenarios.py::test_no_secret_in_parameters_outputs_or_userdata` (master key never in CFN) |
| R7 | Explicit nonzero prices all 5 models + global `max_budget` + per-key rpm/tpm: `litellm-config.template.yaml:22-73` (glm-5/opus-4-8/sonnet-4-6/haiku-4-5/gpt-oss-120b in+out+cache costs, `max_budget: __MAX_BUDGET__` placeholder filled by bootstrap from budgetCapUsd); per-key limits in `run-arm.sh` keygen payload (`--rpm 60 --tpm 200000`) | Review (prices vs PreCog §4.1); spend>0 check = **deploy-time** `smoke-paid.sh` |
| R8 | EIP + Deny-unless-SourceIp: `stacks/proxy_stack.py:70-77` (EIP first, pin references it), `stacks/bedrock_policy.py:100-107` (`build_source_ip_deny`); context-disableable in role mode only | `test_netops_deltas.py::test_source_ip_deny_present_both_modes`, `::test_source_ip_deny_can_be_disabled_in_role_mode_only`, `::test_proxy_eip_exists` |
| R9 | IAM inventory exact; no Lambda/custom resources; explicit LogGroup (no logRetention helper): `stacks/network_stack.py:189-222` | `test_netops_deltas.py::test_iam_inventory_role_mode`, `::test_iam_inventory_key_mode` (zero `AWS::Lambda::*`, zero `Custom::*` both modes); `::test_flow_logs_all_traffic_kms_encrypted` |
| R9a | No operator EIC policy attached; recommended JSON as output only: `stacks/eval_stack.py:128-140` (`RecommendedOperatorEicPolicy`, `OpenTunnel`, port 22, two private IPs) | `test_netops_deltas.py::test_r9a_recommended_eic_policy_is_output_only` |
| R10 | Model access enabled once by CC from admin principal before Day 1 | Runbook item — README "Prerequisites" + "Runbook"; not automatable here |

## Key-mode conditions K1–K4

| ID | Implementation | Test / proof |
|---|---|---|
| K1 | User `harness2-eval-bedrock` (no console access — no LoginProfile resource), R2/R3 statements + MUST SourceIp deny (forced regardless of context): `stacks/proxy_stack.py:101-110`; `stacks/config.py` forces `restrict_source_ip=True` in key mode | `test_config.py::test_key_mode_forces_source_ip_pin`; `test_netops_deltas.py::test_source_ip_deny_present_both_modes` |
| K2 | CDK creates user+policy only, NO `AWS::IAM::AccessKey`; operator-made key goes over EIC into proxy tmpfs `/run/h2gw/aws.env` 0600, never guest/CFN/log: `scripts/setup.sh:213-224` (`step_key_credentials`, reads from operator env, pipes over ssh) | `test_netops_deltas.py::test_key_mode_user_has_no_access_key_resource`; `test_gherkin_scenarios.py::test_no_secret_in_parameters_outputs_or_userdata` |
| K3 | Budget Action attaches to the USER; exec role `iam:Attach/DetachUserPolicy` on that user ARN only: `stacks/guardrails_stack.py:96-100` + role scoping `:62-78` | `test_gherkin_scenarios.py::test_budget_action_targets_role_or_user` (Users in key mode, Roles in role mode) |
| K4 | Teardown checks `list-access-keys` and stops with instructions: `scripts/teardown.sh:90-98` ("K4 FAILED … CC must delete them by hand first") | `test_scripts.py::test_teardown_k4_aborts_on_leftover_access_key`, `::test_teardown_k4_passes_with_no_keys` |

## Non-blocking §12 flags — status

* FORECASTED budget alert: **implemented** (`guardrails_stack.py:128-136`).
* KMS on the flow-log group: **implemented** (`network_stack.py:162-222`).
* Per-key rpm/tpm limits: **implemented** (`run-arm.sh` keygen payload).
* §4.4 NACL and LiteLLM egress pinning: **not implemented** (optional;
  SG matrix + squid deny set + DNS-off are the layered controls).
* snapd/motd-news disable: **implemented** in `guest-bootstrap.sh`.

## What a reviewer should run

```
make verify        # account-literal scan (+self-test), shellcheck ×16,
                   # synth both modes with cdk-nag enforced, 120 pytest
```

All through `dev/no-aws-env.sh` — zero credentials needed or usable.
Items marked **deploy-time** above cannot be verified here and are
enumerated in VERIFICATION.md §2 in bring-up order.
