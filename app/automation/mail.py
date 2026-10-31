"""Mail automation using only the Python standard library."""

from __future__ import annotations

import email
import imaplib
import smtplib
from email.message import EmailMessage
from typing import Any


class MailAutomation:
    def __init__(self, imap_host: str, smtp_host: str, username: str, password: str, imap_port: int = 993, smtp_port: int = 587):
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.username = username
        self.password = password
        self.imap_port = imap_port
        self.smtp_port = smtp_port

    def list_unread(self, folder: str = "INBOX", limit: int = 20) -> dict[str, Any]:
        try:
            mailbox = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            mailbox.login(self.username, self.password)
            mailbox.select(folder)
            status, data = mailbox.search(None, "UNSEEN")
            if status != "OK":
                mailbox.logout()
                return {"ok": False, "error": "IMAP search failed"}
            ids = data[0].split()[-limit:]
            messages = []
            for message_id in ids:
                status, payload = mailbox.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload or not payload[0]:
                    continue
                msg = email.message_from_bytes(payload[0][1])
                messages.append(
                    {
                        "id": message_id.decode(),
                        "from": msg.get("From"),
                        "subject": msg.get("Subject"),
                        "date": msg.get("Date"),
                    }
                )
            mailbox.logout()
            return {"ok": True, "count": len(messages), "messages": messages}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def reply(self, to: str, subject: str, body: str, from_address: str | None = None) -> dict[str, Any]:
        try:
            message = EmailMessage()
            message["To"] = to
            message["From"] = from_address or self.username
            message["Subject"] = subject
            message.set_content(body)
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(message)
            return {"ok": True, "to": to, "subject": subject}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}