"""AWS S3 storage helper (ported from administration backend) with local-cache hydration."""

import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_s3_client = None
_s3_client_lock = threading.Lock()


def s3_enabled() -> bool:
    return bool((os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip())


def s3_key_for(category: str, rel_path: str) -> str:
    prefix = (os.getenv("UPLOAD_S3_PREFIX") or "data/uploads").strip().strip("/")
    clean_rel = rel_path.replace("\\", "/").lstrip("/")
    return f"{prefix}/{category}/{clean_rel}" if prefix else f"{category}/{clean_rel}"


def _botocore_config() -> Config:
    return Config(
        connect_timeout=int(os.getenv("AWS_S3_CONNECT_TIMEOUT", "5")),
        read_timeout=int(os.getenv("AWS_S3_READ_TIMEOUT", "30")),
        retries={"max_attempts": int(os.getenv("AWS_S3_MAX_ATTEMPTS", "2")), "mode": "standard"},
    )


def _bucket_name() -> str:
    bucket = (os.getenv("AWS_S3_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip()
    if not bucket:
        raise ValueError("AWS_S3_BUCKET_NAME environment variable is not configured.")
    return bucket


def _aws_region() -> str:
    return os.getenv("AWS_S3_REGION") or os.getenv("AWS_REGION", "ca-central-1")


def _is_enabled(value: Optional[str], *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _request_security_args() -> dict[str, Any]:
    expected_owner = os.getenv("AWS_S3_EXPECTED_BUCKET_OWNER", "").strip()
    return {"ExpectedBucketOwner": expected_owner} if expected_owner else {}


def _upload_security_args() -> dict[str, Any]:
    algorithm = os.getenv("AWS_S3_SERVER_SIDE_ENCRYPTION", "AES256").strip()
    if algorithm not in {"AES256", "aws:kms"}:
        raise ValueError("AWS_S3_SERVER_SIDE_ENCRYPTION must be 'AES256' or 'aws:kms'.")
    args: dict[str, Any] = {
        "ServerSideEncryption": algorithm,
        "ChecksumAlgorithm": "SHA256",
        "CacheControl": "private, no-store",
        **_request_security_args(),
    }
    if algorithm == "aws:kms":
        kms_key_id = os.getenv("AWS_S3_KMS_KEY_ID", "").strip()
        if kms_key_id:
            args["SSEKMSKeyId"] = kms_key_id
        args["BucketKeyEnabled"] = _is_enabled(os.getenv("AWS_S3_BUCKET_KEY_ENABLED"), default=True)
    return args


def get_s3_client():
    """Returns the shared boto3 S3 client, building it on first use."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    with _s3_client_lock:
        if _s3_client is not None:
            return _s3_client

        access_key = (os.getenv("AWS_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY") or "").strip()
        secret_key = (os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()

        if "AWS_ACCESS_KEY_ID" in os.environ and not os.environ["AWS_ACCESS_KEY_ID"].strip():
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
        if "AWS_SECRET_ACCESS_KEY" in os.environ and not os.environ["AWS_SECRET_ACCESS_KEY"].strip():
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)

        kwargs: dict[str, Any] = {"region_name": _aws_region(), "use_ssl": True, "config": _botocore_config()}
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        _s3_client = boto3.client("s3", **kwargs)
        return _s3_client


def upload_file_to_s3(file_bytes: bytes, s3_key: str, content_type: Optional[str] = None) -> str:
    """Uploads file bytes to S3 and returns the S3 URL."""
    bucket = _bucket_name()
    s3_client = get_s3_client()
    extra_args = _upload_security_args()
    if content_type:
        extra_args["ContentType"] = content_type

    try:
        s3_client.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes, **extra_args)
        logger.info("Successfully uploaded file to S3: %s", s3_key)
        return f"https://{bucket}.s3.{_aws_region()}.amazonaws.com/{s3_key}"
    except (BotoCoreError, ClientError) as exc:
        try:
            simple_args = {"ContentType": content_type} if content_type else {}
            s3_client.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes, **simple_args)
            logger.info("Successfully uploaded file to S3 (standard put): %s", s3_key)
            return f"https://{bucket}.s3.{_aws_region()}.amazonaws.com/{s3_key}"
        except Exception:
            pass
        logger.error("Failed to upload %s to S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 upload error: {exc}") from exc


def download_file_from_s3(s3_key: str) -> bytes:
    """Downloads file content from S3 as bytes."""
    bucket = _bucket_name()
    s3_client = get_s3_client()
    try:
        response = s3_client.get_object(Bucket=bucket, Key=s3_key, **_request_security_args())
        return response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to download %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 download error: {exc}") from exc


def delete_file_from_s3(s3_key: str) -> bool:
    """Deletes a file object from S3."""
    bucket = _bucket_name()
    s3_client = get_s3_client()
    try:
        s3_client.delete_object(Bucket=bucket, Key=s3_key, **_request_security_args())
        logger.info("Successfully deleted %s from S3", s3_key)
        return True
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to delete %s from S3: %s", s3_key, exc)
        raise RuntimeError(f"S3 delete error: {exc}") from exc


def ensure_local_file(path: Path, category: str, rel_path: str) -> Path:
    """Return `path` if present on disk, otherwise hydrate it from S3 when configured."""
    if path.is_file():
        return path
    if not s3_enabled():
        return path
    try:
        data = download_file_from_s3(s3_key_for(category, rel_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except Exception as exc:
        logger.warning("Could not hydrate %s from S3 (%s): %s", path, rel_path, exc)
    return path
