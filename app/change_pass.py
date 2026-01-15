from sqlmodel import Session, select
from passlib.context import CryptContext
# Импортируем init_db, чтобы создать таблицы, если их нет
from db import engine, init_db
from models import User

# === НАСТРОЙКИ ===
NEW_PASSWORD = "560arta1789rit"
USERNAME_TO_CHANGE = "admin"

# === ЛОГИКА ХЕШИРОВАНИЯ ===
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password):
    return pwd_context.hash(password)


# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ===
print("Проверка и создание таблиц базы данных...")
init_db()

# === ОСНОВНОЙ КОД ===
print(f"Попытка сменить пароль для пользователя: {USERNAME_TO_CHANGE}...")

with Session(engine) as session:
    statement = select(User).where(User.username == USERNAME_TO_CHANGE)
    user = session.exec(statement).first()

    if user:
        user.hashed_password = get_password_hash(NEW_PASSWORD)
        session.add(user)
        session.commit()
        print("------------------------------------------------")
        print("✅ УСПЕХ!")
        print(f"Пароль для '{USERNAME_TO_CHANGE}' успешно изменен на: {NEW_PASSWORD}")
        print("------------------------------------------------")
    else:
        # Если пользователя нет (например, база была пустой), создадим его
        print("⚠️ Пользователь не найден. Создаю нового...")
        new_user = User(username=USERNAME_TO_CHANGE, hashed_password=get_password_hash(NEW_PASSWORD))
        session.add(new_user)
        session.commit()
        print("------------------------------------------------")
        print("✅ ПОЛЬЗОВАТЕЛЬ СОЗДАН!")
        print(f"Логин: {USERNAME_TO_CHANGE}")
        print(f"Пароль: {NEW_PASSWORD}")
        print("------------------------------------------------")