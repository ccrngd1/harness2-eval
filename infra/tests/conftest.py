"""Shared fixtures: build the app once per credential mode from
tests/fixtures/context.json (the only sanctioned home of the placeholder
account) and expose assertions.Template objects per stack."""

import json
import pathlib

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

import sys

INFRA_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INFRA_DIR))

from app import build_app  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture_context(**overrides) -> dict:
    ctx = json.loads((FIXTURES / "context.json").read_text())
    ctx.pop("_comment", None)
    ctx.update(overrides)
    return ctx


def build(**overrides) -> cdk.App:
    return build_app(cdk.App(context=fixture_context(**overrides)))


def templates(app: cdk.App) -> dict[str, Template]:
    return {
        name: Template.from_stack(app.node.find_child(name))
        for name in ("H2Network", "H2Proxy", "H2Eval", "H2Guardrails")
    }


@pytest.fixture(scope="session")
def role_app() -> cdk.App:
    return build(credentialMode="role")


@pytest.fixture(scope="session")
def key_app() -> cdk.App:
    return build(credentialMode="key")


@pytest.fixture(scope="session")
def role_templates(role_app) -> dict[str, Template]:
    return templates(role_app)


@pytest.fixture(scope="session")
def key_templates(key_app) -> dict[str, Template]:
    return templates(key_app)


@pytest.fixture(scope="session", params=["role", "key"])
def any_templates(request, role_templates, key_templates) -> dict[str, Template]:
    return role_templates if request.param == "role" else key_templates
