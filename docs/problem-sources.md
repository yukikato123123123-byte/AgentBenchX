# Git problem sources

The initial source is `git@github.com:wongfengchen8-cell/NIFUS-bench.git`, seeded with ID `11111111-1111-4111-8111-111111111111`. It remains an external data source, cached under `storage/problem_sources/<UUID>/repository/`, never an application dependency.

Inspection on 2026-09-14 cloned the real remote over SSH and resolved `main` to `55014c032f9532834cccc780f4d372c90ebcb37f`. The older sibling checkout was inspected first but is not used by the application. The remote uses Harbor tasks with `task.toml`, `instruction.md`, `environment/Dockerfile`, and `tests/test.sh`. Tasks live in dated release directories and nested `db-engineering` directories; no `problems/problem-001` convention is assumed.

The adapter found **29 supported problems: 21 CODING and 8 DATABASE**. It excluded **17 GIS/xarray-spatial tasks**. Exact paths and reasons are in [source-inspection.json](source-inspection.json). Counts describe that commit, not future repository contents.

Mappings: `bugfix` → CODING/BUG_FIX; `linting-*` → CODING/REFACTORING; `database_query_engineering` → DATABASE/SQL, QUERY_OPTIMIZATION or MIGRATION according to metadata tags. Explicit CODING, DATABASE, DATA and API_BACKEND metadata is also accepted. Unknown source categories are reported as unmapped instead of guessed. Add another discovery adapter for repositories with different metadata formats. The stored raw metadata preserves source category, environment settings, tags and verifier/agent timeouts.

```mermaid
flowchart LR
  Register --> CloneOrFetch[Clone bare / fetch]
  CloneOrFetch --> Branch[Detect or select branch]
  Branch --> SHA[Resolve exact commit]
  SHA --> Read[Read regular Git blobs]
  Read --> Validate[Validate Harbor metadata]
  Validate --> Versions[Create immutable ProblemVersions]
```

Git uses argument arrays, disables hooks and file/ext protocols, validates hosts and URL syntax, does not initialize submodules, and never checks out a worktree on the server. Discovery ignores symlinks and caps each metadata blob at 1 MB. Supported URLs are GitHub-style SSH and credential-free HTTPS URLs on `GIT_ALLOWED_HOSTS`. Credential-bearing HTTPS URLs and arbitrary local paths are rejected.

A configured branch is resolved explicitly; otherwise the cloned repository's symbolic HEAD supplies the default. Stored identity is repository URL + branch + full commit SHA + problem path; uniqueness is URL + SHA + path. Repeat syncs do not duplicate versions. PostgreSQL triggers and ORM guards reject version updates/deletes. Historical metadata survives source changes. Each synced commit is pinned under `refs/agentbenchx/<sha>` so fetch/prune and upstream force-pushes cannot make historical objects unreachable. Back up the bare Git cache to retain the source objects as well; upstream task Dockerfiles and package downloads may still have mutable dependencies. Phase 2 must capture image digests and execution dependency manifests.

SSH is supplied by the server's SSH agent/configuration. Use a read-only repository identity and verified GitHub known_hosts; never disable host verification. For Docker, see the optional SSH-agent Compose override in `infrastructure/ssh-agent.compose.yml`. It mounts an agent socket and public known_hosts, never a private key. Native server processes can use the existing SSH agent and SSH configuration. A normal Compose launch works without SSH credentials, but its Sync action will report failure until SSH is configured. Workers must use their own least-privilege repository access or a future authenticated source bundle, never receive the server's key.

Sync failures persist FAILED status with a sanitized error. The current implementation performs bounded Git subprocess calls synchronously in a FastAPI thread. Large-repository/background sync scheduling is a future improvement. The source is not automatically synced at startup.
