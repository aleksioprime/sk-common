from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from sk_common.audit import record
from sk_common.deps import AdminUser, Db
from sk_common.people import service
from sk_common.people.models import Department, Person, StudyClass, SyncRun, User
from sk_common.people.schemas import (
    ClassOut,
    DepartmentOut,
    PersonAdmin,
    PersonPage,
    PersonRow,
    SyncRequest,
    SyncRunOut,
    UserCreate,
    UserOut,
    UserPage,
    UserUpdate,
)
from sk_common.queue import Queue, get_queue
from sk_common.runtime import settings
from sk_common.security import hash_password

router = APIRouter(prefix='/v1/people', tags=['people'])


@router.get('/persons', response_model=PersonPage, summary='Персоны')
async def list_persons(
    db: Db,
    _: AdminUser,
    search: str = '',
    kind: Annotated[str, Query(pattern='^(|student|employee|external)$')] = '',
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PersonPage:
    items, total = await service.list_persons(db, search, kind, offset, limit)
    users = await service.users_by_person(db, [person.id for person in items])
    return PersonPage(items=[_person_row(person, users.get(person.id)) for person in items], total=total)


def _person_row(person: Person, user: User | None) -> PersonRow:
    row = PersonRow.model_validate(person)
    row.user_id = user.id if user else None
    row.is_superuser = bool(user and user.is_superuser)
    return row


@router.put('/persons/{person_id}/admin', response_model=PersonRow, summary='Права администратора персоны')
async def set_person_admin(person_id: int, payload: PersonAdmin, db: Db, admin: AdminUser) -> PersonRow:
    """Выдать права человеку из справочника, даже если он ещё не входил: учётная запись создаётся по его email."""
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Персона не найдена')
    user = (await service.users_by_person(db, [person.id])).get(person.id)
    if user is None:
        if not payload.is_superuser:
            return _person_row(person, None)
        if not person.email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'У персоны нет email: учётную запись не создать')
        if not person.is_active:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Персона отключена в SkolStream')
        user = await service.ensure_user(db, person, person.email)
    if user.id == admin.id and not payload.is_superuser:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Нельзя снять права или отключить самого себя')
    user.is_superuser = payload.is_superuser
    record(db, 'people.person.admin', 'user', user.id, admin.id, person_id=person.id, is_superuser=user.is_superuser)
    await db.commit()
    return _person_row(person, user)


@router.get('/users', response_model=UserPage, summary='Учётные записи')
async def list_users(
    db: Db,
    _: AdminUser,
    search: str = '',
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> UserPage:
    items, total = await service.list_users(db, search, offset, limit)
    return UserPage(items=items, total=total)


@router.patch('/users/{user_id}', response_model=UserOut, summary='Изменить учётную запись')
async def update_user(user_id: int, payload: UserUpdate, db: Db, admin: AdminUser) -> UserOut:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Пользователь не найден')
    changes = payload.model_dump(exclude_none=True)
    if user.id == admin.id and (changes.get('is_superuser') is False or changes.get('is_active') is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Нельзя снять права или отключить самого себя')
    if password := changes.pop('password', None):
        user.password_hash = hash_password(password)
    if (full_name := changes.pop('full_name', None)) is not None:
        await service.set_full_name(db, user, full_name)
    if (is_employee := changes.pop('is_employee', None)) is not None:
        await service.set_employee(db, user, is_employee)
    for field, value in changes.items():
        setattr(user, field, value)
    record(db, 'people.user.update', 'user', user.id, admin.id, **changes, password_set=bool(password))
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)


@router.post('/users', response_model=UserOut, status_code=201, summary='Создать учётную запись')
async def create_user(payload: UserCreate, db: Db, admin: AdminUser) -> UserOut:
    if await service.get_user_by_email(db, payload.email):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, 'Пользователь с таким email уже есть')
    user = await service.find_or_create_user_by_email(db, payload.email)
    if payload.full_name:
        await service.set_full_name(db, user, payload.full_name)
    if payload.password:
        user.password_hash = hash_password(payload.password)
    user.is_superuser = payload.is_superuser
    if payload.is_employee:
        await service.set_employee(db, user, True)
    record(db, 'people.user.create', 'user', user.id, admin.id)
    await db.commit()
    user = await db.get(User, user.id, populate_existing=True)
    return UserOut.model_validate(user)


@router.get('/classes', response_model=list[ClassOut], summary='Учебные классы')
async def list_classes(db: Db, _: AdminUser) -> list[StudyClass]:
    rows = await db.scalars(
        select(StudyClass).where(StudyClass.is_active.is_(True)).order_by(StudyClass.grade, StudyClass.letter)
    )
    return list(rows)


@router.get('/departments', response_model=list[DepartmentOut], summary='Подразделения')
async def list_departments(db: Db, _: AdminUser) -> list[Department]:
    rows = await db.scalars(select(Department).where(Department.is_active.is_(True)).order_by(Department.name))
    return list(rows)


@router.get('/sync', response_model=list[SyncRunOut], summary='Последние синхронизации со SkolStream')
async def list_sync_runs(db: Db, _: AdminUser) -> list[SyncRun]:
    rows = await db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(10))
    return list(rows)


@router.post(
    '/sync', response_model=SyncRunOut, status_code=status.HTTP_202_ACCEPTED, summary='Запустить синхронизацию'
)
async def start_sync(
    payload: SyncRequest, db: Db, admin: AdminUser, queue: Annotated[Queue, Depends(get_queue)]
) -> SyncRun:
    if not settings.skolstream.directory_token:
        raise HTTPException(status.HTTP_409_CONFLICT, 'Не задан токен выгрузки справочников SkolStream')
    running = await db.scalar(select(SyncRun.id).where(SyncRun.status == 'running').limit(1))
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, 'Синхронизация уже идёт')
    run = SyncRun(mode=payload.mode, triggered_by_id=admin.id)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    await queue.enqueue('sync_directory', run.id)
    return run
