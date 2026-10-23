"""H2Network: isolated VPC, subnets, security groups, EIC endpoint, flow logs.

Security-load-bearing choices (NetOps v2 §4, §7; CC directives):

* DNS bypass control, option (a) (§4.2 / C4): the Amazon-provided VPC
  resolver (VPC base+2 and 169.254.169.253) is reachable from every subnet
  regardless of route tables and is NOT filtered by SGs or NACLs, so it is
  a DNS exfiltration/tunnel path from the guest. We disable it for the
  whole VPC (enableDnsSupport=false, enableDnsHostnames=false) and hand
  out 1.1.1.1/9.9.9.9 via a custom DHCP options set instead. The guest has
  no route to those resolvers (private subnet, local route only), so it
  cannot resolve anything; the proxy reaches them through explicit SG
  egress on udp/tcp 53 to exactly those two /32s.

* restrictDefaultSecurityGroup is deliberately FALSE: CDK implements it
  with a Lambda-backed custom resource, which violates NetOps R9
  ("no Lambda anywhere"). setup.sh revokes the default SG's rules
  post-deploy instead (recorded as a deviation for NetOps).

* Availability zone is passed explicitly so synth never needs a
  context-provider lookup (and therefore never needs credentials).
"""

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
)
from constructs import Construct

from . import constants as c
from .config import AppConfig


class H2NetworkStack(Stack):
    # Hardcode a single AZ so Vpc never consults the AZ context provider:
    # synth must work with zero credentials (build-host hard constraint).
    @property
    def availability_zones(self) -> list[str]:
        return [f"{self.region}a"]

    def __init__(self, scope: Construct, construct_id: str, *, config: AppConfig, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(c.VPC_CIDR),
            availability_zones=[f"{config.region}a"],
            enable_dns_support=False,
            enable_dns_hostnames=False,
            restrict_default_security_group=False,  # R9: no Lambda; setup.sh revokes rules
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=26,
                    map_public_ip_on_launch=False,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=26,
                ),
            ],
        )

        self.public_subnet = self.vpc.public_subnets[0]
        self.private_subnet = self.vpc.isolated_subnets[0]

        # §4.2 option (a): custom DHCP options; the VPC resolver is off.
        dhcp = ec2.CfnDHCPOptions(
            self,
            "ExternalResolverDhcpOptions",
            domain_name_servers=c.DNS_RESOLVERS,
        )
        ec2.CfnVPCDHCPOptionsAssociation(
            self,
            "DhcpAssociation",
            dhcp_options_id=dhcp.ref,
            vpc_id=self.vpc.vpc_id,
        )

        # ---- Security groups (full matrix from NetOps §7) ----
        self.eic_sg = ec2.SecurityGroup(
            self,
            "EicSg",
            vpc=self.vpc,
            description="harness2-eval EIC endpoint: egress 22 to eval+proxy only",
            allow_all_outbound=False,
        )
        self.eval_sg = ec2.SecurityGroup(
            self,
            "EvalSg",
            vpc=self.vpc,
            description="harness2-eval guest: egress to proxy 3128/4000 only",
            allow_all_outbound=False,
        )
        self.proxy_sg = ec2.SecurityGroup(
            self,
            "ProxySg",
            vpc=self.vpc,
            description="harness2-eval proxy/gateway: squid+litellm from eval SG only",
            allow_all_outbound=False,
        )

        self.eval_sg.add_egress_rule(
            self.proxy_sg, ec2.Port.tcp(c.SQUID_PORT), "squid via proxy only"
        )
        self.eval_sg.add_egress_rule(
            self.proxy_sg, ec2.Port.tcp(c.LITELLM_PORT), "litellm gateway only"
        )
        self.eval_sg.add_ingress_rule(
            self.eic_sg, ec2.Port.tcp(22), "operator SSH from EIC endpoint only"
        )

        self.proxy_sg.add_ingress_rule(
            self.eval_sg, ec2.Port.tcp(c.SQUID_PORT), "guest to squid"
        )
        self.proxy_sg.add_ingress_rule(
            self.eval_sg, ec2.Port.tcp(c.LITELLM_PORT), "guest to litellm (phase-gated on host)"
        )
        self.proxy_sg.add_ingress_rule(
            self.eic_sg, ec2.Port.tcp(22), "operator SSH from EIC endpoint only"
        )
        self.proxy_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "apt/pypi/bedrock-runtime over TLS"
        )
        self.proxy_sg.add_egress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "apt (ports.ubuntu.com) plain http"
        )
        for resolver in c.DNS_RESOLVERS:
            self.proxy_sg.add_egress_rule(
                ec2.Peer.ipv4(f"{resolver}/32"), ec2.Port.udp(53), "squid DNS (option a)"
            )
            self.proxy_sg.add_egress_rule(
                ec2.Peer.ipv4(f"{resolver}/32"), ec2.Port.tcp(53), "squid DNS over TCP"
            )

        self.eic_sg.add_egress_rule(
            self.eval_sg, ec2.Port.tcp(22), "SSH tunnel to guest"
        )
        self.eic_sg.add_egress_rule(
            self.proxy_sg, ec2.Port.tcp(22), "SSH tunnel to proxy"
        )

        # EC2 Instance Connect Endpoint, in the private subnet (NetOps §4.1).
        self.eic_endpoint = ec2.CfnInstanceConnectEndpoint(
            self,
            "EicEndpoint",
            subnet_id=self.private_subnet.subnet_id,
            security_group_ids=[self.eic_sg.security_group_id],
            preserve_client_ip=False,
        )

        # ---- VPC Flow Logs: ALL traffic to a KMS-encrypted CW log group ----
        flow_key = kms.Key(
            self,
            "FlowLogKey",
            enable_key_rotation=True,
            description="harness2-eval flow-log group encryption",
            removal_policy=RemovalPolicy.DESTROY,
        )
        flow_key.add_to_resource_policy(
            iam.PolicyStatement(
                principals=[iam.ServicePrincipal(f"logs.{config.region}.amazonaws.com")],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:aws:logs:{config.region}:{config.account}:log-group:*"
                        )
                    }
                },
            )
        )
        # Fixed name so teardown.sh can find/delete it even after a
        # partial destroy left it behind.
        self.flow_log_group = logs.LogGroup(
            self,
            "FlowLogGroup",
            log_group_name="/harness2-eval/flow-logs",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=flow_key,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Explicit delivery role, scoped to this log group only (R9).
        flow_role = iam.Role(
            self,
            "FlowLogRole",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
            description="harness2-eval flow-log delivery (scoped to one log group)",
        )
        flow_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[self.flow_log_group.log_group_arn],
            )
        )
        ec2.FlowLog(
            self,
            "VpcFlowLog",
            resource_type=ec2.FlowLogResourceType.from_vpc(self.vpc),
            traffic_type=ec2.FlowLogTrafficType.ALL,
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                self.flow_log_group, flow_role
            ),
        )

        # Outputs consumed by the operator scripts (setup.sh et al.).
        CfnOutput(self, "EicEndpointId", value=self.eic_endpoint.ref)
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(
            self,
            "DefaultSgId",
            value=self.vpc.vpc_default_security_group,
            description="setup.sh revokes this SG's rules post-deploy (R9 deviation)",
        )
