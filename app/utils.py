import os
from typing import List
import pypdf
import shutil  # Для копирования/удаления если нужно


def scan_local_project(project_path: str) -> List[str]:
    """Сканирует папку проекта и возвращает список .py файлов."""
    py_files = []
    if not os.path.exists(project_path):
        return py_files

    for root, dirs, files in os.walk(project_path):
        for file in files:
            if file.endswith(".py"):
                full_path = os.path.join(root, file)
                py_files.append(full_path)
    return py_files


def extract_text_from_pdf(pdf_path: str) -> str:
    text = ""
    try:
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() + "\n"
    except Exception as e:
        return f"Error reading PDF: {e}"
    return text


def read_project_file(file_path: str) -> str:
    """Безопасное чтение файла с диска."""
    if not os.path.exists(file_path):
        return ""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        print(f"Ошибка чтения {file_path}: {e}")
        return ""


def write_project_file(file_path: str, content: str) -> bool:
    """Запись файла на диск. Создает папки, если их нет."""
    try:
        directory = os.path.dirname(file_path)
        if directory and not os.path.exists(directory):
            os.makedirs(directory)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅ Файл записан: {file_path}")
        return True
    except Exception as e:
        print(f"❌ Ошибка записи {file_path}: {e}")
        return False