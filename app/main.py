import os
import shutil
from typing import List, Optional
from datetime import datetime
from fastapi import FastAPI, Depends, HTTPException, status, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
import asyncio
from dotenv import load_dotenv # 1. Импортируем

# 2. Загружаем переменные
load_dotenv()

from .db import init_db, get_session
from .models import User, ReviewReport
from .auth import get_current_user, get_password_hash, create_access_token, verify_password
from .ai_client import (
    get_code_review_async,
    generate_clinerules_async,
    migrate_code_async,
    generate_tests_async,
    scaffold_app_async
)
from .utils import scan_local_project, extract_text_from_pdf, read_project_file, write_project_file

app = FastAPI(title="AI Agent Engineer API")

# --- НАСТРОЙКИ CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Читаем ключ из окружения
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")

if not MISTRAL_API_KEY:
    print("⚠️ ВНИМАНИЕ: MISTRAL_API_KEY не найден в .env!")

BASE_PROJECT_DIR = os.path.expanduser("~/PycharmProjects")

# --- Startup ---
@app.on_event("startup")
def on_startup():
    init_db()
    session = next(get_session())
    # Создаем админа, если нет
    if not session.exec(select(User).where(User.username == "admin")).first():
        session.add(User(username="admin", hashed_password=get_password_hash("admin")))
        session.commit()
    session.close()
    # Запускаем Агента
    asyncio.create_task(autonomous_agent_loop())

# --- Pages ---
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# --- Auth Endpoints ---
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.username == form_data.username)).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect credentials")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

