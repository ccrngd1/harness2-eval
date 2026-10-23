"""One test per Gherkin line from the requirements doc §"Acceptance".

Scenario: Synthesizes cleanly
Scenario: The guest is isolated by construction
Scenario: Cost controls exist
(Script scenarios live in test_scripts.py.)
"""

import json
import re

import pytest
from aws_cdk.assertions import Capture, Match, Template

from conftest import build, templates

SECRET_PATTERNS = re.compile(
    r"(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|aws_secret_access_key|sk-[A-Za-z0-9]{20,}"
    r"|BEGIN [A-Z ]*PRIVATE KEY|AWS_BEARER_TOKEN)",
    re.IGNORECASE,
)


# ---- Scenario: Synthesizes cleanly ------------------------------------------

def test_all_four_stacks_synthesize(any_templates):
    assert set(any_templates) == {"H2Network", "H2Proxy", "H2Eval", "H2Guardrails"}
    for t in any_templates.values():
        assert t.to_json()["Resources"]


def test_nag_no_unsuppressed_findings():
    # AwsSolutionsChecks is wired as a validation plugin; synth() raises
    # on any un-acknowledged error-level finding. Both modes must pass.
    for mode in ("role", "key"):
        build(credentialMode=mode).synth(validate_on_synthesis=True)


def test_every_suppression_has_nontrivial_reason(any_templates):
    # aws-cdk-lib's FlowLog L2 injects its own CloudFormation-Validate::F3002/
    # F3003 acknowledgments (camelCase DestinationOptions quirk); those are
    # framework-internal, not ours. We audit every AwsSolutions-* entry and
    # assert nothing else unexpected appears.
    FRAMEWORK_INTERNAL = {"CloudFormation-Validate::F3002", "CloudFormation-Validate::F3003"}
    found_any = False
    for t in any_templates.values():
        for res in t.to_json()["Resources"].values():
            for sup in (
                res.get("Metadata", {}).get("cdk_nag", {}).get("rules_to_suppress", [])
            ):
                if sup["id"] in FRAMEWORK_INTERNAL:
                    continue
                found_any = True
                assert sup["id"].startswith("AwsSolutions-"), f"unexpected rule id {sup['id']}"
                reason = sup.get("reason", "")
                assert len(reason) >= 40, f"trivial reason for {sup['id']}: {reason!r}"
                assert reason.lower() not in ("n/a", "todo", "suppressed")
    assert found_any, "expected acknowledged findings written to template metadata"


# ---- Scenario: The guest is isolated by construction -------------------------

def test_private_subnet_has_no_default_route(any_templates):
    net = any_templates["H2Network"].to_json()["Resources"]
    # Find the private subnet's route table, then assert no 0.0.0.0/0
    # route is associated with it.
    private_rts = {
        name
        for name, res in net.items()
        if res["Type"] == "AWS::EC2::RouteTable" and "private" in name.lower()
    }
    assert private_rts, "private route table not found"
    for name, res in net.items():
        if res["Type"] != "AWS::EC2::Route":
            continue
        rt_ref = res["Properties"]["RouteTableId"].get("Ref")
        assert rt_ref not in private_rts, (
            f"route {name} attached to private route table {rt_ref}"
        )
        if res["Properties"].get("DestinationCidrBlock") == "0.0.0.0/0":
            assert rt_ref not in private_rts


def test_eval_instance_has_no_instance_profile(any_templates):
    for res in any_templates["H2Eval"].find_resources("AWS::EC2::Instance").values():
        assert "IamInstanceProfile" not in res["Properties"]


def test_eval_instance_imdsv2_hop1_encrypted_root(any_templates):
    any_templates["H2Eval"].has_resource_properties(
        "AWS::EC2::Instance",
        {
            "MetadataOptions": Match.object_like(
                {"HttpTokens": "required", "HttpPutResponseHopLimit": 1}
            ),
            "BlockDeviceMappings": Match.array_with(
                [
                    Match.object_like(
                        {"Ebs": Match.object_like({"Encrypted": True, "VolumeSize": 80})}
                    )
                ]
            ),
        },
    )


