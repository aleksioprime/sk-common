"""Модели пакета для Alembic сервиса: импортируйте этот модуль в реестр моделей (app/models.py)."""

from sk_common import audit
from sk_common.auth import models as auth_models
from sk_common.db import Base
from sk_common.people import models as people_models

__all__ = ['Base', 'audit', 'auth_models', 'people_models']
