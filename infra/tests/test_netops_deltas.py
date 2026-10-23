"""NetOps v2 §12 / section N delta assertions not already covered by the
Gherkin scenarios: budget default/override (CC directive B), IAM
enumeration (R1/R9), DNS option (a), proxy IMDS, fixed addressing, R8/K1
SourceIp pin, CRIS policy shapes, flow logs, EIC, and the gateway-IP
prefix-collision regex (see BAD_GATEWAY_PREFIX below)."""

import json
import re

import pytest
from aws_cdk.assertions import Match

from conftest import build, templates


# ---- CC directive B: budget default and override -----------------------------

def test_budget_default_3500_comes_from_cdk_json():
    # cdk.json holds the ONLY copy of the default.
    import pathlib
    cdk_json = json.loads(
        (pathlib.Path(__file__).parents[1] / "cdk.json").read_text()
    )
    assert cdk_json["context"]["budgetCapUsd"] == 3500
    t = templates(build())["H2Guardrails"]
    (b,) = t.find_resources("AWS::Budgets::Budget").values()
    assert b["Properties"]["Budget"]["BudgetLimit"]["Amount"] == 3500


def test_budget_override_500():
    t = templates(build(budgetCapUsd=500))["H2Guardrails"]
    (b,) = t.find_resources("AWS::Budgets::Budget").values()
    assert b["Properties"]["Budget"]["BudgetLimit"]["Amount"] == 500


def test_budget_not_filtered_to_bedrock_service(any_templates):
    (b,) = any_templates["H2Guardrails"].find_resources("AWS::Budgets::Budget").values()
    filters = json.dumps(b["Properties"]["Budget"].get("CostFilters", {}))
    assert "Bedrock" not in filters, "CC-B: no Service=Bedrock-only filter"


def test_budget_tag_filter_optional():
    (b_off,) = (
        templates(build(budgetCostTagFilter=False))["H2Guardrails"]
        .find_resources("AWS::Budgets::Budget")
        .values()
    )
    assert not b_off["Properties"]["Budget"].get("CostFilters")
    (b_on,) = (
        templates(build(budgetCostTagFilter=True))["H2Guardrails"]
        .find_resources("AWS::Budgets::Budget")
        .values()
    )
    tags = json.dumps(b_on["Properties"]["Budget"]["CostFilters"])
    assert "harness2-eval" in tags


# ---- N.10 / R1 / R9: exact IAM inventory, no Lambda/custom resources ----------

ROLE_MODE_ROLES = {
    "gateway",       # proxy instance role
    "budget",        # budget action exec role
    "flow",          # flow log delivery role
}

def _iam_inventory(tmpls):
    roles, users, lambdas, custom = [], [], [], []
    for name, t in tmpls.items():
        for rname, res in t.to_json()["Resources"].items():
            if res["Type"] == "AWS::IAM::Role":
                roles.append(f"{name}/{rname}")
            elif res["Type"] == "AWS::IAM::User":
                users.append(f"{name}/{rname}")
            elif res["Type"] == "AWS::Lambda::Function":
                lambdas.append(f"{name}/{rname}")
            elif res["Type"].startswith("Custom::") or res["Type"] == "AWS::CloudFormation::CustomResource":
                custom.append(f"{name}/{rname}")
    return roles, users, lambdas, custom


def test_iam_inventory_role_mode(role_templates):
    roles, users, lambdas, custom = _iam_inventory(role_templates)
    assert len(roles) == 3, f"role mode must have EXACTLY gateway+budget+flow roles, got {roles}"
    assert users == []
    assert lambdas == [], f"R9: no Lambda anywhere, got {lambdas}"
    assert custom == [], f"R9: no custom resources, got {custom}"


def test_iam_inventory_key_mode(key_templates):
    roles, users, lambdas, custom = _iam_inventory(key_templates)
    assert len(roles) == 2, f"key mode: budget+flow roles only, got {roles}"
    assert len(users) == 1, f"key mode: exactly the harness2-eval-bedrock user, got {users}"
    assert lambdas == [] and custom == []


def test_key_mode_user_has_no_access_key_resource(key_templates):
    # K2: the access key is created BY HAND, never in CFN.
    for t in key_templates.values():
        assert not t.to_json()["Resources"] or not [
            r for r in t.to_json()["Resources"].values()
            if r["Type"] == "AWS::IAM::AccessKey"
        ]


