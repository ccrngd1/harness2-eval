"""Scenario: Scripts are safe to run / Scenario: Teardown is complete.

Every test runs the real script under a PATH of stub binaries:
  * setup.sh --dry-run: the aws/cdk/npx/ssh/scp stubs FAIL THE TEST if
    invoked at all (zero AWS calls is a hard requirement).
  * teardown.sh --dry-run: the aws stub answers read-only verbs from
    fixtures and fails the test on any mutating verb.
Plus shellcheck over every script and the account-literal scan wrapper.
"""

import os
import pathlib
import stat
import subprocess

import pytest

INFRA = pathlib.Path(__file__).parents[1]
SCRIPTS = INFRA / "scripts"
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# CC directive A: the only sanctioned 12-digit literal lives in
# tests/fixtures/; everything here derives from it at runtime.
PLACEHOLDER = (FIXTURES / "placeholder-account.txt").read_text().strip()
OTHER_ACCOUNT = "9" * 12

FAIL_STUB = """#!/usr/bin/env bash
echo "STUB-VIOLATION: {name} invoked with: $*" >&2
exit 97
"""

READONLY_AWS_STUB = f"""#!/usr/bin/env bash
joined="$*"
case "$joined" in
  *delete-*|*detach-*|*create-*|*revoke-*|*modify-*|*terminate-*|*attach-*|*put-*|*stop-*|*run-instances*)
    echo "STUB-VIOLATION: mutating aws call: $joined" >&2; exit 97 ;;
  *"sts get-caller-identity"*Account*) echo "{PLACEHOLDER}"; exit 0 ;;
  *cloudformation*describe-stacks*DenyPolicyArn*) echo "arn:aws:iam::{PLACEHOLDER}:policy/H2G-deny-STUB"; exit 0 ;;
  *cloudformation*describe-stacks*GatewayRoleName*) echo "H2Proxy-GatewayRole-STUB"; exit 0 ;;
  *cloudformation*describe-stacks*) echo "stub-output-value"; exit 0 ;;
  *iam*list-attached-role-policies*) echo ""; exit 0 ;;
  *iam*list-attached-user-policies*) echo ""; exit 0 ;;
  *iam*list-access-keys*) echo "${{H2_STUB_ACCESS_KEYS:-}}"; exit 0 ;;
  *ec2*describe-snapshots*) echo "snap-0123456789abcdef0"; exit 0 ;;
  *ec2*describe-volumes*) echo ""; exit 0 ;;
  *ec2*describe-instances*) echo "i-0123456789abcdef0"; exit 0 ;;
  *logs*describe-log-groups*) echo "/harness2-eval/flow-logs"; exit 0 ;;
  *resourcegroupstaggingapi*get-resources*) echo ""; exit 0 ;;
  *) echo "STUB-VIOLATION: unexpected aws call: $joined" >&2; exit 97 ;;
esac
"""

# Happy-path stub for non-dry-run setup tests: sts answers with the
# placeholder account; everything else succeeds silently.
OK_STUB = (
    "#!/usr/bin/env bash\n"
    f'case "$*" in *"sts get-caller-identity"*Account*) echo {PLACEHOLDER};;'
    f" *\"sts get-caller-identity\"*Arn*) echo arn:aws:iam::{PLACEHOLDER}:user/stub;;"
    " *) echo stub-ok;; esac\nexit 0\n"
)


def make_ok_stub_dir(tmp_path) -> pathlib.Path:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    for name in ("aws", "cdk", "npx", "ssh", "scp"):
        p = stub_dir / name
        p.write_text(OK_STUB)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return stub_dir


def make_stub_dir(tmp_path, aws_stub: str) -> pathlib.Path:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    for name in ("cdk", "npx", "ssh", "scp", "dig"):
        p = stub_dir / name
        p.write_text(FAIL_STUB.format(name=name))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    aws = stub_dir / "aws"
    aws.write_text(aws_stub)
    aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
    return stub_dir


def run_script(script, args, stub_dir, extra_env=None, stdin=""):
    env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/root"),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        timeout=60,
    )


# ---- setup.sh --dry-run: ZERO AWS calls -----------------------------------------