def test_eval_sg_egress_only_proxy_3128_4000(any_templates):
    net = any_templates["H2Network"].to_json()["Resources"]
    egress_ports = []
    for name, res in net.items():
        if res["Type"] == "AWS::EC2::SecurityGroupEgress" and name.startswith("EvalSgto"):
            p = res["Properties"]
            egress_ports.append((p["FromPort"], p["ToPort"]))
            assert "DestinationSecurityGroupId" in p, "eval egress must target the proxy SG, not a CIDR"
    assert sorted(egress_ports) == [(3128, 3128), (4000, 4000)]
    # The EvalSg resource itself must carry only the dummy 255.255.255.255/32
    # disable-rule CDK uses for allow_all_outbound=False.
    for res in net.values():
        if res["Type"] == "AWS::EC2::SecurityGroup" and "guest" in res["Properties"].get(
            "GroupDescription", ""
        ):
            for rule in res["Properties"].get("SecurityGroupEgress", []):
                assert rule.get("CidrIp") == "255.255.255.255/32"


def test_no_sg_ingress_from_world(any_templates):
    for t in any_templates.values():
        for res in t.to_json()["Resources"].values():
            if res["Type"] == "AWS::EC2::SecurityGroup":
                for rule in res["Properties"].get("SecurityGroupIngress", []):
                    assert rule.get("CidrIp") != "0.0.0.0/0"
                    assert rule.get("CidrIpv6") != "::/0"
            if res["Type"] == "AWS::EC2::SecurityGroupIngress":
                assert res["Properties"].get("CidrIp") != "0.0.0.0/0"
                assert res["Properties"].get("CidrIpv6") != "::/0"


def test_no_secret_in_parameters_outputs_or_userdata(any_templates):
    for name, t in any_templates.items():
        tj = t.to_json()
        for section in ("Parameters", "Outputs"):
            blob = json.dumps(tj.get(section, {}))
            assert not SECRET_PATTERNS.search(blob), f"{name} {section} matches a secret pattern"
        for res in tj["Resources"].values():
            if res["Type"] == "AWS::EC2::Instance":
                ud = json.dumps(res["Properties"].get("UserData", {}))
                assert not SECRET_PATTERNS.search(ud), f"{name} user-data matches a secret pattern"


# ---- Scenario: Cost controls exist -------------------------------------------

def test_budget_has_50_80_100_actual_and_100_forecast(any_templates):
    budgets = any_templates["H2Guardrails"].find_resources("AWS::Budgets::Budget")
    assert len(budgets) == 1
    (budget,) = budgets.values()
    notifs = budget["Properties"]["NotificationsWithSubscribers"]
    got = {
        (n["Notification"]["NotificationType"], n["Notification"]["Threshold"])
        for n in notifs
    }
    assert got == {("ACTUAL", 50), ("ACTUAL", 80), ("ACTUAL", 100), ("FORECASTED", 100)}


def test_budget_action_attaches_deny_policy_at_100(any_templates):
    t = any_templates["H2Guardrails"]
    actions = t.find_resources("AWS::Budgets::BudgetsAction")
    assert len(actions) == 1
    (action,) = actions.values()
    p = action["Properties"]
    assert p["ActionType"] == "APPLY_IAM_POLICY"
    assert p["ApprovalModel"] == "AUTOMATIC"
    assert p["ActionThreshold"]["Value"] == 100
    assert "PolicyArn" in p["Definition"]["IamActionDefinition"]


def test_budget_action_targets_role_or_user():
    role_t = templates(build(credentialMode="role"))["H2Guardrails"]
    (ra,) = role_t.find_resources("AWS::Budgets::BudgetsAction").values()
    assert "Roles" in ra["Properties"]["Definition"]["IamActionDefinition"]
    key_t = templates(build(credentialMode="key"))["H2Guardrails"]
    (ka,) = key_t.find_resources("AWS::Budgets::BudgetsAction").values()
    assert "Users" in ka["Properties"]["Definition"]["IamActionDefinition"]
