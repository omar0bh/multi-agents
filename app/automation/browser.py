"""Browser automation engine.

Playwright is optional.  When it is not installed, methods return a structured
error so the agent can report the missing dependency instead of crashing.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


import os
from pathlib import Path


class BrowserAutomationEngine:
    def __init__(self, headless: bool = True, browser: str | None = None):
        self.headless = headless
        self.browser = (browser or os.getenv("BROWSER_ENGINE", "brave")).lower().strip()

    def brave_executable(self) -> str | None:
        explicit = os.getenv("BRAVE_EXECUTABLE_PATH")
        if explicit and Path(explicit).exists():
            return explicit

        candidates = []
        if os.name == "nt":
            candidates = [
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            ]
        elif os.sys.platform == "darwin":
            candidates = [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")]
        else:
            candidates = [
                Path("/usr/bin/brave-browser"),
                Path("/usr/bin/brave"),
                Path("/usr/bin/brave-browser-stable"),
                Path("/opt/brave.com/brave/brave-browser"),
                Path("/snap/bin/brave"),
            ]
        for candidate in candidates:
            if candidate and candidate.exists():
                return str(candidate)
        return None

    def available(self) -> bool:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        if self.browser == "brave":
            return self.brave_executable() is not None
        return True

    def _launch(self, playwright):
        if self.browser == "brave":
            executable = self.brave_executable()
            if not executable:
                raise RuntimeError("Brave browser was not found. Install Brave or set BRAVE_EXECUTABLE_PATH.")
            return playwright.chromium.launch(headless=self.headless, executable_path=executable)
        return playwright.chromium.launch(headless=self.headless)

    def scrape_page(self, url: str, selector: str | None = None, timeout_ms: int = 30000) -> dict[str, Any]:
        if not self.available():
            if self.browser == "brave":
                return {"ok": False, "error": "Playwright and/or Brave browser is not installed. Run: pip install playwright; sudo apt install brave-browser (or set BRAVE_EXECUTABLE_PATH)."}
            return {"ok": False, "error": "Playwright is not installed. Run: pip install playwright"}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = self._launch(playwright)
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
                browser = self._launch(playwright)
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