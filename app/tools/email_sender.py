import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def send_email(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: str | Path | None = None,
    sender_email: str | None = None,
    app_password: str | None = None,
) -> dict:
    """Send an email using Gmail or standard SMTP with optional file attachment (e.g. CV)."""
    user = sender_email or os.getenv("SMTP_EMAIL", "")
    password = app_password or os.getenv("SMTP_PASSWORD", "")

    if not user or not password:
        return {
            "ok": False,
            "error": "Missing SMTP credentials. Add SMTP_EMAIL and SMTP_PASSWORD to .env (use a Google App Password for Gmail).",
        }

    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path:
        att = Path(attachment_path).expanduser().resolve()
        if att.is_file():
            try:
                part = MIMEBase("application", "octet-stream")
                with open(att, "rb") as f:
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={att.name}")
                msg.attach(part)
            except Exception as exc:
                logger.warning("Could not attach file %s: %s", att, exc)

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)

        return {
            "ok": True,
            "to": to_email,
            "subject": subject,
            "attachment": str(attachment_path) if attachment_path else None,
        }
    except Exception as exc:
        logger.exception("Failed to send email to %s: %s", to_email, exc)
        return {
            "ok": False,
            "error": str(exc),
        }
