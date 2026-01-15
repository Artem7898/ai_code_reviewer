import os
from sqlmodel import SQLModel, create_engine, Session

# Файл БД будет лежать в корне проекта
DATABASE_URL = "sqlite:///./database.db"

connect_args = {"check_same_thread": False} # Нужно для SQLite в многопотоке
engine = create_engine(DATABASE_URL, connect_args=connect_args)

def get_session():
    with Session(engine) as session:
        yield session

def init_db():
    SQLModel.metadata.create_all(engine)