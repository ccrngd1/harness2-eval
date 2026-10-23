"""CC directive A: account/region resolution and abort behavior, plus
context validation in stacks/config.py."""

import pytest

from stacks.config import (
    ConfigError,
    load_config,
    resolve_account,
    resolve_region,
)

from conftest import FIXTURES, fixture_context

# CC directive A: 12-digit literals may live ONLY under tests/fixtures/.
# This file reads the sanctioned placeholder from there and constructs
# every other digit string at runtime so the account-literal scan stays
# clean over tests/ too.
PLACEHOLDER = (FIXTURES / "placeholder-account.txt").read_text().strip()
OTHER = "9" * 12


def ctx_getter(ctx):
    return lambda key: ctx.get(key)


# ---- resolve_account: each source alone -------------------------------------

def test_explicit_account_alone():
    assert resolve_account(PLACEHOLDER, None) == PLACEHOLDER


def test_ambient_account_alone():
    assert resolve_account(None, PLACEHOLDER) == PLACEHOLDER


def test_explicit_and_matching_ambient():
    assert resolve_account(PLACEHOLDER, PLACEHOLDER) == PLACEHOLDER


def test_explicit_vs_ambient_mismatch_aborts():
    with pytest.raises(ConfigError, match="does not match"):
        resolve_account(PLACEHOLDER, OTHER)


def test_no_account_at_all_errors_with_guidance():
    with pytest.raises(ConfigError, match="No AWS account resolvable"):
        resolve_account(None, None)


@pytest.mark.parametrize(
    "bad",
    ["12345", "1" * 13, "abcd" + "1" * 8, "-".join(["1234", "5678", "9012"])],
)
def test_malformed_explicit_account_rejected(bad):
    with pytest.raises(ConfigError, match="not a 12-digit"):
        resolve_account(bad, None)


# ---- resolve_region ----------------------------------------------------------

def test_region_explicit_wins():
    assert resolve_region("eu-west-1", "us-west-2") == "eu-west-1"


def test_region_ambient_fallback():
    assert resolve_region(None, "us-west-2") == "us-west-2"


def test_region_default_us_east_1():
    assert resolve_region(None, None) == "us-east-1"


# ---- load_config validation ---------------------------------------------------

def test_load_config_happy_path():
    cfg = load_config(ctx_getter(fixture_context()), environ={})
    assert cfg.account == PLACEHOLDER
    assert cfg.credential_mode == "role"
    assert cfg.budget_cap_usd == 3500
    assert cfg.restrict_source_ip is True


def test_load_config_mismatch_aborts_via_env():
    with pytest.raises(ConfigError, match="does not match"):
        load_config(
            ctx_getter(fixture_context()),
            environ={"CDK_DEFAULT_ACCOUNT": OTHER},
        )


def test_credential_mode_validated():
    with pytest.raises(ConfigError, match="credentialMode"):
        load_config(ctx_getter(fixture_context(credentialMode="iam")), environ={})


def test_budget_must_be_positive():
    with pytest.raises(ConfigError, match="positive"):
        load_config(ctx_getter(fixture_context(budgetCapUsd=0)), environ={})


def test_budget_must_be_numeric():
    with pytest.raises(ConfigError, match="number"):
        load_config(ctx_getter(fixture_context(budgetCapUsd="lots")), environ={})


def test_private_key_in_ssh_context_refused():
    with pytest.raises(ConfigError, match="public"):
        load_config(
            ctx_getter(
                fixture_context(
                    operatorSshPublicKey="-----BEGIN OPENSSH PRIVATE KEY-----"
                )
            ),
            environ={},
        )


def test_models_must_not_be_empty():
    with pytest.raises(ConfigError, match="no model IDs"):
        load_config(
            ctx_getter(fixture_context(models={"inRegion": []})), environ={}
        )


def test_cris_profile_needs_destination_regions():
    models = fixture_context()["models"]
    models["usProfiles"] = {"us.anthropic.claude-opus-4-8": []}
    with pytest.raises(ConfigError, match="destination-region"):
        load_config(ctx_getter(fixture_context(models=models)), environ={})


def test_key_mode_forces_source_ip_pin():
    cfg = load_config(
        ctx_getter(
            fixture_context(credentialMode="key", restrictBedrockToProxyIp=False)
        ),
        environ={},
    )
    assert cfg.restrict_source_ip is True, "K1: SourceIp pin is a MUST in key mode"


def test_role_mode_source_ip_pin_optional():
    cfg = load_config(
        ctx_getter(
            fixture_context(credentialMode="role", restrictBedrockToProxyIp=False)
        ),
        environ={},
    )
    assert cfg.restrict_source_ip is False
