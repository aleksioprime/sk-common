import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sk_common.db import Base, IdMixin, TimestampMixin

PERSON_SOURCE_SKOLSTREAM = 'skolstream'
PERSON_SOURCE_LOCAL = 'local'

person_departments = Table(
    'person_departments',
    Base.metadata,
    Column('person_id', BigInteger, ForeignKey('persons.id', ondelete='CASCADE'), primary_key=True),
    Column('department_id', BigInteger, ForeignKey('departments.id', ondelete='CASCADE'), primary_key=True),
)


class AcademicYear(IdMixin, TimestampMixin, Base):
    __tablename__ = 'academic_years'

    skolstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    name: Mapped[str] = mapped_column(String(50))
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    date_start: Mapped[date | None] = mapped_column(Date)
    date_end: Mapped[date | None] = mapped_column(Date)


class StudyClass(IdMixin, TimestampMixin, Base):
    __tablename__ = 'classes'

    skolstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    academic_year_id: Mapped[int | None] = mapped_column(ForeignKey('academic_years.id', ondelete='SET NULL'))
    grade: Mapped[int | None] = mapped_column(Integer)
    letter: Mapped[str] = mapped_column(String(10), default='', server_default='')
    name: Mapped[str] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')

    academic_year: Mapped[AcademicYear | None] = relationship(lazy='joined')


class Department(IdMixin, TimestampMixin, Base):
    __tablename__ = 'departments'

    skolstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')


class Person(IdMixin, TimestampMixin, Base):
    """Человек: ученик или сотрудник из SkolStream либо внешний участник."""

    __tablename__ = 'persons'

    skolstream_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    source: Mapped[str] = mapped_column(String(20), default=PERSON_SOURCE_LOCAL, server_default=PERSON_SOURCE_LOCAL)
    last_name: Mapped[str] = mapped_column(String(100), default='', server_default='')
    first_name: Mapped[str] = mapped_column(String(100), default='', server_default='')
    middle_name: Mapped[str] = mapped_column(String(100), default='', server_default='')
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    gender: Mapped[str | None] = mapped_column(String(10))
    birth_date: Mapped[date | None] = mapped_column(Date)
    is_student: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    is_employee: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')
    class_id: Mapped[int | None] = mapped_column(ForeignKey('classes.id', ondelete='SET NULL'))
    organization: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    study_class: Mapped[StudyClass | None] = relationship(lazy='joined')
    departments: Mapped[list[Department]] = relationship(secondary=person_departments, lazy='selectin')

    @property
    def full_name(self) -> str:
        return ' '.join(part for part in (self.last_name, self.first_name, self.middle_name) if part)

    @property
    def short_name(self) -> str:
        initials = ''.join(f'{part[0]}.' for part in (self.first_name, self.middle_name) if part)
        return f'{self.last_name} {initials}'.strip()

    @property
    def is_external(self) -> bool:
        return self.source == PERSON_SOURCE_LOCAL


class User(IdMixin, TimestampMixin, Base):
    """Учётная запись для входа. Связана с персоной один к одному."""

    __tablename__ = 'users'

    email: Mapped[str] = mapped_column(String(255), unique=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey('persons.id', ondelete='SET NULL'), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default='true')
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    # Вход по паролю; без пароля — только по ссылке из письма, QR или SkolStream
    password_hash: Mapped[str | None] = mapped_column(String(200))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person | None] = relationship(lazy='joined')

    def block_reason(self) -> str | None:
        """Почему вход запрещён (None — разрешён). Персона, отключённая в SkolStream, не входит;
        администраторов это не касается, чтобы ошибка синхронизации не заперла их снаружи."""
        if not self.is_active:
            return 'Учётная запись отключена. Обратитесь к организаторам.'
        person = self.person
        if (
            person is not None
            and person.source == PERSON_SOURCE_SKOLSTREAM
            and not person.is_active
            and not self.is_superuser
        ):
            return 'Учётная запись отключена в SkolStream. Обратитесь к организаторам.'
        return None


class SyncRun(IdMixin, Base):
    """Запуск синхронизации справочников со SkolStream."""

    __tablename__ = 'sync_runs'

    source: Mapped[str] = mapped_column(String(30), default='skolstream', server_default='skolstream')
    mode: Mapped[str] = mapped_column(String(20))  # full | incremental
    status: Mapped[str] = mapped_column(String(20), default='running', server_default='running')
    stats: Mapped[dict] = mapped_column(JSONB, default=dict, server_default='{}')
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
