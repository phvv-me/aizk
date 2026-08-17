import pytest
from aws_cdk import App
from aws_cdk.assertions import Match, Template

from infra.aws.config import DeploymentConfig, enabled
from infra.aws.stack import AizkAwsStack

_DIGEST = "0123456789abcdef" * 4


def template(
    compute: bool = False,
    billing_email: str = "",
    logto: bool = False,
) -> Template:
    """Build the isolated stack with valid synthetic deployment inputs."""
    app = App()
    config = DeploymentConfig(
        deploy_compute=compute,
        image_digest=_DIGEST if compute else "",
        web_image_digest=_DIGEST if compute and logto else "",
        public_url="https://memory.example.com" if logto else None,
        logto_url="https://tenant.logto.app" if logto else None,
        logto_client_id="management-client" if logto else "",
        web_client_id="web-client" if logto else "",
        billing_email=billing_email,
    )
    return Template.from_stack(AizkAwsStack(app, "TestStack", config))


def test_bootstrap_creates_only_the_bounded_image_repository() -> None:
    stack = template()

    stack.resource_count_is("AWS::ECR::Repository", 1)
    stack.resource_count_is("AWS::S3::Bucket", 1)
    stack.resource_count_is("AWS::Lambda::Function", 0)
    stack.resource_count_is("AWS::Budgets::Budget", 0)
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
                                    {"selection": Match.object_like({"countNumber": 4})}
                                )
                            ]
                        }
                    )
                )
            },
        },
    )
    stack.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def test_runtime_is_serverless_bounded_and_recovers_every_fifteen_minutes() -> None:
    stack = template(compute=True)

    stack.resource_count_is("AWS::Lambda::Function", 2)
    stack.resource_count_is("AWS::Lambda::Url", 1)
    stack.resource_count_is("AWS::Scheduler::Schedule", 2)
    stack.resource_count_is("AWS::Budgets::Budget", 1)
    stack.resource_count_is("AWS::IAM::Policy", 3)
    stack.resource_count_is("AWS::ApiGatewayV2::Api", 0)
    stack.resource_count_is("AWS::Events::Rule", 0)
    stack.resource_count_is("AWS::CloudWatch::Alarm", 0)
    stack.resource_count_is("AWS::SNS::Topic", 0)
    stack.resource_count_is("AWS::EC2::NatGateway", 0)
    stack.resource_count_is("AWS::EC2::VPC", 0)
    stack.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Name": "craizk-staging-worker-recovery",
            "ScheduleExpression": "rate(15 minutes)",
            "Target": Match.object_like(
                {
                    "Input": '{"kind":"worker"}',
                    "RetryPolicy": {
                        "MaximumEventAgeInSeconds": 900,
                        "MaximumRetryAttempts": 2,
                    },
                }
            ),
        },
    )
    stack.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Name": "craizk-staging-mcp-warm",
            "ScheduleExpression": "rate(5 minutes)",
            "Target": Match.object_like(
                {
                    "Input": '{"kind":"warm"}',
                    "RetryPolicy": {
                        "MaximumEventAgeInSeconds": 300,
                        "MaximumRetryAttempts": 0,
                    },
                }
            ),
        },
    )
    stack.has_resource_properties(
        "AWS::Lambda::Url",
        {"AuthType": "AWS_IAM", "InvokeMode": "BUFFERED"},
    )
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "craizk-staging-mcp",
            "MemorySize": 2048,
            "Timeout": 60,
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "FASTMCP_HOME": "/tmp/fastmcp",
                        "AIZK_MONTHLY_TOTAL_OPERATION_LIMIT": "10000",
                        "AIZK_MONTHLY_USER_REMEMBER_LIMIT": "50",
                        "AIZK_ARTIFACT_INGEST_ENABLED": "true",
                        "AIZK_ARTIFACT_MALWARE_SCAN_ENABLED": "false",
                        "AIZK_OBJECT_STORE_AWS_NATIVE": "true",
                        "AIZK_OBJECT_STORE_UPLOAD_BYTE_LIMIT": "4194304",
                        "AIZK_OBJECT_STORE_USER_BYTE_LIMIT": "1073741824",
                        "AIZK_PROFILING": "true",
                        "AIZK_RECALL_ACCESS_RECORDING_ENABLED": "false",
                        "AIZK_RECALL_COMMUNITIES_ENABLED": "true",
                        "AIZK_RECALL_ENTITY_CATALOG_ENABLED": "true",
                        "AIZK_RECALL_GRAPH_EXPANSION_ENABLED": "true",
                        "AIZK_RECALL_PROFILES_ENABLED": "false",
                        "AIZK_RECALL_RAPTOR_ENABLED": "false",
                        "AIZK_RECALL_SOURCES_FIRST": "true",
                        "AIZK_AWS_PARAMETER_ENV": Match.string_like_regexp(
                            "/craizk/staging/database-url"
                        ),
                        "AIZK_EMBED_EXTRA_BODY": (
                            '{"provider":{"order":["DeepInfra"],"allow_fallbacks":false}}'
                        ),
                        "AIZK_LLM_EXTRA_BODY": (
                            '{"reasoning":{"enabled":false},"session_id":"aizk-extractor"}'
                        ),
                    }
                )
            },
        },
    )
    rendered = str(stack.to_json())
    assert "resolve:ssm-secure" not in rendered
    [public] = stack.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"FunctionName": "craizk-staging-mcp"}},
    ).values()
    assert "/craizk/staging/admin-database-url" not in str(public["Properties"]["Environment"])
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "craizk-staging-worker",
            "MemorySize": 2048,
            "Timeout": 840,
        },
    )


