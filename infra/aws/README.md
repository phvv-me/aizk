# AWS deployment

This OpenTofu stack keeps the first cloud profile small. CockroachDB Basic remains outside the
AWS account. AWS hosts one immutable Lambda image as three functions. The MCP function handles
stateless HTTP requests, EventBridge Scheduler invokes a bounded queue drain every minute, and
the setup function applies migrations only when an operator invokes it.

The safe bootstrap creates the ECR repository without compute.

```sh
tofu init
tofu apply
```

Build the `lambda` target for one architecture, push it to the output repository, and record its
digest. AWS requires a single architecture image and a build without provenance metadata.

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

Set `deploy_compute` to `true`, set the digest without `sha256`, provide the CockroachDB URLs and
OpenRouter key through an uncommitted variable file, then apply again. Invoke the setup function
before enabling traffic.

```sh
aws lambda invoke --function-name aizk-cockroachdb-setup /tmp/aizk-setup.json
```

`AWS_IAM` is the Function URL default. A public `NONE` URL is appropriate only after the extra
application environment contains complete AIZK Logto settings and a stable public MCP URL. The
stack installs both permissions AWS requires for public Function URLs as of October 2025.

Sensitive variables are still present in OpenTofu state. Store production state in an encrypted,
access-controlled remote backend. The example file contains placeholders only.

The model lanes require OpenRouter zero data retention and deny provider data collection. The
reranker remains disabled because the July 2026 live check found no eligible zero data retention
reranking endpoint.

Provision the external CockroachDB Basic cluster through the pinned `ccloud` profile documented
in `src/deploy/cockroachdb/README.md`. That CLI is the second contest tool beside Distributed
Vector Indexing. No CockroachDB cluster is created by this AWS stack.
