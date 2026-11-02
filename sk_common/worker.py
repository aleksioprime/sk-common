"""Общие задачи воркера arq: письма и синхронизация справочника SkolStream.

Сервис добавляет их в свой WorkerSettings:
    functions = [*worker.FUNCTIONS, ...]
    cron_jobs = [*worker.CRON_JOBS, ...]
"""

import logging

from arq import cron
from arq.connections import RedisSettings

from sk_common.mail import send_mail
from sk_common.people.sync import run_directory_sync, start_scheduled_sync
from sk_common.runtime import settings
from sk_common.skolstream import get_skolstream_client

logger = logging.getLogger(__name__)


async def send_email(ctx: dict, to: str, subject: str, text: str, html: str | None = None) -> None:
    await send_mail(to, subject, text, html)
    logger.info('Письмо «%s» отправлено на %s', subject, to)


async def sync_directory(ctx: dict, run_id: int) -> dict:
    """Запуск по кнопке «Обновить из SkolStream» (POST /api/v1/people/sync)."""
    return await run_directory_sync(get_skolstream_client(), run_id)


async def frequent_sync(ctx: dict) -> None:
    """Изменения справочника SkolStream каждые 10 минут: новый ученик виден почти сразу."""
    await start_scheduled_sync(get_skolstream_client(), 'incremental')


async def nightly_sync(ctx: dict) -> None:
    """Полный срез ночью: отключает тех, кого нет в SkolStream, и чистит журнал запусков."""
    await start_scheduled_sync(get_skolstream_client(), 'full')


FUNCTIONS = [send_email, sync_directory]
CRON_JOBS = [
    cron(nightly_sync, hour={3}, minute={0}),
    cron(frequent_sync, minute=set(range(2, 60, 10))),
]


def redis_settings() -> RedisSettings:
    return RedisSettings(host=settings.redis.host, port=settings.redis.port, database=settings.redis.db)