def test_logto_makes_the_function_url_public_only_after_app_auth_is_enabled() -> None:
    stack = template(compute=True, logto=True)

    stack.resource_count_is("AWS::Lambda::Function", 3)
    stack.has_resource_properties(
        "AWS::Lambda::Url",
        {"AuthType": "NONE"},
    )
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "craizk-staging-mcp",
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "AIZK_LOGTO_URL": "https://tenant.logto.app",
                        "AIZK_LOGTO_MANAGEMENT_RESOURCE": "https://tenant.logto.app/api",
                        "AIZK_MCP_PUBLIC_URL": "https://memory.example.com",
                        "AIZK_REQUIRE_AUTH": "true",
                        "AIZK_WEB_FUNCTION_NAME": Match.any_value(),
                    }
                )
            },
        },
    )
    stack.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": "craizk-staging-web",
            "MemorySize": 1024,
            "Timeout": 60,
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "AIZK_LOGTO_URL": "https://tenant.logto.app",
                        "AIZK_WEB_CLIENT_ID": "web-client",
                        "AIZK_WEB_PUBLIC_URL": "https://memory.example.com",
                        "AIZK_WEB_API_URL": "https://memory.example.com",
                        "AIZK_MCP_PUBLIC_URL": "https://memory.example.com",
                        "AIZK_AWS_PARAMETER_ENV": Match.string_like_regexp(
                            "/craizk/staging/web-client-secret"
                        ),
                    }
                )
            },
        },
    )


def test_budget_reports_gross_cost_without_hiding_usage_behind_credits() -> None:
    stack = template(compute=True, billing_email="owner@example.com")

    stack.has_resource_properties(
        "AWS::Budgets::Budget",
        {
            "Budget": {
                "BudgetLimit": {"Amount": 10, "Unit": "USD"},
                "BudgetType": "COST",
                "TimeUnit": "MONTHLY",
                "CostTypes": {
                    "IncludeCredit": False,
                    "IncludeRefund": False,
                    "UseBlended": False,
                },
            },
            "NotificationsWithSubscribers": Match.array_with(
                [
                    Match.object_like(
                        {
                            "Notification": Match.object_like({"Threshold": 100}),
                            "Subscribers": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Address": "owner@example.com",
                                            "SubscriptionType": "EMAIL",
                                        }
                                    )
                                ]
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


def test_public_url_rejects_an_mcp_path_that_would_duplicate_the_resource() -> None:
    with pytest.raises(ValueError, match="origin without a path"):
        DeploymentConfig(public_url="https://memory.example.com/mcp")


def test_deployment_config_rejects_unready_compute_or_partial_logto() -> None:
    with pytest.raises(ValueError, match="image digest"):
        DeploymentConfig(deploy_compute=True)

    with pytest.raises(ValueError, match="every public"):
        DeploymentConfig(
            deploy_compute=True,
            image_digest=_DIGEST,
            logto_url="https://tenant.logto.app",
        )

    with pytest.raises(ValueError, match="web image digest"):
        DeploymentConfig(
            deploy_compute=True,
            image_digest=_DIGEST,
            public_url="https://memory.example.com",
            logto_url="https://tenant.logto.app",
            logto_client_id="mcp-client",
            web_client_id="web-client",
        )


def test_deployment_defaults_to_isolated_singapore_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AIZK_AWS_REGION", raising=False)
    config = DeploymentConfig.from_environment()
    assert config.region == "ap-southeast-1"
    assert config.name == "craizk-staging"
    assert config.database_url_parameter == "/craizk/staging/database-url"

    monkeypatch.setenv("AIZK_AWS_REGION", "ap-northeast-1")
    assert DeploymentConfig.from_environment().region == "ap-northeast-1"

    monkeypatch.setenv("AIZK_AWS_RECALL_ACCESS_RECORDING_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_COMMUNITIES_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_ENTITY_CATALOG_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_GRAPH_EXPANSION_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_PROFILES_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_RAPTOR_ENABLED", "false")
    monkeypatch.setenv("AIZK_AWS_RECALL_SOURCES_FIRST", "true")
    lean = DeploymentConfig.from_environment()
    assert not lean.recall_access_recording_enabled
    assert not lean.recall_communities_enabled
    assert not lean.recall_entity_catalog_enabled
    assert not lean.recall_graph_expansion_enabled
    assert not lean.recall_profiles_enabled
    assert not lean.recall_raptor_enabled
    assert lean.recall_sources_first
