"""Browser end-to-end: browse live -> filter -> watch -> Start -> 5/5 RUNNING -> Stop -> 0 sessions -> clips tab.

Run: python scripts/e2e.py [screenshot_dir]   (needs network + playwright + chromium)
"""
import os
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import expect, sync_playwright

PORT, URL = 8798, "http://127.0.0.1:8798"
CHROME = "/opt/pw-browsers/chromium"


def main():
    shots = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    out = tempfile.mkdtemp(prefix="hypercut-e2e-")
    srv = subprocess.Popen([sys.executable, "-m", "hypercut", "--port", str(PORT), "--out", out, "--quality", "worst"])
    try:
        time.sleep(2)
        with sync_playwright() as p:
            b = p.chromium.launch(**({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
            pg = b.new_page(viewport={"width": 1366, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: "getLayoutMap" in str(e) or errors.append(str(e)))  # third-party Twitch player noise

            pg.goto(URL)
            expect(pg.locator("#grid .card").first).to_be_visible(timeout=60000)
            assert "channels live" in pg.locator("#liveInfo").inner_text()
            for plat in ("twitch", "kick"):  # both platforms present + filter works
                pg.select_option("#plat", plat)
                cards = pg.locator("#grid .card")
                assert cards.count() > 0, f"no {plat} channels"
                assert {cards.nth(i).get_attribute("data-p") for i in range(min(5, cards.count()))} == {plat}
            pg.select_option("#plat", "twitch")
            first = pg.locator("#grid .card").first
            login = first.get_attribute("data-l")
            pg.fill("#q", login)
            expect(pg.locator("#grid .card").first).to_have_attribute("data-l", login)
            pg.screenshot(path=f"{shots}/e2e-home.png")

            pg.locator("#grid .card").first.click()
            expect(pg.locator("iframe.player")).to_have_attribute("src", f"https://player.twitch.tv/?channel={login}&parent=localhost&parent=127.0.0.1&muted=false")
            assert pg.url.endswith(f"#/watch/twitch/{login}")
            pg.click("#fav")
            expect(pg.locator("#fav")).to_have_attribute("aria-pressed", "true")
            pg.click("#toggle")
            expect(pg.locator("#watchTerm .count")).to_have_text("5/5 RUNNING", timeout=90000)
            expect(pg.locator("#toggle")).to_have_text("Stop clipping")
            pg.screenshot(path=f"{shots}/e2e-watch.png")

            pg.click("[data-tab=clipping]")
            expect(pg.locator("#sessions .term")).to_have_count(1)
            pg.click("#sessions [data-stop]")
            expect(pg.locator("#sessions .empty")).to_be_visible(timeout=30000)
            pg.click("[data-tab=clips]")
            expect(pg.locator("#clips .empty, #clips video").first).to_be_visible()

            pg.goto(URL + "#/watch/kick/xqc")  # deep link straight to a channel
            expect(pg.locator("iframe.player")).to_have_attribute("src", "https://player.kick.com/xqc")

            m = b.new_page(viewport={"width": 375, "height": 812}, device_scale_factor=2)
            m.goto(URL)
            expect(m.locator("#grid .card").first).to_be_visible(timeout=60000)
            assert m.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll on mobile"
            m.screenshot(path=f"{shots}/e2e-mobile.png")
            assert not errors, errors
            b.close()
        print("E2E PASSED", shots)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
