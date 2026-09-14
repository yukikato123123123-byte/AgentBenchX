# Shared API contract

openapi.json is generated from FastAPI's Pydantic input/output contracts using `python scripts/export_schemas.py`. Worker and future language SDKs can generate clients from it. Schema version is the API version; breaking worker-protocol changes require a new version. Agent bytes use a multipart upload and authenticated lease-bound download; result artifacts are bounded UTF-8 fields in the result payload.
