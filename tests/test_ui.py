"""Dashboard clip-review behaviour in a real browser (local server only; skipped without playwright/chromium)."""
import json
import os
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")

from hypercut import hype, web  # noqa: E402

CHROME = "/opt/pw-browsers/chromium"


@pytest.fixture
def page(tmp_path):
    clips = tmp_path / "clips"
    clips.mkdir()
    for i, score in enumerate((5, 1)):
        (clips / f"kick_a_{i}.mp4").write_bytes(b"v")
        hype.write(clips / f"kick_a_{i}.json", {"score": score, "status": "pending"})
    app = web.App(tmp_path, {}, None, None)
    s = web.make_server(app, port=0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(**({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
        except Exception:
            pytest.skip("no chromium")
        pg = b.new_page()
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith("http://127.0.0.1") else r.abort())
        pg.goto(f"http://127.0.0.1:{s.server_address[1]}/")
        pg.click("[data-tab=clips]")
        pg.wait_for_selector("#clips .card")
        yield pg, clips
        b.close()
    s.shutdown()
    s.server_close()


def order(pg):
    return pg.eval_on_selector_all("#clips .card", "els => els.map(e => e.dataset.clip)")


def test_poll_does_not_reorder_cards_under_pointer(page):
    pg, clips = page
    assert order(pg) == ["kick_a_0.mp4", "kick_a_1.mp4"]
    hype.write(clips / "kick_a_1.json", {"score": 9, "status": "pending"})  # would now sort first
    pg.hover("#clips .card")
    pg.evaluate("loadClips()")
    assert order(pg) == ["kick_a_0.mp4", "kick_a_1.mp4"]
    pg.mouse.move(0, 0)
    pg.evaluate("loadClips()")
    assert order(pg) == ["kick_a_1.mp4", "kick_a_0.mp4"]


def test_review_updates_fresh_clip_not_stale_object(page):
    pg, _ = page
    held = []
    pg.route("**/api/clip-status", lambda r: held.append(r))
    pg.evaluate("() => { window.p = review(document.querySelector('#clips .card'), 'approved'); }")
    pg.wait_for_timeout(200)
    pg.evaluate("clipData = clipData.map(c => ({...c}))")  # a poll replaced the objects mid-request
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps({}))
    pg.evaluate("window.p")
    assert pg.evaluate("clipData.find(c => c.name === 'kick_a_0.mp4').status") == "approved"
