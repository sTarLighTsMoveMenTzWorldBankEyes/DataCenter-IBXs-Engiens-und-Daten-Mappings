#!/usr/bin/env python3
"""Institutional API-Masterbook CLI.

Orchestrates multi-repository publication across scientific archives
(Harvard Dataverse, Zenodo, OSF, Software Heritage) and generates a
cryptographically hashed `publication_certificate.json` proof manifest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class HttpResponse:
    """Normalized HTTP response wrapper."""

    status: int
    body: dict[str, Any] | str


def compute_sha256(file_paths: list[Path]) -> str:
    """Compute a deterministic SHA-256 digest over one or more local files."""
    hasher = hashlib.sha256()
    for path in sorted(file_paths, key=lambda p: p.as_posix()):
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    timeout: int = 30,
) -> HttpResponse:
    """Execute an HTTP request using urllib and parse JSON responses when present."""
    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", "InstitutionalMasterbook/2.3 (+https://github.com/EU-UNION-AI-PACT)")
    data: bytes | None = raw_body
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed: dict[str, Any] | str = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = raw
            return HttpResponse(status=resp.status, body=parsed)
    except urllib.error.HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {method} {url}: {err_text}") from exc


def build_multipart_file_payload(
    field_name: str, file_path: Path
) -> tuple[bytes, str]:
    """Build a multipart/form-data body for single-file uploads."""
    boundary = f"----MasterbookBoundary{uuid.uuid4().hex}"
    mime_type, _ = mimetypes.guess_type(file_path.name)
    content_type = mime_type or "application/octet-stream"
    file_bytes = file_path.read_bytes()

    parts = [
        f"--{boundary}\r\n".encode("utf-8"),
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def resolve_base_url(cfg: dict[str, Any], use_sandbox: bool) -> str:
    """Return sandbox URL when requested and available, else production base URL."""
    if use_sandbox and cfg.get("sandbox_url"):
        return str(cfg["sandbox_url"]).rstrip("/")
    return str(cfg["base_url"]).rstrip("/")


def load_dotenv_if_present(dotenv_path: Path) -> None:
    """Load KEY=VALUE pairs from a local .env file without overwriting existing env vars."""
    if not dotenv_path.is_file():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_auth_headers(cfg: dict[str, Any], required: bool = True) -> dict[str, str]:
    """Read the institution's API token from environment variables."""
    auth_cfg = cfg.get("auth", {})
    env_var = auth_cfg.get("env_var")
    token = os.environ.get(env_var, "").strip() if env_var else ""
    if not token:
        if required:
            raise ValueError(
                f"Missing API token in environment variable {env_var!r} "
                f"for {cfg.get('name', 'institution')}."
            )
        return {}

    header = auth_cfg.get("header", "Authorization")
    prefix = auth_cfg.get("prefix")
    value = f"{prefix} {token}" if prefix else token
    return {header: value}


