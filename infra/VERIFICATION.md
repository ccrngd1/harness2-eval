# VERIFICATION — harness2-eval infra

**Status: NEW BUILD, NEVER DEPLOYED.** Everything below is split into
what was verified ON THIS BUILD HOST (no AWS credentials ever reached
cdk, the scripts, or pytest — all runs go through `dev/no-aws-env.sh`,
an `env -i` wrapper) and what CANNOT be verified until the first real
deploy. Nothing in the second list should be taken on faith.

Build date: 2026-10-22. Toolchain: Python 3.11.2, aws-cdk-lib 2.274.0,
constructs 10.8.1, cdk-nag 3.0.2, aws-cdk CLI 2.1146.1 (local
node_modules), pytest 9.1.1, shellcheck 0.11.0 (via shellcheck-py).

---

## 1. Verified here (build host, credential-free)

### 1.1 Synth, both credential modes

```
make synth   # == dev/no-aws-env.sh dev/synth-local.sh role|key
```

* `cdk synth --no-notices` exits 0 for `credentialMode=role` (cdk.out/)
  and `credentialMode=key` (cdk.out.key/), 4 stacks each (H2Network,
  H2Proxy, H2Eval, H2Guardrails).
* Context comes entirely from `tests/fixtures/context.json` → `-c`
  flags. No credentials, no `fromLookup`, no context-provider calls
  (AMIs via Canonical SSM parameter refs; AZ hardcoded `{region}a`).

### 1.2 cdk-nag (AwsSolutionsChecks, error-on-unsuppressed)

* Zero unsuppressed findings, both modes (validation plugin fails synth
  otherwise; `tests/test_gherkin_scenarios.py::test_nag_no_unsuppressed_findings`
  additionally synthesizes with `validate_on_synthesis=True`).
* 7 acknowledged rule/stack pairs, identical in both modes, each with a
  written justification in `stacks/nag_suppressions.py` (audited by
  `test_every_suppression_has_nontrivial_reason` — ≥40-char reasons,
  written into template metadata):
  | Stack | Rule | One-line reason |
  |---|---|---|
  | H2Network | VPC7 | false positive: FlowLog L2 (type ALL, KMS CW group) IS attached |
  | H2Proxy | EC28 | no detailed monitoring on a <2-week disposable sandbox |
  | H2Proxy | EC29 | termination protection off: teardown must destroy unattended |
  | H2Proxy | IAM5 | R8/K1 Deny-unless-SourceIp=EIP (a Deny with wildcards narrows) |
  | H2Eval | EC28 | as above |
  | H2Eval | EC29 | as above |
  | H2Guardrails | IAM5 | kill-switch Deny bedrock:* on * is the design |
  * IAM5 is NOT acknowledged for any Allow statement (R2); gateway
    Allows are invoke-only and resource-scoped (`test_gateway_allows_only_invoke_actions`).
  * Note: aws-cdk-lib's FlowLog construct injects two framework-internal
    `CloudFormation-Validate::F3002/F3003` acknowledgments of its own
    (camelCase DestinationOptions quirk). Not ours; excluded from the audit.

### 1.3 pytest — 120 passed, 0 failed, 0 skipped

```
make test    # == dev/no-aws-env.sh .venv/bin/python -m pytest tests/ -q
```

* `tests/test_config.py` (22): CC directive A resolution — explicit
  account alone, ambient alone, both matching, explicit-vs-ambient
  mismatch ABORTS, no-account errors with guidance, malformed accounts
  rejected; region explicit→ambient→us-east-1; context validation
  (credentialMode, budget positive/numeric, private-key-in-context
  refused, empty models refused, CRIS profile needs destinations, K1
  forces the SourceIp pin in key mode).
* `tests/test_gherkin_scenarios.py` (22): every Gherkin line from the
  requirements — 4 stacks synth; nag clean; suppression reasons; private
  subnet has no 0.0.0.0/0 route; eval instance has no instance profile,
  IMDSv2 hop 1, encrypted 80 GB root; eval SG egress exactly proxy-SG
  3128+4000; no SG ingress from 0.0.0.0/0 or ::/0 anywhere; no secret
  patterns in Parameters/Outputs/user-data; Budget notifications
  ACTUAL 50/80/100 + FORECASTED 100; Budget Action APPLY_IAM_POLICY,
  AUTOMATIC, at 100, targeting Roles (role mode) / Users (key mode).
