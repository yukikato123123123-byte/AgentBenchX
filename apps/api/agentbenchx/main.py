import asyncio
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from . import responses as out
from .config import Settings
from .db import database
from .events import RedisQueue
from .git_sources import GitRepositoryManager, ProblemSourceManager, SourceError
from .models import (
    Agent,
    AgentVersion,
    Evaluation,
    Problem,
    ProblemSource,
    ProblemVersion,
    RunnerJob,
    Submission,
    TestResult,
    Worker,
)
from .schemas import (
    AgentCreate,
    HealthResponse,
    JobHeartbeat,
    ResultInput,
    SourceCreate,
    SubmissionCreate,
    WorkerHeartbeat,
    WorkerRegister,
)
from .services import AgentManager, EvaluationManager, require, serialize
from .storage import StorageError, storage_backend

logging.basicConfig(
    level=logging.INFO, format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine, sessions = database(settings)
    queue = RedisQueue(settings.redis_url)
    storage = storage_backend(settings)
    git = GitRepositoryManager(settings.storage_root, settings.git_allowed_hosts)

    def maintenance():
        with sessions() as db:
            EvaluationManager(db, settings, queue, storage).reap()

    @asynccontextmanager
    async def lifespan(app):
        async def loop():
            while True:
                try:
                    await asyncio.to_thread(maintenance)
                except Exception:
                    logging.getLogger(__name__).exception("Job maintenance failed")
                await asyncio.sleep(settings.reaper_interval_seconds)

        task = asyncio.create_task(loop())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        queue.redis.close()
        engine.dispose()

    app = FastAPI(title="AgentBenchX", version="0.1.0", lifespan=lifespan)
    app.state.sessions, app.state.queue, app.state.git = sessions, queue, git
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Lease-Token"],
    )

    def db_session():
        with sessions() as db:
            yield db

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer authentication required")
        return authorization[7:]

    def admin(authorization: str | None = Header(default=None)):
        if not secrets.compare_digest(bearer(authorization), settings.admin_token):
            raise HTTPException(403, "Invalid administrator token")

    def worker_auth(id: str, db=Depends(db_session), authorization: str | None = Header(default=None)):
        worker = db.scalar(select(Worker).where(Worker.id == id).with_for_update())
        if worker is None:
            raise HTTPException(404, "Worker not found")
        digest = hashlib.sha256(bearer(authorization).encode()).hexdigest()
        if not secrets.compare_digest(digest, worker.token_hash):
            raise HTTPException(403, "Invalid worker credentials")
        return worker

    def manager(db=Depends(db_session)):
        return EvaluationManager(db, settings, queue, storage)

    @app.exception_handler(StorageError)
    async def storage_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "Object storage unavailable"})

    def stored_response(key, filename):
        try:
            stream = storage.open(key)
        except FileNotFoundError:
            raise HTTPException(404, "Artifact unavailable") from None

        def chunks():
            try:
                while data := stream.read(65536):
                    yield data
            finally:
                stream.close()

        return StreamingResponse(
            chunks(), media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.exception_handler(SourceError)
    async def source_error(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": "Record conflicts with existing data"})

    prefix = "/api/v1"
    protected = [Depends(admin)]

    @app.get(prefix + "/health", response_model=HealthResponse)
    def health():
        database_ok = redis_ok = False
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                database_ok = True
        except Exception:
            pass
        try:
            redis_ok = bool(queue.redis.ping())
        except RedisError:
            pass
        return JSONResponse(
            status_code=200 if database_ok and redis_ok else 503,
            content={
                "status": "ok" if database_ok and redis_ok else "degraded",
                "database": database_ok,
                "redis": redis_ok,
            },
        )

    @app.post(prefix + "/agents", dependencies=protected, response_model=out.AgentResponse, status_code=201)
    def create_agent(data: AgentCreate, db=Depends(db_session)):
        agent = Agent(**data.model_dump())
        db.add(agent)
        db.commit()
        return serialize(agent)

    @app.post(
        prefix + "/agents/{id}/versions",
        dependencies=protected,
        response_model=out.AgentVersionResponse,
        status_code=201,
    )
    async def upload(id: str, file: UploadFile = File(...), db=Depends(db_session)):
        content = await file.read(settings.upload_limit + 1)
        return serialize(AgentManager(db, settings, storage).upload(id, file.filename, content))

    @app.get(
        prefix + "/agents/{id}/versions",
        dependencies=protected,
        response_model=list[out.AgentVersionResponse],
    )
    def agent_versions(id: str, db=Depends(db_session)):
        require(db, Agent, id)
        return [
            serialize(v)
            for v in db.scalars(
                select(AgentVersion).where(AgentVersion.agent_id == id).order_by(AgentVersion.version.desc())
            )
        ]

    @app.post(
        prefix + "/problem-sources",
        dependencies=protected,
        response_model=out.ProblemSourceResponse,
        status_code=201,
    )
    def source_register(data: SourceCreate, db=Depends(db_session)):
        return serialize(ProblemSourceManager(db, git).register(data.model_dump()))

    @app.post(
        prefix + "/problem-sources/{id}/sync",
        dependencies=protected,
        response_model=out.ProblemSourceResponse,
    )
    def source_sync(id: str, db=Depends(db_session)):
        require(db, ProblemSource, id)
        source = ProblemSourceManager(db, git).sync(id)
        queue.publish("source.synced", source_id=id)
        return serialize(source)

    @app.get(
        prefix + "/problems/{id}/versions",
        dependencies=protected,
        response_model=list[out.ProblemVersionResponse],
    )
    def problem_versions(id: str, db=Depends(db_session)):
        require(db, Problem, id)
        return [
            serialize(v)
            for v in db.scalars(
                select(ProblemVersion)
                .where(ProblemVersion.problem_id == id)
                .order_by(ProblemVersion.created_at.desc())
            )
        ]

    @app.post(
        prefix + "/submissions",
        dependencies=protected,
        response_model=list[out.SubmissionResponse],
        status_code=201,
    )
    def submit(data: SubmissionCreate, service=Depends(manager)):
        return [serialize(s) for s in service.submit(data)]

    @app.post(prefix + "/workers/register", response_model=out.WorkerCredentialsResponse, status_code=201)
    def register_worker(data: WorkerRegister, service=Depends(manager)):
        # Admission is open on the existing trusted network; subsequent requests
        # still require the unique worker token and, for active jobs, its lease.
        return service.register(data)

    @app.post(prefix + "/workers/{id}/heartbeat", response_model=out.WorkerResponse)
    def worker_heartbeat(data: WorkerHeartbeat, worker=Depends(worker_auth), service=Depends(manager)):
        return serialize(service.worker_heartbeat(worker, data.status))

    @app.post(prefix + "/workers/{id}/jobs/claim", response_model=out.JobClaimResponse | None)
    def claim(worker=Depends(worker_auth), service=Depends(manager)):
        return service.claim(worker)

    @app.post(prefix + "/workers/{id}/jobs/{job_id}/heartbeat", response_model=out.RunnerJobResponse)
    def heartbeat(job_id: str, data: JobHeartbeat, worker=Depends(worker_auth), service=Depends(manager)):
        return serialize(service.heartbeat(worker, job_id, data))

    @app.get(prefix + "/workers/{id}/jobs/{job_id}/agent")
    def download(
        job_id: str, x_lease_token: str = Header(), worker=Depends(worker_auth), service=Depends(manager)
    ):
        job = service.owned_job(worker, job_id, x_lease_token)
        submission = require(service.db, Submission, job.submission_id)
        version = require(service.db, AgentVersion, submission.agent_version_id)
        key = version.storage_path
        service.db.rollback()  # Release ownership locks before streaming remote bytes.
        return stored_response(key, "agent.py")

    @app.post(prefix + "/workers/{id}/jobs/{job_id}/result", response_model=out.EvaluationResponse)
    def result(job_id: str, data: ResultInput, worker=Depends(worker_auth), service=Depends(manager)):
        return serialize(service.result(worker, job_id, data))

    @app.get(prefix + "/evaluations/{id}/artifacts/{name}", dependencies=protected)
    def artifact(id: str, name: str, db=Depends(db_session)):
        if name not in {
            "stdout.log",
            "stderr.log",
            "agent_output.log",
            "patch.diff",
            "test_result.json",
            "analysis.md",
        }:
            raise HTTPException(404, "Artifact not found")
        evaluation = require(db, Evaluation, id)
        key = f"{evaluation.artifact_path}/{name}"
        db.rollback()
        return stored_response(key, name)

    def listing(model, db, limit=200, offset=0):
        records = [
            serialize(row)
            for row in db.scalars(select(model).order_by(model.created_at.desc()).limit(limit).offset(offset))
        ]
        if model == Problem:
            for record in records:
                latest = db.scalar(
                    select(ProblemVersion)
                    .where(ProblemVersion.problem_id == record["id"])
                    .order_by(ProblemVersion.created_at.desc())
                    .limit(1)
                )
                record["latest_version"] = serialize(latest) if latest else None
        return records

    def install_read_routes(path, model):
        from fastapi import Query

        def collection(
            db=Depends(db_session), limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)
        ):
            return listing(model, db, limit, offset)

        def detail(id: str, db=Depends(db_session)):
            item = serialize(require(db, model, id))
            if model == Evaluation:
                submission = require(db, Submission, item["submission_id"])
                item.update(
                    submission=serialize(submission),
                    job=serialize(require(db, RunnerJob, item["job_id"])),
                    agent_version=serialize(require(db, AgentVersion, submission.agent_version_id)),
                    problem_version=serialize(require(db, ProblemVersion, submission.problem_version_id)),
                    tests=[
                        serialize(t)
                        for t in db.scalars(select(TestResult).where(TestResult.evaluation_id == id))
                    ],
                )
            return item

        app.add_api_route(
            prefix + path,
            collection,
            methods=["GET"],
            dependencies=protected,
            response_model=list[getattr(out, model.__name__ + "Response")],
            name=f"list_{model.__tablename__}",
        )
        app.add_api_route(
            prefix + path + "/{id}",
            detail,
            methods=["GET"],
            dependencies=protected,
            response_model=out.EvaluationDetailResponse
            if model == Evaluation
            else getattr(out, model.__name__ + "Response"),
            name=f"get_{model.__tablename__}",
        )

    for path, model in [
        ("/agents", Agent),
        ("/problem-sources", ProblemSource),
        ("/problems", Problem),
        ("/submissions", Submission),
        ("/workers", Worker),
        ("/jobs", RunnerJob),
        ("/evaluations", Evaluation),
    ]:
        install_read_routes(path, model)

    @app.get(prefix + "/dashboard", dependencies=protected)
    def dashboard(db=Depends(db_session)):
        jobs = db.scalars(select(RunnerJob).order_by(RunnerJob.attempt.desc())).all()
        latest = {}
        for job in jobs:
            latest.setdefault(job.submission_id, job)
        current = list(latest.values())
        evaluations = db.scalars(select(Evaluation)).all()
        completed = sum(j.status == "COMPLETED" for j in current)
        failed = sum(j.status in {"FAILED", "TIMEOUT"} for j in current)
        runtime = sum(e.metrics.get("runtime_seconds", 0) for e in evaluations)
        costs = [e.metrics.get("total_cost") for e in evaluations]
        tokens = [e.metrics.get("total_tokens") for e in evaluations]
        cost = sum(costs) if costs and all(v is not None for v in costs) else None
        total_tokens = sum(tokens) if tokens and all(v is not None for v in tokens) else None
        total = len(current)
        final_evals = [e for e in evaluations if e.job_id in {j.id for j in current}]
        from .models import now

        job_rows = []
        for job in current[:200]:
            submission = db.get(Submission, job.submission_id)
            problem_version = db.get(ProblemVersion, submission.problem_version_id)
            agent_version = db.get(AgentVersion, submission.agent_version_id)
            agent = db.get(Agent, agent_version.agent_id)
            evaluation = next((e for e in evaluations if e.job_id == job.id), None)
            elapsed = (
                max(0, ((job.finished_at or now()) - job.started_at).total_seconds()) if job.started_at else 0
            )
            job_rows.append(
                {
                    **serialize(job),
                    "problem_name": problem_version.problem_path.rsplit("/", 1)[-1],
                    "agent_name": agent.name,
                    "agent_version": agent_version.version,
                    "runtime_seconds": evaluation.metrics["runtime_seconds"] if evaluation else elapsed,
                }
            )
        return {
            "summary": {
                "total_problems": total,
                "completed": completed,
                "running": sum(j.status in {"CLAIMED", "RUNNING"} for j in current),
                "queued": sum(j.status == "QUEUED" for j in current),
                "failed": failed,
                "success_rate": completed / (completed + failed) if completed + failed else 0,
                "total_runtime": runtime,
                "average_runtime": runtime / len(evaluations) if evaluations else 0,
                "total_cost": cost,
                "average_cost": cost / len(evaluations) if cost is not None and evaluations else None,
                "total_tokens": total_tokens,
                "score": sum(e.metrics.get("score", 0) for e in final_evals) / len(final_evals)
                if final_evals
                else 0,
            },
            "workers": listing(Worker, db),
            "jobs": job_rows,
            "evaluations": listing(Evaluation, db),
        }

    @app.websocket(prefix + "/events")
    async def events(socket: WebSocket):
        if socket.headers.get("origin") not in (None, settings.web_origin):
            await socket.close(code=1008)
            return
        await socket.accept()
        redis = None
        try:
            message = await asyncio.wait_for(socket.receive_json(), timeout=10)
            if not secrets.compare_digest(str(message.get("token", "")), settings.admin_token):
                await socket.close(code=1008)
                return
            redis = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe("abx:events")
                # Confirm Redis has installed the subscription before advertising readiness.
                while True:
                    ack = await pubsub.get_message(timeout=5)
                    if ack and ack["type"] == "subscribe":
                        break
                await socket.send_json({"type": "connected"})
                while True:
                    item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                    if item:
                        await socket.send_text(item["data"])
                    else:
                        await socket.send_json({"type": "ping"})
        except (WebSocketDisconnect, asyncio.TimeoutError, RedisError, RuntimeError):
            pass
        finally:
            if redis:
                await redis.aclose()

    return app


app = create_app()
