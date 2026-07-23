from collections.abc import Mapping
from typing import cast

from aws_cdk import (
    CfnOutput,
    Duration,
    Environment,
    RemovalPolicy,
    SecretValue,
    Stack,
    Tags,
)
from aws_cdk import aws_apigatewayv2 as gateway
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk.aws_apigatewayv2_authorizers import HttpJwtAuthorizer
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from .config import DeploymentConfig


class AizkAwsStack(Stack):
    """Deploy the bounded Lambda alpha around an external CockroachDB Basic cluster."""

    def __init__(self, scope: Construct, construct_id: str, config: DeploymentConfig) -> None:
        super().__init__(scope, construct_id, env=Environment(region="us-east-1"))
        self.config = config
        self.repository = self._repository()
        self._outputs()
        if config.deploy_compute:
            self._runtime()
        Tags.of(self).add("Application", config.name)
        Tags.of(self).add("ManagedBy", "AWS CDK")
        Tags.of(self).add("Project", "AIZK CockroachDB hackathon")

    def _repository(self) -> ecr.Repository:
        repository = ecr.Repository(
            self,
            "Repository",
            repository_name=self.config.name,
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.IMMUTABLE,
            encryption=ecr.RepositoryEncryption.AES_256,
            removal_policy=RemovalPolicy.RETAIN,
            empty_on_delete=False,
        )
        repository.add_lifecycle_rule(
            description="Keep only the two newest immutable images",
            max_image_count=2,
            rule_priority=1,
        )
        return repository

    def _runtime(self) -> None:
        shared = self._shared_environment()
        worker = self._image_function(
            "Worker",
            command="aizk.commands.aws.worker_handler",
            memory=2048,
            timeout=840,
            concurrency=1,
            environment=shared
            | {
                "AIZK_ADMIN_DATABASE_URL": self._secret(self.config.admin_database_url_parameter),
                "AIZK_QUEUE_BATCH_SIZE": "8",
            },
        )
        public = self._image_function(
            "Mcp",
            command="aizk.commands.aws.mcp_handler",
            memory=1024,
            timeout=25,
            concurrency=2,
            environment=shared
            | {
                "AIZK_LOGTO_CLIENT_ID": self.config.logto_client_id,
                "AIZK_LOGTO_CLIENT_SECRET": self._secret(
                    self.config.logto_client_secret_parameter
                ),
                "AIZK_OAUTH_CLIENT_ID": self.config.oauth_client_id,
                "AIZK_OAUTH_CLIENT_SECRET": self._secret(
                    self.config.oauth_client_secret_parameter
                ),
                "AIZK_MCP_PUBLIC_URL": self.config.public_url,
                "AIZK_WORKER_FUNCTION_NAME": worker.function_name,
            },
        )
        worker.grant_invoke(public)
        setup = self._image_function(
            "Setup",
            command="aizk.commands.aws.setup_handler",
            memory=1024,
            timeout=300,
            concurrency=1,
            environment=shared
            | {"AIZK_ADMIN_DATABASE_URL": self._secret(self.config.admin_database_url_parameter)},
        )
        events.Rule(
            self,
            "WorkerRecovery",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[
                cast(
                    "events.IRuleTarget",
                    targets.LambdaFunction(
                        cast("lambda_.IFunction", worker),
                        max_event_age=Duration.minutes(15),
                        retry_attempts=1,
                    ),
                )
            ],
        )
        api = self._api(public)
        self._cost_controls(public, worker)
        CfnOutput(self, "McpUrl", value=f"{api.api_endpoint}/mcp")
        CfnOutput(self, "SetupFunctionName", value=setup.function_name)

    def _shared_environment(self) -> dict[str, str]:
        provider = '{"provider":{"zdr":true,"data_collection":"deny"}}'
        return {
            "AIZK_DATABASE_BACKEND": "cockroachdb",
            "AIZK_DATABASE_URL": self._secret(self.config.database_url_parameter),
            "AIZK_AUTO_SETUP": "false",
            "AIZK_DB_NULL_POOL": "true",
            "AIZK_DB_POOL_SIZE": "1",
            "AIZK_DB_POOL_MAX_OVERFLOW": "0",
            "AIZK_EMBED_URL": "https://openrouter.ai/api/v1",
            "AIZK_EMBED_API_KEY": self._secret(self.config.openrouter_key_parameter),
            "AIZK_EMBED_MODEL": "qwen/qwen3-embedding-8b",
            "AIZK_EMBED_DIM": "1024",
            "AIZK_EMBED_EXTRA_BODY": provider,
            "AIZK_LLM_URL": "https://openrouter.ai/api/v1",
            "AIZK_LLM_API_KEY": self._secret(self.config.openrouter_key_parameter),
            "AIZK_LLM_MODEL": "deepseek/deepseek-v4-flash",
            "AIZK_LLM_EXTRA_BODY": provider,
            "AIZK_LOGTO_URL": self.config.logto_url,
            "AIZK_REQUIRE_AUTH": "true",
            "AIZK_ARTIFACT_INGEST_ENABLED": "false",
            "AIZK_RERANK_ENABLED": "false",
            "AIZK_EXTRACT_BACKEND": "llm",
            "AIZK_EXTRACTION_GATE_ENABLED": "false",
            "AIZK_GRAPH_ENTITY_SEEDING": "false",
            "AIZK_SERVE_WITH_WORKER": "false",
            "AIZK_BACKUP_ENABLED": "false",
            "AIZK_ARTIFACT_DISPATCH_ENABLED": "false",
            "AIZK_ARTIFACT_INTEGRITY_ENABLED": "false",
            "AIZK_CHUNK_RECOVERY_ENABLED": "false",
            "AIZK_COMMUNITIES_ENABLED": "false",
            "AIZK_DECAY_ENABLED": "false",
            "AIZK_DEDUP_ENABLED": "false",
            "AIZK_INSIGHT_ENABLED": "false",
            "AIZK_PROFILE_PROJECTION_ENABLED": "false",
            "AIZK_PROFILE_REFRESH_ENABLED": "false",
            "AIZK_RAPTOR_ENABLED": "false",
            "AIZK_SESSION_PROMOTE_ENABLED": "false",
            "AIZK_LOG_JSON": "true",
            "AIZK_MONTHLY_TOTAL_OPERATION_LIMIT": "10000",
            "AIZK_MONTHLY_USER_OPERATION_LIMIT": "500",
            "AIZK_MONTHLY_TOTAL_REMEMBER_LIMIT": "1000",
            "AIZK_MONTHLY_USER_REMEMBER_LIMIT": "50",
        }

    def _image_function(
        self,
        construct_id: str,
        *,
        command: str,
        memory: int,
        timeout: int,
        concurrency: int,
        environment: Mapping[str, str],
    ) -> lambda_.DockerImageFunction:
        name = f"{self.config.name}-{construct_id.lower()}"
        log_group = logs.LogGroup(
            self,
            f"{construct_id}Logs",
            log_group_name=f"/aws/lambda/{name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        return lambda_.DockerImageFunction(
            self,
            construct_id,
            function_name=name,
            code=lambda_.DockerImageCode.from_ecr(
                self.repository,
                tag_or_digest=f"sha256:{self.config.image_digest}",
                cmd=[command],
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=memory,
            timeout=Duration.seconds(timeout),
            reserved_concurrent_executions=concurrency,
            environment=dict(environment),
            log_group=log_group,
            logging_format=lambda_.LoggingFormat.JSON,
        )

    def _api(self, public: lambda_.DockerImageFunction) -> gateway.HttpApi:
        integration = HttpLambdaIntegration("McpIntegration", cast("lambda_.IFunction", public))
        api = gateway.HttpApi(
            self,
            "Api",
            api_name=self.config.name,
            default_integration=integration,
            create_default_stage=True,
        )
        authorizer = HttpJwtAuthorizer(
            "LogtoAuthorizer",
            f"{self.config.logto_url.rstrip('/')}/oidc",
            jwt_audience=[f"{self.config.public_url.rstrip('/')}/mcp"],
        )
        api.add_routes(
            path="/mcp",
            methods=[gateway.HttpMethod.ANY],
            integration=integration,
            authorizer=authorizer,
            authorization_scopes=["control"],
        )
        stage = api.default_stage
        if stage is None:
            raise RuntimeError("HTTP API did not create its default stage")
        resource = stage.node.default_child
        if not isinstance(resource, gateway.CfnStage):
            raise TypeError("HTTP API default stage has an unexpected resource")
        resource.default_route_settings = gateway.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=2,
            throttling_rate_limit=2,
        )
        return api

    def _cost_controls(
        self,
        public: lambda_.DockerImageFunction,
        worker: lambda_.DockerImageFunction,
    ) -> None:
        topic = sns.Topic(self, "EmergencyCostStop")
        topic.add_to_resource_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                principals=[cast("iam.IPrincipal", iam.ServicePrincipal("budgets.amazonaws.com"))],
                resources=[topic.topic_arn],
            )
        )
        breaker_log = logs.LogGroup(
            self,
            "CostCircuitBreakerLogs",
            log_group_name=f"/aws/lambda/{self.config.name}-cost-stop",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        breaker = lambda_.Function(
            self,
            "CostCircuitBreaker",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                "import boto3, os\n"
                "def handler(event, context):\n"
                "    client = boto3.client('lambda')\n"
                "    for name in os.environ['FUNCTIONS'].split(','):\n"
                "        client.put_function_concurrency("
                "FunctionName=name, ReservedConcurrentExecutions=0)\n"
                "    return {'stopped': len(os.environ['FUNCTIONS'].split(','))}\n"
            ),
            timeout=Duration.seconds(30),
            memory_size=128,
            function_name=f"{self.config.name}-cost-stop",
            environment={"FUNCTIONS": f"{public.function_name},{worker.function_name}"},
            log_group=breaker_log,
        )
        breaker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:PutFunctionConcurrency"],
                resources=[public.function_arn, worker.function_arn],
            )
        )
        breaker.add_event_source(event_sources.SnsEventSource(cast("sns.ITopic", topic)))
        subscribers = [
            budgets.CfnBudget.SubscriberProperty(
                address=topic.topic_arn,
                subscription_type="SNS",
            )
        ]
        if self.config.billing_email:
            subscribers.append(
                budgets.CfnBudget.SubscriberProperty(
                    address=self.config.billing_email,
                    subscription_type="EMAIL",
                )
            )
        budgets.CfnBudget(
            self,
            "EmergencyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{self.config.name}-emergency",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=self.config.emergency_budget_usd,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers if threshold == 100 else subscribers[-1:],
                )
                for threshold in ((10, 30, 50, 100) if self.config.billing_email else (100,))
            ],
        )

    def _secret(self, parameter_name: str) -> str:
        return SecretValue.ssm_secure(parameter_name).unsafe_unwrap()

    def _outputs(self) -> None:
        CfnOutput(self, "EcrRepositoryUrl", value=self.repository.repository_uri)