def publish_zenodo(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Create a Zenodo deposition, upload artifacts, publish, and return proof."""
    base_url = resolve_base_url(cfg, use_sandbox)
    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "persistent_id": "10.5281/zenodo.0000000",
            "concept_doi": "10.5281/zenodo.0000000",
            "landing_page": f"{base_url}/records/0000000",
            "api_receipt_endpoint": f"{base_url}/api/records/0000000",
        }

    headers = get_auth_headers(cfg, required=True)
    license_id = metadata.get("license", "EUPL-1.2")
    dep_payload = {
        "metadata": {
            "title": metadata["project_title"],
            "upload_type": "software",
            "description": metadata.get("description", metadata["project_title"]),
            "license": license_id,
            "creators": [
                {
                    "name": metadata["author"]["name"],
                    "orcid": metadata["author"].get("orcid", "").replace("https://orcid.org/", ""),
                }
            ],
            "prereserve_doi": True,
        }
    }
    dep_resp = http_request(
        "POST", f"{base_url}/api/deposit/depositions", headers=headers, json_body=dep_payload
    )
    dep_data = dep_resp.body if isinstance(dep_resp.body, dict) else {}
    dep_id = dep_data["id"]
    bucket_url = dep_data["links"]["bucket"]

    for file_path in files:
        put_url = f"{bucket_url}/{urllib.parse.quote(file_path.name)}"
        put_headers = {**headers, "Content-Type": "application/octet-stream"}
        http_request("PUT", put_url, headers=put_headers, raw_body=file_path.read_bytes())

    pub_resp = http_request(
        "POST", f"{base_url}/api/deposit/depositions/{dep_id}/actions/publish", headers=headers
    )
    pub_data = pub_resp.body if isinstance(pub_resp.body, dict) else {}
    record_id = pub_data.get("id", dep_id)
    return {
        "institution": cfg["name"],
        "status": "PUBLISHED",
        "persistent_id": pub_data.get("doi", ""),
        "concept_doi": pub_data.get("conceptdoi", ""),
        "landing_page": pub_data.get("links", {}).get("html", f"{base_url}/records/{record_id}"),
        "api_receipt_endpoint": f"{base_url}/api/records/{record_id}",
    }


def ensure_dataverse_space(
    base_url: str,
    headers: dict[str, str],
    metadata: dict[str, Any],
) -> str:
    """Ensure the target sub-dataverse space exists; create & publish it via API if missing."""
    space_cfg = metadata.get("dataverse_space")
    if not space_cfg:
        return metadata.get("dataverse_parent_alias", "root")

    alias = space_cfg["alias"].strip()
    parent_alias = space_cfg.get("parent_alias", ":root").strip()
    if parent_alias == "root":
        parent_alias = ":root"
    encoded_alias = urllib.parse.quote(alias, safe="")

    try:
        resp = http_request("GET", f"{base_url}/api/dataverses/{encoded_alias}", headers=headers)
        if isinstance(resp.body, dict) and not resp.body.get("data", {}).get("isReleased", True):
            http_request(
                "POST",
                f"{base_url}/api/dataverses/{encoded_alias}/actions/:publish",
                headers=headers,
            )
        return alias
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise

    author_email = metadata.get("author", {}).get("email", "contact@example.org")
    space_payload = {
        "name": space_cfg.get("name", metadata["project_title"]),
        "alias": alias,
        "dataverseContacts": [{"contactEmail": author_email}],
        "affiliation": space_cfg.get("affiliation", "Independent Research"),
        "description": space_cfg.get("description", metadata.get("description", "")),
        "dataverseType": space_cfg.get("dataverse_type", "RESEARCH_PROJECTS"),
    }
    http_request(
        "POST",
        f"{base_url}/api/dataverses/{urllib.parse.quote(parent_alias, safe='')}",
        headers=headers,
        json_body=space_payload,
    )
    http_request(
        "POST",
        f"{base_url}/api/dataverses/{encoded_alias}/actions/:publish",
        headers=headers,
    )
    return alias


def publish_dataverse(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Ensure sub-dataverse space exists, create dataset, upload files, publish, and return proof."""
    base_url = resolve_base_url(cfg, use_sandbox)
    space_alias = metadata.get("dataverse_space", {}).get(
        "alias", metadata.get("dataverse_parent_alias", "root")
    )
    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "dataverse_space": space_alias,
            "space_url": f"{base_url}/dataverse/{space_alias}",
            "persistent_id": "doi:10.7910/DVN/DRYRUN",
            "cryptographic_unf": "UNF:6:dryrun==",
            "landing_page": "https://doi.org/10.7910/DVN/DRYRUN",
            "api_receipt_endpoint": f"{base_url}/api/datasets/:persistentId/?persistentId=doi:10.7910/DVN/DRYRUN",
        }

    headers = get_auth_headers(cfg, required=True)
    target_alias = ensure_dataverse_space(base_url, headers, metadata)
    license_id = metadata.get("license", "EUPL-1.2")
    desc_text = metadata.get("description", metadata["project_title"])
    if license_id:
        desc_text = f"{desc_text} (License: {license_id})"
    dataset_payload = {
        "datasetVersion": {
            "license": {"name": "CC0 1.0", "uri": "http://creativecommons.org/publicdomain/zero/1.0"},
            "metadataBlocks": {
                "citation": {
                    "fields": [
                        {"typeName": "title", "multiple": False, "typeClass": "primitive", "value": metadata["project_title"]},
                        {
                            "typeName": "author",
                            "multiple": True,
                            "typeClass": "compound",
                            "value": [
                                {
                                    "authorName": {"typeName": "authorName", "multiple": False, "typeClass": "primitive", "value": metadata["author"]["name"]},
                                    "authorIdentifierScheme": {"typeName": "authorIdentifierScheme", "multiple": False, "typeClass": "controlledVocabulary", "value": "ORCID"},
                                    "authorIdentifier": {
                                        "typeName": "authorIdentifier",
                                        "multiple": False,
                                        "typeClass": "primitive",
                                        "value": metadata["author"].get("orcid", "").replace("https://orcid.org/", ""),
                                    },
                                }
                            ],
                        },
                        {
                            "typeName": "dsDescription",
                            "multiple": True,
                            "typeClass": "compound",
                            "value": [
                                {
                                    "dsDescriptionValue": {
                                        "typeName": "dsDescriptionValue",
                                        "multiple": False,
                                        "typeClass": "primitive",
                                        "value": desc_text,
                                    }
                                }
                            ],
                        },
                        {
                            "typeName": "datasetContact",
                            "multiple": True,
                            "typeClass": "compound",
                            "value": [
                                {
                                    "datasetContactName": {
                                        "typeName": "datasetContactName",
                                        "multiple": False,
                                        "typeClass": "primitive",
                                        "value": metadata["author"]["name"],
                                    },
                                    "datasetContactEmail": {
                                        "typeName": "datasetContactEmail",
                                        "multiple": False,
                                        "typeClass": "primitive",
                                        "value": metadata["author"].get("email", "contact@example.org"),
                                    },
                                }
                            ],
                        },
                        {"typeName": "subject", "multiple": True, "typeClass": "controlledVocabulary", "value": ["Computer and Information Science"]},
                    ]
                }
            },
        }
    }
    create_resp = http_request(
        "POST",
        f"{base_url}/api/dataverses/{urllib.parse.quote(target_alias, safe='')}/datasets",
        headers=headers,
        json_body=dataset_payload,
    )
    create_body = create_resp.body if isinstance(create_resp.body, dict) else {}
    persistent_id = create_body["data"]["persistentId"]
    encoded_pid = urllib.parse.quote(persistent_id, safe="")

    last_unf = ""
    for file_path in files:
        body, content_type = build_multipart_file_payload("file", file_path)
        upload_headers = {**headers, "Content-Type": content_type}
        upload_resp = http_request(
            "POST",
            f"{base_url}/api/datasets/:persistentId/add?persistentId={encoded_pid}",
            headers=upload_headers,
            raw_body=body,
        )
        if isinstance(upload_resp.body, dict):
            uploaded_files = upload_resp.body.get("data", {}).get("files", [])
            if uploaded_files:
                last_unf = uploaded_files[0].get("dataFile", {}).get("UNF", "")

    http_request(
        "POST",
        f"{base_url}/api/datasets/:persistentId/actions/:publish?persistentId={encoded_pid}&type=major",
        headers=headers,
    )
    doi_path = persistent_id.replace("doi:", "")
    return {
        "institution": cfg["name"],
        "status": "PUBLISHED",
        "dataverse_space": target_alias,
        "space_url": f"{base_url}/dataverse/{target_alias}",
        "persistent_id": persistent_id,
        "cryptographic_unf": last_unf,
        "landing_page": f"https://doi.org/{doi_path}",
        "api_receipt_endpoint": f"{base_url}/api/datasets/:persistentId/?persistentId={encoded_pid}",
    }


