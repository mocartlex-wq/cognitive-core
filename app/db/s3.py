import urllib3
from minio import Minio

from app.config import settings

_client: Minio | None = None


def get_s3() -> Minio:
    global _client
    if _client is None:
        # Bounded HTTP client: when MinIO is unreachable, fail fast (a few
        # seconds) instead of urllib3's long default retries — a sync minio
        # call on the async event loop would otherwise block the whole worker.
        _http = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=2.0, read=5.0),
            retries=urllib3.Retry(total=1, connect=1, read=1, backoff_factor=0.2),
            maxsize=10,
        )
        _client = Minio(
            settings.s3_endpoint.replace("http://", "").replace("https://", ""),
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
            http_client=_http,
        )
    return _client


def snapshot_key(
    domain: str,
    snapshot_id: str,
    owner_user_id: str | None = None,
) -> str:
    """S3-ключ для L4-снапшота.

    PR #23 multi-tenant: per-owner path prefix. None = legacy admin путь.
    Format:
      - owner-aware: l4/<owner_uuid>/<domain>/<id>.json
      - legacy:     l4/<domain>/<id>.json (admin/migration)
    """
    sid = str(snapshot_id)
    if owner_user_id:
        return f"l4/{owner_user_id}/{domain}/{sid}.json"
    return f"l4/{domain}/{sid}.json"


def media_key(
    kind: str,
    media_id: str,
    filename: str,
    owner_user_id: str | None = None,
) -> str:
    """S3-ключ для media (video/image/audio + frames).

    PR #23 multi-tenant:
      - owner-aware: <kind>/<owner_uuid>/<media_id>/<filename>
      - legacy:     <kind>/<media_id>/<filename>
    """
    if owner_user_id:
        return f"{kind}/{owner_user_id}/{media_id}/{filename}"
    return f"{kind}/{media_id}/{filename}"


def init_s3() -> None:
    """Создаёт бакет для L4, если его нет."""
    client = get_s3()
    bucket = settings.s3_bucket
    found = client.bucket_exists(bucket)
    if not found:
        client.make_bucket(bucket)
        # Object Lock настраивается при создании бакета через mc или API
        # В MVP: создаём без Object Lock, включаем через MinIO Console (порт 9001)
