# Stage 2 — private Cloudflare R2 readiness

Scope: code inspection, local storage inspection by file metadata only, local PostgreSQL disposable-schema integration tests, and boto3 Stubber tests with fixture credentials. No actual R2 requests/account login/bucket creation, production credentials, production DB connection/migration, environment file edits or file migration.

## Implementation

`create_app()` selects one backend from `STORAGE_BACKEND` and passes it into AgentManager/EvaluationManager. LOCAL remains the default. S3CompatibleStorageBackend uses the installed/pinned boto3 dependency (1.43.93) and explicit server-only S3 credentials. Endpoint and region come from Settings. R2 region is `auto`; no production us-east-1 override. R2 endpoint must be explicitly supplied (omitting it selects SDK AWS behavior, not R2). HTTPS origin validation now rejects embedded credentials, query/fragment and bucket paths.

AgentManager computes SHA-256, writes the agent object, then commits its immutable version record. EvaluationManager authenticates/locks Worker, locks and validates job ownership/token/lease, checks an existing Evaluation, writes six artifacts, rechecks lease expiry and commits result rows. The row locks fence concurrent submission; the second request sees the committed Evaluation. There is no need to re-query idempotency after storage while holding the same locks; UNIQUE(evaluations.job_id) remains a final guard.

Agent endpoint: GET /api/v1/workers/{worker_id}/jobs/{job_id}/agent. Worker bearer + job ownership/lease checks precede backend access. DB read locks are released before streaming. Artifact endpoint: GET /api/v1/evaluations/{evaluation_id}/artifacts/{name}; requires admin bearer and one of six allowed filenames. Server proxy streams bytes; no R2 URL/redirect/credentials are returned. Missing object maps to 404, backend operation failure to sanitized 503. If a GET stream fails after headers start, a truncated response is possible; the Worker SHA-256/size verification prevents execution of a truncated/wrong agent.

## Logical keys

- agents/{agent_id}/{agent_version_id}/agent.py
- evaluations/{evaluation_id}/{problem_id}/stdout.log
- evaluations/{evaluation_id}/{problem_id}/stderr.log
- evaluations/{evaluation_id}/{problem_id}/agent_output.log
- evaluations/{evaluation_id}/{problem_id}/patch.diff
- evaluations/{evaluation_id}/{problem_id}/test_result.json
- evaluations/{evaluation_id}/{problem_id}/analysis.md

The last segment of the evaluation directory is Problem ID, NOT ProblemVersion ID. DB stores the agent key, evaluation directory prefix, log/patch keys, and test/score/metrics data. No bucket/public URLs. Bucket name/endpoint are server configuration only.

## Minimal additions in this step

- storage.py: SHA-256 user metadata, Content-Type and Content-MD5 on PUT; HEAD verifies length and SHA-256 metadata before write success; backend head/delete methods; local head/delete equivalents. Conditional IfNoneMatch=* preserved. Delete is an internal maintenance capability, not an HTTP endpoint or automatic garbage collector.
- config.py: HTTPS origin validation; nonempty configurable region.
- tests/test_storage.py: endpoint/region, metadata mismatch, local/S3 deletion, HEAD missing, sanitized delete failure, missing artifact, unauthorized proxy access, credential/log non-exposure, upload/DB-commit failure and retry tests.
- docs/r2-readiness.md: this report.

No schema/migration/model/service/API route changes. No new dependency was needed.

## Integrity and compatibility

A write performs one PutObject + one HeadObject. PUT carries Content-MD5 (R2-supported transport checksum), SHA-256 metadata and length-preserving bytes. HEAD checks size and uploaded SHA-256 metadata. This is NOT independent whole-object GET/SHA-256 readback; metadata is uploader supplied. Agent download still checks bytes against the independently stored immutable AgentVersion SHA-256 and size. ETag is never treated as SHA-256; tests deliberately return a non-SHA ETag.

Optional automatic SDK checksum algorithms are set to when_required for request/response compatibility; explicit Content-MD5 remains sent. Do not replace this with unsupported full-object SHA256 headers or add AWS-only ACL/KMS/PublicAccessBlock calls to R2. The selected R2 API docs list conditional PUT, Content-MD5, HEAD, GET and DELETE support.

Result artifacts: 6 PUT + 6 HEAD per successful first result, still one Worker result POST. Agent upload: 1 PUT + 1 HEAD; download: 1 GET. Repeated accepted result: no additional artifact writes/HEAD checks. SDK retry attempts, replay after uncommitted failure and user artifact GETs add operations. Redis/SQL application paths are unchanged by these storage checks. At 600/3000/6000 results, artifact PUT totals are 3600/18000/36000 and HEAD totals are the same; these are R2 operations, not Upstash commands.

## Consistency limitations

Partial storage failure or DB commit failure can leave orphan objects. No distributed storage/DB transaction is claimed. In injected upload and DB commit failures, no Evaluation was committed and the job remained CLAIMED. A subsequent valid submission completed; accepted-result replay added no writes. Retry uses a new evaluation UUID/key prefix if the previous transaction never committed. If commit succeeded but response was lost, the existing Evaluation is returned. Pending retries remain lease bounded; expired attempts are rejected/recovered under the existing semantics.

No blanket automatic delete on DB exception: an ambiguous commit failure could have actually committed. A future maintenance process must compare objects to DB references before removing orphans, with a grace period and a separate authorized operation. Deleting already referenced objects would break result availability; the new internal delete method is not called from normal result processing.

