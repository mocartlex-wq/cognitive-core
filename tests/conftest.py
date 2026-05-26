import httpx
import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api_url():
    return "http://localhost:8000"


@pytest.fixture
def api_key():
    return "key-design-001"


@pytest.fixture
def headers(api_key):
    return {"X-API-Key": api_key, "Content-Type": "application/json"}


@pytest.fixture
async def client(api_url):
    async with httpx.AsyncClient(base_url=api_url, timeout=60.0) as c:
        yield c


@pytest.fixture
def known_domain():
    return f"test_{__name__.rsplit('.', 1)[-1]}".replace("_", "")[:32]
