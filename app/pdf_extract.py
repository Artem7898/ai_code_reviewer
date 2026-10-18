"""Trusted PDF parser subprocess with hard POSIX limits and no inherited secrets."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def extract_pdf(data: bytes, *, max_pages: int = 40, max_text_bytes: int = 262144) -> str:
    from .utils import ProjectLimitError, ProjectReadError

    if os.name != "posix" or not 1 <= max_pages <= 100 or not 1 <= max_text_bytes <= 2097152:
        raise ProjectReadError("PDF parsing requires supported POSIX resource limits")
    if len(data) > 4 * 1024 * 1024:
        raise ProjectLimitError("PDF exceeds the isolated parser input limit")
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).absolute()),
                str(max_pages),
                str(max_text_bytes),
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProjectReadError("PDF parser unavailable or exceeded its time limit") from exc
    if result.returncode or len(result.stdout) > max_text_bytes:
        raise ProjectReadError("PDF is invalid, encrypted or exceeds parser resource limits")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProjectReadError("PDF extraction produced invalid text") from exc


def _main() -> int:
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (3, 4))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    import io

    from pypdf import PdfReader

    max_pages, max_text_bytes = int(sys.argv[1]), int(sys.argv[2])
    data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        return 1
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or len(reader.pages) > max_pages:
        return 1
    output = bytearray()
    for page in reader.pages:
        output.extend(((page.extract_text() or "") + "\n").encode("utf-8"))
        if len(output) > max_text_bytes:
            return 1
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception:
        raise SystemExit(1) from None
