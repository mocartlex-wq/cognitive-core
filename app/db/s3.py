import uuid
from minio import Minio
from app.config import settings

_client: Minio | None = None


def get_s3() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            settings.s3_endpoint.replace("http://", "").replace("https://", ""),
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        )
    return _client


def snapshot_key(domain: str, snapshot_id: str) -> str:
    """S3-ключ для L4-снапшота."""
    sid = str(snapshot_id)
    return f"l4/{domain}/{sid}.json"


def init_s3() -> None:
    """Создаёт бакет для L4, если его нет."""
    client = get_s3()
    bucket = settings.s3_bucket
    found = client.bucket_exists(bucket)
    if not found:
        client.make_bucket(bucket)
        # Object Lock настраивается при создании бакета через mc или API
        # В MVP: создаём без Object Lock, включаем через MinIO Console (порт 9001)
