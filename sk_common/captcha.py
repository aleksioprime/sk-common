"""Yandex SmartCaptcha: проверка токена с формы на сервере. Без ключей проверка выключена."""

import logging

import httpx

from sk_common.runtime import settings

logger = logging.getLogger(__name__)

VALIDATE_URL = 'https://smartcaptcha.yandexcloud.net/validate'


class Captcha:
    @property
    def enabled(self) -> bool:
        return settings.captcha.enabled

    async def verify(self, token: str | None, ip: str | None) -> bool:
        if not self.enabled:
            return True
        if not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(
                    VALIDATE_URL, data={'secret': settings.captcha.server_key, 'token': token, 'ip': ip or ''}
                )
        except httpx.HTTPError as exc:
            # Сервис капчи недоступен — не блокируем вход, лимиты по адресу и IP всё равно действуют
            logger.warning('SmartCaptcha недоступна: %s', exc)
            return True
        if response.status_code != 200:
            logger.warning('SmartCaptcha ответила %s', response.status_code)
            return True
        return response.json().get('status') == 'ok'


_captcha = Captcha()


def get_captcha() -> Captcha:
    return _captcha
