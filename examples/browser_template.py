"""Template for sources that need a real browser (Playwright + virtual display).
Works inside the candyresolver container (Xvfb + Chromium are preinstalled)."""
from pyvirtualdisplay import Display
from playwright.sync_api import sync_playwright


def resolve(ctx):
    found = []
    disp = Display(visible=False, size=(1280, 720))
    disp.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,  # runs on the virtual display; headless=True also works
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],  # --no-sandbox is REQUIRED in docker
            )
            page = browser.new_page()
            page.on("response", lambda r: found.append(r.url) if ".m3u8" in r.url or ".mp4" in r.url else None)
            page.goto("https://example.com", timeout=30000)
            page.wait_for_timeout(5000)
            browser.close()
    finally:
        disp.stop()
    return {"streams": [{"url": u, "quality": "auto", "format": "hls" if ".m3u8" in u else "mp4"} for u in found]}
