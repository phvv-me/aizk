variable "name" {
  description = "Prefix shared by the isolated AWS resources."
  type        = string
  default     = "aizk-cockroachdb"
}

variable "aws_region" {
  description = "AWS region. us-east-1 is the default low-cost Lambda region."
  type        = string
  default     = "us-east-1"
}

variable "deploy_compute" {
  description = "Create Lambda resources after the immutable image digest is in ECR."
  type        = bool
  default     = false
}

variable "image_digest" {
  description = "ECR image digest without the sha256 prefix."
  type        = string
  default     = ""

  validation {
    condition     = var.image_digest == "" || can(regex("^[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be empty or a 64-character lowercase SHA-256 digest."
  }
}

variable "cockroach_database_url" {
  description = "Restricted CockroachDB SQLAlchemy URL for request traffic."
  type        = string
  sensitive   = true
  default     = ""
}

variable "cockroach_admin_database_url" {
  description = "Owner CockroachDB SQLAlchemy URL for setup and maintenance."
  type        = string
  sensitive   = true
  default     = ""
}

variable "cockroach_ca_certificate" {
  description = "Optional PEM root certificate when the cluster is not signed by a system CA."
  type        = string
  default     = ""
}

variable "openrouter_api_key" {
  description = "OpenRouter key shared by the zero data retention model lanes."
  type        = string
  sensitive   = true
  default     = ""
}

variable "embed_model" {
  description = "OpenRouter embedding model slug."
  type        = string
  default     = "qwen/qwen3-embedding-8b"
}

variable "llm_model" {
  description = "OpenRouter extraction model slug."
  type        = string
  default     = "deepseek/deepseek-v4-flash"
}

variable "function_url_auth_type" {
  description = "Lambda Function URL authorization. AWS_IAM is the safe default."
  type        = string
  default     = "AWS_IAM"

  validation {
    condition     = contains(["AWS_IAM", "NONE"], var.function_url_auth_type)
    error_message = "function_url_auth_type must be AWS_IAM or NONE."
  }
}

variable "application_environment" {
  description = "Additional AIZK settings such as complete Logto OAuth configuration."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "worker_schedule" {
  description = "EventBridge Scheduler expression for bounded queue drains."
  type        = string
  default     = "rate(1 minute)"
}

variable "alert_email" {
  description = "Optional email for AWS Budget notifications."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget whose notices fire near 1, 5, and 10 USD by default."
  type        = number
  default     = 10
}
