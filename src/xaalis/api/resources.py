"""Ressources partagées du processus API (pool PostgreSQL, client Redis).

Elles sont créées dans le ``lifespan`` de l'application et exposées aux routes par
injection de dépendances : aucune variable globale, et les tests peuvent fournir
leurs propres ressources (conteneurs éphémères).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from xaalis.config import Settings
from xaalis.infra.db import create_engine, create_sessionmaker
from xaalis.infra.ratelimit import RateLimiter


@dataclass(frozen=True, slots=True)
class Resources:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter

    @classmethod
    def from_settings(cls, settings: Settings) -> Resources:
        engine = create_engine(settings)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        return cls(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            rate_limiter=RateLimiter(redis),
        )

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


def get_resources(request: Request) -> Resources:
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


async def get_session(resources: ResourcesDep) -> AsyncIterator[AsyncSession]:
    async with resources.sessionmaker() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