def test_setup_dry_run_zero_aws_calls(tmp_path):
    stub = make_stub_dir(tmp_path, FAIL_STUB.format(name="aws"))
    r = run_script(SCRIPTS / "setup.sh", ["--dry-run", "--state", str(tmp_path / "st")], stub)
    assert r.returncode == 0, r.stderr
    assert "STUB-VIOLATION" not in r.stderr, f"dry-run touched a binary it must not: {r.stderr}"
    assert "[dry-run]" in r.stdout


def test_setup_dry_run_prints_full_plan(tmp_path):
    stub = make_stub_dir(tmp_path, FAIL_STUB.format(name="aws"))
    r = run_script(SCRIPTS / "setup.sh", ["--dry-run", "--state", str(tmp_path / "st")], stub)
    for marker in (
        "cdk deploy",
        "instance-status-ok",
        "cloud-init.disabled",
        "modify-instance-metadata-options",
        "revoke-security-group-ingress",
        "smoke-isolation",
        "create-snapshot",
        "baseline-clean",
    ):
        assert marker in r.stdout, f"dry-run plan missing: {marker}"


def test_setup_dry_run_does_not_write_state(tmp_path):
    stub = make_stub_dir(tmp_path, FAIL_STUB.format(name="aws"))
    state = tmp_path / "st"
    r = run_script(SCRIPTS / "setup.sh", ["--dry-run", "--state", str(state)], stub)
    assert r.returncode == 0
    assert state.read_text() == "", "dry-run must not mark steps done"


def test_setup_rejects_bad_account(tmp_path):
    stub = make_stub_dir(tmp_path, FAIL_STUB.format(name="aws"))
    r = run_script(
        SCRIPTS / "setup.sh",
        ["--dry-run", "--account", "12345", "--state", str(tmp_path / "st")],
        stub,
    )
    assert r.returncode != 0
    assert "12-digit" in r.stderr


def test_setup_aborts_on_smoke_failure(tmp_path):
    # Non-dry-run with full stubs; H2_TEST_FORCE_SMOKE_FAIL simulates a
    # failing isolation smoke test. setup.sh must die and NOT mark the
    # smoke step (or later steps) done.
    stub_dir = make_ok_stub_dir(tmp_path)
    state = tmp_path / "st"
    r = run_script(
        SCRIPTS / "setup.sh",
        ["--state", str(state)],
        stub_dir,
        extra_env={
            "H2_TEST_FORCE_SMOKE_FAIL": "1",
            "H2_SETUP_ASSUME_YES": PLACEHOLDER,
        },
    )
    assert r.returncode != 0
    assert "smoke test FAILED" in r.stderr
    done = state.read_text()
    assert "smoke-closed" not in done
    assert "guest-bootstrap" not in done
    assert "snapshot-baseline" not in done


def test_setup_resume_skips_done_steps(tmp_path):
    stub = make_stub_dir(tmp_path, FAIL_STUB.format(name="aws"))
    state = tmp_path / "st"
    state.write_text("deploy\nwait-instances\n")
    r = run_script(SCRIPTS / "setup.sh", ["--dry-run", "--state", str(state)], stub)
    assert r.returncode == 0
    assert "step deploy: already done, skipping" in r.stdout
    assert "step wait-instances: already done, skipping" in r.stdout
    assert "step harden\n" in r.stdout or "== step harden" in r.stdout


def test_setup_typed_confirmation_mismatch_aborts(tmp_path):
    stub_dir = make_ok_stub_dir(tmp_path)
    r = run_script(
        SCRIPTS / "setup.sh",
        ["--state", str(tmp_path / "st")],
        stub_dir,
        stdin=OTHER_ACCOUNT + "\n",
    )
    assert r.returncode != 0
    assert "does not match" in r.stderr


# ---- teardown.sh --dry-run: read-only only ----------------------------------------

