"""Fixed addressing and naming shared by all four stacks.

Values are frozen by NetOps spec v2 §4.1 (change C5). The proxy sits at
10.99.0.10 because AWS reserves .0-.3 of each subnet; nothing may ever
reference a ".1" host address in this VPC.
"""

VPC_CIDR = "10.99.0.0/24"
PUBLIC_SUBNET_CIDR = "10.99.0.0/26"
PRIVATE_SUBNET_CIDR = "10.99.0.64/26"
PROXY_IP = "10.99.0.10"
GUEST_IP = "10.99.0.70"

SQUID_PORT = 3128
LITELLM_PORT = 4000

# Squid/proxy DNS per NetOps §4.2 option (a): the VPC resolver is disabled,
# so the proxy resolves via these two public resolvers only.
DNS_RESOLVERS = ["1.1.1.1", "9.9.9.9"]

PROJECT_TAG_KEY = "project"
PROJECT_TAG_VALUE = "harness2-eval"

# Canonical public SSM parameters for Ubuntu 24.04 (noble) current AMIs.
# Resolved at deploy time; no lookup/context-provider call at synth time.
UBUNTU_2404_AMD64_SSM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
)
UBUNTU_2404_ARM64_SSM = (
    "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
)

EVAL_INSTANCE_TYPE = "m7i.xlarge"
PROXY_INSTANCE_TYPE = "t4g.small"

KEY_MODE_USER_NAME = "harness2-eval-bedrock"
