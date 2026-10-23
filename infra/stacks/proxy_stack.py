"""H2Proxy: the proxy/gateway instance plus the Bedrock credential.

credentialMode=role (default): an instance role on this box only, holding
the §6.4 invoke-only policy. No long-lived secret exists anywhere.

credentialMode=key: the IAM user harness2-eval-bedrock with the same
policy. The CDK creates the user and policy ONLY; CC creates the access
key by hand and setup.sh copies it over EIC into proxy tmpfs
(/run/h2gw/aws.env). It never touches CloudFormation.

Both instances in this app are raw CfnInstance: the L2 ec2.Instance
always manufactures a role+profile, which would violate R1 ("exactly one
InstanceProfile, proxy only") and R9 (minimal IAM inventory).

User-data here is deliberately tiny and secret-free: operator public key,
swapfile, hostname. Everything else (squid, nftables, LiteLLM, h2-egress)
is installed by scripts/proxy/proxy-bootstrap.sh, which setup.sh copies
over EIC and runs as root — so the whole proxy config is reviewable in
this repo rather than buried in user-data.
"""

from aws_cdk import (
    CfnOutput,
    Fn,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
)
from constructs import Construct

from . import constants as c
from .bedrock_policy import build_invoke_statements, build_source_ip_deny
from .config import AppConfig
from .network_stack import H2NetworkStack


def _operator_key_user_data(config: AppConfig, extra: str = "") -> str:
    # C7: EIC SendSSHPublicKey needs IMDS, so a static operator public key
    # is installed instead. A public key is not a secret.
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh",
            f"echo '{config.operator_ssh_public_key}' > /home/ubuntu/.ssh/authorized_keys",
            "chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys",
            "chmod 0600 /home/ubuntu/.ssh/authorized_keys",
            extra,
            "",
        ]
    )


class H2ProxyStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        network: H2NetworkStack,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        ami = ec2.MachineImage.from_ssm_parameter(
            c.UBUNTU_2404_ARM64_SSM, os=ec2.OperatingSystemType.LINUX
        ).get_image(self)

        # EIP first: the R8/K1 SourceIp pin references it.
        self.eip = ec2.CfnEIP(self, "ProxyEip", domain="vpc")

        statements = build_invoke_statements(
            config.models, config.account, config.region
        )
        if config.restrict_source_ip:
            statements.append(build_source_ip_deny(self.eip.attr_public_ip))

        self.gateway_role = None
        self.gateway_user = None
        instance_profile_ref = None

        if config.credential_mode == "role":
            self.gateway_role = iam.Role(
                self,
                "GatewayRole",
                assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                description=(
                    "harness2-eval gateway: bedrock invoke-only on listed "
                    "models, nothing else (NetOps R2)"
                ),
            )
            for st in statements:
                self.gateway_role.add_to_policy(st)
            profile = iam.CfnInstanceProfile(
                self, "GatewayInstanceProfile", roles=[self.gateway_role.role_name]
            )
            instance_profile_ref = profile.ref
        else:
            self.gateway_user = iam.User(
                self, "GatewayUser", user_name=c.KEY_MODE_USER_NAME
            )
            policy = iam.Policy(
                self,
                "GatewayUserPolicy",
                statements=statements,
            )
            policy.attach_to_user(self.gateway_user)

        swap_and_hosts = "\n".join(
            [
                # t4g.small has 2 GB; squid+LiteLLM+Postgres want a cushion.
                "if [ ! -f /swapfile ]; then",
                "  fallocate -l 1G /swapfile && chmod 600 /swapfile",
                "  mkswap /swapfile && swapon /swapfile",
                "  echo '/swapfile none swap sw 0 0' >> /etc/fstab",
                "fi",
            ]
        )

        self.instance = ec2.CfnInstance(
            self,
            "ProxyInstance",
            instance_type=c.PROXY_INSTANCE_TYPE,
            image_id=ami.image_id,
            subnet_id=network.public_subnet.subnet_id,
            private_ip_address=c.PROXY_IP,
            security_group_ids=[network.proxy_sg.security_group_id],
            iam_instance_profile=instance_profile_ref,
            metadata_options=ec2.CfnInstance.MetadataOptionsProperty(
                http_endpoint="enabled",
                http_tokens="required",
                http_put_response_hop_limit=1,
                http_protocol_ipv6="disabled",
            ),
            block_device_mappings=[
                ec2.CfnInstance.BlockDeviceMappingProperty(
                    device_name="/dev/sda1",
                    ebs=ec2.CfnInstance.EbsProperty(
                        volume_size=20,
                        volume_type="gp3",
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
            user_data=Fn.base64(_operator_key_user_data(config, swap_and_hosts)),
        )

        ec2.CfnEIPAssociation(
            self,
            "ProxyEipAssoc",
            allocation_id=self.eip.attr_allocation_id,
            instance_id=self.instance.ref,
        )

        CfnOutput(self, "ProxyInstanceId", value=self.instance.ref)
        CfnOutput(self, "ProxyPrivateIp", value=c.PROXY_IP)
        CfnOutput(self, "ProxyPublicIp", value=self.eip.attr_public_ip)
        CfnOutput(self, "ProxyAmiId", value=ami.image_id)
        if self.gateway_role is not None:
            CfnOutput(self, "GatewayRoleName", value=self.gateway_role.role_name)
        if self.gateway_user is not None:
            CfnOutput(self, "GatewayUserName", value=self.gateway_user.user_name)