def publish_osf(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Create an OSF project node and upload files via WaterButler."""
    base_url = resolve_base_url(cfg, use_sandbox)
    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "persistent_id": "osf-dryrun",
            "landing_page": "https://osf.io/dryrun/",
            "api_receipt_endpoint": f"{base_url}/v2/nodes/dryrun/",
        }

    headers = get_auth_headers(cfg, required=True)
    node_payload = {
        "data": {
            "type": "nodes",
            "attributes": {
                "title": metadata["project_title"],
                "category": "project",
                "description": metadata.get("description", ""),
                "public": True,
            },
        }
    }
    node_resp = http_request("POST", f"{base_url}/v2/nodes/", headers=headers, json_body=node_payload)
    node_data = node_resp.body["data"] if isinstance(node_resp.body, dict) else {}
    node_id = node_data["id"]

    files_host = "files.test.osf.io" if use_sandbox else "files.osf.io"
    for file_path in files:
        wb_url = f"https://{files_host}/v1/resources/{node_id}/providers/osfstorage/?kind=file&name={urllib.parse.quote(file_path.name)}"
        http_request("PUT", wb_url, headers=headers, raw_body=file_path.read_bytes())

    return {
        "institution": cfg["name"],
        "status": "CREATED",
        "persistent_id": node_id,
        "landing_page": node_data.get("links", {}).get("html", f"https://osf.io/{node_id}/"),
        "api_receipt_endpoint": f"{base_url}/v2/nodes/{node_id}/",
    }


def publish_software_heritage(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    _files: list[Path],
    _use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Trigger Software Heritage archival for the configured Git repository URL."""
    base_url = cfg["base_url"].rstrip("/")
    repo_url = metadata.get("git_repository_url", "").strip()
    if not repo_url:
        raise ValueError("Missing 'git_repository_url' in metadata for Software Heritage.")

    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "origin_url": repo_url,
            "save_request_status": "pending",
            "api_receipt_endpoint": f"{base_url}/api/1/origin/save/git/url/{repo_url}/",
        }

    headers = get_auth_headers(cfg, required=False)
    save_url = f"{base_url}/api/1/origin/save/git/url/{repo_url}/"
    resp = http_request("POST", save_url, headers=headers)
    body = resp.body if isinstance(resp.body, dict) else {}
    return {
        "institution": cfg["name"],
        "status": "ARCHIVAL_REQUESTED",
        "origin_url": repo_url,
        "save_request_status": body.get("save_request_status", "accepted"),
        "save_task_status": body.get("save_task_status", "not created"),
        "api_receipt_endpoint": save_url,
    }


