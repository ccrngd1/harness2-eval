"""cdk-nag acknowledgments, each with a written justification.

cdk-nag 3.x reads acknowledgments via the aws-cdk-lib core Validations
API (Acknowledgment entries in construct metadata). The app enables
write_suppressions_to_cloud_formation so every acknowledged rule also
lands in the synthesized template metadata, where tests assert that each
entry carries a non-trivial reason.

NetOps R2 forbids suppressing IAM5 on the gateway role's Allow
statements, and nothing here does: those are resource-scoped. Entries
below are the complete list.
"""

from aws_cdk import Acknowledgment, Validations

from .config import AppConfig


def _ack(scope, rule_id: str, reason: str):
    Validations.of(scope).acknowledge(Acknowledgment(id=rule_id, reason=reason))


def apply_suppressions(network, proxy, eval_stack, guardrails, config: AppConfig):
    _ack(
        network,
        "AwsSolutions-VPC7",
        "False positive: the VPC has Flow Logs (traffic type ALL to a "
        "KMS-encrypted CloudWatch log group in this same stack); the rule "
        "does not see the FlowLog L2 attached via "
        "FlowLogResourceType.from_vpc.",
    )

    for stack in (proxy, eval_stack):
        _ack(
            stack,
            "AwsSolutions-EC28",
            "Detailed monitoring adds cost on a <2-week disposable sandbox; "
            "the isolation evidence NetOps asked for is VPC Flow Logs plus "
            "the squid and LiteLLM logs, not CloudWatch instance metrics.",
        )
        _ack(
            stack,
            "AwsSolutions-EC29",
            "Termination protection is OFF by design: teardown.sh must be "
            "able to destroy the sandbox unattended, and nothing durable "
            "lives on the instances (snapshots carry state).",
        )

    _ack(
        guardrails,
        "AwsSolutions-IAM5",
        "The kill-switch managed policy is Deny bedrock:* on Resource:* by "
        "design: at 100% budget it must deny every Bedrock action for the "
        "gateway credential. A Deny with wildcards grants nothing.",
    )
    if config.restrict_source_ip:
        _ack(
            proxy,
            "AwsSolutions-IAM5",
            "R8/K1 SourceIp pin: an explicit Deny bedrock:* on Resource:* "
            "unless aws:SourceIp is the proxy EIP. A Deny with wildcards "
            "narrows access; the Allow statements stay resource-scoped per "
            "NetOps §6.4 and IAM5 is NOT acknowledged for any Allow.",
        )
