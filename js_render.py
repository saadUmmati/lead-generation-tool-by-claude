"""
js_render.py — optional JavaScript-rendering fallback, used only for
businesses whose website returned nothing useful from a plain HTTP fetch
(see enrich.py). Many modern sites inject their real contact info via
client-side JavaScript, which a plain fetch never sees.

This uses Playwright's Chromium - the same kind of browser gosom itself
already uses under the hood for Google Maps - run headless (no GPU
required; it renders in software). It is entirely optional: if the
`playwright` package isn't installed, or `playwright install chromium`
was never run, this quietly reports itself unavailable and the rest of
the tool works exactly as before, just without this fallback.

One shared browser is launched for the whole enrichment run (not one
per business - that would be far too heavy) and a semaphore limits how
many pages render at once, since headless Chromium tabs are meaningfully
heavier than a plain HTTP request even without a GPU.
"""

import asyncio

try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


class BrowserPool:
    def __init__(self, max_concurrent_renders: int = 2):
        self._playwright = None
        self._browser = None
        self._sem = asyncio.Semaphore(max_concurrent_renders)

    async def start(self) -> tuple[bool, str]:
        """Returns (started, note) - note explains why not, if it didn't."""
        if not _HAS_PLAYWRIGHT:
            return False, "Playwright isn't installed - skipping the JS-render fallback."
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            return True, "JS-render fallback ready."
        except Exception as e:
            return False, (
                f"Playwright's browser couldn't start ({e}) - has "
                "'playwright install chromium' been run? Continuing without "
                "the JS-render fallback."
            )

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def render(self, url: str, timeout_ms: int = 15000) -> str | None:
        if not self._browser:
            return None
        async with self._sem:
            page = None
            try:
                page = await self._browser.new_page()
                await page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                return await page.content()
            except Exception:
                return None
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
