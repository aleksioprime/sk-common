import uuid
from datetime import date

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sk_common.people.models import (
    PERSON_SOURCE_LOCAL,
    PERSON_SOURCE_SKOLSTREAM,
    AcademicYear,
    Department,
    Person,
    StudyClass,
    User,
)
from sk_common.runtime import settings
from sk_common.security import now_utc
from sk_common.skolstream import SkolstreamProfile


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _date(value) -> date | None:
    return date.fromisoformat(value) if value else None


# --- Пользователи ---------------------------------------------------------------------------


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == normalize_email(email)))


async def ensure_user(session: AsyncSession, person: Person, email: str) -> User:
    """Учётная запись для персоны. Если email уже занят другой записью — привязывает её к персоне."""
    email = normalize_email(email)
    user = await session.scalar(select(User).where(User.person_id == person.id))
    if user is None:
        user = await get_user_by_email(session, email)
    if user is None:
        user = User(email=email, person=person)
        session.add(user)
    elif user.person_id is None:
        user.person = person
    if email in {normalize_email(item) for item in settings.bootstrap_admin_emails}:
        user.is_superuser = True
    await session.flush()
    return user


def split_full_name(full_name: str) -> tuple[str, str, str]:
    parts = full_name.split()
    return (parts + ['', ''])[0], (parts + ['', ''])[1], ' '.join(parts[2:])


async def set_full_name(session: AsyncSession, user: User, full_name: str) -> None:
    """ФИО учётной записи. Персоны из SkolStream не трогаем: их данные приходят из справочника."""
    full_name = ' '.join(full_name.split())
    if not full_name:
        return
    person = user.person
    if person is None:
        person = Person(source='local', email=user.email)
        session.add(person)
        user.person = person
    elif person.source == 'skolstream':
        return
    person.last_name, person.first_name, person.middle_name = split_full_name(full_name)
    await session.flush()


async def set_employee(session: AsyncSession, user: User, value: bool) -> None:
    if user.person is None:
        user.person = Person(source='local', email=user.email)
        session.add(user.person)
    user.person.is_employee = value
    await session.flush()


async def find_or_create_user_by_email(session: AsyncSession, email: str) -> User:
    """Вход по email: существующая учётка, затем персона из SkolStream с этим email, затем новый внешний участник."""
    email = normalize_email(email)
    user = await get_user_by_email(session, email)
    if user is not None:
        return user
    person = await session.scalar(
        select(Person)
        .where(func.lower(Person.email) == email, Person.is_active.is_(True))
        .order_by(Person.source.desc())  # skolstream раньше local
        .limit(1)
    )
    if person is None:
        person = Person(email=email, source=PERSON_SOURCE_LOCAL)
        session.add(person)
        await session.flush()
    return await ensure_user(session, person, email)


# --- Профиль из SkolStream при входе --------------------------------------------------------


async def upsert_person_from_profile(session: AsyncSession, profile: SkolstreamProfile) -> Person:
    skolstream_id = _uuid(profile.id)
    person = await session.scalar(select(Person).where(Person.skolstream_id == skolstream_id))
    if person is None and profile.email:
        # Внешний участник мог зарегистрироваться по email раньше, чем впервые вошёл через SkolStream
        person = await session.scalar(
            select(Person).where(func.lower(Person.email) == profile.email, Person.source == PERSON_SOURCE_LOCAL)
        )
    if person is None:
        person = Person()
        session.add(person)

    person.skolstream_id = skolstream_id
    person.source = PERSON_SOURCE_SKOLSTREAM
    person.last_name = profile.last_name
    person.first_name = profile.first_name
    person.middle_name = profile.middle_name
    person.email = profile.email or person.email
    person.gender = profile.gender
    person.is_student = profile.is_student
    person.is_employee = profile.is_employee
    person.is_active = True
    person.synced_at = now_utc()
    if profile.class_ids:
        study_class = await session.scalar(
            select(StudyClass).where(StudyClass.skolstream_id.in_([_uuid(i) for i in profile.class_ids]))
        )
        person.class_id = study_class.id if study_class else person.class_id
    await session.flush()
    return person


# --- Списки ---------------------------------------------------------------------------------


async def list_persons(
    session: AsyncSession, search: str = '', kind: str = '', offset: int = 0, limit: int = 50
) -> tuple[list[Person], int]:
    query = select(Person)
    if search:
        pattern = f'%{search.strip()}%'
        query = query.where(
            or_(
                Person.last_name.ilike(pattern),
                Person.first_name.ilike(pattern),
                Person.email.ilike(pattern),
            )
        )
    if kind == 'student':
        query = query.where(Person.is_student.is_(True))
    elif kind == 'employee':
        query = query.where(Person.is_employee.is_(True))
    elif kind == 'external':
        query = query.where(Person.source == PERSON_SOURCE_LOCAL)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(
        query.order_by(Person.last_name, Person.first_name, Person.id).offset(offset).limit(limit)
    )
    return list(rows.unique()), total or 0


