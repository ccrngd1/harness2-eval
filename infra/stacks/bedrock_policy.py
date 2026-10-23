"""Bedrock invoke-only policy statements per NetOps §6.4 (condition R2/R3).

Shapes, verbatim from the spec:

* In-region FMs (e.g. zai.glm-5, openai.gpt-oss-120b-1:0):
  resource arn:aws:bedrock:<region>::foundation-model/<id>,
  condition aws:RequestedRegion = <region>.
* us.* profiles: the profile ARN in <region> PLUS the FM ARN in every
  destination region, conditioned on bedrock:InferenceProfileArn = the
  profile ARN. No blanket RequestedRegion on these: it breaks routing.
* global.* profiles: profile ARN + regional FM ARNs (as us.*) PLUS the
  region-less FM ARN arn:aws:bedrock:::foundation-model/<id> with
  aws:RequestedRegion = "unspecified" (per the AWS global-CRIS docs).

The account is always the caller-resolved one (never a literal).
"""

from aws_cdk import aws_iam as iam

from .config import ModelConfig

INVOKE_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
]


def _fm_id(profile_id: str) -> str:
    return profile_id.split(".", 1)[1]


def build_invoke_statements(
    models: ModelConfig, account: str, region: str
) -> list[iam.PolicyStatement]:
    statements: list[iam.PolicyStatement] = []

    if models.in_region_model_ids:
        statements.append(
            iam.PolicyStatement(
                sid="InRegionFoundationModels",
                actions=INVOKE_ACTIONS,
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/{mid}"
                    for mid in models.in_region_model_ids
                ],
                conditions={"StringEquals": {"aws:RequestedRegion": region}},
            )
        )

    def profile_statements(profile_id: str, dest_regions: list[str], sid: str, is_global: bool):
        profile_arn = (
            f"arn:aws:bedrock:{region}:{account}:inference-profile/{profile_id}"
        )
        fm = _fm_id(profile_id)
        # The profile itself is invoked in-region; no RequestedRegion pin
        # (the service fans out to destination regions).
        statements.append(
            iam.PolicyStatement(
                sid=f"{sid}Profile",
                actions=INVOKE_ACTIONS,
                resources=[profile_arn],
            )
        )
        fm_resources = [
            f"arn:aws:bedrock:{dest}::foundation-model/{fm}" for dest in dest_regions
        ]
        statements.append(
            iam.PolicyStatement(
                sid=f"{sid}DestinationFms",
                actions=INVOKE_ACTIONS,
                resources=fm_resources,
                conditions={
                    "StringEquals": {"bedrock:InferenceProfileArn": profile_arn}
                },
            )
        )
        if is_global:
            statements.append(
                iam.PolicyStatement(
                    sid=f"{sid}RegionlessFm",
                    actions=INVOKE_ACTIONS,
                    resources=[f"arn:aws:bedrock:::foundation-model/{fm}"],
                    conditions={
                        "StringEquals": {
                            "bedrock:InferenceProfileArn": profile_arn,
                            "aws:RequestedRegion": "unspecified",
                        }
                    },
                )
            )

    for i, (pid, dests) in enumerate(sorted(models.us_profiles.items())):
        profile_statements(pid, dests, f"UsCris{i}", is_global=False)
    for i, (pid, dests) in enumerate(sorted(models.global_profiles.items())):
        profile_statements(pid, dests, f"GlobalCris{i}", is_global=True)

    return statements


def build_source_ip_deny(proxy_eip: str) -> iam.PolicyStatement:
    """R8/K1: stolen credentials are useless off the proxy's Elastic IP."""
    return iam.PolicyStatement(
        sid="DenyUnlessProxyEip",
        effect=iam.Effect.DENY,
        actions=["bedrock:*"],
        resources=["*"],
        conditions={"NotIpAddress": {"aws:SourceIp": [f"{proxy_eip}/32"]}},
    )
