# sk-common

Общая основа школьных сервисов гимназии (SK Contest, SK Survey, SK Diagnostic) на FastAPI и SQLAlchemy:

- вход по ссылке из письма, по паролю и через SkolStream (OAuth2 с PKCE), сессии с ротацией refresh-токена;
- справочник людей из SkolStream: ученики, сотрудники, классы, подразделения, учебные годы, внешние участники;
- синхронизация справочника: изменения каждые 10 минут, полный срез ночью, запуск по кнопке;
- журнал действий, защита от CSRF по Origin, лимиты попыток в Redis, Yandex SmartCaptcha, почта, очередь arq.

Сервисы подключают пакет одинаково, поэтому пользователь входит в любой из них одним способом,
а исправление входа или синхронизации делается один раз.

## Подключение

`requirements.txt` сервиса — архив тега с GitHub (git в образе не нужен):

```
sk-common @ https://github.com/aleksioprime/sk-common/archive/refs/tags/v0.1.1.tar.gz
```

Для разработки пакета вместе с сервисом: `pip install -e ../sk-common`.

### Настройки

```python
# app/core/config.py
import sk_common
from sk_common.config import AuthSettings, DatabaseSettings, ServiceSettings


class Settings(ServiceSettings):
    project_name: str = 'SK Survey'
    app_url: str = 'http://localhost:8601'
    # свои поля сервиса


settings = Settings()
sk_common.configure(settings)
```

Модули пакета читают настройки только во время вызова, поэтому `configure` достаточно вызвать
в модуле настроек сервиса, который импортируют приложение, воркер, Alembic и тесты.

Значения по умолчанию вложенных настроек (`db`, `auth` и т. д.) сервис меняет наследником, а не экземпляром:
pydantic-settings собирает вложенную модель из переменных окружения заново.

```python
class SurveyDatabaseSettings(DatabaseSettings):
    user: str = 'sk_survey'
    password: str = 'sk_survey'
    name: str = 'sk_survey'


class Settings(ServiceSettings):
    db: SurveyDatabaseSettings = SurveyDatabaseSettings()
```

Переменные окружения: `SECRET_KEY`, `APP_URL`, `CORS_ORIGINS`, `BOOTSTRAP_ADMIN_EMAILS`, `DB__*`, `REDIS__*`,
`SMTP__*`, `SKOLSTREAM__BASE_URL`, `SKOLSTREAM__CLIENT_ID`, `SKOLSTREAM__CLIENT_SECRET`,
`SKOLSTREAM__DIRECTORY_TOKEN`, `CAPTCHA__*`, `AUTH__*`.

### Приложение

```python
from fastapi import APIRouter, FastAPI

from sk_common.auth.router import create_auth_router
from sk_common.people.router import router as people_router
from sk_common.web import add_common_middleware, setup_logging

setup_logging()
app = FastAPI()
add_common_middleware(app)  # проверка Origin, журнал запросов, CORS
api = APIRouter(prefix='/api')
api.include_router(create_auth_router())
api.include_router(people_router)
app.include_router(api)
```

Зависимости для своих обработчиков — `sk_common.deps`: `Db`, `CurrentUser`, `OptionalUser`, `AdminUser`.

Ответ `GET /api/v1/auth/me` сервис расширяет своей схемой и функцией:

```python
from sk_common.auth.schemas import Me


class SurveyMe(Me):
    can_create_surveys: bool = False


async def me_extra(db, request, user) -> dict:
    return {'can_create_surveys': bool(user.person and user.person.is_employee)}


api.include_router(create_auth_router(me_model=SurveyMe, me_extra=me_extra))
```

Одноразовый вход, привязанный к учётной записи (например, QR на распечатанном листе), —
`sk_common.auth.service.issue_personal_login(..., purpose='judge_qr')`; способ входа сессии
читает `sk_common.auth.router.session_method`.

### Воркер

```python
from sk_common import worker


class WorkerSettings:
    functions = [*worker.FUNCTIONS, ...]
    cron_jobs = [*worker.CRON_JOBS, ...]
    redis_settings = worker.redis_settings()
```

