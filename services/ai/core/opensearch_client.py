"""Async AWS OpenSearch (including OpenSearch Serverless `aoss`) HTTP client with SigV4 signing."""

import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

_TIMEOUT_SEC = float(os.getenv("OPENSEARCH_TIMEOUT_SEC", "30"))


def opensearch_host() -> str:
    raw = os.getenv("OPENSEARCH_HOST", "").strip().rstrip("/")
    if not raw:
        return ""
    return raw if raw.startswith(("http://", "https://")) else f"https://{raw}"


def opensearch_index() -> str:
    return os.getenv("OPENSEARCH_INDEX", "compliance-references").strip() or "compliance-references"


def is_opensearch_enabled() -> bool:
    return bool(opensearch_host())


def _region_and_service(endpoint: str) -> tuple[str, str]:
    parsed = urlparse(endpoint)
    hostname = parsed.hostname or ""
    region = os.getenv("AWS_REGION", "").strip() or os.getenv("AWS_DEFAULT_REGION", "").strip()
    if not region and ".amazonaws.com" in hostname:
        parts = hostname.split(".")
        if len(parts) >= 4:
            region = parts[1]
    region = region or "ca-central-1"
    service = os.getenv("OPENSEARCH_SERVICE", "").strip() or ("aoss" if ".aoss." in hostname else "es")
    return region, service


def _signed_headers(method: str, url: str, body: bytes) -> Dict[str, str]:
    endpoint = opensearch_host()
    region, service = _region_and_service(endpoint)
    creds = boto3.Session(region_name=region).get_credentials()
    if creds is None:
        raise RuntimeError("AWS credentials are not available for OpenSearch SigV4 signing.")
    frozen = creds.get_frozen_credentials()
    parsed = urlparse(url)
    headers: Dict[str, str] = {"Host": parsed.netloc, "Content-Type": "application/json"}
    req = AWSRequest(method=method.upper(), url=url, data=body, headers=headers)
    SigV4Auth(frozen, service, region).add_auth(req)
    return dict(req.headers.items())


async def request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    raw_body: Optional[str] = None,
    timeout: float = _TIMEOUT_SEC,
) -> httpx.Response:
    endpoint = opensearch_host()
    if not endpoint:
        raise RuntimeError("OPENSEARCH_HOST is not configured.")
    url = f"{endpoint}/{path.lstrip('/')}"
    if raw_body is not None:
        body_bytes = raw_body.encode("utf-8")
    elif json_body is not None:
        body_bytes = json.dumps(json_body, default=str).encode("utf-8")
    else:
        body_bytes = b""
    headers = _signed_headers(method, url, body_bytes)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(method.upper(), url, content=body_bytes, headers=headers)


async def request_json(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    raw_body: Optional[str] = None,
    timeout: float = _TIMEOUT_SEC,
) -> Dict[str, Any]:
    resp = await request(method, path, json_body=json_body, raw_body=raw_body, timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenSearch {method} {path} failed ({resp.status_code}): {resp.text[:400]}")
    return resp.json() if resp.content else {}


async def bulk(operations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute NDJSON bulk operations against OpenSearch."""
    if not operations:
        return {"errors": False, "items": []}
    ndjson = "\n".join(json.dumps(op, default=str) for op in operations) + "\n"
    data = await request_json("POST", "/_bulk", raw_body=ndjson)
    if data.get("errors"):
        failed = [it for it in data.get("items", []) if any(v.get("error") for v in it.values())]
        if failed:
            raise RuntimeError(f"OpenSearch bulk write had errors: {str(failed[0])[:400]}")
    return data
