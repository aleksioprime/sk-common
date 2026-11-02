from email.message import EmailMessage

import aiosmtplib

from sk_common.runtime import settings


async def send_mail(to: str, subject: str, text: str, html: str | None = None) -> None:
    message = EmailMessage()
    message['From'] = settings.smtp.sender
    message['To'] = to
    message['Subject'] = subject
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype='html')

    await aiosmtplib.send(
        message,
        hostname=settings.smtp.host,
        port=settings.smtp.port,
        username=settings.smtp.user or None,
        password=settings.smtp.password or None,
        use_tls=settings.smtp.use_ssl,
        timeout=30,
    )
