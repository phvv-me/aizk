terraform {
  required_version = ">= 1.12.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.55"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = var.name
      ManagedBy   = "OpenTofu"
      Project     = "AIZK CockroachDB hackathon"
    }
  }
}