Команда `sk_common.cli.bootstrap()` выдаёт права администратора адресам из `BOOTSTRAP_ADMIN_EMAILS`
(и задаёт `BOOTSTRAP_ADMIN_PASSWORD`, если пароля ещё нет) — вызывайте её при старте API.

### Миграции

Таблицы пакета (`persons`, `users`, `classes`, `departments`, `academic_years`, `person_departments`,
`auth_sessions`, `magic_link_tokens`, `sync_runs`, `audit_log`) создаёт Alembic сервиса.
В реестр моделей сервиса добавьте `from sk_common import models as common_models`, базу моделей
берите из `sk_common.db` (`Base`, `IdMixin`, `TimestampMixin`).

Первая миграция нового сервиса создаёт функцию `next_id()` для первичных ключей:

```python
from sk_common.db import DROP_NEXT_ID_SQL, NEXT_ID_SQL


def upgrade() -> None:
    for statement in NEXT_ID_SQL:
        op.execute(statement)


def downgrade() -> None:
    for statement in DROP_NEXT_ID_SQL:
        op.execute(statement)
```

Если новая версия пакета меняет модели, в заметках к тегу это сказано, и каждый сервис при обновлении
делает свою миграцию (`alembic revision --autogenerate`).

### Тесты сервиса

`sk_common.testing`: `FakeQueue`, `FakeSkolstream`, `directory_payload()`, `login_as()` и UUID тестовых
ученика, учителя, класса. Подмена в приложении:

```python
app.dependency_overrides[get_queue] = lambda: queue
app.dependency_overrides[get_skolstream_client] = lambda: skolstream
```

## SkolStream

Подключение сервиса к SkolStream (в репозитории SkolStream):

- вход — `docs/guides/dev/oauth-apps.md`: OAuth-приложение Confidential + Authorization code,
  redirect `https://<сервис>/api/v1/auth/skolstream/callback` — **ровно один адрес**, включить
  «Skip authorization»;
- справочник — `docs/guides/dev/directory-export.md`: токен выгрузки в админке SkolStream, отдельный на сервис.

Пакет не передаёт `redirect_uri` ни в `/o/authorize/`, ни в `/o/token/`: шлюз перед SkolStream отвечает
403 «Forbidden / Transaction ID» на запросы из интернета, в параметрах которых есть адрес `https://…`.
Без `redirect_uri` SkolStream возвращает на единственный зарегистрированный адрес (RFC 6749, 4.1.1).
Поэтому у OAuth-приложения сервиса должен быть зарегистрирован ровно один адрес возврата.

Сервис представляется SkolStream как `<project_name через дефис>/<version> (+<app_url>)`:
шлюз перед SkolStream отклоняет типовые заголовки библиотек.

Сеть: внутренний DNS гимназии отдаёт для `skolstream.sk.ru` адрес ingress, до которого из DMZ нет маршрута.
В проде backend и worker сервиса ходят на публичный адрес:

```yaml
extra_hosts:
  - "skolstream.sk.ru:${SKOLSTREAM__PUBLIC_IP:-91.221.164.121}"
```

Правила справочника:
- персона из SkolStream ищется по UUID, email не ключ (бывает временным);
- внешнего участника, который позже вошёл через SkolStream, сливаем по email;
- поля из SkolStream в сервисе только для чтения, редактируются только внешние персоны;
- пропавшие в полном срезе получают `is_active=false` и не удаляются; пустой полный срез никого не отключает;
- отключённая в SkolStream персона не входит, кроме администраторов сервиса;
- роли SkolStream не переносятся, права назначаются в сервисе.

## Разработка пакета

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker run -d --rm --name skc-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16-alpine
docker run -d --rm --name skc-redis -p 56379:6379 redis:7-alpine
DB__PORT=55432 REDIS__PORT=56379 .venv/bin/pytest -q
```

Версии — теги `vX.Y.Z`. Сервисы указывают тег явно и обновляются по одному, после зелёных проверок.