# --- NEW: Admin / History Endpoint ---
@app.get("/api/reports")
async def get_reports(
        skip: int = 0,
        limit: int = 20,
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    """Возвращает список всех отчетов (Админ-панель)"""
    statements = select(ReviewReport).order_by(ReviewReport.id.desc()).offset(skip).limit(limit)
    reports = session.exec(statements).all()
    return reports

# --- Работа с файлами (Загрузка и Скан) ---

@app.post("/api/upload-and-review")
async def upload_and_review(
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    temp_path = f"temp_{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        content = ""
        content_type = "code"

        if file.filename.endswith(".pdf"):
            content = extract_text_from_pdf(temp_path)
            content_type = "pdf"
        else:
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

        review_text = await get_code_review_async(content, context=f"File: {file.filename}", api_key=MISTRAL_API_KEY)

        report = ReviewReport(
            project_name="Uploaded File",
            file_path=file.filename,
            content_type=content_type,
            summary=content[:200] + "...",
            review_result=review_text,
            status="completed"
        )
        session.add(report)
        session.commit()
        session.refresh(report)

        return {"status": "success", "report_id": report.id, "review": review_text}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@app.post("/api/scan-local-project")
async def scan_project(
        project_name: str = Form(...),
        current_user: User = Depends(get_current_user),
        session: Session = Depends(get_session)
):
    project_path = os.path.join(BASE_PROJECT_DIR, project_name)
    if not os.path.exists(project_path):
        raise HTTPException(status_code=404, detail=f"Project {project_name} not found")

    py_files = scan_local_project(project_path)
    results = []

    for file_path in py_files:
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                code = f.read()
            if len(code) > 4000: code = code[:4000] + "\n... (truncated)"

            review_text = await get_code_review_async(code, context=f"File: {file_path}", api_key=MISTRAL_API_KEY)

            report = ReviewReport(
                project_name=project_name,
                file_path=file_path,
                content_type="code",
                summary="Local scan",
                review_result=review_text,
                status="completed"
            )
            session.add(report)
            results.append({"file": file_path, "status": "ok"})
        except Exception as e:
            results.append({"file": file_path, "status": "error", "msg": str(e)})

    session.commit()
    return {"scanned_count": len(results), "details": results}

# --- NEW: Endpoints for Extended Features (Migrate, Tests, Scaffold) ---

@app.post("/api/migrate-code")
async def migrate_code(
        file_path: str = Form(...),
        stack: str = Form("Python 3.11"),
        current_user: User = Depends(get_current_user)
):
    if os.path.commonpath([os.path.abspath(file_path), os.path.abspath(BASE_PROJECT_DIR)]) != os.path.abspath(BASE_PROJECT_DIR):
        raise HTTPException(status_code=403, detail="Доступ разрешен только к PycharmProjects")

    code = read_project_file(file_path)
    if not code: raise HTTPException(status_code=404, detail="Файл не найден")

    migrated = await migrate_code_async(code, stack, MISTRAL_API_KEY)
    dir_name = os.path.dirname(file_path)
    file_name = os.path.basename(file_path)
    new_path = os.path.join(dir_name, f"{os.path.splitext(file_name)[0]}_migrated.py")

    if write_project_file(new_path, migrated):
        return {"status": "success", "new_file": new_path}
    else:
        raise HTTPException(status_code=500, detail="Ошибка записи")

@app.post("/api/generate-tests")
async def generate_tests(
        file_path: str = Form(...),
        current_user: User = Depends(get_current_user)
):
    code = read_project_file(file_path)
    if not code: raise HTTPException(status_code=404, detail="Файл не найден")

    project_dir = os.path.dirname(file_path)
    clinerules_path = os.path.join(project_dir, ".clinerules")
    context = read_project_file(clinerules_path)

    tests = await generate_tests_async(code, MISTRAL_API_KEY, context)
    test_path = os.path.join(project_dir, f"test_{os.path.basename(file_path)}")

    if write_project_file(test_path, tests):
        return {"status": "success", "test_file": test_path}
    else:
        raise HTTPException(status_code=500, detail="Ошибка записи")

@app.post("/api/scaffold-app")
async def scaffold_app(
        description: str = Form(...),
        stack: str = Form("FastAPI"),
        project_name: str = Form("my_new_api"),
        current_user: User = Depends(get_current_user)
):
    scaffold = await scaffold_app_async(description, MISTRAL_API_KEY, stack)

    target_dir = os.path.join(BASE_PROJECT_DIR, project_name)
    os.makedirs(target_dir, exist_ok=True)

    guide_path = os.path.join(target_dir, "scaffold_code.md")
    write_project_file(guide_path, scaffold)

    return {"status": "success", "guide_path": guide_path}

# --- Улучшенный Агент (Clinerules + Context + Strict Schedule) ---

AGENT_SCAN_INTERVAL = 3600

async def autonomous_agent_loop():
    print(f"🤖 [АГЕНТ]: Запущен. Строго контролирую {BASE_PROJECT_DIR}")

    while True:
        try:
            if not os.path.exists(BASE_PROJECT_DIR):
                await asyncio.sleep(AGENT_SCAN_INTERVAL);
                continue

            projects = [d for d in os.listdir(BASE_PROJECT_DIR) if os.path.isdir(os.path.join(BASE_PROJECT_DIR, d))]
            session = next(get_session())

            for project_name in projects:
                project_path = os.path.join(BASE_PROJECT_DIR, project_name)
                clinerules_path = os.path.join(project_path, ".clinerules")

                # 1. Долгосрочная память: Проверка .clinerules
                context = ""
                if not os.path.exists(clinerules_path):
                    print(f"🧠 [АГЕНТ]: .clinerules не найден в {project_name}. Создаю знания...")

                    all_files = []
                    for root, dirs, files in os.walk(project_path):
                        for f in files:
                            all_files.append(os.path.join(root, f))

                    req_path = os.path.join(project_path, "requirements.txt")
                    requirements = read_project_file(req_path)

                    rules_content = await generate_clinerules_async(all_files, requirements, MISTRAL_API_KEY)
                    write_project_file(clinerules_path, rules_content)
                    context = rules_content
                else:
                    print(f"📖 [АГЕНТ]: Использую существующие .clinerules для {project_name}")
                    context = read_project_file(clinerules_path)

                # 2. Сканирование с учетом контекста
                py_files = scan_local_project(project_path)
                for file_path in py_files:
                    try:
                        code = read_project_file(file_path)
                        if len(code) > 4000: code = code[:4000]

                        review = await get_code_review_async(
                            code,
                            context=f"Rules: {context}\nFile: {file_path}",
                            api_key=MISTRAL_API_KEY
                        )

                        report = ReviewReport(
                            project_name=project_name,
                            file_path=file_path,
                            content_type="code",
                            summary="Agent Scan (Context Aware)",
                            review_result=review,
                            status="completed"
                        )
                        session.add(report)
                    except Exception as e:
                        print(f"Error analyzing {file_path}: {e}")

            session.commit()
            session.close()
            print(f"🤖 [АГЕНТ]: Цикл завершен. Пауза {AGENT_SCAN_INTERVAL} сек.")
            await asyncio.sleep(AGENT_SCAN_INTERVAL)

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            await asyncio.sleep(60)