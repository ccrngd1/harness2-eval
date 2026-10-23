"""Context resolution for the harness2-eval CDK app.

Account/region resolution order (CC directive A, 2026-10-22 17:05 ET):
  1. explicit ``-c account=`` / ``-c region=``
  2. ambient credentials via CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION
  3. region only: default us-east-1

If an explicit account is given AND ambient credentials resolve to a
different account, we abort: deploying "to the account I typed" with
credentials for another account is always a mistake. No account literal
is ever hardcoded; local synth/tests pass the fixture account explicitly.
"""

import os
import re
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised for any unusable context; the app exits non-zero with the message."""


_ACCOUNT_RE = re.compile(r"^[0-9]{12}$")


def resolve_account(explicit: str | None, env_account: str | None) -> str:
    if explicit is not None:
        explicit = str(explicit).strip()
        if not _ACCOUNT_RE.match(explicit):
            raise ConfigError(
                f"-c account={explicit!r} is not a 12-digit AWS account ID."
            )
        if env_account and env_account != explicit:
            raise ConfigError(
                "Explicit -c account="
                + explicit
                + " does not match the account of the ambient credentials "
                "(CDK_DEFAULT_ACCOUNT=" + env_account + "). Aborting: you are "
                "about to deploy to a different account than your credentials "
                "belong to. Fix the credentials or drop the -c account flag."
            )
        return explicit
    if env_account:
        return env_account
    raise ConfigError(
        "No AWS account resolvable. Pass -c account=<12-digit-id> or run with "
        "credentials so CDK_DEFAULT_ACCOUNT is set. (Local synth/tests: use "
        "the placeholder in tests/fixtures/, e.g. "
        "-c account=$(cat tests/fixtures/placeholder-account.txt).)"
    )


def resolve_region(explicit: str | None, env_region: str | None) -> str:
    if explicit:
        return str(explicit).strip()
    if env_region:
        return env_region
    return "us-east-1"


@dataclass
class ModelConfig:
    """Bedrock model IDs and CRIS profile shapes, all from context (§6.4)."""

    # Plain in-region foundation models (no CRIS): condition RequestedRegion=<region>
    in_region_model_ids: list[str] = field(default_factory=list)
    # us.* inference profiles: {profile_id: [destination regions]}
    us_profiles: dict[str, list[str]] = field(default_factory=dict)
    # global.* inference profiles: {profile_id: [destination regions]}
    global_profiles: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def _fm_id(profile_id: str) -> str:
        # Strip the us./global. routing prefix to get the foundation-model id.
        return profile_id.split(".", 1)[1]

    def all_ids(self) -> list[str]:
        return (
            list(self.in_region_model_ids)
            + list(self.us_profiles)
            + list(self.global_profiles)
        )


@dataclass
class AppConfig:
    account: str
    region: str
    credential_mode: str  # "role" | "key"
    alert_email: str
    operator_ssh_public_key: str
    budget_cap_usd: float
    models: ModelConfig
    # R8: Deny-unless-SourceIp=EIP on the Bedrock policy. SHOULD in role
    # mode (context flag, default on); MUST in key mode (always on there).
    restrict_source_ip: bool = True
    # Optional cost-allocation-tag budget filter for main-account use (§6.5).
    budget_cost_tag_filter: bool = False


def _require(node_try_get, key: str) -> object:
    val = node_try_get(key)
    if val is None:
        raise ConfigError(
            f"Required context key {key!r} is missing. Set it in cdk.json, "
            f"cdk.context.json, or with -c {key}=<value> "
            "(see cdk.context.example.json)."
        )
    return val


def _as_bool(val: object, key: str) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"Context key {key!r} must be a boolean, got {val!r}.")


def load_config(node_try_get, environ: dict | None = None) -> AppConfig:
    """Build AppConfig from a node.try_get_context callable and the process env."""
    env = os.environ if environ is None else environ

    account = resolve_account(
        node_try_get("account"), env.get("CDK_DEFAULT_ACCOUNT")
    )
    region = resolve_region(
        node_try_get("region"),
        env.get("CDK_DEFAULT_REGION") or env.get("AWS_REGION"),
    )

    credential_mode = str(_require(node_try_get, "credentialMode")).strip()
    if credential_mode not in ("role", "key"):
        raise ConfigError(
            f"credentialMode must be 'role' or 'key', got {credential_mode!r}."
        )

    raw_cap = _require(node_try_get, "budgetCapUsd")
    try:
        budget_cap = float(raw_cap)
    except (TypeError, ValueError):
        raise ConfigError(f"budgetCapUsd must be a number, got {raw_cap!r}.") from None
    if budget_cap <= 0:
        raise ConfigError(f"budgetCapUsd must be positive, got {budget_cap}.")

    alert_email = str(_require(node_try_get, "alertEmail")).strip()
    if "@" not in alert_email:
        raise ConfigError(f"alertEmail {alert_email!r} does not look like an email.")

    ssh_key = str(_require(node_try_get, "operatorSshPublicKey")).strip()
    if not ssh_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise ConfigError(
            "operatorSshPublicKey must be an SSH *public* key line "
            "(ssh-ed25519/ssh-rsa/ecdsa-sha2-...). Never put a private key here."
        )
    if "PRIVATE KEY" in ssh_key:
        raise ConfigError("operatorSshPublicKey contains a private key. Refusing.")

    models_ctx = _require(node_try_get, "models")
    if isinstance(models_ctx, str):
        # -c models='{"inRegion":[...]}' passes a JSON string.
        import json

        try:
            models_ctx = json.loads(models_ctx)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Context 'models' is not valid JSON: {exc}") from None
    if not isinstance(models_ctx, dict):
        raise ConfigError("Context key 'models' must be an object; see example.")
    models = ModelConfig(
        in_region_model_ids=list(models_ctx.get("inRegion", [])),
        us_profiles=dict(models_ctx.get("usProfiles", {})),
        global_profiles=dict(models_ctx.get("globalProfiles", {})),
    )
    if not models.all_ids():
        raise ConfigError("Context 'models' lists no model IDs at all.")
    for pid, regions in list(models.us_profiles.items()) + list(
        models.global_profiles.items()
    ):
        if not regions:
            raise ConfigError(
                f"Inference profile {pid!r} has an empty destination-region "
                "list; CRIS policies need every destination region (§6.4)."
            )

    restrict_ip_raw = node_try_get("restrictBedrockToProxyIp")
    restrict_source_ip = (
        True if restrict_ip_raw is None else _as_bool(restrict_ip_raw, "restrictBedrockToProxyIp")
    )
    if credential_mode == "key":
        # K1: the SourceIp pin is a MUST in key mode, not operator-selectable.
        restrict_source_ip = True

    tag_filter_raw = node_try_get("budgetCostTagFilter")
    budget_cost_tag_filter = (
        False if tag_filter_raw is None else _as_bool(tag_filter_raw, "budgetCostTagFilter")
    )

    return AppConfig(
        account=account,
        region=region,
        credential_mode=credential_mode,
        alert_email=alert_email,
        operator_ssh_public_key=ssh_key,
        budget_cap_usd=budget_cap,
        models=models,
        restrict_source_ip=restrict_source_ip,
        budget_cost_tag_filter=budget_cost_tag_filter,
    )
