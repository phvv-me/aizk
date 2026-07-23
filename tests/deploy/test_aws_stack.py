import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from infra.aws.config import DeploymentConfig, enabled
from infra.aws.stack import AizkAwsStack

_DIGEST = "0123456789abcdef" * 4


def template(compute: bool = False, billing_email: str = "") -> Template:
    """Build the isolated stack with valid synthetic deployment inputs."""
    app = App()
    config = DeploymentConfig(
        deploy_compute=compute,
        image_digest=_DIGEST if compute else "",
        public_url="https://memory.example.com",
        logto_url="https://tenant.logto.app",
        logto_client_id="management-client",
        oauth_client_id="mcp-client",
        billing_email=billing_email,
    )
    return Template.from_stack(AizkAwsStack(app, "TestStack", config))


def test_bootstrap_creates_only_the_bounded_image_repository() -> None:
    stack = template()

    stack.resource_count_is("AWS::ECR::Repository", 1)
    stack.resource_count_is("AWS::Lambda::Function", 0)
    stack.has_resource_properties(
        "AWS::ECR::Repository",
        {
            "ImageTagMutability": "IMMUTABLE",
            "ImageScanningConfiguration": {"ScanOnPush": True},
            "LifecyclePolicy": {
                "LifecyclePolicyText": Match.serialized_json(
                    Match.object_like(
                        {
                            "rules": [
                                Match.object_like(
                                    {"selection": Match.object_like({"countNumber": 2})}
                                )
                            ]
                        }
                    )
                )
            },
        },
    )


def test_runtime_is_serverless_bounded_and_recovers_every_fifteen_minutes() -> None:
    stack = template(compute=True)

    stack.resource_count_is("AWS::Lambda::Function", 4)
    stack.resource_count_is("AWS::ApiGatewayV2::Api", 1)
    stack.resource_count_is("AWS::Events::Rule", 1)
    stack.resource_count_is("AWS::EC2::NatGateway", 0)
    stack.resource_count_is("AWS::EC2::VPC", 0)
    stack.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 0)
    stack.has_resource_properties(
        "AWS::Events::Rule",
        {"ScheduleExpression": "rate(15 minutes)"},
    )
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "aizk-cockroachdb-mcp",
            "MemorySize": 1024,
            "ReservedConcurrentExecutions": 2,
            "Timeout": 25,
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "AIZK_MONTHLY_TOTAL_OPERATION_LIMIT": "10000",
                        "AIZK_MONTHLY_USER_REMEMBER_LIMIT": "50",
                        "AIZK_ARTIFACT_INGEST_ENABLED": "false",
                    }
                )
            },
        },
    )
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "aizk-cockroachdb-worker",
            "MemorySize": 2048,
            "ReservedConcurrentExecutions": 1,
            "Timeout": 840,
        },
    )


def test_edge_uses_logto_jwt_throttling_and_an_emergency_budget() -> None:
    stack = template(compute=True, billing_email="owner@example.com")

    stack.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": {
                "Audience": ["https://memory.example.com/mcp"],
                "Issuer": "https://tenant.logto.app/oidc",
            },
        },
    )
    stack.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": {
                "ThrottlingBurstLimit": 2,
                "ThrottlingRateLimit": 2,
            }
        },
    )
    stack.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": {
                "BudgetLimit": {"Amount": 10, "Unit": "USD"},
                "BudgetType": "COST",
                "TimeUnit": "MONTHLY",
            },
            "NotificationsWithSubscribers": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Notification": Match.object_like({"Threshold": 100}),
                            "Subscribers": Match.array_with(
                                [Match.object_like({"SubscriptionType": "SNS"})]
                            ),
                        }
                    )
                ]
            ),
        },
    )


@pytest.mark.parametrize("value", ["1", "TRUE", "yes"])
def test_enabled_accepts_explicit_truthy_values(value: str) -> None:
    assert enabled(value)


def test_deployment_config_rejects_unready_compute() -> None:
    with pytest.raises(ValueError, match="image digest"):
        DeploymentConfig(deploy_compute=True)

    with pytest.raises(ValueError, match="real public"):
        DeploymentConfig(
            deploy_compute=True,
            image_digest=_DIGEST,
            logto_url="https://replace.logto.app",
        )
