"""Capture sanitized Jenkins API responses for contract tests."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from pydantic import HttpUrl, ValidationError

from jenkins_stats import HttpJsonTransport, JsonValue

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

LOGGER = logging.getLogger(__name__)
ITEM_TREE = "fullName,displayName,url,jobs[name,url],builds[number]{0,0}"
BUILD_FIELDS = "_class,number,url,timestamp,duration,building,inProgress,result"
SENSITIVE_KEYS = frozenset(
    {
        "actions",
        "artifacts",
        "causes",
        "changeSet",
        "consoleText",
        "culprits",
        "description",
        "displayDescription",
        "environment",
        "envVars",
        "parameters",
    }
)


@dataclass(frozen=True)
class CaptureRequest:
    """A single Jenkins JSON request to capture."""

    name: str
    url: HttpUrl
    tree: str | None = None


@dataclass(frozen=True)
class PageAssignment:
    """A parsed paginated build-page capture assignment."""

    name: str
    job_url: HttpUrl
    offset: int
    limit: int


@dataclass(frozen=True)
class CaptureConfig:
    """Validated command-line configuration for a capture run."""

    base_url: HttpUrl
    out: Path
    jenkins_version: str
    plugins: Mapping[str, str]
    sanitized_origin: str
    requests: Sequence[CaptureRequest]
    force: bool
    dry_run: bool
    allow_insecure: bool
    forbidden_values: Sequence[str] = field(default_factory=tuple)


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Capture sanitized Jenkins JSON contract fixtures. Credentials are "
            "read from JENKINS_USER and JENKINS_TOKEN."
        )
    )
    parser.add_argument("--base-url", required=True, help="Jenkins controller URL.")
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output fixture directory to create.",
    )
    parser.add_argument(
        "--jenkins-version",
        required=True,
        help="Human-readable Jenkins version/configuration label.",
    )
    parser.add_argument(
        "--plugin",
        action="append",
        default=[],
        metavar="SHORT_NAME=VERSION",
        help="Plugin version metadata to record; repeat as needed.",
    )
    parser.add_argument(
        "--item",
        action="append",
        default=[],
        metavar="NAME=URL",
        help=(
            "Capture an item/container/job URL with the client item tree. The "
            "root controller item is always captured as root_api.json."
        ),
    )
    parser.add_argument(
        "--build-page",
        action="append",
        default=[],
        metavar="NAME=JOB_URL,OFFSET,LIMIT",
        help="Capture a retained-build page from a job URL.",
    )
    parser.add_argument(
        "--timing",
        action="append",
        default=[],
        metavar="NAME=BUILD_URL",
        help="Capture BUILD_URL/wfapi/describe for Pipeline timing.",
    )
    parser.add_argument(
        "--sanitized-origin",
        default="https://jenkins.example/",
        help="Origin used to replace the real controller origin in fixtures.",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        metavar="TEXT",
        help="Additional text that must not appear in generated fixtures.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite files in an existing output directory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log the requests that would be made without contacting Jenkins.",
    )
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="Allow an HTTP Jenkins base URL for controlled local instances.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _parse_assignment(value: str, *, option: str) -> tuple[str, str]:
    """Parse a NAME=VALUE option assignment."""
    name, separator, assigned_value = value.partition("=")
    if separator == "" or name == "" or assigned_value == "":
        raise ValueError(f"{option} must use NAME=VALUE syntax")
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"{option} name may contain only letters, numbers, _ and -")
    return name, assigned_value


def _parse_plugin(value: str) -> tuple[str, str]:
    """Parse a plugin metadata assignment."""
    return _parse_assignment(value, option="--plugin")


def _parse_http_url(value: str, *, option: str) -> HttpUrl:
    try:
        return HttpUrl(value)
    except ValidationError as exc:
        raise ValueError(f"{option} must be a valid HTTP(S) URL") from exc


def _parse_page_assignment(value: str) -> PageAssignment:
    """Parse a build-page capture assignment."""
    name, rest = _parse_assignment(value, option="--build-page")
    job_url, offset_text, limit_text = rest.rsplit(",", 2)
    try:
        offset = int(offset_text)
        limit = int(limit_text)
    except ValueError as exc:
        raise ValueError("--build-page OFFSET and LIMIT must be integers") from exc
    if offset < 0 or limit <= 0:
        raise ValueError("--build-page OFFSET must be >= 0 and LIMIT must be > 0")
    return PageAssignment(
        name=name,
        job_url=_parse_http_url(job_url, option="--build-page JOB_URL"),
        offset=offset,
        limit=limit,
    )


def _build_page_tree(page: PageAssignment) -> str:
    """Return the Jenkins tree projection for a retained-build page."""
    return f"allBuilds[{BUILD_FIELDS}]{{{page.offset},{page.offset + page.limit}}}"


def _normalize_directory_url(url: HttpUrl) -> str:
    """Normalize an item, job, or build URL to a slash-terminated URL."""
    return str(url).rstrip("/") + "/"


def _request_url(url: HttpUrl) -> HttpUrl:
    """Return the Jenkins api/json URL for an item or job URL."""
    return HttpUrl(_normalize_directory_url(url) + "api/json")


def _timing_url(build_url: HttpUrl) -> HttpUrl:
    """Return the Pipeline REST timing URL for a build URL."""
    return HttpUrl(_normalize_directory_url(build_url) + "wfapi/describe")


def _config_from_args(args: argparse.Namespace) -> CaptureConfig:
    """Convert argparse output to a typed capture configuration."""
    base_url = _parse_http_url(args.base_url, option="--base-url")
    plugins = dict(_parse_plugin(value) for value in args.plugin)
    requests = [
        CaptureRequest(name="root_api", url=_request_url(base_url), tree=ITEM_TREE)
    ]
    for value in args.item:
        name, url = _parse_assignment(value, option="--item")
        requests.append(
            CaptureRequest(
                name=name,
                url=_request_url(_parse_http_url(url, option="--item URL")),
                tree=ITEM_TREE,
            )
        )
    for value in args.build_page:
        page = _parse_page_assignment(value)
        requests.append(
            CaptureRequest(
                name=page.name,
                url=_request_url(page.job_url),
                tree=_build_page_tree(page),
            )
        )
    for value in args.timing:
        name, url = _parse_assignment(value, option="--timing")
        requests.append(
            CaptureRequest(
                name=name,
                url=_timing_url(_parse_http_url(url, option="--timing BUILD_URL")),
            )
        )

    forbidden_values = [*args.forbid, str(base_url).rstrip("/")]
    return CaptureConfig(
        base_url=base_url,
        out=args.out,
        jenkins_version=args.jenkins_version,
        plugins=plugins,
        sanitized_origin=args.sanitized_origin,
        requests=requests,
        force=args.force,
        dry_run=args.dry_run,
        allow_insecure=args.allow_insecure,
        forbidden_values=forbidden_values,
    )


def _origin_prefix(url: HttpUrl) -> str:
    """Return the scheme and authority portion of a URL."""
    parsed = urlsplit(str(url))
    if parsed.scheme == "" or parsed.netloc == "":
        raise ValueError(f"Expected absolute URL with origin: {url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _sanitize_value(
    value: JsonValue, *, real_origin: str, sanitized_origin: str
) -> JsonValue:
    """Remove known sensitive fields and replace the real Jenkins origin."""
    if isinstance(value, str):
        return value.replace(real_origin, sanitized_origin.rstrip("/"))
    if isinstance(value, list):
        return [
            _sanitize_value(
                item,
                real_origin=real_origin,
                sanitized_origin=sanitized_origin,
            )
            for item in value
        ]
    if isinstance(value, dict):
        sanitized: dict[str, JsonValue] = {}
        for key, child in value.items():
            if key in SENSITIVE_KEYS:
                continue
            sanitized[key] = _sanitize_value(
                child,
                real_origin=real_origin,
                sanitized_origin=sanitized_origin,
            )
        return sanitized
    return value


def _fixture_metadata(config: CaptureConfig) -> dict[str, JsonValue]:
    """Build the generated fixture metadata document."""
    return {
        "fixture_set": config.out.name,
        "source": {
            "jenkins_version": config.jenkins_version,
            "plugins": dict(config.plugins),
        },
        "expected_trees": {
            "item": ITEM_TREE,
            "build_fields": BUILD_FIELDS,
        },
        "captured_requests": [
            {
                "name": request.name,
                "url": str(request.url).replace(
                    _origin_prefix(config.base_url),
                    config.sanitized_origin.rstrip("/"),
                ),
                "tree": request.tree,
                "file": f"{request.name}.json",
            }
            for request in config.requests
        ],
    }


def _write_json(path: Path, value: JsonValue) -> None:
    """Write one formatted JSON file."""
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _validate_no_forbidden_text(out: Path, forbidden_values: Iterable[str]) -> None:
    """Ensure generated fixture files do not contain known sensitive strings."""
    for path in out.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_values:
            if forbidden and forbidden in text:
                raise RuntimeError(
                    f"Generated fixture {path} still contains forbidden text: "
                    f"{forbidden!r}"
                )


def _prepare_output_dir(path: Path, *, force: bool) -> None:
    """Create or validate the fixture output directory."""
    if path.exists() and not path.is_dir():
        raise FileExistsError(f"Output path is not a directory: {path}")
    if path.exists() and any(path.iterdir()) and not force:
        raise FileExistsError(f"Output directory is not empty; pass --force: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _log_requests(requests: Iterable[CaptureRequest]) -> None:
    """Log the requests in the order they will be performed."""
    for request in requests:
        LOGGER.info("capture %s: %s tree=%r", request.name, request.url, request.tree)


def capture(config: CaptureConfig) -> None:
    """Capture and write one fixture set."""
    real_origin = _origin_prefix(config.base_url)
    _log_requests(config.requests)
    if config.dry_run:
        return

    username = os.environ.get("JENKINS_USER")
    api_token = os.environ.get("JENKINS_TOKEN")
    if username is None or api_token is None:
        raise RuntimeError("Set JENKINS_USER and JENKINS_TOKEN before capturing")

    _prepare_output_dir(config.out, force=config.force)
    with HttpJsonTransport(
        config.base_url,
        username=username,
        api_token=api_token,
        allow_insecure=config.allow_insecure,
    ) as transport:
        for request in config.requests:
            LOGGER.info("fetching %s", request.name)
            response = transport.get_json(request.url, tree=request.tree)
            sanitized = _sanitize_value(
                response,
                real_origin=real_origin,
                sanitized_origin=config.sanitized_origin,
            )
            _write_json(config.out / f"{request.name}.json", sanitized)

        _write_json(config.out / "index.json", _fixture_metadata(config))
        forbidden_values = [*config.forbidden_values, username, api_token]
        _validate_no_forbidden_text(config.out, forbidden_values)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixture capture command."""
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if cast("bool", args.verbose) else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    try:
        capture(_config_from_args(args))
    except (FileExistsError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
