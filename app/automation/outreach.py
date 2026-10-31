"""Outreach campaign generator and sender."""

from __future__ import annotations

from typing import Any

from app.automation.mail import MailAutomation


class OutreachEngine:
    def __init__(self, mailer: MailAutomation):
        self.mailer = mailer

    def build_proposal(self, business_name: str, city: str, service: str = "website") -> str:
        return (
            f"Hello {business_name},\n\n"
            f"I noticed your business in {city} may benefit from a modern {service}. "
            "I help local businesses launch fast, mobile-friendly websites with booking, "
            "maps, and WhatsApp integration.\n\n"
            "Would you be open to a short 10-minute call this week?\n\n"
            "Best regards,\n"
            "AI Web Solutions"
        )

    def send_campaign(self, leads: list[dict[str, Any]], dry_run: bool = True) -> dict[str, Any]:
        results = []
        for lead in leads:
            email_address = (lead.get("emails") or [None])[0]
            if not email_address:
                results.append({"lead": lead.get("url"), "ok": False, "error": "No email address"})
                continue
            body = self.build_proposal(lead.get("name") or "your business", lead.get("city") or "your city")
            if dry_run:
                results.append({"lead": lead.get("url"), "to": email_address, "ok": True, "dry_run": True, "preview": body[:500]})
            else:
                sent = self.mailer.reply(email_address, "Quick website idea for your business", body)
                results.append({"lead": lead.get("url"), "to": email_address, **sent})
        return {"ok": True, "count": len(results), "results": results, "dry_run": dry_run}