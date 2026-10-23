"""H2Guardrails: AWS Budget, alerts at 50/80/100%, Budget Action at 100%.

The Budget amount is the single budgetCapUsd context value (default 3500
in cdk.json; CC decision 2026-10-22, Minimum tier). The same value is fed
to LiteLLM max_budget by setup.sh — LiteLLM is the PRIMARY hard stop
(overshoot ~one in-flight request); this Budget lags billing by ~8-24 h
and is only the backstop that limits damage after a proxy compromise.

Budget scope per NetOps §6.5: NO Service=Bedrock-only filter (Marketplace
line items escape it). Default is unfiltered account cost (dedicated
sandbox account). Optional budgetCostTagFilter switches to the
project=harness2-eval cost-allocation tag for main-account use (activate
the tag >=24 h before Day 1).

The Budget Action (APPLY_IAM_POLICY, AUTOMATIC) attaches a deny-bedrock
managed policy to the gateway credential: the ROLE in role mode, the USER
in key mode. Its execution role trusts budgets.amazonaws.com and may only
Attach/DetachRolePolicy (or UserPolicy) on that one principal, with
iam:PolicyARN pinned to the deny policy.
"""

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_budgets as budgets,
    aws_iam as iam,
)
from constructs import Construct

from . import constants as c
from .config import AppConfig
from .proxy_stack import H2ProxyStack


class H2GuardrailsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: AppConfig,
        proxy: H2ProxyStack,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # The deny policy the Budget Action attaches at 100%.
        self.deny_policy = iam.ManagedPolicy(
            self,
            "BedrockDenyPolicy",
            description="harness2-eval kill switch: attached by the Budget Action at 100%",
            statements=[
                iam.PolicyStatement(
                    sid="DenyAllBedrock",
                    effect=iam.Effect.DENY,
                    actions=["bedrock:*"],
                    resources=["*"],
                )
            ],
        )

        # Budget Action execution role, scoped to exactly one attach target.
        action_role = iam.Role(
            self,
            "BudgetActionRole",
            assumed_by=iam.ServicePrincipal("budgets.amazonaws.com"),
            description="harness2-eval Budget Action: attach the deny policy to the gateway credential",
        )
        policy_arn_condition = {
            "ArnEquals": {"iam:PolicyARN": self.deny_policy.managed_policy_arn}
        }
        if config.credential_mode == "role":
            assert proxy.gateway_role is not None
            action_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                    resources=[proxy.gateway_role.role_arn],
                    conditions=policy_arn_condition,
                )
            )
            action_def = budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=self.deny_policy.managed_policy_arn,
                    roles=[proxy.gateway_role.role_name],
                )
            )
        else:
            assert proxy.gateway_user is not None
            action_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["iam:AttachUserPolicy", "iam:DetachUserPolicy"],
                    resources=[proxy.gateway_user.user_arn],
                    conditions=policy_arn_condition,
                )
            )
            action_def = budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=self.deny_policy.managed_policy_arn,
                    users=[proxy.gateway_user.user_name],
                )
            )

        cost_filters = None
        if config.budget_cost_tag_filter:
            # Cost-allocation tag filter (main-account use). The tag must be
            # ACTIVATED in Billing >=24 h before Day 1 or it matches nothing.
            cost_filters = {
                "TagKeyValue": [
                    f"user:{c.PROJECT_TAG_KEY}${c.PROJECT_TAG_VALUE}"
                ]
            }

        subscriber = budgets.CfnBudget.SubscriberProperty(
            subscription_type="EMAIL", address=config.alert_email
        )
        notifications = [
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="ACTUAL",
                    comparison_operator="GREATER_THAN",
                    threshold=threshold,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[subscriber],
            )
            for threshold in (50, 80, 100)
        ]
        # FORECASTED 100% (SHOULD, NetOps §6.5).
        notifications.append(
            budgets.CfnBudget.NotificationWithSubscribersProperty(
                notification=budgets.CfnBudget.NotificationProperty(
                    notification_type="FORECASTED",
                    comparison_operator="GREATER_THAN",
                    threshold=100,
                    threshold_type="PERCENTAGE",
                ),
                subscribers=[subscriber],
            )
        )

        self.budget = budgets.CfnBudget(
            self,
            "SpendBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="harness2-eval-cap",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=config.budget_cap_usd, unit="USD"
                ),
                cost_filters=cost_filters,
            ),
            notifications_with_subscribers=notifications,
        )

        self.action = budgets.CfnBudgetsAction(
            self,
            "DenyAtFullSpend",
            budget_name="harness2-eval-cap",
            action_type="APPLY_IAM_POLICY",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(
                type="PERCENTAGE", value=100
            ),
            approval_model="AUTOMATIC",
            definition=action_def,
            execution_role_arn=action_role.role_arn,
            notification_type="ACTUAL",
            subscribers=[
                budgets.CfnBudgetsAction.SubscriberProperty(
                    type="EMAIL", address=config.alert_email
                )
            ],
        )
        self.action.add_resource_dependency(self.budget)

        CfnOutput(self, "BudgetCapUsd", value=str(config.budget_cap_usd))
        CfnOutput(self, "DenyPolicyArn", value=self.deny_policy.managed_policy_arn)
