"""Общие служебные команды сервиса."""

from sk_common.people.service import find_or_create_user_by_email, set_employee
from sk_common.runtime import settings
from sk_common.security import hash_password
from sk_common.uow import UnitOfWork


async def bootstrap() -> None:
    """Администраторы из BOOTSTRAP_ADMIN_EMAILS. Пароль BOOTSTRAP_ADMIN_PASSWORD задаётся, только если его ещё нет."""
    async with UnitOfWork() as uow:
        for email in settings.bootstrap_admin_emails:
            user = await find_or_create_user_by_email(uow.session, email)
            user.is_superuser = True
            await set_employee(uow.session, user, True)
            if settings.bootstrap_admin_password and not user.password_hash:
                user.password_hash = hash_password(settings.bootstrap_admin_password)
                print(f'Задан пароль администратора {email}')
