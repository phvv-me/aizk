mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_resource "aws_iam_role" {
    defaults = {
      arn = "arn:aws:iam::123456789012:role/aizk-test"
      id  = "aizk-test"
    }
  }

  mock_resource "aws_ecr_repository" {
    defaults = {
      repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/aizk-test"
    }
  }

  mock_resource "aws_lambda_function" {
    defaults = {
      arn = "arn:aws:lambda:us-east-1:123456789012:function:aizk-test"
    }
  }
}

run "bootstrap" {
  command = plan

  assert {
    condition     = aws_ecr_repository.aizk.image_tag_mutability == "IMMUTABLE"
    error_message = "The image repository must reject mutable tags."
  }

  assert {
    condition     = length(aws_lambda_function.mcp) == 0
    error_message = "Bootstrap must not create compute before an image is pushed."
  }
}

run "runtime" {
  command = plan

  variables {
    deploy_compute               = true
    image_digest                 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    cockroach_database_url       = "cockroachdb+asyncpg://app@cluster/aizk"
    cockroach_admin_database_url = "cockroachdb+asyncpg://admin@cluster/aizk"
    openrouter_api_key           = "synthetic-test-key"
  }

  assert {
    condition     = aws_lambda_function.worker[0].reserved_concurrent_executions == 1
    error_message = "Only one serverless worker may drain the queue concurrently."
  }

  assert {
    condition     = aws_lambda_function.mcp[0].environment[0].variables["AIZK_RERANK_ENABLED"] == "false"
    error_message = "The cloud profile must not use a reranker without a zero data retention endpoint."
  }
}
