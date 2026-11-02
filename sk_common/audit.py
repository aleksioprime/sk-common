from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from sk_common.db import Base, IdMixin


class AuditLog(IdMixin, Base):
    __tablename__ = 'audit_log'

    actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


def record(
    session: AsyncSession,
    action: str,
    entity: str,
    entity_id: int | None = None,
    actor_id: int | None = None,
    **data,
) -> None:
    """Записать действие в журнал в рамках текущей транзакции."""
    session.add(AuditLog(actor_id=actor_id, action=action, entity=entity, entity_id=entity_id, data=data))