def test_proxy_has_instance_profile_role_mode_only(role_templates, key_templates):
    (inst_r,) = role_templates["H2Proxy"].find_resources("AWS::EC2::Instance").values()
    assert "IamInstanceProfile" in inst_r["Properties"]
    (inst_k,) = key_templates["H2Proxy"].find_resources("AWS::EC2::Instance").values()
    assert "IamInstanceProfile" not in inst_k["Properties"]


# ---- N.11 / C4: DNS option (a) -------------------------------------------------

def test_vpc_dns_support_disabled(any_templates):
    any_templates["H2Network"].has_resource_properties(
        "AWS::EC2::VPC",
        {"EnableDnsSupport": False, "EnableDnsHostnames": False},
    )


def test_dhcp_options_external_resolvers(any_templates):
    any_templates["H2Network"].has_resource_properties(
        "AWS::EC2::DHCPOptions",
        {"DomainNameServers": ["1.1.1.1", "9.9.9.9"]},
    )


def test_proxy_sg_dns_egress_only_to_two_resolvers(any_templates):
    net = any_templates["H2Network"].to_json()["Resources"]
    dns_cidrs = set()
    for res in net.values():
        if res["Type"] == "AWS::EC2::SecurityGroup" and "proxy" in res["Properties"].get(
            "GroupDescription", ""
        ):
            for rule in res["Properties"].get("SecurityGroupEgress", []):
                if rule.get("FromPort") == 53:
                    dns_cidrs.add(rule["CidrIp"])
    assert dns_cidrs == {"1.1.1.1/32", "9.9.9.9/32"}


# ---- N.8 / C3: proxy IMDSv2 hop 1; guest handled in gherkin tests --------------

def test_proxy_imdsv2_hop1(any_templates):
    any_templates["H2Proxy"].has_resource_properties(
        "AWS::EC2::Instance",
        {
            "MetadataOptions": Match.object_like(
                {"HttpTokens": "required", "HttpPutResponseHopLimit": 1}
            )
        },
    )


# ---- §4.1 fixed addressing + the gateway-IP-prefix regex ------------------------

def test_fixed_ips_and_cidrs(any_templates):
    any_templates["H2Network"].has_resource_properties(
        "AWS::EC2::VPC", {"CidrBlock": "10.99.0.0/24"}
    )
    any_templates["H2Proxy"].has_resource_properties(
        "AWS::EC2::Instance", {"PrivateIpAddress": "10.99.0.10"}
    )
    any_templates["H2Eval"].has_resource_properties(
        "AWS::EC2::Instance", {"PrivateIpAddress": "10.99.0.70"}
    )
    subnets = {
        res["Properties"]["CidrBlock"]
        for res in any_templates["H2Network"].find_resources("AWS::EC2::Subnet").values()
    }
    assert subnets == {"10.99.0.0/26", "10.99.0.64/26"}


# The proxy is .10: the gateway IP truncated to end in ".1" followed by a
# non-digit anywhere means someone typo'd the network address +1 for the
# proxy. The literal never appears in this file (the scan below covers
# tests/ too), hence the escaped construction.
BAD_GATEWAY_PREFIX = re.compile(r"10\.99\.0\.1(?![0-9])")


def test_no_bare_gateway_prefix_in_templates(any_templates):
    for name, t in any_templates.items():
        blob = json.dumps(t.to_json())
        m = BAD_GATEWAY_PREFIX.search(blob)
        assert not m, f"{name}: truncated proxy IP at ...{blob[max(0, m.start()-40):m.end()+40]}..."


