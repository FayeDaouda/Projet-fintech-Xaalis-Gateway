"""Fixtures partagées : PostgreSQL 16 et Redis réels, schéma créé par Alembic.

- En CI, les services sont fournis par GitHub Actions : ``XAALIS_TEST_DATABASE_URL``
  et ``XAALIS_TEST_REDIS_URL`` pointent dessus.
- En local, sans ces variables, Testcontainers démarre des conteneurs éphémères.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from xaalis.api.app import create_app
from xaalis.api.resources import Resources
from xaalis.config import Settings

ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("XAALIS_TEST_DATABASE_URL"):
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    if url := os.environ.get("XAALIS_TEST_REDIS_URL"):
        yield url
        return
    from testcontainers.community.redis import RedisContainer

    with RedisContainer(REDIS_IMAGE) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    return database_url


@pytest.fixture(scope="session")
def settings(migrated_database_url: str, redis_url: str) -> Settings:
    return Settings(
        environment="test",
        database_url=migrated_database_url,
        redis_url=redis_url,
        log_json=False,
        log_level="WARNING",
        api_key_cache_seconds=0,  # les tests révoquent des clés et attendent un effet immédiat
    )


@pytest.fixture(scope="session")
async def resources(settings: Settings) -> AsyncIterator[Resources]:
    res = Resources.from_settings(settings)
    yield res
    await res.aclose()


@pytest.fixture(scope="session")
async def app(resources: Resources) -> AsyncIterator[FastAPI]:
    application = create_app(resources=resources)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
