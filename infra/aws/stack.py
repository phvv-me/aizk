import json
from collections.abc import Mapping
from typing import cast

from aws_cdk import (
    CfnOutput,
    Duration,
    Environment,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from .config import DeploymentConfig


class AizkAwsStack(Stack):
    """Deploy one bounded Lambda staging service around CockroachDB Cloud."""

    def __init__(self, scope: Construct, construct_id: str, config: DeploymentConfig) -> None:
        super().__init__(scope, construct_id, env=Environment(region=config.region))
        self.config = config
        self.repository = self._repository()
        self.artifact_bucket = self._artifact_bucket()
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
            description="Keep the current and previous API and web images",
            max_image_count=4,
            rule_priority=1,
        )
        return repository

    def _artifact_bucket(self) -> s3.Bucket:
        bucket = s3.Bucket(
            self,
            "ArtifactBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        return bucket

    def _runtime(self) -> None:
        shared = self._shared_environment()
        web = (
            self._image_function(
                "Web",
                command=None,
                image_digest=self.config.web_image_digest,
                memory=1024,
                timeout=60,
                environment=self._web_environment(),
                parameters=self._web_parameters(),
            )
            if self.config.logto_enabled
            else None
        )
        worker = self._image_function(
            "Worker",
            command="aizk.commands.aws_worker.worker_handler",
            image_digest=self.config.image_digest,
            memory=2048,
            timeout=840,
            environment=shared
            | {
                "AIZK_QUEUE_BATCH_SIZE": "8",
            },
            parameters=self._shared_parameters()
            | {"AIZK_ADMIN_DATABASE_URL": self.config.admin_database_url_parameter},
        )
        public_environment = shared | {"AIZK_WORKER_FUNCTION_NAME": worker.function_name}
        if web is not None:
            public_environment["AIZK_WEB_FUNCTION_NAME"] = web.function_name
        if self.config.logto_enabled:
            public_environment |= self._logto_environment()
        public = self._image_function(
            "Mcp",
            command="aizk.commands.aws_mcp.mcp_handler",
            image_digest=self.config.image_digest,
            memory=2048,
            timeout=60,
            environment=public_environment,
            parameters=self._shared_parameters()
            | (self._logto_parameters() if self.config.logto_enabled else {}),
        )
        self._grant_artifacts(worker)
        self._grant_artifacts(public)
        worker.grant_invoke(public)
        if web is not None:
            web.grant_invoke(public)
        self._schedules(worker, public)
        self._budget()
        endpoint = public.add_function_url(
            auth_type=(
                lambda_.FunctionUrlAuthType.NONE
                if self.config.logto_enabled
                else lambda_.FunctionUrlAuthType.AWS_IAM
            ),
            invoke_mode=lambda_.InvokeMode.BUFFERED,
        )
        CfnOutput(self, "McpUrl", value=f"{endpoint.url}mcp")
        CfnOutput(self, "WorkerFunctionName", value=worker.function_name)
        if web is not None:
            CfnOutput(self, "WebFunctionName", value=web.function_name)

    def _grant_artifacts(self, function: lambda_.DockerImageFunction) -> None:
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetBucketLocation", "s3:ListBucket"],
                resources=[self.artifact_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": ["objects/*"]}},
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:AbortMultipartUpload",
                    "s3:DeleteObject",
                    "s3:GetObject",
                    "s3:ListMultipartUploadParts",
                    "s3:PutObject",
                ],
                resources=[self.artifact_bucket.arn_for_objects("objects/*")],
            )
        )

    def _shared_environment(self) -> dict[str, str]:
        return {
            "FASTMCP_HOME": "/tmp/fastmcp",
            "AIZK_DATABASE_BACKEND": "cockroachdb",
            "AIZK_AUTO_SETUP": "false",
            "AIZK_DB_NULL_POOL": str(self.config.db_null_pool).lower(),
            "AIZK_DB_POOL_SIZE": "1",
            "AIZK_DB_POOL_MAX_OVERFLOW": "0",
            "AIZK_EMBED_URL": "https://openrouter.ai/api/v1",
            "AIZK_EMBED_MODEL": "qwen/qwen3-embedding-8b",
            "AIZK_EMBED_DIM": "1024",
            "AIZK_EMBED_EXTRA_BODY": (
                '{"provider":{"order":["DeepInfra"],"allow_fallbacks":false}}'
            ),
            "AIZK_LLM_URL": "https://openrouter.ai/api/v1",
            "AIZK_LLM_MODEL": "deepseek/deepseek-v4-flash-0731",
            "AIZK_LLM_EXTRA_BODY": (
                '{"reasoning":{"enabled":false},"session_id":"aizk-extractor"}'
            ),
            "AIZK_OBJECT_STORE_BUCKET": self.artifact_bucket.bucket_name,
            "AIZK_OBJECT_STORE_AWS_NATIVE": "true",
            "AIZK_OBJECT_STORE_UPLOAD_BYTE_LIMIT": "4194304",
            "AIZK_OBJECT_STORE_USER_BYTE_LIMIT": "1073741824",
            "AIZK_ARTIFACT_INGEST_ENABLED": "true",
            "AIZK_ARTIFACT_MALWARE_SCAN_ENABLED": "false",
            "AIZK_API_PUBLIC_URL": self.config.public_url or "",
            "AIZK_SPA_CLIENT_ID": self.config.spa_client_id,
            "AIZK_STATIC_ROOT": "/var/task/static",
            "AIZK_CAPTION_ENABLED": "true",
            "AIZK_CAPTION_PRIMARY_MODEL": "google/gemini-2.5-flash-lite",
            "AIZK_CAPTION_FALLBACK_MODELS": '["qwen/qwen3-vl-8b-instruct"]',
            "AIZK_RERANK_ENABLED": "false",
            "AIZK_EXTRACT_BACKEND": "llm",
            "AIZK_EXTRACTION_GATE_ENABLED": "false",
            "AIZK_GRAPH_ENTITY_SEEDING": "false",
            "AIZK_SERVE_WITH_WORKER": "false",
            "AIZK_BACKUP_ENABLED": "false",
            "AIZK_ARTIFACT_DISPATCH_ENABLED": "false",
            "AIZK_ARTIFACT_INTEGRITY_ENABLED": "false",
            "AIZK_CHUNK_RECOVERY_ENABLED": "false",
            "AIZK_COMMUNITIES_ENABLED": "true",
            "AIZK_DECAY_ENABLED": "false",
            "AIZK_DEDUP_ENABLED": "false",
            "AIZK_INSIGHT_ENABLED": "false",
            "AIZK_PROFILE_PROJECTION_ENABLED": "false",
            "AIZK_PROFILE_REFRESH_ENABLED": "false",
            "AIZK_RAPTOR_ENABLED": "false",
            "AIZK_SESSION_PROMOTE_ENABLED": "false",
            "AIZK_LOG_JSON": "true",
            "AIZK_PROFILING": "true",
            "AIZK_FIND_ACCESS_RECORDING_ENABLED": str(
                self.config.find_access_recording_enabled
            ).lower(),
            "AIZK_FIND_COMMUNITIES_ENABLED": str(self.config.find_communities_enabled).lower(),
            "AIZK_FIND_ENTITY_CATALOG_ENABLED": str(
                self.config.find_entity_catalog_enabled
            ).lower(),
            "AIZK_FIND_GRAPH_EXPANSION_ENABLED": str(
                self.config.find_graph_expansion_enabled
            ).lower(),
            "AIZK_FIND_PROFILES_ENABLED": str(self.config.find_profiles_enabled).lower(),
            "AIZK_FIND_RAPTOR_ENABLED": str(self.config.find_raptor_enabled).lower(),
            "AIZK_FIND_SOURCES_FIRST": str(self.config.find_sources_first).lower(),
            "AIZK_MONTHLY_TOTAL_OPERATION_LIMIT": "10000",
            "AIZK_MONTHLY_USER_OPERATION_LIMIT": "500",
            "AIZK_MONTHLY_TOTAL_KEEP_LIMIT": "1000",
            "AIZK_MONTHLY_USER_KEEP_LIMIT": "50",
        }

    def _logto_environment(self) -> dict[str, str]:
        if self.config.public_url is None or self.config.logto_url is None:
            raise RuntimeError("validated Logto configuration is incomplete")
        return {
            "AIZK_LOGTO_URL": self.config.logto_url,
            "AIZK_LOGTO_MANAGEMENT_RESOURCE": f"{self.config.logto_url.rstrip('/')}/api",
            "AIZK_LOGTO_CLIENT_ID": self.config.logto_management_client_id,
            "AIZK_MCP_PUBLIC_URL": self.config.public_url,
            "AIZK_REQUIRE_AUTH": "true",
        }

    def _shared_parameters(self) -> dict[str, str]:
        return {
            "AIZK_DATABASE_URL": self.config.database_url_parameter,
            "AIZK_CAPTION_API_KEY": self.config.openrouter_key_parameter,
            "AIZK_EMBED_API_KEY": self.config.openrouter_key_parameter,
            "AIZK_LLM_API_KEY": self.config.openrouter_key_parameter,
        }

    def _logto_parameters(self) -> dict[str, str]:
        return {
            "AIZK_LOGTO_CLIENT_SECRET": self.config.logto_client_secret_parameter,
        }

    def _web_environment(self) -> dict[str, str]:
        if self.config.public_url is None or self.config.logto_url is None:
            raise RuntimeError("validated web configuration is incomplete")
        return {
            "AIZK_LOGTO_URL": self.config.logto_url,
            "AIZK_WEB_CLIENT_ID": self.config.web_client_id,
            "AIZK_WEB_PUBLIC_URL": self.config.public_url,
            "AIZK_WEB_API_URL": self.config.public_url,
            "AIZK_MCP_PUBLIC_URL": self.config.public_url,
        }

    def _web_parameters(self) -> dict[str, str]:
        return {
            "AIZK_WEB_CLIENT_SECRET": self.config.web_client_secret_parameter,
            "AIZK_WEB_SESSION_SECRET": self.config.web_session_secret_parameter,
        }

    def _image_function(
        self,
        construct_id: str,
        *,
        command: str | None,
        image_digest: str,
        memory: int,
        timeout: int,
        environment: Mapping[str, str],
        parameters: Mapping[str, str],
    ) -> lambda_.DockerImageFunction:
        name = f"{self.config.name}-{construct_id.lower()}"
        log_group = logs.LogGroup(
            self,
            f"{construct_id}Logs",
            log_group_name=f"/aws/lambda/{name}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        function = lambda_.DockerImageFunction(
            self,
            construct_id,
            function_name=name,
            code=lambda_.DockerImageCode.from_ecr(
                self.repository,
                tag_or_digest=f"sha256:{image_digest}",
                cmd=[command] if command is not None else None,
            ),
            architecture=lambda_.Architecture.X86_64,
            memory_size=memory,
            timeout=Duration.seconds(timeout),
            environment=dict(environment)
            | {
                "AIZK_AWS_PARAMETER_ENV": json.dumps(
                    parameters,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            },
            log_group=log_group,
            logging_format=lambda_.LoggingFormat.JSON,
        )
        parameter_arns = [
            self.format_arn(
                service="ssm",
                resource="parameter",
                resource_name=name.removeprefix("/"),
            )
            for name in sorted(set(parameters.values()))
        ]
        function.add_to_role_policy(
            iam.PolicyStatement(actions=["ssm:GetParameters"], resources=parameter_arns)
        )
        return function

    def _schedules(
        self,
        worker: lambda_.DockerImageFunction,
        public: lambda_.DockerImageFunction,
    ) -> None:
        role = iam.Role(
            self,
            "WorkerSchedulerRole",
            assumed_by=cast(
                iam.IPrincipal,
                iam.ServicePrincipal("scheduler.amazonaws.com"),
            ),
        )
        worker.grant_invoke(role)
        public.grant_invoke(role)
        scheduler.CfnSchedule(
            self,
            "WorkerRecovery",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            name=f"{self.config.name}-worker-recovery",
            schedule_expression="rate(15 minutes)",
            state="ENABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=worker.function_arn,
                role_arn=role.role_arn,
                input='{"kind":"worker"}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=900,
                    maximum_retry_attempts=2,
                ),
            ),
        )
        scheduler.CfnSchedule(
            self,
            "McpWarm",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            name=f"{self.config.name}-mcp-warm",
            schedule_expression="rate(5 minutes)",
            state="ENABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=public.function_arn,
                role_arn=role.role_arn,
                input='{"kind":"warm"}',
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(
                    maximum_event_age_in_seconds=300,
                    maximum_retry_attempts=0,
                ),
            ),
        )

    def _budget(self) -> None:
        subscribers = (
            [
                budgets.CfnBudget.SubscriberProperty(
                    address=self.config.billing_email,
                    subscription_type="EMAIL",
                )
            ]
            if self.config.billing_email
            else []
        )
        notifications = (
            [
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=threshold,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                )
                for threshold in (10, 30, 50, 100)
            ]
            if subscribers
            else None
        )
        budgets.CfnBudget(
            self,
            "MonthlyBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=f"{self.config.name}-monthly",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=self.config.monthly_budget_usd,
                    unit="USD",
                ),
                cost_types=budgets.CfnBudget.CostTypesProperty(
                    include_credit=False,
                    include_refund=False,
                    use_blended=False,
                ),
            ),
            notifications_with_subscribers=notifications,
        )

    def _outputs(self) -> None:
        CfnOutput(self, "EcrRepositoryUrl", value=self.repository.repository_uri)
        CfnOutput(self, "ArtifactBucketName", value=self.artifact_bucket.bucket_name)