def test_no_bare_gateway_prefix_in_scripts_and_source():
    import pathlib
    infra = pathlib.Path(__file__).parents[1]
    offenders = []
    for path in infra.rglob("*"):
        rel = path.relative_to(infra)
        parts = rel.parts
        if not path.is_file():
            continue
        if parts[0] in (".venv", "node_modules", ".git", "__pycache__") or parts[0].startswith("cdk.out"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if BAD_GATEWAY_PREFIX.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# ---- R8/K1: SourceIp pin ---------------------------------------------------------

def _bedrock_policy_statements(tmpl):
    stmts = []
    for res in tmpl.to_json()["Resources"].values():
        if res["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            stmts.extend(res["Properties"]["PolicyDocument"]["Statement"])
    return stmts


def test_source_ip_deny_present_both_modes(any_templates):
    denies = [
        s
        for s in _bedrock_policy_statements(any_templates["H2Proxy"])
        if s["Effect"] == "Deny" and "NotIpAddress" in s.get("Condition", {})
    ]
    assert len(denies) == 1
    assert denies[0]["Action"] == "bedrock:*"


def test_source_ip_deny_can_be_disabled_in_role_mode_only():
    t = templates(build(credentialMode="role", restrictBedrockToProxyIp=False))["H2Proxy"]
    assert not [
        s for s in _bedrock_policy_statements(t) if s["Effect"] == "Deny"
    ]
    tk = templates(build(credentialMode="key", restrictBedrockToProxyIp=False))["H2Proxy"]
    assert [
        s for s in _bedrock_policy_statements(tk) if s["Effect"] == "Deny"
    ], "K1: key mode must keep the pin regardless of the flag"


# ---- §6.4 CRIS policy shapes -----------------------------------------------------

def test_cris_statement_shapes(any_templates):
    stmts = {s["Sid"]: s for s in _bedrock_policy_statements(any_templates["H2Proxy"]) if "Sid" in s}
    # in-region FMs: RequestedRegion pinned.
    inr = stmts["InRegionFoundationModels"]
    assert inr["Condition"]["StringEquals"]["aws:RequestedRegion"] == "us-east-1"
    # us. profile: profile ARN allow w/o blanket RequestedRegion...
    us_prof = stmts["UsCris0Profile"]
    assert "Condition" not in us_prof or "aws:RequestedRegion" not in us_prof.get(
        "Condition", {}
    ).get("StringEquals", {})
    # ...and per-destination FM ARNs gated by bedrock:InferenceProfileArn.
    us_dest = stmts["UsCris0DestinationFms"]
    assert "bedrock:InferenceProfileArn" in us_dest["Condition"]["StringEquals"]
    assert len(us_dest["Resource"]) == 3  # us-east-1/us-east-2/us-west-2 from fixture
    # global. adds the region-less FM ARN with RequestedRegion=unspecified.
    regionless = [s for sid, s in stmts.items() if sid.endswith("RegionlessFm")]
    assert len(regionless) == 2  # sonnet + haiku global profiles in fixture
    for s in regionless:
        assert s["Condition"]["StringEquals"]["aws:RequestedRegion"] == "unspecified"
        assert ":::foundation-model/" in s["Resource"]


def test_gateway_allows_only_invoke_actions(any_templates):
    for s in _bedrock_policy_statements(any_templates["H2Proxy"]):
        if s["Effect"] == "Allow":
            actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            assert set(actions) <= {
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            }, f"gateway Allow must be invoke-only, got {actions}"


# ---- N.9 / flow logs, EIC, EIP ----------------------------------------------------

def test_flow_logs_all_traffic_kms_encrypted(any_templates):
    t = any_templates["H2Network"]
    t.has_resource_properties("AWS::EC2::FlowLog", {"TrafficType": "ALL"})
    (lg,) = t.find_resources("AWS::Logs::LogGroup").values()
    assert "KmsKeyId" in lg["Properties"]
    assert lg["Properties"]["RetentionInDays"] == 30


def test_eic_endpoint_in_private_subnet_no_preserve_ip(any_templates):
    (eic,) = any_templates["H2Network"].find_resources(
        "AWS::EC2::InstanceConnectEndpoint"
    ).values()
    assert eic["Properties"]["PreserveClientIp"] is False


def test_proxy_eip_exists(any_templates):
    assert len(any_templates["H2Proxy"].find_resources("AWS::EC2::EIP")) == 1


def test_r9a_recommended_eic_policy_is_output_only(any_templates):
    # No EIC IAM policy resource; just the JSON recommendation output.
    outputs = any_templates["H2Eval"].to_json()["Outputs"]
    assert "RecommendedOperatorEicPolicy" in outputs
    value = outputs["RecommendedOperatorEicPolicy"]["Value"]
    if isinstance(value, dict):  # Fn::Sub-wrapped for region/account tokens
        value = value["Fn::Sub"]
    policy = json.loads(value)
    assert policy["Statement"], "output must carry a valid policy document"
    assert policy["Statement"][0]["Action"] == "ec2-instance-connect:OpenTunnel"
