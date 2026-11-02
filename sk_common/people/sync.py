"""Запуск синхронизации справочников со SkolStream (вызывается из воркера)."""

import logging
from datetime import timedelta

from sqlalchemy import delete, select, update

from sk_common.audit import record
from sk_common.people.models import SyncRun
from sk_common.people.service import apply_directory
from sk_common.runtime import settings
from sk_common.security import now_utc
from sk_common.skolstream import SkolstreamClient
from sk_common.uow import UnitOfWork

logger = logging.getLogger(__name__)

# Запас при отсчёте от времени запуска, если SkolStream не прислал generated_at
CURSOR_OVERLAP = timedelta(minutes=5)
# Запуск дольше этого считается оборвавшимся и не блокирует следующие
STALE_RUN = timedelta(minutes=30)
# Сколько хранить журнал запусков
KEEP_RUNS = timedelta(days=30)


def since_for(last_run: SyncRun) -> str:
    """Точка отсчёта выгрузки изменений: generated_at прошлого ответа SkolStream (его часы),
    иначе время прошлого запуска с запасом."""
    cursor = (last_run.stats or {}).get('cursor')
    return cursor or (last_run.started_at - CURSOR_OVERLAP).isoformat()


async def start_scheduled_sync(client: SkolstreamClient, mode: str, uow_factory=UnitOfWork) -> dict | None:
    """Плановая синхронизация из воркера: incremental — каждые 10 минут, full — ночью.
    Пропускается, если нет токена или идёт другой запуск; ночью чистит старый журнал."""
    if not settings.skolstream.directory_token:
        return None
    async with uow_factory() as uow:
        await uow.session.execute(
            update(SyncRun)
            .where(SyncRun.status == 'running', SyncRun.started_at < now_utc() - STALE_RUN)
            .values(status='failed', error='Запуск оборвался', finished_at=now_utc())
        )
        if await uow.session.scalar(select(SyncRun.id).where(SyncRun.status == 'running').limit(1)):
            return None
        if mode == 'full':
            await uow.session.execute(delete(SyncRun).where(SyncRun.started_at < now_utc() - KEEP_RUNS))
        run = SyncRun(mode=mode)
        uow.session.add(run)
        await uow.session.flush()
        run_id = run.id
    return await run_directory_sync(client, run_id, uow_factory)


async def run_directory_sync(client: SkolstreamClient, run_id: int, uow_factory=UnitOfWork) -> dict:
    async with uow_factory() as uow:
        run = await uow.session.get(SyncRun, run_id)
        full = run.mode == 'full'
        since = None
        if not full:
            last = await uow.session.scalar(
                select(SyncRun)
                .where(SyncRun.status == 'success', SyncRun.id != run_id)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            )
            if last is not None:
                since = since_for(last)

    try:
        payload = await client.fetch_directory(since)
        async with uow_factory() as uow:
            stats = await apply_directory(uow.session, payload, full=full or since is None)
            if payload.get('generated_at'):
                stats['cursor'] = str(payload['generated_at'])
            run = await uow.session.get(SyncRun, run_id)
            run.status, run.stats, run.finished_at = 'success', stats, now_utc()
            record(uow.session, 'people.sync', 'sync_run', run_id, run.triggered_by_id, **stats)
        logger.info('Синхронизация %s завершена: %s', run_id, stats)
        return stats
    except Exception as exc:
        logger.exception('Синхронизация %s не удалась', run_id)
        async with uow_factory() as uow:
            run = await uow.session.get(SyncRun, run_id)
            run.status, run.error, run.finished_at = 'failed', str(exc)[:2000], now_utc()
        raise
