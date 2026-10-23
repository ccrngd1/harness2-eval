#!/usr/bin/env python3
"""harness2-eval infra CDK app.

Four stacks: H2Network, H2Proxy, H2Eval, H2Guardrails. Synth needs no
credentials and performs no context-provider lookups; account and region
come from context or CDK_DEFAULT_* (stacks/config.py). cdk-nag
AwsSolutionsChecks runs on every synth; suppressions live in nag_suppressions.py
with written justifications.
"""

import sys

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from stacks import constants as c
from stacks.config import ConfigError, load_config
from stacks.eval_stack import H2EvalStack
from stacks.guardrails_stack import H2GuardrailsStack
from stacks.nag_suppressions import apply_suppressions
from stacks.network_stack import H2NetworkStack
from stacks.proxy_stack import H2ProxyStack


def build_app(app: cdk.App | None = None) -> cdk.App:
    app = app or cdk.App()
    try:
        config = load_config(app.node.try_get_context)
    except ConfigError as exc:
        print(f"harness2-eval config error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    env = cdk.Environment(account=config.account, region=config.region)

    network = H2NetworkStack(app, "H2Network", config=config, env=env)
    proxy = H2ProxyStack(app, "H2Proxy", config=config, network=network, env=env)
    eval_stack = H2EvalStack(app, "H2Eval", config=config, network=network, env=env)
    guardrails = H2GuardrailsStack(
        app, "H2Guardrails", config=config, proxy=proxy, env=env
    )

    cdk.Tags.of(app).add(c.PROJECT_TAG_KEY, c.PROJECT_TAG_VALUE)
    # cdk-nag 3.x: a validation plugin, not an Aspect. Suppressions land in
    # template metadata so tests (and NetOps) can audit every reason.
    cdk.Validations.of(app).add_plugins(
        AwsSolutionsChecks(app, verbose=True, write_suppressions_to_cloud_formation=True)
    )
    apply_suppressions(network, proxy, eval_stack, guardrails, config)
    return app


if __name__ == "__main__":
    build_app().synth()
