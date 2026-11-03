"""Подставные очередь и SkolStream для тестов сервисов, вход в тестовом клиенте."""

from sk_common.auth.service import open_session
from sk_common.people.models import Person, User
from sk_common.runtime import settings
from sk_common.skolstream import SkolstreamProfile

TEACHER_ID = '4f6b2c1e-0000-4000-8000-000000000001'
STUDENT_ID = '4f6b2c1e-0000-4000-8000-000000000002'
CLASS_ID = '7a1d0c55-0000-4000-8000-000000000010'
YEAR_ID = '7a1d0c55-0000-4000-8000-000000000020'
DEPT_ID = '7a1d0c55-0000-4000-8000-000000000030'


class FakeQueue:
    def __init__(self):
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue(self, function, *args, **kwargs):
        self.jobs.append((function, args, kwargs))


class FakeSkolstream:
    def __init__(self):
        self.profile = SkolstreamProfile(
            id=STUDENT_ID,
            email='student@skolkovo.example.org',
            last_name='Иванова',
            first_name='Анна',
            middle_name='Сергеевна',
            is_student=True,
            class_ids=[CLASS_ID],
        )
        self.directory = directory_payload()
        self.exchanged: list[tuple[str, str]] = []
        self.requested_since: list[str | None] = []

    def authorize_url(self, state, code_challenge):
        return f'https://skolstream.test/o/authorize/?state={state}&code_challenge={code_challenge}'

    async def exchange_code(self, code, code_verifier):
        self.exchanged.append((code, code_verifier))
        return 'skolstream-access-token'

    async def fetch_profile(self, access_token):
        return self.profile

    async def fetch_directory(self, updated_since=None):
        self.requested_since.append(updated_since)
        return self.directory


def directory_payload(users=None) -> dict:
    return {
        'academic_years': [
            {'id': YEAR_ID, 'name': '2026/2027', 'is_current': True, 'date_start': '2026-09-01', 'date_end': None}
        ],
        'classes': [{'id': CLASS_ID, 'academic_year_id': YEAR_ID, 'grade': 9, 'letter': 'А', 'name': '9А'}],
        'departments': [{'id': DEPT_ID, 'name': 'Кафедра естественных наук'}],
        'users': users
        if users is not None
        else [
            {
                'id': TEACHER_ID,
                'email': 'teacher@skolkovo.example.org',
                'last_name': 'Петров',
                'first_name': 'Иван',
                'middle_name': 'Олегович',
                'is_employee': True,
                'department_ids': [DEPT_ID],
            },
            {
                'id': STUDENT_ID,
                'email': 'student@skolkovo.example.org',
                'last_name': 'Иванова',
                'first_name': 'Анна',
                'is_student': True,
                'class_ids': [CLASS_ID],
            },
        ],
    }


async def login_as(db, client, email='admin@skolkovo.example.org', is_superuser=False, last_name='Админов') -> User:
    """Создать пользователя и положить cookie сессии в клиент."""
    person = Person(email=email, last_name=last_name, first_name='Тест')
    db.add(person)
    await db.flush()
    user = User(email=email, person=person, is_superuser=is_superuser)
    db.add(user)
    await db.flush()
    access, _ = await open_session(db, user, 'magic_link', 'pytest', '127.0.0.1')
    await db.commit()
    client.cookies.set(settings.auth.access_cookie, access)
    return user
