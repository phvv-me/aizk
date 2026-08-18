# AWS deployment

This CDK stack deploys an isolated AIZK staging profile in AWS Singapore. CockroachDB Cloud
remains external to the AWS account. One Logto protected Lambda Function URL serves the product
site, documentation, browser application, HTTP API, and modern MCP endpoint. It
never reuses another AIZK deployment or its identity boundary.

## Live shape

- One immutable ECR repository retaining the current and previous API and web images
- One private encrypted S3 bucket for original artifacts
- One MCP Lambda and one worker Lambda from the same API image digest
- One internal web Lambda running the same SvelteKit application as the regular deployment
- One public Lambda Function URL with the site at `/`, docs at `/docs/`, UI at `/app/dashboard`, and MCP at `/mcp`
- One EventBridge Scheduler recovery call every fifteen minutes and one warm call every five minutes
- Six SSM SecureString parameters for database, model, Logto, and web session credentials
- Seven-day CloudWatch log retention
- One ten dollar monthly gross-cost budget that excludes credits and refunds

There is no API Gateway, SNS topic, custom alarm, VPC, NAT gateway, load balancer, EC2 service,
setup Lambda, or cost-breaker Lambda. The regional account quota currently bounds all Lambda
functions to ten concurrent executions. AWS will not permit reserved concurrency until that quota
is raised above the ten executions it requires to remain unreserved.

## AWS login

The pinned AWS CLI image stores the AIZK staging login outside the repository.

```sh
AWS_PROFILE=aizk sh infra/aws/aws-cli.sh login --remote
AWS_PROFILE=aizk sh infra/aws/aws-cli.sh sts get-caller-identity --region ap-southeast-1
```

CDK currently needs exported short-lived credentials because its Node SDK does not consume the new
AWS CLI login session directly. Export them only into the deployment shell and never print them.

## Deploy

The first deployment creates only the ECR repository.

```sh
CDK_OUTDIR=cdk.out uv run --no-sync python -m infra.aws.app
uv run --no-sync python -m pytest tests/deploy --no-cov
npx --yes aws-cdk@2.1136.0 bootstrap --app 'uv run --no-sync python -m infra.aws.app'
npx --yes aws-cdk@2.1136.0 deploy --app 'uv run --no-sync python -m infra.aws.app'
```

Verify that the bundled public client is Native, distinct from the Management API client and owns
every exact agent callback before building anything.

```sh
uv run --no-sync python -m infra.aws.logto verify
```

Build both Lambda targets for one architecture without provenance metadata. Push an immutable tag
for each target and resolve both digests from ECR. The API image serves the public site, docs, API,
MCP, and worker. The web image runs the production SvelteKit application behind the public Lambda.

```sh
docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --target lambda \
  --build-arg AIZK_DOCS_SITE_URL="$AIZK_AWS_PUBLIC_URL" \
  --build-arg AIZK_DOCS_MCP_CLIENT_ID="$(jq -r '.mcpServers.aizk.oauth.client_id' plugins/aizk/.codex-plugin/plugin.json)" \
  --build-context sqlalchemy=../sqlalchemy \
  -f src/deploy/Dockerfile \
  -t "$ECR_REPOSITORY:$IMMUTABLE_TAG" \
  --push \
  .

docker buildx build \
  --platform linux/amd64 \
  --provenance=false \
  --target lambda-web \
  --build-context sqlalchemy=../sqlalchemy \
  -f src/deploy/Dockerfile \
  -t "$ECR_REPOSITORY:$WEB_IMMUTABLE_TAG" \
  --push \
  .
```

Store the application database URL, migration database URL, demonstration OpenRouter key, Logto
secrets, and web session key in these SecureString parameters. Secret values must not enter source
files, CDK variables, shell history, or CloudFormation. Each Lambda role reads only its assigned
parameters during cold start.

```text
/craizk/staging/database-url
/craizk/staging/admin-database-url
/craizk/staging/openrouter-api-key
/craizk/staging/logto-management-client-secret
/craizk/staging/web-client-secret
/craizk/staging/web-session-secret
```

The URLs use `cockroachdb+asyncpg`, port 26257, database `craizk_staging`, and
`sslmode=verify-full`. The MCP Lambda receives only the restricted application URL. The worker also
receives the migration URL.

Deploy compute with the lowercase digest and no `sha256` prefix.