* `tests/test_netops_deltas.py` (37): CC directive B — budget default
  3500 read from cdk.json (single copy) and override 500 both asserted;
  no Service=Bedrock cost filter; optional tag filter on/off. IAM
  enumeration — role mode EXACTLY 3 roles (gateway, budget-action,
  flow-log)/0 users, key mode EXACTLY 2 roles/1 user, zero Lambdas and
  zero custom resources in both (R1/R9); no AWS::IAM::AccessKey anywhere
  (K2); proxy instance profile in role mode only. DNS option (a) — VPC
  EnableDnsSupport=false, DHCP options 1.1.1.1/9.9.9.9, proxy SG :53
  egress to exactly those two /32s. Proxy IMDSv2 hop 1. Fixed addressing
  (10.99.0.0/24, /26+/26, .10, .70) and the truncated-proxy-IP regex
  over templates AND the whole source tree. R8/K1 SourceIp deny present
  in both modes, disable honored in role mode only. §6.4 CRIS shapes —
  in-region RequestedRegion pin; us. profile ARN + per-destination FM
  ARNs under bedrock:InferenceProfileArn; global. adds the region-less
  FM ARN with RequestedRegion=unspecified; no blanket RequestedRegion on
  CRIS. Flow log ALL + KMS + 30-day. EIC endpoint preserveClientIp=false;
  R9a policy is an output only. Proxy EIP exists.
* `tests/test_allowlists.py` (5): §5.1 setup CONNECT list frozen
  byte-for-byte (9 hosts), setup HTTP:80 list frozen (2 hosts), run and
  closed lists EMPTY, no §5.2 contamination name on any list.
* `tests/test_scripts.py` (34): script harness under stub binaries —
  see 1.5; plus shellcheck parametrized over all 16 shell files and the
  account-literal scan + self-test + planted-literal test.

### 1.4 Account-literal check (CC directive A)

```
make lint    # includes dev/check-no-account-ids.sh (+ --self-test)
```

* `dev/check-no-account-ids.sh` scans the tree for bare 12-digit runs
  (`(?<![0-9A-Za-z])[0-9]{12}(?![0-9A-Za-z])`) and the dashed
  4-4-4-digit form, excluding `.venv/ node_modules/ cdk.out*/
  tests/fixtures/`: **clean**.
* `--self-test` plants bare and dashed literals in a temp tree and
  asserts the scan fails, and that a 13-digit run does NOT trip it:
  **passed**. Wired into `make lint` and pytest
  (`test_check_no_account_ids_*`), so a literal fails the build.
* Test code reads the placeholder from
  `tests/fixtures/placeholder-account.txt` at runtime; no literal
  appears outside `tests/fixtures/`.

### 1.5 Operator-script dry-runs (stubbed AWS, zero real calls)

Verified by pytest AND by hand with poisoned stubs on PATH:

* `setup.sh --dry-run`: **exit 0, ZERO invocations of aws/cdk/npx/ssh/
  scp** (stubs exit 97 and fail the test if touched); prints the full
  plan (cdk deploy → wait → cloud-init.disabled → IMDS off on guest →
  default-SG revoke → proxy bootstrap → smoke-isolation → h2-egress
  setup → guest bootstrap → closed → demo + squid zero-CONNECT check →
  baseline-clean snapshot); does not write the state file.
* `setup.sh` (stubbed non-dry-run): typed-confirmation mismatch aborts;
  forced smoke-test failure (H2_TEST_FORCE_SMOKE_FAIL hook) aborts
  without marking the smoke step or anything after it done; resume skips
  steps recorded in the state file.
* `teardown.sh --dry-run`: exit 0 using READ-ONLY stub verbs only (any
  mutating verb in the stub exits 97); lists cdk destroy, tagged
  snapshot/log-group deletions, the §9-step-8 order, and the manual
  checklist. K4: aborts when the stub reports a leftover access key on
  harness2-eval-bedrock; passes when none.
* `run-arm.sh --dry-run`: snapshot `pre-run-N` printed BEFORE
  `/key/generate` (order asserted); disarm path prints h2-egress closed,
  keys.env wipe, `/key/delete`, log export.
* `restore-snapshot.sh --dry-run`: resolves the tag alias via read-only
  describe-snapshots and prints
  `create-replace-root-volume-task ... --delete-replaced-root-volume`.

### 1.6 shellcheck

