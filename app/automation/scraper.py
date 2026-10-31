"""Lead and contact scraping helpers."""

from __future__ import annotations

import re
from typing import Any

import httpx


class LeadScraper:
    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def fetch_leads_from_urls(self, urls: list[str]) -> dict[str, Any]:
        leads = []
        errors = []
        for url in urls:
            try:
                response = httpx.get(url, timeout=self.timeout, follow_redirects=True)
                text = response.text
                emails = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)))
                phones = sorted(set(re.findall(r"(?:\+?\d[\d\s().-]{7,}\d)", text)))
                leads.append(
                    {
                        "url": url,
                        "status_code": response.status_code,
                        "emails": emails[:20],
                        "phones": phones[:20],
                        "title": self._title(text),
                    }
                )
            except Exception as exc:
                errors.append({"url": url, "error": str(exc)})
        return {"ok": not errors or bool(leads), "leads": leads, "errors": errors, "count": len(leads)}

    @staticmethod
    def _title(text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
        return match.group(1).strip() if match else ""