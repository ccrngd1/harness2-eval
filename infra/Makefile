# harness2-eval infra — local verification only. NOTHING here deploys.
# Every target that touches cdk/pytest/scripts runs through
# dev/no-aws-env.sh (env -i: host AWS credentials can never leak in).
#
#   make venv     one-time: python venv + node deps
#   make synth    cdk synth, both credential modes, fixture context
#   make nag      synth with cdk-nag validation enforced (same as synth)
#   make test     full pytest suite (CDK assertions + script harness)
#   make lint     shellcheck everything + account-literal scan (+ self-test)
#   make dry-run  operator scripts under poisoned/stubbed binaries
#   make verify   all of the above
#
# Deploying is a deliberate, separate act: scripts/setup.sh from an
# operator workstation WITH credentials. Never from this Makefile.

SHELL := /bin/bash
NOAWS := ./dev/no-aws-env.sh
VENV  := .venv/bin

.PHONY: venv synth nag test lint dry-run verify clean

venv:
	python3 -m venv .venv
	$(VENV)/pip install -r requirements.txt -r requirements-dev.txt
	npm ci --no-audit --no-fund

synth:
	$(NOAWS) ./dev/synth-local.sh role
	$(NOAWS) ./dev/synth-local.sh key --output cdk.out.key

# cdk-nag runs as a validation plugin on every synth; this alias exists so
# "make nag" is an explicit checklist item. Unsuppressed findings fail synth.
nag: synth

test:
	$(NOAWS) $(VENV)/python -m pytest tests/ -q

lint:
	$(NOAWS) bash dev/check-no-account-ids.sh --self-test
	$(NOAWS) bash dev/check-no-account-ids.sh
	cd scripts && $(abspath $(VENV))/shellcheck -x setup.sh teardown.sh run-arm.sh restore-snapshot.sh lib/common.sh
	$(VENV)/shellcheck -x scripts/guest/*.sh scripts/guest/h2-jail scripts/proxy/*.sh scripts/proxy/h2-egress dev/*.sh

# Script dry-runs under stubs happen inside pytest (tests/test_scripts.py:
# the aws/cdk/ssh stubs FAIL if setup.sh --dry-run invokes them at all).
dry-run:
	$(NOAWS) $(VENV)/python -m pytest tests/test_scripts.py -q

verify: lint synth test
	@echo "verify complete: lint + synth(+nag) + pytest all green"

clean:
	rm -rf cdk.out cdk.out.key .pytest_cache tests/__pycache__ stacks/__pycache__
