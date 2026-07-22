output "ecr_repository_url" {
  description = "Push the Lambda image here before enabling compute."
  value       = aws_ecr_repository.aizk.repository_url
}

output "mcp_function_url" {
  description = "MCP endpoint base URL when compute is enabled."
  value       = var.deploy_compute ? aws_lambda_function_url.mcp[0].function_url : null
}

output "setup_function_name" {
  description = "Invoke this function after each migration-bearing image deployment."
  value       = var.deploy_compute ? aws_lambda_function.setup[0].function_name : null
}