```sh
export AIZK_AWS_DEPLOY_COMPUTE=true
export AIZK_AWS_REGION=ap-southeast-1
export AIZK_AWS_IMAGE_DIGEST=REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS
export AIZK_AWS_WEB_IMAGE_DIGEST=REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS
export AIZK_AWS_MONTHLY_BUDGET_USD=10
CDK_OUTDIR=cdk.out uv run --no-sync python -m infra.aws.app
uv run --no-sync python -m pytest tests/deploy --no-cov
uv run --no-sync python -m infra.aws.logto verify
npx --yes aws-cdk@2.1136.0 deploy --app 'uv run --no-sync python -m infra.aws.app'
```

`AIZK_AWS_PUBLIC_URL` is the Function URL origin with no path. AIZK appends `/mcp` when it
constructs its OAuth resource identifier. Supplying an URL that already ends in `/mcp` is rejected
because it would create an invalid `/mcp/mcp` audience.

The Lambda profile keeps one connection per warm execution environment by default. Set
`AIZK_AWS_DB_NULL_POOL=true` only as a diagnostic fallback when a database proxy or connection
limit requires every transaction to open a fresh connection. Reusing the connection avoids paying
CockroachDB Cloud TLS setup on every `find` while transaction-local authority prevents caller state
from leaking through the pool.

An optional `AIZK_AWS_BILLING_EMAIL` adds direct budget notices at ten, thirty, fifty, and one
hundred percent. The budget exists without an email, but it cannot notify anyone.

## Artifact storage

The staging profile accepts each file through a short-lived single-use capability URL. The MCP
`keep` tool declares the filename, media type, exact byte size, and SHA-256 before the caller sends
any bytes. Intake verifies the declaration again from the received bytes before S3 persistence.

The deployed limits are 4 MiB for one file and 1 GiB of original bytes for one user. The cumulative
quota is committed under a caller-specific CockroachDB lock, so concurrent uploads cannot race
past it. The allowlist covers PDF, supported image formats, OOXML, EPUB, UTF-8 text, Markdown,
HTML, XML, JSON, and delimited text. Executables and unknown or mismatched formats are refused.

The bucket blocks public access and requires TLS. It uses bucket-owner enforcement with SSE-S3.
Each Lambda can access only the `objects/*` prefix. Original objects use random immutable
keys. OAuth state stays with each client and Logto, so Lambda needs no shared authorization store.

The invited hackathon profile deliberately disables malware scanning to keep the serverless demo
small and inexpensive. Do not open registration or accept arbitrary public uploads in this mode.
A production public deployment must restore a fail-closed scanner or a quarantine workflow before
promoting any object into the readable prefix.

## Migrate and verify

Database setup is an explicit event on the worker function. Missing or unknown event kinds fail
closed.

```sh
AWS_PROFILE=aizk sh infra/aws/aws-cli.sh lambda invoke \
  --region ap-southeast-1 \
  --function-name craizk-staging-worker \
  --cli-binary-format raw-in-base64-out \
  --payload '{"kind":"setup"}' \
  /dev/stdout
```

Normal recovery uses `{"kind":"worker"}`. Every successful `keep` also invokes the worker
asynchronously. Scheduler polling remains necessary for delayed jobs, usage records admitted after
a response, failed wake hints, and stale leases.

The release gate verifies modern discovery, all five tools, private S3 upload, worker extraction,
identity resolution, and grounded retrieval. EventBridge Scheduler builds the cached MCP
application every five minutes to reduce cold starts. [The performance study](../../hackathon/PERFORMANCE.md)
explains the operation map and retrieval tradeoffs.

## Logto authentication

The deployed Function URL uses public AWS invocation because AIZK verifies Logto tokens directly.
The stack permits that mode only when the Logto issuer, Management API client, SPA client and
public AIZK URL are complete. Partial authentication configuration fails synthesis.

The client ID embedded in the documentation must belong to a separate Native public application.
Never use the Management API machine-to-machine client. Register
`http://localhost:8912/callback` for Claude Code and `http://127.0.0.1:8912/callback` for clients
that use the numeric loopback address. Record that Native application ID in both bundled plugin
manifests. The image build reads it from the Codex manifest and verifies that the Claude manifest
and generated setup guide agree.

The browser application uses its own Traditional Web client. Register the exact callback at
`$AIZK_AWS_PUBLIC_URL/auth/sign-in-callback`, store its application secret in
`/craizk/staging/web-client-secret`, and set its public client ID as `AIZK_AWS_WEB_CLIENT_ID`.
Generate an independent session key of at least 32 bytes for
`/craizk/staging/web-session-secret`. The public Lambda forwards only browser application paths to
the internal web Lambda. The browser exchanges its user token with the public API at the same
origin.

The staging MCP URL is `$AIZK_AWS_PUBLIC_URL/mcp`.
