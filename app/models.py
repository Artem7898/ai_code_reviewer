from typing import Optional
from sqlmodel import Field, SQLModel
from datetime import datetime

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    hashed_password: str

class ReviewReport(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_name: str
    file_path: str
    content_type: str # "code" или "pdf"
    summary: str # Краткое содержание (генерация)
    review_result: str # Ответ от Mistral
    status: str = "pending" # pending, completed, failed
    created_at: datetime = Field(default_factory=datetime.utcnow)