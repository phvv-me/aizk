# AWS deployment

This AWS CDK stack keeps the invitation-only alpha small and bounded. CockroachDB Basic remains
outside the AWS account. AWS hosts one immutable image as an MCP Lambda, a queue worker, and an
operator-invoked setup function. A fourth small function stops the public and worker functions if
the emergency budget is reached.

The initial deployment creates only an ECR repository. It does not create compute or contact
CockroachDB Cloud.

```sh
chefe run infra-check
chefe run infra-bootstrap
chefe run infra-deploy
```

Read the `EcrRepositoryUrl` output, authenticate Docker to ECR, then build and push the Lambda
target. Lambda requires one architecture and an image without provenance metadata.

```sh
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --target lambda \
  --build-context patos=../../patos \
  --build-context rls=../../rls \
  --build-context mainboard=../../mainboard \
  -f src/deploy/Dockerfile \
  -t "$ECR_REPOSITORY:aizk" \
  --push \
  .
```

Resolve the pushed image digest and store the five secrets as SecureString parameters. The values
must not be placed in source files or CDK environment variables.

```sh
aws ssm put-parameter --type SecureString --name /aizk/cockroachdb/database-url --value 'REPLACE'
aws ssm put-parameter --type SecureString --name /aizk/cockroachdb/admin-database-url --value 'REPLACE'
aws ssm put-parameter --type SecureString --name /aizk/openrouter/api-key --value 'REPLACE'
aws ssm put-parameter --type SecureString --name /aizk/logto/management-client-secret --value 'REPLACE'
aws ssm put-parameter --type SecureString --name /aizk/logto/mcp-client-secret --value 'REPLACE'
```

Set the nonsecret deployment inputs in the shell that runs CDK.

```sh
export AIZK_AWS_DEPLOY_COMPUTE=true
export AIZK_AWS_IMAGE_DIGEST=REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS
export AIZK_AWS_PUBLIC_URL=https://memory.example.com
export AIZK_AWS_LOGTO_URL=https://tenant.logto.app
export AIZK_AWS_LOGTO_CLIENT_ID=REPLACE
export AIZK_AWS_OAUTH_CLIENT_ID=REPLACE
export AIZK_AWS_BILLING_EMAIL=owner@example.com
chefe run infra-check
chefe run infra-deploy
```

Invoke the setup function once after every migration-bearing deployment. An empty JSON object is
enough.

```sh
aws lambda invoke --function-name aizk-cockroachdb-setup --payload '{}' /tmp/aizk-setup.json
```

The MCP function has 1 GiB of memory, a 25 second timeout, and reserved concurrency of two. The
worker has 2 GiB, a 14 minute timeout, a queue batch of eight, and reserved concurrency of one.
Every durable `remember` write wakes the worker asynchronously. EventBridge retries recovery every
15 minutes. Logs expire after seven days.

The alpha accepts text memories only. File and preserved URL ingestion stay disabled until the
artifact scanner and object upload boundary have a complete serverless implementation. This avoids
paying for unused S3 infrastructure or claiming a path that is not operational.

The deployment is sized for 25 accounts invited through Logto. Persistent database counters admit
at most 10,000 total operations, 1,000 total remembers, 500 operations per account, and 50 remembers
per account each calendar month. API Gateway also limits the edge to two requests per second with a
burst of two.

With the default ten dollar monthly emergency budget and a billing email, AWS sends notices at one,
three, five, and ten dollars. The ten dollar notification also invokes the cost circuit breaker,
which sets MCP and worker reserved concurrency to zero. After investigating the charge, restore the
limits explicitly.

```sh
aws lambda put-function-concurrency --function-name aizk-cockroachdb-mcp --reserved-concurrent-executions 2
aws lambda put-function-concurrency --function-name aizk-cockroachdb-worker --reserved-concurrent-executions 1
```

The design has no VPC, NAT gateway, load balancer, EC2 instance, Fargate service, RDS database, or
local model host. The main variable costs are OpenRouter calls and the external CockroachDB plan.
Reserved concurrency, monthly database quotas, short log retention, the ECR lifecycle, and the
emergency breaker bound the AWS side. Confirm current AWS pricing before the real deployment.

The model lanes require OpenRouter zero data retention and deny provider data collection. Reranking
remains disabled because the July 2026 live check found no eligible zero data retention reranking
endpoint.

Provision the external CockroachDB Basic cluster through the pinned `ccloud` profile documented in
`src/deploy/cockroachdb/README.md`. That CLI is the second contest tool beside Distributed Vector
Indexing. This CDK stack never creates the database cluster.

Run a load probe after the first deployment. Keep API Gateway only if MCP request latency stays
below its 25 second integration limit at the 95th percentile. The measured local Lambda image cold
start was about nine seconds and a warm MCP initialize request took a few milliseconds on July 23,
2026. Production network and model latency still require a real cloud measurement.
