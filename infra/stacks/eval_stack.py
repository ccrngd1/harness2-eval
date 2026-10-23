"""H2Eval: the untrusted guest.

No instance profile, no role, IMDSv2 required with hop limit 1 (setup.sh
disables the IMDS endpoint entirely after cloud-init finishes), encrypted
80 GB gp3 root, fixed IP 10.99.0.70 in the isolated subnet.

User-data is minimal hardening only, no secrets: operator public key
(C7), netplan/resolved DNS removal (§3: use-dns:false and an empty
resolved config, so even if VPC DNS were ever re-enabled the guest has no
resolver configured), snapd/motd-news off. Toolchain install happens in
scripts/guest/guest-bootstrap.sh during phase `setup`, not here.
"""

from aws_cdk import (
    CfnOutput,
    Fn,
    Stack,
    aws_ec2 as ec2,
)
from constructs import Construct

from . import constants as c
from .config import AppConfig
from .network_stack import H2NetworkStack

_GUEST_HARDENING = r"""
# --- DNS: the guest must have NO resolver at all (NetOps §3) ---
# The override file reuses the SAME definition name cloud-init generated
# (the real interface, e.g. ens5) so netplan merges dhcp4-overrides into
# it; a match:-based definition would not reliably override 50-cloud-init.
IFACE="$(ip -o link show | awk -F': ' '$2 ~ /^(en|eth)/ {print $2; exit}')"
mkdir -p /etc/netplan
cat > /etc/netplan/90-h2-no-dns.yaml <<EOF
network:
  version: 2
  ethernets:
    ${IFACE}:
      dhcp4: true
      dhcp4-overrides: {use-dns: false}
EOF
chmod 0600 /etc/netplan/90-h2-no-dns.yaml
mkdir -p /etc/systemd/resolved.conf.d
cat > /etc/systemd/resolved.conf.d/90-h2-no-dns.conf <<'EOF'
[Resolve]
DNS=
FallbackDNS=
DNSStubListener=no
EOF
netplan apply || true
systemctl restart systemd-resolved || true

# Hostname must resolve locally with no DNS (NetOps §3 manage_etc_hosts).
H="$(hostname)"
grep -q " ${H}$" /etc/hosts || echo "127.0.1.1 ${H}" >> /etc/hosts

# --- phone-home hygiene (NetOps §3) ---
systemctl disable --now snapd.service snapd.socket snapd.seeded.service 2>/dev/null || true
sed -i 's/^ENABLED=.*/ENABLED=0/' /etc/default/motd-news 2>/dev/null || true
systemctl disable --now motd-news.timer 2>/dev/null || true
"""


class H2EvalStack(Stack):
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
            c.UBUNTU_2404_AMD64_SSM, os=ec2.OperatingSystemType.LINUX
        ).get_image(self)

        user_data = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh",
                f"echo '{config.operator_ssh_public_key}' > /home/ubuntu/.ssh/authorized_keys",
                "chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys",
                "chmod 0600 /home/ubuntu/.ssh/authorized_keys",
                _GUEST_HARDENING,
                "",
            ]
        )

        self.instance = ec2.CfnInstance(
            self,
            "EvalInstance",
            instance_type=c.EVAL_INSTANCE_TYPE,
            image_id=ami.image_id,
            subnet_id=network.private_subnet.subnet_id,
            private_ip_address=c.GUEST_IP,
            security_group_ids=[network.eval_sg.security_group_id],
            # No iam_instance_profile, ever (R1).
            metadata_options=ec2.CfnInstance.MetadataOptionsProperty(
                http_endpoint="enabled",  # disabled by setup.sh after cloud-init
                http_tokens="required",
                http_put_response_hop_limit=1,
                http_protocol_ipv6="disabled",
            ),
            block_device_mappings=[
                ec2.CfnInstance.BlockDeviceMappingProperty(
                    device_name="/dev/sda1",
                    ebs=ec2.CfnInstance.EbsProperty(
                        volume_size=80,
                        volume_type="gp3",
                        encrypted=True,
                        delete_on_termination=True,
                    ),
                )
            ],
            user_data=Fn.base64(user_data),
        )

        CfnOutput(self, "EvalInstanceId", value=self.instance.ref)
        CfnOutput(self, "EvalPrivateIp", value=c.GUEST_IP)
        CfnOutput(self, "EvalAmiId", value=ami.image_id)
        # R9a: we do NOT create or attach an operator EIC policy; we only
        # print the recommended JSON for CC to apply to his own principal.
        CfnOutput(
            self,
            "RecommendedOperatorEicPolicy",
            description=(
                "Recommended (NOT created) IAM policy for the operator "
                "principal: OpenTunnel on this endpoint, port 22, the two "
                "private IPs. Apply it yourself if you want least privilege."
            ),
            value=Fn.sub(
                '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                '"Action":"ec2-instance-connect:OpenTunnel",'
                '"Resource":"arn:aws:ec2:${AWS::Region}:${AWS::AccountId}:'
                'instance-connect-endpoint/*",'
                '"Condition":{"StringEquals":{"ec2-instance-connect:remotePort":"22",'
                '"ec2-instance-connect:privateIpAddress":'
                f'["{c.GUEST_IP}","{c.PROXY_IP}"]' + "}}}]}"
            ),
        )
