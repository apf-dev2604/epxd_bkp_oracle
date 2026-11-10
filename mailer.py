#!/usr/bin/env python3
"""User's SMTP Mailer interface, with TLS verification and safe configuration."""
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional

class Mailer:
    def __init__(self, enabled=False, smtp_host="localhost", smtp_port=25,
                 sender="oracle-refresh@localhost", recipients=None,
                 username=None, password=None, use_tls=False):
        self.enabled = self._bool(enabled)
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.sender = sender
        self.recipients = self._split_recipients(recipients)
        self.username = username or None
        self.password = password or None
        self.use_tls = self._bool(use_tls)

    @staticmethod
    def _bool(value):
        return str(value).lower() in ('1', 'true', 'yes', 'on') if isinstance(value, str) else bool(value)

    @staticmethod
    def _split_recipients(recipients) -> list[str]:
        if not recipients:
            return []
        if isinstance(recipients, str):
            return [x.strip() for x in recipients.split(',') if x.strip()]
        return [str(x).strip() for x in recipients if str(x).strip()]

    def send(
          self,
          subject: str,
          body: str,
          attachments: Optional[Iterable[str]] = None )-> bool:

        if not self.enabled or not self.recipients:
            return False

        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Subject"] = subject
        msg.set_content(body)

        for attachment in attachments or []:
            path = Path(attachment)

            if path.is_file():
                msg.add_attachment(
                    path.read_bytes(),
                    maintype="application",
                    subtype="octet-stream",
                    filename=path.name
                )

        if self.username and not self.use_tls:
            raise RuntimeError(
                "Refusing authenticated SMTP without STARTTLS"
            )

        if self.username and not self.password:
            raise RuntimeError(
                "SMTP username provided but password missing"
            )

        try:
            with smtplib.SMTP(
                self.smtp_host,
                self.smtp_port,
                timeout=30
            ) as smtp:

                # Identify SMTP client
                smtp.ehlo()

                # Establish encrypted connection
                if self.use_tls:
                    if not smtp.has_extn("starttls"):
                        raise RuntimeError(
                            "SMTP server does not advertise STARTTLS"
                        )

                    smtp.starttls(
                        context=ssl.create_default_context()
                    )
                    smtp.ehlo()

                # Authenticate to Gmail
                if self.username:
                        smtp.login(
                        self.username,
                        self.password
                    )

                # Submit email
                smtp.send_message(msg)

            return True

        except smtplib.SMTPAuthenticationError as exc:
            raise RuntimeError(
                f"SMTP authentication rejected (code {exc.smtp_code}). "
                "Verify the Gmail App Password and account permissions."
            ) from exc

        except smtplib.SMTPServerDisconnected as exc:
            raise RuntimeError(
                "SMTP server unexpectedly disconnected. "
                "Check Gmail authentication policy, "
                "network connectivity, and STARTTLS settings."
            ) from exc

        except (smtplib.SMTPException, OSError) as exc:
            raise RuntimeError(
                f"SMTP connection or delivery failed: "
               f"{type(exc).__name__}: {exc}"
            ) from exc     