def test_teardown_dry_run_read_only(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(SCRIPTS / "teardown.sh", ["--dry-run"], stub)
    assert r.returncode == 0, r.stderr
    assert "STUB-VIOLATION" not in r.stderr


def test_teardown_dry_run_lists_deletions(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(SCRIPTS / "teardown.sh", ["--dry-run"], stub)
    assert "cdk destroy" in r.stdout
    assert "delete-snapshot" in r.stdout      # fixture snapshot listed for deletion
    assert "delete-log-group" in r.stdout     # fixture log group listed
    assert "manual leftovers" in r.stdout


def test_teardown_k4_aborts_on_leftover_access_key(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(
        SCRIPTS / "teardown.sh",
        ["--dry-run", "--credential-mode", "key"],
        stub,
        extra_env={"H2_STUB_ACCESS_KEYS": "AKIASTUBSTUBSTUBSTUB"},
    )
    assert r.returncode != 0
    assert "K4 FAILED" in r.stderr


def test_teardown_k4_passes_with_no_keys(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(SCRIPTS / "teardown.sh", ["--dry-run", "--credential-mode", "key"], stub)
    assert r.returncode == 0, r.stderr
    assert "K4 OK" in r.stdout


# ---- run-arm.sh / restore-snapshot.sh dry-runs -------------------------------------

def test_run_arm_dry_run(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(SCRIPTS / "run-arm.sh", ["--dry-run", "--run-id", "1"], stub)
    assert r.returncode == 0, r.stderr
    assert "STUB-VIOLATION" not in r.stderr
    out = r.stdout
    # Order: snapshot BEFORE key generation; disarm closes+wipes+deletes.
    assert out.index("pre-run-1") < out.index("/key/generate")
    assert "/key/delete" in out
    assert "keys.env" in out
    assert "h2-egress closed" in out or "h2-egress\\ closed" in out.replace("'", "")


def test_run_arm_requires_run_id(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(SCRIPTS / "run-arm.sh", ["--dry-run"], stub)
    assert r.returncode != 0
    assert "--run-id" in r.stderr


def test_restore_snapshot_dry_run(tmp_path):
    stub = make_stub_dir(tmp_path, READONLY_AWS_STUB)
    r = run_script(
        SCRIPTS / "restore-snapshot.sh", ["--dry-run", "--snapshot", "baseline-clean"], stub
    )
    assert r.returncode == 0, r.stderr
    assert "create-replace-root-volume-task" in r.stdout
    assert "--delete-replaced-root-volume" in r.stdout


# ---- shellcheck over every script ----------------------------------------------------

ALL_SCRIPTS = sorted(
    [
        *SCRIPTS.glob("*.sh"),
        SCRIPTS / "lib" / "common.sh",
        *(SCRIPTS / "guest").glob("*.sh"),
        SCRIPTS / "guest" / "h2-jail",
        *(SCRIPTS / "proxy").glob("*.sh"),
        SCRIPTS / "proxy" / "h2-egress",
        *(INFRA / "dev").glob("*.sh"),
    ]
)


def test_scripts_inventory_complete():
    # If someone adds a script, it must land in the shellcheck sweep.
    assert len(ALL_SCRIPTS) >= 16


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda p: str(p.relative_to(INFRA)))
def test_shellcheck(script):
    shellcheck = INFRA / ".venv" / "bin" / "shellcheck"
    r = subprocess.run(
        [str(shellcheck), "-x", str(script)],
        capture_output=True,
        text=True,
        cwd=str(script.parent),
    )
    assert r.returncode == 0, r.stdout + r.stderr


# ---- account-literal scan (CC directive A) + self-test --------------------------------

def test_check_no_account_ids_passes_on_tree():
    r = subprocess.run(
        ["bash", str(INFRA / "dev" / "check-no-account-ids.sh"), str(INFRA)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_no_account_ids_self_test():
    r = subprocess.run(
        ["bash", str(INFRA / "dev" / "check-no-account-ids.sh"), "--self-test"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "self-test passed" in r.stdout


def test_check_no_account_ids_catches_planted_literal(tmp_path):
    (tmp_path / "planted.py").write_text(f'ACCOUNT = "{"21" * 6}"\n')
    r = subprocess.run(
        ["bash", str(INFRA / "dev" / "check-no-account-ids.sh"), str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert "planted.py" in r.stdout