All 16 shell files pass `shellcheck -x` with zero findings (0.11.0):
4 operator scripts + lib/common.sh, 5 guest files (incl. extensionless
`h2-jail`), 2 proxy shell files (incl. extensionless `h2-egress`), 4
dev/ scripts. Parametrized in pytest so a new failing script breaks
`make test` too.

### 1.7 Supply-chain pins (checked against live sources on 2026-10-22)

* Node 22.23.4 linux-x64.tar.xz, sha256
  `1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce`
  (fetched from nodejs.org SHASUMS256; guest-bootstrap fails closed on
  mismatch).
* LiteLLM 1.106.2 (not 1.82.7/1.82.8 — those are explicitly refused in
  `dev/regen-litellm-lock.sh` and `proxy-bootstrap.sh` installs with
  `--require-hashes` from the 144-package lock, including prisma 0.15.0
  for virtual keys). Verified in the pinned source: `/key/delete`
  endpoint, `LITELLM_LOCAL_MODEL_COST_MAP`, `ui_access_mode`,
  `allow_client_side_credentials`, `store_model_in_db`,
  `store_prompts_in_spend_logs`, litellm_settings `max_budget`,
  cache cost keys, allowed_routes `llm_api_routes`.
  **Unconfirmed:** the `DISABLE_ADMIN_UI` env var was NOT found in the
  1.106.2 source; it is set belt-and-braces in the systemd unit but the
  verified control is `general_settings.ui_access_mode: admin_only`.
* uv 0.13.5 (wheel + sdist hashes pinned), pyyaml 6.0.3.
* Git pins: harness2 `1cfe254`, OpenCode `23fb5e05`, harvey-labs
  `b58a28ae`, Bun 1.3.11, Codex 0.144.6 — cloned shallow-by-SHA in
  guest-bootstrap; npm installs use `--ignore-scripts` and
  `--frozen-lockfile` where a lockfile exists.

---

## 2. NOT verified — requires the first real deploy

None of the following has ever run against AWS. Treat each as a claim
to be checked during bring-up, in this order:

1. **CloudFormation acceptance.** Templates synthesize and pass
   assertions, but CFN itself (EIC endpoint in this VPC shape,
   Budgets Action APPLY_IAM_POLICY wiring, DHCP options with DNS
   support off) has not accepted them. First `cdk deploy` is the test.
2. **The network actually isolates.** SG matrix, no-default-route,
   DNS-off behavior, EIC reachability — asserted on templates only.
   `scripts/guest/smoke-isolation.sh` exists precisely to verify this
   live from three vantage points; setup.sh ABORTS if it fails.
3. **squid/nftables/LiteLLM behavior.** squid.conf parses in review but
   has not served a request; the nftables phase gate, owner-match IMDS
   rule, and LiteLLM cost tracking (R7: spend > 0 after one call per
   model) are untested until `smoke-isolation.sh`/`smoke-paid.sh` run.
4. **Bedrock policy shapes against the real control plane.** The CRIS
   statements match AWS documentation (§6.4) but no InvokeModel has
   been issued. Model IDs in context are from the PreCog research doc;
   verify each against `aws bedrock list-foundation-models` before use.
5. **Budget Action end-to-end.** AWS Budgets evaluates spend only a few
   times a day — the deny-at-100% path cannot be exercised quickly even
   after deploy. LiteLLM `max_budget` is the primary cap; the Budget is
   the backstop (CC directive B).
6. **Guest bootstrap.** Pinned downloads through squid, bwrap jail on
   Ubuntu 24.04's AppArmor userns restriction, harness2-demo offline —
   all scripted, none executed (hard constraint: no Harness2/Bun/squid/
   LiteLLM on this build host).
7. **Costs.** The README cost table is an ESTIMATE from public pricing
   pages on 2026-10-22 — verify against the Cost Explorer after a day
   of soak before trusting it.

## 3. Deviations (full list in README/NETOPS-CHECKLIST)

* `restrictDefaultSecurityGroup=false` (CDK implements it with a
  Lambda custom resource → R9 violation); `setup.sh` revokes the
  default SG's rules post-deploy instead.
* Flow-log group has a fixed name (`/harness2-eval/flow-logs`) so
  teardown can find stragglers after a failed destroy.
* `DISABLE_ADMIN_UI` env var unconfirmed in LiteLLM 1.106.2 (see 1.7).