## Security and least privilege

The application requires only bucket-scoped object operations. Use R2 Object Read & Write restricted to the specific bucket, not Account Admin Read & Write. Do not grant bucket creation, public configuration or account management to the server. No Worker/cloud credential exchange or browser direct access. Current paths do not require CORS on R2 or presigned URLs.

Private access is external account state: mock tests verify authenticated application behavior, NOT an actual bucket policy. R2 does not implement S3 GetPublicAccessBlock/GetBucketPolicyStatus, so cannot certify privacy using those AWS checks. During setup leave r2.dev public access disabled and attach no public custom domain. Audit any Cloudflare Worker bindings/routes that could expose the bucket. Keep secrets only in Render secret environment, never repo, logs, command arguments, Mac config or reports.

## Environment names (values intentionally omitted)

STORAGE_BACKEND
S3_ENDPOINT_URL
S3_BUCKET
S3_REGION
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY

Cloudflare preparation requires bucket name, correct account/jurisdiction S3 endpoint, Access Key ID and Secret Access Key. Render maps those credentials to the S3_* names above, not invented R2_* settings. Set backend selection to s3 and region to auto when configuring R2. Copy the exact endpoint from Cloudflare; EU/US/FedRAMP jurisdiction buckets require their jurisdiction endpoint.

## Setup steps — instructions only, not executed

1. Create/select a dedicated private R2 bucket. Disable r2.dev access; no public custom domain or public Worker proxy.
2. Create bucket-restricted Object Read & Write S3 credentials. Securely record keys outside source/logs; no account-management credential in Render.
3. Copy the correct S3 endpoint and bucket name; configure the six Render variables. Do not activate s3 over an existing local dataset until its referenced files have been copied and verified.
4. Later, with explicit live-testing authorization, PUT/HEAD/GET/SHA-256/DELETE a uniquely named disposable test object. Verify unsigned direct access is denied and API authorization works. Confirm keys cannot access other buckets. No test-object or bucket operations were performed here.
5. Migrate and verify referenced existing agent/artifact files at identical logical keys; keep original files/backups until validation and rollback window end.
6. Coordinate the previously reviewed production migration separately, then deploy the API and validate a real Mac via the existing public HTTPS API. This stage does not execute production migration or Mac jobs.

## Current local inventory and migration plan

Inspected project storage/ directory, without reading credential/log file contents or actual environment values:

| Category | Observed local data | Action |
| --- | --- | --- |
| agents/ | 1 file, 123292 bytes | Persistent; migrate DB-referenced agent to identical agents/... key |
| evaluations/ | Directory absent; 0 observed files | No observed local evaluation objects to transfer; future artifacts use R2 in s3 mode |
| problem_sources/ | 24 files, 2368025 bytes | Local Git bare repository, not directly addressable through this storage backend |
| Other root files | Development/server/build logs, diagnostic screenshots and .gitkeep | Not referenced evaluation/agent objects; archive separately if needed, never upload whole storage/ indiscriminately |

Inventory is the project's default/Compose storage directory, not a production-account inventory. Before actual migration compare a DB reference manifest to local files; check size and SHA-256. Copy to the exact logical keys through backend put(), then stream GET and compare full bytes/hash. Existing DB key/path values do not need rewriting. A missing referenced local file must block cutover; do not quietly switch to an empty bucket. Never delete source files during initial copy/verification.

problem_sources is used by git clone/fetch/rev-parse/read and update-ref refs/agentbenchx/{sha}. R2 object keys cannot replace a POSIX bare repository. It can usually be rebuilt from the upstream source, but locally pinned historical commits may disappear after upstream deletion/force-push. Preserve important history as a separately managed Git backup/bundle or upstream mirror with a restore procedure before discarding local cache. Simply uploading its directory does not make the current Git code R2-capable. Worker temporary evaluation workspace stays on the Mac; identity and pending-result/receipt files also stay on Mac for recovery and must not be indiscriminately uploaded (pending data contains a lease token).

## Readiness

R2-ready implementation: YES, subject to live provider verification. Preparation-stage code/mock verification: PASS. Actual production connection readiness: NOT READY until bucket privacy, scoped credentials, live integrity/access checks and referenced-file transfer are verified. No actual cloud success is claimed.

## References (checked 2026-09-14)

- https://developers.cloudflare.com/r2/api/s3/api/
- https://developers.cloudflare.com/r2/api/tokens/
- https://developers.cloudflare.com/r2/buckets/public-buckets/

## Final verification

- Storage-only run before the final additional HEAD/delete-error test: 13 passed.
- Final full regression including all 14 storage cases: **72 passed, 1 skipped**, 10.05 seconds.
- Existing command: `.venv/bin/python scripts/test_integration.py --tb=short`, invoked from /tmp/abx_r2_runner.py with sanitized environment and an isolated working directory without .env. Only local 127.0.0.1:55432 PostgreSQL disposable schemas and local Redis test DB were used; production schema was not migrated.
- `.venv/bin/ruff check apps/api workers scripts tests`: PASS.
- Existing optional live NIFUS test skipped; two existing FastAPI/Starlette deprecation warnings remain.
- No actual account login, cloud credentials, R2 network call, live bucket creation/deletion, production migration or file transfer was performed.
