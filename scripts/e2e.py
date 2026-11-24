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
    cmd = [sys.executable, "-m", "hypercut", "--port", str(PORT), "--out", out, "--quality", "worst"]
    srv = subprocess.Popen(cmd)
    try:
        time.sleep(2)
        with sync_playwright() as p:
            b = p.chromium.launch(**({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
            pg = b.new_page(viewport={"width": 1366, "height": 900})
            errors = []
            # third-party Twitch player noise is not ours to fix
            pg.on("pageerror", lambda e: "getLayoutMap" in str(e) or errors.append(str(e)))
            # CSP refusals and any console error raised by our own origin; embeds log their own noise
            # (a 404 from /api/frame is the expected "no footage yet" while the first segment records)
            pg.on("console", lambda m: m.type == "error" and (m.location.get("url", "").startswith(URL)
                                                              or "Content Security Policy" in m.text)
                  and "/api/frame" not in m.location.get("url", "") and errors.append(m.text))

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
            player = f"https://player.twitch.tv/?channel={login}&parent=localhost&parent=127.0.0.1&muted=false"
            expect(pg.locator("iframe.player")).to_have_attribute("src", player)
            assert pg.url.endswith(f"#/watch/twitch/{login}")
            pg.click("#fav")
            expect(pg.locator("#fav")).to_have_attribute("aria-pressed", "true")
            pg.click("#toggle")
            expect(pg.locator("#watchTerm .count")).to_have_text("5/5 RUNNING", timeout=90000)
            expect(pg.locator("#toggle")).to_have_text("Stop clipping")
            assert pg.title() == "(1 clipping) HyperCut", pg.title()
            pg.screenshot(path=f"{shots}/e2e-watch.png")

            # set facecam: needs a recorded segment, so retry until the first one lands
            for _ in range(30):
                pg.click("#camOpen")
                try:
                    expect(pg.locator("#cam")).to_be_visible(timeout=3000)
                    break
                except AssertionError:
                    time.sleep(2)
            expect(pg.locator("#camImg")).to_be_visible()
            assert pg.evaluate("document.querySelector('#camImg').naturalWidth") > 0
            r = pg.locator("#stage").bounding_box()
            pg.mouse.move(r["x"] + r["width"] * .7, r["y"] + r["height"] * .1)
            pg.mouse.down()
            pg.mouse.move(r["x"] + r["width"] * .95, r["y"] + r["height"] * .4, steps=5)
            pg.mouse.up()
            expect(pg.locator("#camSave")).to_be_enabled()
            pg.screenshot(path=f"{shots}/e2e-facecam.png")
            pg.click("#camSave")
            expect(pg.locator("#camMsg")).to_contain_text("Facecam saved")
            crop = pg.evaluate(f"fetch('/api/layout?platform=twitch&login={login}').then(r => r.json())")["crop"]
            assert 0.65 < crop["x"] < 0.75 and 0.2 < crop["w"] < 0.3, crop
            pg.click("#camOpen")
            expect(pg.locator("#camBox")).to_be_visible()  # saved box comes back
            pg.click("#camClear")
            expect(pg.locator("#camMsg")).to_contain_text("removed")
            assert pg.evaluate(f"fetch('/api/layout?platform=twitch&login={login}').then(r => r.json())")["crop"] is None

            pg.click("[data-tab=clipping]")
            expect(pg.locator("#sessions .term")).to_have_count(1)
            pg.click("#sessions [data-stop]")
            expect(pg.locator("#sessions .empty")).to_be_visible(timeout=30000)
            pg.click("[data-tab=clips]")
            expect(pg.locator("#clips .empty, #clips video").first).to_be_visible()
            assert pg.title() == "HyperCut", pg.title()

            pg.goto(URL + "#/watch/kick/xqc")  # deep link straight to a channel
            expect(pg.locator("iframe.player")).to_have_attribute("src", "https://player.kick.com/xqc")

            m = b.new_page(viewport={"width": 375, "height": 812}, device_scale_factor=2)
            m.goto(URL)
            expect(m.locator("#grid .card").first).to_be_visible(timeout=60000)
            assert m.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal scroll on mobile"
            m.screenshot(path=f"{shots}/e2e-mobile.png")

            down = {"items": [], "errors": {"twitch": "timed out", "kick": "timed out"}, "favorites": []}
            m.route("**/api/live", lambda r: r.fulfill(json=down))  # both platforms down -> empty state
            m.reload()
            expect(m.locator("#grid .empty")).to_contain_text("Couldn't reach Twitch or Kick")
            m.fill("#q", "x")  # searching must not wipe the message
            expect(m.locator("#grid .empty")).to_be_visible()
            assert not errors, errors
            b.close()
        print("E2E PASSED", shots)
    finally:
        srv.terminate()


if __name__ == "__main__":
    main()
