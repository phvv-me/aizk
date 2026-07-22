locals {
  image_uri = "${aws_ecr_repository.aizk.repository_url}@sha256:${var.image_digest}"
  provider_policy = jsonencode({
    provider = {
      zdr             = true
      data_collection = "deny"
    }
  })
  environment = merge(
    {
      AIZK_DATABASE_BACKEND           = "cockroachdb"
      AIZK_DATABASE_URL               = var.cockroach_database_url
      AIZK_ADMIN_DATABASE_URL         = var.cockroach_admin_database_url
      AIZK_DB_SSL_ROOT_CERTIFICATE    = var.cockroach_ca_certificate
      AIZK_AUTO_SETUP                 = "false"
      AIZK_DB_NULL_POOL               = "true"
      AIZK_EMBED_URL                  = "https://openrouter.ai/api/v1"
      AIZK_EMBED_API_KEY              = var.openrouter_api_key
      AIZK_EMBED_MODEL                = var.embed_model
      AIZK_EMBED_DIM                  = "1024"
      AIZK_EMBED_EXTRA_BODY           = local.provider_policy
      AIZK_LLM_URL                    = "https://openrouter.ai/api/v1"
      AIZK_LLM_API_KEY                = var.openrouter_api_key
      AIZK_LLM_MODEL                  = var.llm_model
      AIZK_LLM_EXTRA_BODY             = local.provider_policy
      AIZK_RERANK_ENABLED             = "false"
      AIZK_EXTRACT_BACKEND            = "llm"
      AIZK_EXTRACTION_GATE_ENABLED    = "false"
      AIZK_GRAPH_ENTITY_SEEDING       = "false"
      AIZK_SERVE_WITH_WORKER          = "false"
      AIZK_BACKUP_ENABLED             = "false"
      AIZK_ARTIFACT_DISPATCH_ENABLED  = "false"
      AIZK_ARTIFACT_INTEGRITY_ENABLED = "false"
      AIZK_COMMUNITIES_ENABLED        = "false"
      AIZK_INSIGHT_ENABLED            = "false"
      AIZK_PROFILE_PROJECTION_ENABLED = "false"
      AIZK_PROFILE_REFRESH_ENABLED    = "false"
      AIZK_RAPTOR_ENABLED             = "false"
      AIZK_LOG_JSON                   = "true"
    },
    var.application_environment,
  )
}

resource "aws_ecr_repository" "aizk" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "aizk" {
  repository = aws_ecr_repository.aizk.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the five newest immutable images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = var.deploy_compute ? toset(["mcp", "worker", "setup"]) : toset([])

  name              = "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "mcp" {
  count = var.deploy_compute ? 1 : 0

  function_name = "${var.name}-mcp"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]
  memory_size   = 2048
  timeout       = 900

  image_config {
    command = ["aizk.commands.aws.mcp_handler"]
  }

  environment {
    variables = local.environment
  }

  ephemeral_storage {
    size = 1024
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
  ]

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9a-f]{64}$", var.image_digest))
      error_message = "Push the Lambda image and set image_digest before deploy_compute."
    }
  }
}

resource "aws_lambda_function" "worker" {
  count = var.deploy_compute ? 1 : 0

  function_name                  = "${var.name}-worker"
  role                           = aws_iam_role.lambda.arn
  package_type                   = "Image"
  image_uri                      = local.image_uri
  architectures                  = ["x86_64"]
  memory_size                    = 2048
  timeout                        = 900
  reserved_concurrent_executions = 1

  image_config {
    command = ["aizk.commands.aws.worker_handler"]
  }

  environment {
    variables = local.environment
  }

  ephemeral_storage {
    size = 1024
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
  ]
}

resource "aws_lambda_function" "setup" {
  count = var.deploy_compute ? 1 : 0

  function_name = "${var.name}-setup"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]
  memory_size   = 1024
  timeout       = 900

  image_config {
    command = ["aizk.commands.aws.setup_handler"]
  }

  environment {
    variables = local.environment
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_logs,
  ]
}

resource "aws_lambda_function_url" "mcp" {
  count = var.deploy_compute ? 1 : 0

  function_name      = aws_lambda_function.mcp[0].function_name
  authorization_type = var.function_url_auth_type
  invoke_mode        = "BUFFERED"
}

resource "aws_lambda_permission" "public_url" {
  count = var.deploy_compute && var.function_url_auth_type == "NONE" ? 1 : 0

  statement_id           = "FunctionUrlAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.mcp[0].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "public_invoke" {
  count = var.deploy_compute && var.function_url_auth_type == "NONE" ? 1 : 0

  statement_id             = "FunctionUrlInvokeAllowPublicAccess"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.mcp[0].function_name
  principal                = "*"
  invoked_via_function_url = true
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  count = var.deploy_compute ? 1 : 0

  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  count = var.deploy_compute ? 1 : 0

  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.worker[0].arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  count = var.deploy_compute ? 1 : 0

  name   = "invoke-worker"
  role   = aws_iam_role.scheduler[0].id
  policy = data.aws_iam_policy_document.scheduler_invoke[0].json
}

resource "aws_scheduler_schedule" "worker" {
  count = var.deploy_compute ? 1 : 0

  name                = "${var.name}-worker"
  schedule_expression = var.worker_schedule

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.worker[0].arn
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ source = "aizk.scheduler" })

    retry_policy {
      maximum_event_age_in_seconds = 900
      maximum_retry_attempts       = 1
    }
  }
}

resource "aws_budgets_budget" "monthly" {
  count = var.alert_email == "" ? 0 : 1

  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = toset([10, 50, 100])
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