def publish_figshare(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Create a Figshare article, upload files, publish, and return proof."""
    base_url = resolve_base_url(cfg, use_sandbox)
    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "article_id": 0,
            "persistent_id": "10.6084/m9.figshare.0000000",
            "landing_page": "https://figshare.com/articles/dataset/dryrun/0000000",
            "api_receipt_endpoint": f"{base_url}/v2/articles/0",
        }

    headers = get_auth_headers(cfg, required=True)
    article_payload: dict[str, Any] = {
        "title": metadata["project_title"],
        "description": (
            f"{metadata.get('description', metadata['project_title'])}\n\n"
            f"License: {metadata.get('license', 'EUPL-1.2')}"
        ),
        "defined_type": "dataset",
        "categories": metadata.get("figshare_categories", [25357, 28918]),
        "keywords": metadata.get("keywords", ["research"]),
    }
    create_resp = http_request(
        "POST", f"{base_url}/v2/account/articles", headers=headers, json_body=article_payload
    )
    create_body = create_resp.body if isinstance(create_resp.body, dict) else {}
    location = str(create_body.get("location", ""))
    article_id = location.rstrip("/").split("/")[-1]

    for file_path in files:
        file_bytes = file_path.read_bytes()
        md5_hex = hashlib.md5(file_bytes).hexdigest()
        init_resp = http_request(
            "POST",
            f"{base_url}/v2/account/articles/{article_id}/files",
            headers=headers,
            json_body={"name": file_path.name, "size": len(file_bytes), "md5": md5_hex},
        )
        init_body = init_resp.body if isinstance(init_resp.body, dict) else {}
        file_location = str(init_body.get("location", ""))
        file_meta = http_request("GET", file_location, headers=headers).body
        if isinstance(file_meta, dict) and file_meta.get("upload_url"):
            upload_url = str(file_meta["upload_url"])
            http_request("PUT", f"{upload_url}/1", headers=headers, raw_body=file_bytes)
            http_request("POST", file_location, headers=headers)

    http_request("POST", f"{base_url}/v2/account/articles/{article_id}/publish", headers=headers)
    article_info = http_request("GET", f"{base_url}/v2/articles/{article_id}", headers=headers).body
    info_dict = article_info if isinstance(article_info, dict) else {}
    return {
        "institution": cfg["name"],
        "status": "PUBLISHED",
        "article_id": article_id,
        "persistent_id": info_dict.get("doi", ""),
        "landing_page": info_dict.get("url_public_html", f"https://figshare.com/articles/dataset/{article_id}"),
        "api_receipt_endpoint": f"{base_url}/v2/articles/{article_id}",
    }


def request_dryad_token(base_url: str, client_id: str, client_secret: str) -> str:
    """Exchange Dryad OAuth2 client_id and client_secret for a Bearer access token."""
    form = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")
    resp = http_request(
        "POST",
        f"{base_url}/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        raw_body=form,
    )
    body = resp.body if isinstance(resp.body, dict) else {}
    token = str(body.get("access_token", "")).strip()
    if not token:
        raise RuntimeError(f"Dryad /oauth/token response did not contain access_token: {body}")
    return token


def publish_dryad(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Create a Dryad dataset submission, upload files, and return proof."""
    base_url = resolve_base_url(cfg, use_sandbox)
    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "persistent_id": "doi:10.5061/dryad.dryrun",
            "landing_page": f"{base_url}/stash/dataset/doi:10.5061/dryad.dryrun",
            "api_receipt_endpoint": f"{base_url}/api/v2/datasets/doi%3A10.5061%2Fdryad.dryrun",
        }

    headers = get_auth_headers(cfg, required=False)
    if not headers:
        client_id = os.environ.get("DRYAD_CLIENT_ID", "").strip()
        client_secret = os.environ.get("DRYAD_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            headers = {"Authorization": f"Bearer {request_dryad_token(base_url, client_id, client_secret)}"}
        else:
            headers = get_auth_headers(cfg, required=True)
    name_parts = metadata["author"]["name"].rsplit(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else name_parts[0]
    payload = {
        "title": metadata["project_title"],
        "abstract": metadata.get("description", metadata["project_title"]),
        "authors": [
            {
                "firstName": first_name,
                "lastName": last_name,
                "orcid": metadata["author"].get("orcid", "").replace("https://orcid.org/", ""),
            }
        ],
    }
    create_resp = http_request("POST", f"{base_url}/api/v2/datasets", headers=headers, json_body=payload)
    body = create_resp.body if isinstance(create_resp.body, dict) else {}
    dataset_id = str(body.get("identifier", ""))
    encoded_id = urllib.parse.quote(dataset_id, safe="")

    for file_path in files:
        http_request(
            "PUT",
            f"{base_url}/api/v2/datasets/{encoded_id}/files/{urllib.parse.quote(file_path.name)}",
            headers=headers,
            raw_body=file_path.read_bytes(),
        )

    return {
        "institution": cfg["name"],
        "status": "SUBMITTED",
        "persistent_id": dataset_id,
        "landing_page": f"{base_url}/stash/dataset/{dataset_id}",
        "api_receipt_endpoint": f"{base_url}/api/v2/datasets/{encoded_id}",
    }


def publish_internet_archive(
    cfg: dict[str, Any],
    metadata: dict[str, Any],
    files: list[Path],
    _use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Upload artifacts to Internet Archive via IAS3 API and return proof."""
    base_url = cfg["base_url"].rstrip("/")
    slug = "".join(
        ch if ch.isalnum() or ch in "-_" else "-"
        for ch in metadata["project_title"].lower()
    ).strip("-")[:48] or "masterbook-artifact"
    identifier = f"masterbook-{slug}"

    if dry_run:
        return {
            "institution": cfg["name"],
            "status": "DRY_RUN",
            "identifier": identifier,
            "landing_page": f"https://archive.org/details/{identifier}",
            "api_receipt_endpoint": f"https://archive.org/metadata/{identifier}",
        }

    headers = get_auth_headers(cfg, required=True)
    headers["x-archive-auto-make-bucket"] = "1"
    headers["x-archive-meta-title"] = metadata["project_title"]
    headers["x-archive-meta-mediatype"] = "data"

    for file_path in files:
        put_url = f"{base_url}/{identifier}/{urllib.parse.quote(file_path.name)}"
        http_request("PUT", put_url, headers=headers, raw_body=file_path.read_bytes())

    return {
        "institution": cfg["name"],
        "status": "ARCHIVED",
        "identifier": identifier,
        "landing_page": f"https://archive.org/details/{identifier}",
        "api_receipt_endpoint": f"https://archive.org/metadata/{identifier}",
    }


HANDLERS = {
    "zenodo": publish_zenodo,
    "dataverse": publish_dataverse,
    "osf": publish_osf,
    "figshare": publish_figshare,
    "dryad": publish_dryad,
    "internet_archive": publish_internet_archive,
    "software_heritage": publish_software_heritage,
}


def run_masterbook(
    registry_path: Path,
    metadata_path: Path,
    output_path: Path,
    selected_institutions: list[str] | None,
    use_sandbox: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Execute the 5-stage Masterbook pipeline and write publication_certificate.json."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    base_dir = metadata_path.parent
    artifact_files = [
        (base_dir / rel_path).resolve() for rel_path in metadata.get("artifact_files", [])
    ]
    missing = [str(p) for p in artifact_files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Artifact file(s) not found: {', '.join(missing)}")

    local_sha256 = compute_sha256(artifact_files) if artifact_files else ""
    institutions_cfg: dict[str, Any] = registry.get("institutions", {})

    targets = (
        selected_institutions
        if selected_institutions
        else [key for key, cfg in institutions_cfg.items() if cfg.get("enabled")]
    )

    certifications: list[dict[str, Any]] = []
    for key in targets:
        if key not in institutions_cfg:
            raise KeyError(f"Unknown institution key in --institutions: {key}")
        cfg = institutions_cfg[key]
        driver = cfg.get("driver", key)
        handler = HANDLERS.get(driver)
        if handler is None:
            certifications.append(
                {
                    "institution": cfg.get("name", key),
                    "role": cfg.get("role", "manual"),
                    "status": "MANUAL_OR_QUERY_ONLY",
                    "docs_url": cfg.get("docs_url", ""),
                    "endpoints": cfg.get("endpoints", {}),
                    "note": "No automated publish handler (query/verify API or manual/SFTP/JWK required).",
                }
            )
            continue

        try:
            receipt = handler(cfg, metadata, artifact_files, use_sandbox, dry_run)
        except Exception as exc:  # noqa: BLE001 - record failure in manifest
            receipt = {
                "institution": cfg.get("name", key),
                "status": "ERROR",
                "error": str(exc),
            }
        certifications.append(receipt)

    certificate = {
        "masterbook_version": registry.get("masterbook_version", "1.0.0"),
        "project_title": metadata["project_title"],
        "author_orcid": metadata["author"].get("orcid", ""),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sandbox_mode": use_sandbox,
        "dry_run": dry_run,
        "local_artifact_sha256": local_sha256,
        "artifact_files": [p.name for p in artifact_files],
        "certifications": certifications,
    }
    output_path.write_text(json.dumps(certificate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return certificate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Institutional API-Masterbook: Multi-repository publish & proof generator."
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("endpoints_registry.json"),
        help="Path to endpoints_registry.json (default: endpoints_registry.json)",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        help="Path to package metadata JSON (see package_metadata.example.json)",
    )
    parser.add_argument(
        "--list-institutions",
        action="store_true",
        help="List all registered institutions, roles, drivers, and auth env vars.",
    )
    parser.add_argument(
        "--export-masterbook",
        type=Path,
        help="Generate a unified Markdown Masterbook file (e.g. MASTERBOOK.md) from the registry.",
    )
    parser.add_argument(
        "--export-submissions",
        type=Path,
        help="Export all submission/deposit endpoints across primary and --extra-registry files.",
    )
    parser.add_argument(
        "--extra-registry",
        type=Path,
        action="append",
        default=[],
        help="Additional registry JSON file(s) to include in --export-submissions.",
    )
    parser.add_argument(
        "--probe-endpoints",
        type=Path,
        help="Probe public/keyless endpoints in the registry and write a JSON status report.",
    )
    parser.add_argument(
        "--save-verified",
        action="store_true",
        help="Persist verified connection URLs and probe statuses back into the registry JSON.",
    )
    parser.add_argument(
        "--query",
        metavar="INSTITUTION_KEY",
        help="Query a registered institution's verified/read endpoint and print the response.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("publication_certificate.json"),
        help="Output path for the proof manifest (default: publication_certificate.json)",
    )
    parser.add_argument(
        "--institutions",
        nargs="+",
        help="Specific institution keys to target (default: all enabled in registry)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Use sandbox/demo endpoints where supported (Zenodo, Dataverse, OSF).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute hashes and simulate API calls without network requests.",
    )
    return parser.parse_args(argv)


def print_institutions(registry_path: Path) -> None:
    """Print a summary table of all institutions in the registry."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for key, cfg in registry.get("institutions", {}).items():
        env_var = cfg.get("auth", {}).get("env_var", "-")
        enabled = "enabled" if cfg.get("enabled") else "disabled"
        print(
            f"{key:<24} | {cfg.get('role', '-'):<12} | "
            f"{cfg.get('driver', key):<18} | {enabled:<8} | {env_var}"
        )


def export_masterbook_markdown(registry_path: Path, output_path: Path) -> int:
    """Generate a unified Markdown Masterbook grouped by category from the registry."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    institutions: dict[str, Any] = registry.get("institutions", {})

    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for key, cfg in institutions.items():
        category = cfg.get("category", "Sonstige")
        grouped.setdefault(category, []).append((key, cfg))

    lines = [
        f"# Institutional API-Masterbook (v{registry.get('masterbook_version', '1.0.0')})",
        "",
        f"Gesamtzahl registrierter Institutionen & Endpunkte: **{len(institutions)}**",
        "",
        "Dieses Masterbook wird direkt aus `endpoints_registry.json` generiert.",
        "",
    ]
    for category, items in grouped.items():
        lines.extend([f"## {category}", ""])
        lines.append("| Key | Institution | Rolle | Treiber | Auth / Env-Var | Base URL | Endpunkte | Doku |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for key, cfg in items:
            auth = cfg.get("auth", {})
            auth_str = auth.get("env_var") or auth.get("type", "-")
            endpoints = "<br>".join(
                f"`{k}`: `{v}`" for k, v in cfg.get("endpoints", {}).items()
            ) or "-"
            docs = cfg.get("docs_url", "-")
            lines.append(
                f"| `{key}` | {cfg.get('name', key)} | `{cfg.get('role', '-')}` | "
                f"`{cfg.get('driver', key)}` | `{auth_str}` | `{cfg.get('base_url', '-')}` | "
                f"{endpoints} | {docs} |"
            )
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return len(institutions)


SUBMISSION_KEYS = (
    "create_space",
    "create_dataset",
    "upload_file",
    "publish",
    "save_origin",
    "submit_tx",
    "pin_file",
    "create_release",
    "transfer",
    "sword",
    "submit",
    "submit_portal",
    "submit_contact",
)


def export_submissions_markdown(
    registry_paths: list[Path], output_path: Path
) -> int:
    """Export all submission/deposit endpoints from one or more registries into Markdown."""
    all_entries: list[tuple[str, str, dict[str, Any]]] = []
    for reg_path in registry_paths:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        for key, cfg in data.get("institutions", {}).items():
            all_entries.append((reg_path.name, key, cfg))

    lines = [
        "# Alle Einreichungs-Endpunkte (Submission & Deposit Catalog)",
        "",
        f"Gesamtzahl erfasster Institutionen: **{len(all_entries)}**",
        "",
        "*(Hinweis: Die 53.804 deduplizierten Einzel-Domains mit `submit_contact`-Endpunkten liegen zusätzlich in `global_domains_index.jsonl`.)*",
        "",
        "| Quelle | Key | Institution | Kategorie | Auth | Einreichungs-Endpunkte |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for src_name, key, cfg in all_entries:
        eps = cfg.get("endpoints", {})
        sub_parts = [f"`{k}`: `{eps[k]}`" for k in SUBMISSION_KEYS if k in eps]
        if not sub_parts:
            sub_parts = [f"`docs`: {cfg.get('docs_url', '-')}"]
        auth = cfg.get("auth", {})
        auth_str = auth.get("env_var") or auth.get("type", "-")
        lines.append(
            f"| `{src_name}` | `{key}` | {cfg.get('name', key)} | "
            f"{cfg.get('category', '-')} | `{auth_str}` | {'<br>'.join(sub_parts)} |"
        )
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return len(all_entries)


def candidate_probe_urls(cfg: dict[str, Any]) -> list[str]:
    """Build an ordered list of candidate GET URLs for probing an institution."""
    eps = cfg.get("endpoints", {})
    base = str(cfg.get("base_url", "")).rstrip("/")
    candidates: list[str] = []

    def add_raw(raw_val: str) -> None:
        raw = str(raw_val).strip()
        if not raw or "{" in raw or raw.startswith("N/A"):
            return
        if raw.startswith(("GET ", "HEAD ", "POST ")):
            raw = raw.split(" ", 1)[1].strip()
        if " (" in raw:
            raw = raw.split(" (", 1)[0].strip()
        url = raw if raw.startswith("http") else (f"{base}{raw}" if raw.startswith("/") and base else "")
        if url.startswith("http") and url not in candidates:
            candidates.append(url)

    for field in ("validate", "verify", "stac_api", "cables", "rdap_bootstrap", "indicators", "countries"):
        if field in eps:
            add_raw(eps[field])
    for k, val in eps.items():
        if k not in ("submit", "sword", "transfer", "create_space", "create_dataset", "upload_file", "publish"):
            add_raw(str(val))
    if base:
        add_raw(f"{base}/")
    if cfg.get("docs_url"):
        add_raw(str(cfg["docs_url"]))
    return candidates


def probe_single_institution(key: str, cfg: dict[str, Any], timeout: int = 12) -> dict[str, Any]:
    """Probe candidate URLs with fallback and classify live/auth-gated/unreachable endpoints."""
    urls = candidate_probe_urls(cfg)
    if not urls:
        return {"key": key, "institution": cfg.get("name", key), "status": "SKIPPED", "url": ""}

    requires_auth = cfg.get("auth", {}).get("type", "none") != "none"
    last_result: dict[str, Any] = {}

    for url in urls:
        start = time.monotonic()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; InstitutionalMasterbook/2.3)",
                "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.8",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed_ms = round((time.monotonic() - start) * 1000, 1)
                return {
                    "key": key,
                    "institution": cfg.get("name", key),
                    "category": cfg.get("category", "-"),
                    "status": "OK",
                    "http_status": resp.status,
                    "latency_ms": elapsed_ms,
                    "url": url,
                }
        except urllib.error.HTTPError as exc:
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            if exc.code in (401, 403) and requires_auth:
                last_result = {
                    "key": key,
                    "institution": cfg.get("name", key),
                    "category": cfg.get("category", "-"),
                    "status": "AUTH_REQUIRED",
                    "http_status": exc.code,
                    "latency_ms": elapsed_ms,
                    "url": url,
                }
                continue
            last_result = {
                "key": key,
                "institution": cfg.get("name", key),
                "category": cfg.get("category", "-"),
                "status": "HTTP_ERROR",
                "http_status": exc.code,
                "latency_ms": elapsed_ms,
                "url": url,
            }
        except Exception as exc:  # noqa: BLE001
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            if not last_result:
                last_result = {
                    "key": key,
                    "institution": cfg.get("name", key),
                    "category": cfg.get("category", "-"),
                    "status": "UNREACHABLE",
                    "error": str(exc),
                    "latency_ms": elapsed_ms,
                    "url": url,
                }

    return last_result


def probe_registry_endpoints(
    registry_path: Path,
    output_path: Path,
    selected_institutions: list[str] | None = None,
    max_workers: int = 16,
) -> dict[str, Any]:
    """Probe selected or all keyless/public endpoints in the registry concurrently."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    institutions: dict[str, Any] = registry.get("institutions", {})

    if selected_institutions:
        targets = [(k, institutions[k]) for k in selected_institutions if k in institutions]
    else:
        targets = list(institutions.items())

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(probe_single_institution, k, cfg) for k, cfg in targets]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: r["key"])
    ok_count = sum(1 for r in results if r["status"] == "OK")
    auth_count = sum(1 for r in results if r["status"] == "AUTH_REQUIRED")
    report = {
        "masterbook_version": registry.get("masterbook_version", "2.3.0"),
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_probed": len(results),
        "reachable_ok": ok_count,
        "auth_required": auth_count,
        "results": results,
    }
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def persist_verified_probes(registry_path: Path, report: dict[str, Any]) -> int:
    """Persist verified connection URLs and probe statuses into the registry JSON."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    institutions: dict[str, Any] = registry.get("institutions", {})
    verified_at = report.get("timestamp_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    updated = 0
    for item in report.get("results", []):
        key = item.get("key")
        if key in institutions and item.get("status") in ("OK", "AUTH_REQUIRED"):
            institutions[key]["verified_connection"] = {
                "status": item["status"],
                "http_status": item.get("http_status"),
                "verified_url": item.get("url", ""),
                "latency_ms": item.get("latency_ms"),
                "verified_at_utc": verified_at,
            }
            updated += 1
    registry["masterbook_version"] = "2.4.0"
    registry_path.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return updated


def query_institution_endpoint(registry_path: Path, key: str) -> dict[str, Any]:
    """Query a registered institution's verified/primary GET endpoint and return parsed output."""
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    institutions: dict[str, Any] = registry.get("institutions", {})
    if key not in institutions:
        raise KeyError(f"Unknown institution key: {key}")
    cfg = institutions[key]
    verified_url = cfg.get("verified_connection", {}).get("verified_url", "")
    urls = [verified_url] if verified_url else candidate_probe_urls(cfg)
    if not urls or not urls[0].startswith("http"):
        raise ValueError(f"No queryable HTTP URL found for {key}")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; InstitutionalMasterbook/2.4)",
        "Accept": "application/json, application/xml, text/plain;q=0.9, */*;q=0.8",
        **get_auth_headers(cfg, required=False),
    }
    resp = http_request("GET", urls[0], headers=headers, timeout=15)
    return {
        "key": key,
        "institution": cfg.get("name", key),
        "url": urls[0],
        "http_status": resp.status,
        "data": resp.body if isinstance(resp.body, dict) else str(resp.body)[:1000],
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    load_dotenv_if_present(Path(".env"))
    args = parse_args(argv)
    if args.list_institutions:
        print_institutions(args.registry)
        return 0
    if args.export_masterbook:
        count = export_masterbook_markdown(args.registry, args.export_masterbook)
        print(f"Exported {count} institutions to {args.export_masterbook}")
        return 0
    if args.export_submissions:
        registries = [args.registry, *args.extra_registry]
        count = export_submissions_markdown(registries, args.export_submissions)
        print(f"Exported {count} submission entries to {args.export_submissions}")
        return 0
    if args.probe_endpoints:
        report = probe_registry_endpoints(args.registry, args.probe_endpoints, args.institutions)
        msg = (
            f"Probed {report['total_probed']} endpoints -> {report['reachable_ok']} OK, "
            f"{report.get('auth_required', 0)} AUTH_REQUIRED (wrote {args.probe_endpoints})"
        )
        if args.save_verified:
            updated = persist_verified_probes(args.registry, report)
            msg += f" | persisted {updated} verified connections into {args.registry}"
        print(msg)
        return 0
    if args.query:
        result = query_institution_endpoint(args.registry, args.query)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if not args.metadata:
        print(
            "Error: --metadata is required unless --list-institutions, --export-masterbook, or --export-submissions is set.",
            file=sys.stderr,
        )
        return 2
    try:
        certificate = run_masterbook(
            registry_path=args.registry,
            metadata_path=args.metadata,
            output_path=args.output,
            selected_institutions=args.institutions,
            use_sandbox=args.sandbox,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {args.output} (SHA-256: {certificate['local_artifact_sha256']})")
    for item in certificate["certifications"]:
        print(f" - {item['institution']}: {item['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