async def users_by_person(session: AsyncSession, person_ids: list[int]) -> dict[int, User]:
    rows = await session.scalars(select(User).where(User.person_id.in_(person_ids or [0])))
    return {user.person_id: user for user in rows.unique()}


async def list_users(session: AsyncSession, search: str = '', offset: int = 0, limit: int = 50):
    query = select(User).outerjoin(User.person)
    if search:
        pattern = f'%{search.strip()}%'
        query = query.where(or_(User.email.ilike(pattern), Person.last_name.ilike(pattern)))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = await session.scalars(query.order_by(User.email).offset(offset).limit(limit))
    return list(rows.unique()), total or 0


# --- Синхронизация справочников -------------------------------------------------------------


async def apply_directory(session: AsyncSession, payload: dict, full: bool) -> dict:
    """Применить выгрузку SkolStream. При полной выгрузке отсутствующие записи помечаются неактивными."""
    stats = {'academic_years': 0, 'classes': 0, 'departments': 0, 'persons': 0, 'deactivated': 0}
    synced_at = now_utc()

    years = {}
    for item in payload.get('academic_years', []):
        year = await _upsert(session, AcademicYear, item['id'])
        year.name = item.get('name') or ''
        year.is_current = bool(item.get('is_current'))
        year.date_start = _date(item.get('date_start'))
        year.date_end = _date(item.get('date_end'))
        years[str(item['id'])] = year
        stats['academic_years'] += 1
    await session.flush()

    classes = {}
    for item in payload.get('classes', []):
        study_class = await _upsert(session, StudyClass, item['id'])
        year = years.get(str(item.get('academic_year_id'))) or await _by_skolstream(
            session, AcademicYear, item.get('academic_year_id')
        )
        study_class.academic_year_id = year.id if year else None
        study_class.grade = item.get('grade')
        study_class.letter = item.get('letter') or ''
        study_class.name = item.get('name') or f'{item.get("grade") or ""}{item.get("letter") or ""}'
        study_class.is_active = True
        classes[str(item['id'])] = study_class
        stats['classes'] += 1

    departments = {}
    for item in payload.get('departments', []):
        department = await _upsert(session, Department, item['id'])
        department.name = item.get('name') or ''
        department.is_active = True
        departments[str(item['id'])] = department
        stats['departments'] += 1
    await session.flush()

    seen_persons: set[uuid.UUID] = set()
    for item in payload.get('users', []):
        profile = SkolstreamProfile(
            id=str(item['id']),
            email=(item.get('email') or '').strip().lower(),
            last_name=item.get('last_name') or '',
            first_name=item.get('first_name') or '',
            middle_name=item.get('middle_name') or '',
            gender=item.get('gender'),
            is_student=bool(item.get('is_student')),
            is_employee=bool(item.get('is_employee')),
        )
        person = await upsert_person_from_profile(session, profile)
        person.is_active = bool(item.get('is_active', True))
        class_ids = [str(i) for i in item.get('class_ids') or []]
        found = [classes.get(i) or await _by_skolstream(session, StudyClass, i) for i in class_ids]
        found = [c for c in found if c is not None]
        person.class_id = found[0].id if found else None
        dept_list = [
            departments.get(str(i)) or await _by_skolstream(session, Department, i)
            for i in item.get('department_ids') or []
        ]
        await person.awaitable_attrs.departments
        person.departments = [d for d in dept_list if d is not None]
        person.synced_at = synced_at
        seen_persons.add(person.skolstream_id)
        stats['persons'] += 1
    await session.flush()

    if full and not seen_persons:
        # Пустой список пользователей в полном срезе — почти наверняка сбой на стороне SkolStream:
        # не отключаем всех разом
        stats['deactivation_skipped'] = True
    elif full:
        conditions = [Person.source == PERSON_SOURCE_SKOLSTREAM, Person.is_active.is_(True)]
        conditions.append(Person.skolstream_id.not_in(seen_persons))
        result = await session.execute(update(Person).where(*conditions).values(is_active=False))
        stats['deactivated'] = result.rowcount or 0
        for model, seen in ((StudyClass, classes), (Department, departments)):
            conditions = [model.skolstream_id.is_not(None)]
            if seen:
                conditions.append(model.skolstream_id.not_in([_uuid(key) for key in seen]))
            await session.execute(update(model).where(*conditions).values(is_active=False))
    return stats


async def _by_skolstream(session: AsyncSession, model, external_id):
    key = _uuid(external_id)
    if key is None:
        return None
    return await session.scalar(select(model).where(model.skolstream_id == key))


async def _upsert(session: AsyncSession, model, external_id):
    instance = await _by_skolstream(session, model, external_id)
    if instance is None:
        instance = model(skolstream_id=_uuid(external_id))
        session.add(instance)
    return instance
