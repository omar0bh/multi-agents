"""Browser automation engine.

Playwright is optional.  When it is not installed, methods return a structured
error so the agent can report the missing dependency instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BrowserAutomationEngine:
    def __init__(self, headless: bool = True):
        self.headless = headless

    def available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def scrape_page(self, url: str, selector: str | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "error": "Playwright is not installed. Run: pip install playwright && playwright install chromium"}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if selector:
                    elements = page.locator(selector)
                    values = [elements.nth(i).inner_text() for i in range(min(elements.count(), 100))]
                    result = {"ok": True, "url": url, "selector": selector, "values": values, "count": len(values)}
                else:
                    result = {"ok": True, "url": url, "title": page.title(), "text": page.inner_text("body")[:20000]}
                browser.close()
                return result
        except Exception as exc:
            logger.exception("Browser scrape failed")
            return {"ok": False, "url": url, "error": str(exc)}

    def login_and_capture(self, url: str, username_selector: str, password_selector: str, username: str, password: str, submit_selector: str | None = None) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "error": "Playwright is not installed."}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=self.headless)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded")
                page.fill(username_selector, username)
                page.fill(password_selector, password)
                if submit_selector:
                    page.click(submit_selector)
                page.wait_for_load_state("networkidle", timeout=30000)
                result = {"ok": True, "url": page.url, "title": page.title(), "text": page.inner_text("body")[:10000]}
                browser.close()
                return result
        except Exception as exc:
            logger.exception("Browser login failed")
            return {"ok": False, "url": url, "error": str(exc)}