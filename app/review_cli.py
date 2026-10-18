"""Read-only diff reviewer for local use and unprivileged pull-request CI."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Literal, cast

from .ai_client import AIProvider, ProviderError
from .analysis import ReviewAnalyzer
from .sarif import review_to_sarif
from .settings import Settings
from .utils import collect_project


def _write_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


async def run(arguments: argparse.Namespace) -> int:
    provider_name = cast(Literal["static", "ollama"], arguments.provider)
    settings = Settings(ai_provider=provider_name)
    project = await asyncio.to_thread(Path(cast(str, arguments.project)).absolute)
    files = await asyncio.to_thread(
        collect_project,
        project,
        mode="full" if arguments.full else "diff",
        base_ref=cast(str, arguments.base_ref),
        max_files=settings.max_review_files,
        max_file_bytes=settings.max_file_bytes,
        max_total_bytes=settings.max_review_chars,
    )
    async with AIProvider(settings) as provider:
        result = await ReviewAnalyzer(settings, provider).review(files)
    report = result.model_dump(mode="json")
    report["metadata"] = {
        "base_ref": arguments.base_ref,
        "snapshot": {source.path: source.sha256 for source in files},
    }
    await asyncio.to_thread(_write_atomic, Path(cast(str, arguments.output)), report)
    await asyncio.to_thread(
        _write_atomic, Path(cast(str, arguments.sarif)), review_to_sarif(result)
    )
    print(
        json.dumps(
            {"files": len(files), "findings": len(result.findings), "provider": provider_name}
        )
    )
    return int(
        bool(arguments.fail_on_error)
        and any(finding.severity == "error" for finding in result.findings)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "project", help="Explicit project directory; source is never executed or modified"
    )
    parser.add_argument("--base-ref", default="HEAD")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Review all bounded Python files instead of the Git diff",
    )
    parser.add_argument(
        "--provider",
        choices=("static", "ollama"),
        default="static",
        help="Cloud analysis uses the authenticated worker API and durable budget",
    )
    parser.add_argument("--output", default="review.json")
    parser.add_argument("--sarif", default="review.sarif")
    parser.add_argument("--fail-on-error", action="store_true")
    try:
        return asyncio.run(run(parser.parse_args()))
    except (ValueError, OSError, ProviderError) as exc:
        parser.exit(
            2,
            f"Review could not complete ({type(exc).__name__}); verify input, limits and provider settings.\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
