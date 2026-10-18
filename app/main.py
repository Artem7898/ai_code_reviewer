"""Authenticated asynchronous API; all analysis runs in a separate worker."""

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jwt import InvalidTokenError
from pydantic import BaseModel
from sqlmodel import col, select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import (
    create_access_token,
    decode_identity,
    dummy_password_hash,
    verify_and_update_password,
)
from .db import Database
from .http_security import LoginLimiter, RequestBoundary
from .jobs import JobKind, JobLimitError, JobStore, JSONValue
from .models import ReviewJob, ReviewReport, User
from .patches import read_artifacts
from .schemas import SourceFile
from .settings import REPOSITORY_ROOT, Settings, get_settings
from .utils import collect_project, get_project, read_source, read_upload, relative_parts

oauth2 = OAuth2PasswordBearer(tokenUrl="token")


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    result: dict[str, JSONValue] | None
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    database: Database
    jobs: JobStore
    dummy_hash: str
    limiter: LoginLimiter


def runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


async def current_user(request: Request, token: Annotated[str, Depends(oauth2)]) -> User:
    state = runtime(request)
    try:
        identity = decode_identity(token, state.settings)
    except InvalidTokenError as exc:
        raise HTTPException(
            401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    async with state.database.session() as session:
        user = await session.get(User, identity.user_id)
    if user is None or not user.is_active or user.auth_revision != identity.auth_revision:
        raise HTTPException(
            401, "Invalid or expired credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    return user


type ActiveUser = Annotated[User, Depends(current_user)]


def _owner(user: User) -> int:
    if user.id is None:
        raise HTTPException(401, "Invalid credentials")
    return user.id


def _job_response(job: ReviewJob, *, include_result: bool = True) -> JobResponse:
    result = (
        cast(dict[str, JSONValue], json.loads(job.result))
        if include_result and job.result
        else None
    )
    return JobResponse(
        id=job.id,
        kind=job.kind,
        status=job.status,
        progress=job.progress,
        result=result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


async def _enqueue(
    state: Runtime, user: User, kind: JobKind, payload: dict[str, JSONValue]
) -> JobResponse:
    digest = hashlib.sha256(
        json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    try:
        job = await state.jobs.enqueue(_owner(user), kind, payload, digest)
    except JobLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(413, "Job input exceeds the configured limit") from exc
    return _job_response(job)


def _snapshot(files: list[SourceFile]) -> list[JSONValue]:
    return cast(list[JSONValue], [file.model_dump(mode="json") for file in files])


async def _project(state: Runtime, project_name: str) -> Path:
    try:
        return await asyncio.to_thread(get_project, state.settings.projects_root, project_name)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, "Select an existing project inside PROJECTS_ROOT") from exc


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    configured = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        db = database or Database(configured)
        try:
            await db.initialize()
            application.state.runtime = Runtime(
                configured,
                db,
                JobStore(db),
                await dummy_password_hash(),
                LoginLimiter(configured.login_rate_limit),
            )
            yield
        finally:
            await db.close()

    application = FastAPI(title="AI Code Reviewer", version="0.2.0", lifespan=lifespan)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=configured.allowed_hosts)
    if configured.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=configured.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    application.add_middleware(
        RequestBoundary,
        max_bytes=configured.max_upload_bytes + 65536,
        concurrency=configured.http_concurrency,
    )
    application.mount(
        "/static", StaticFiles(directory=REPOSITORY_ROOT / "static", check_dir=False), name="static"
    )
    templates = Jinja2Templates(directory=REPOSITORY_ROOT / "templates")

    @application.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="index.html", context={"request": request}
        )

    @application.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz")
    async def readiness(request: Request) -> dict[str, str]:
        async with runtime(request).database.session() as session:
            await (await session.connection()).exec_driver_sql("SELECT 1")
        return {"status": "ok"}

    @application.post("/token", response_model=TokenResponse)
    async def login(
        request: Request, form: Annotated[OAuth2PasswordRequestForm, Depends()]
    ) -> TokenResponse:
        state = runtime(request)
        if not state.limiter.consume(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
        async with state.database.session() as session:
            user = (await session.exec(select(User).where(User.username == form.username))).first()
        observed_hash = user.hashed_password if user else state.dummy_hash
        valid, replacement = await verify_and_update_password(form.password, observed_hash)
        if user is None or not user.is_active or not valid:
            raise HTTPException(
                401, "Incorrect credentials", headers={"WWW-Authenticate": "Bearer"}
            )
        async with state.database.session() as session:
            current = await session.get(User, user.id)
            if current is None or not current.is_active or current.hashed_password != observed_hash:
                raise HTTPException(
                    401, "Incorrect credentials", headers={"WWW-Authenticate": "Bearer"}
                )
            if replacement:
                connection = await session.connection()
                updated = await connection.exec_driver_sql(
                    'UPDATE "user" SET hashed_password = ? WHERE id = ? AND hashed_password = ? AND is_active = 1',
                    (replacement, current.id, observed_hash),
                )
                if updated.rowcount != 1:
                    raise HTTPException(
                        401, "Incorrect credentials", headers={"WWW-Authenticate": "Bearer"}
                    )
                await session.commit()
            user = current
        return TokenResponse(access_token=create_access_token(user, state.settings))

    @application.get("/api/config")
    async def configuration(request: Request, user: ActiveUser) -> dict[str, JSONValue]:
        state = runtime(request)

        def names() -> list[str]:
            root = state.settings.projects_root
            if not root.is_dir():
                return []
            projects: list[str] = []
            with os.scandir(root) as entries:
                for entry in entries:
                    if len(projects) >= 1000:
                        break
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        projects.append(entry.name)
            return sorted(projects)

        try:
            projects = await asyncio.to_thread(names)
        except OSError as exc:
            raise HTTPException(
                503, "Project directory is unavailable; check server access permissions"
            ) from exc
        return {
            "provider": state.settings.ai_provider,
            "model": state.settings.mistral_model
            if state.settings.ai_provider == "mistral"
            else state.settings.ollama_model,
            "read_only": True,
            "projects": cast(list[JSONValue], projects),
            "limits": {
                "max_upload_bytes": state.settings.max_upload_bytes,
                "max_files": state.settings.max_review_files,
                "max_file_bytes": state.settings.max_file_bytes,
            },
        }

    @application.get("/api/reports", response_model=list[ReviewReport])
    async def reports(
        request: Request,
        user: ActiveUser,
        skip: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> list[ReviewReport]:
        async with runtime(request).database.session() as session:
            statement = select(ReviewReport)
            if not user.is_admin:
                statement = statement.where(ReviewReport.owner_id == _owner(user))
            return list(
                (
                    await session.exec(
                        statement.order_by(col(ReviewReport.id).desc()).offset(skip).limit(limit)
                    )
                ).all()
            )

    @application.get("/api/jobs", response_model=list[JobResponse])
    async def jobs(
        request: Request,
        user: ActiveUser,
        skip: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> list[JobResponse]:
        return [
            _job_response(job, include_result=False)
            for job in await runtime(request).jobs.list(_owner(user), limit, skip)
        ]

    async def owned_job(request: Request, user: User, job_id: str) -> ReviewJob:
        job = await runtime(request).jobs.get(_owner(user), job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @application.get("/api/jobs/{job_id}", response_model=JobResponse)
    async def job_detail(request: Request, user: ActiveUser, job_id: str) -> JobResponse:
        return _job_response(await owned_job(request, user, job_id))

    @application.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(request: Request, user: ActiveUser, job_id: str) -> JobResponse:
        job = await runtime(request).jobs.cancel(_owner(user), job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return _job_response(job)

    @application.get("/api/jobs/{job_id}/sarif")
    async def sarif(request: Request, user: ActiveUser, job_id: str) -> JSONResponse:
        job = await owned_job(request, user, job_id)
        result = _job_response(job).result
        if job.status != "completed" or result is None or not isinstance(result.get("sarif"), dict):
            raise HTTPException(409, "No completed SARIF result for this job")
        return JSONResponse(result["sarif"], media_type="application/sarif+json")

    @application.get("/api/jobs/{job_id}/artifacts")
    async def artifacts(request: Request, user: ActiveUser, job_id: str) -> dict[str, str]:
        job = await owned_job(request, user, job_id)
        result = _job_response(job).result
        artifact_id = result.get("artifact_id") if result else None
        if job.status != "completed" or not isinstance(artifact_id, str):
            raise HTTPException(409, "No completed artifacts for this job")
        try:
            return await asyncio.to_thread(
                read_artifacts, runtime(request).settings.data_dir / "artifacts", artifact_id
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Artifacts are unavailable") from exc

    @application.post("/api/scan-local-project", response_model=JobResponse, status_code=202)
    async def scan(
        request: Request,
        user: ActiveUser,
        project_name: Annotated[str, Form(min_length=1, max_length=150)],
        mode: Annotated[Literal["diff", "full"], Form()] = "diff",
        base_ref: Annotated[str, Form(max_length=200)] = "HEAD",
    ) -> JobResponse:
        state = runtime(request)
        project = await _project(state, project_name)
        try:
            files = await asyncio.to_thread(
                collect_project,
                project,
                mode=mode,
                base_ref=base_ref,
                max_files=state.settings.max_review_files,
                max_file_bytes=state.settings.max_file_bytes,
                max_total_bytes=state.settings.max_review_chars,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(
                400, "Project selection is invalid, unavailable, or exceeds its limits"
            ) from exc
        return await _enqueue(
            state,
            user,
            "review",
            {
                "project_name": project_name,
                "mode": mode,
                "base_ref": base_ref,
                "snapshot": _snapshot(files),
            },
        )

    @application.post("/api/upload-and-review", response_model=JobResponse, status_code=202)
    async def upload(
        request: Request, user: ActiveUser, file: Annotated[UploadFile, File()]
    ) -> JobResponse:
        state = runtime(request)
        filename = file.filename or ""
        try:
            if len(relative_parts(filename)) != 1 or Path(filename).suffix.lower() not in {
                ".py",
                ".txt",
                ".md",
                ".pdf",
            }:
                raise ValueError("Invalid upload name")
        except ValueError as exc:
            await file.close()
            raise HTTPException(400, "Use a simple .py, .txt, .md or .pdf filename") from exc
        directory = state.settings.data_dir / "uploads"
        await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise HTTPException(500, "Private upload storage is unavailable")
        upload_id = uuid4().hex
        target = directory / upload_id
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                while chunk := await file.read(65536):
                    total += len(chunk)
                    if total > state.settings.max_upload_bytes:
                        raise HTTPException(413, "Upload exceeds limit")
                    await asyncio.to_thread(output.write, chunk)
            source = await asyncio.to_thread(
                read_upload,
                state.settings.data_dir,
                upload_id,
                filename,
                max_file_bytes=min(state.settings.max_upload_bytes, state.settings.max_file_bytes),
                max_pages=state.settings.max_pdf_pages,
            )
            if len(source.content) > state.settings.max_review_chars:
                raise HTTPException(413, "Upload exceeds context limit")
            return await _enqueue(
                state, user, "upload", {"filename": filename, "snapshot": _snapshot([source])}
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "Upload could not be safely decoded within limits") from exc
        finally:
            await file.close()
            target.unlink(missing_ok=True)

    async def generate(
        request: Request,
        user: User,
        kind: Literal["migrate", "tests"],
        project_name: str,
        file_path: str,
        instructions: str,
    ) -> JobResponse:
        state = runtime(request)
        project = await _project(state, project_name)
        try:
            source = await asyncio.to_thread(
                read_source, project, file_path, max_file_bytes=state.settings.max_file_bytes
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "Select a regular file inside the project") from exc
        return await _enqueue(
            state,
            user,
            kind,
            {
                "project_name": project_name,
                "file_path": file_path,
                "instructions": instructions,
                "snapshot": _snapshot([source]),
            },
        )

    @application.post("/api/migrate-code", response_model=JobResponse, status_code=202)
    async def migrate(
        request: Request,
        user: ActiveUser,
        project_name: Annotated[str, Form(max_length=150)],
        file_path: Annotated[str, Form(max_length=1024)],
        stack: Annotated[str, Form(max_length=1000)] = "Python 3.14",
    ) -> JobResponse:
        return await generate(
            request,
            user,
            "migrate",
            project_name,
            file_path,
            f"Migrate to {stack}. Preserve behavior and propose a small unified diff.",
        )

    @application.post("/api/generate-tests", response_model=JobResponse, status_code=202)
    async def tests(
        request: Request,
        user: ActiveUser,
        project_name: Annotated[str, Form(max_length=150)],
        file_path: Annotated[str, Form(max_length=1024)],
    ) -> JobResponse:
        return await generate(
            request,
            user,
            "tests",
            project_name,
            file_path,
            "Propose focused regression tests; do not modify source.",
        )

    @application.post("/api/scaffold-app", response_model=JobResponse, status_code=202)
    async def scaffold(
        request: Request,
        user: ActiveUser,
        description: Annotated[str, Form(min_length=1, max_length=10000)],
        project_name: Annotated[str, Form(max_length=150)] = "new_project",
        stack: Annotated[str, Form(max_length=1000)] = "FastAPI",
    ) -> JobResponse:
        try:
            if len(relative_parts(project_name)) != 1:
                raise ValueError("Invalid project name")
        except ValueError as exc:
            raise HTTPException(400, "Use a simple project name") from exc
        return await _enqueue(
            runtime(request),
            user,
            "scaffold",
            {
                "project_name": project_name,
                "instructions": f"Create a {stack} scaffold: {description}",
            },
        )

    return application


app = create_app()
