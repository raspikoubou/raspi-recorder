"""説明書用の実画面を、隔離した合成入力環境で撮影する開発用コマンド。

PLAYWRIGHT_BROWSERS_PATH=/path/to/browsers TZ=Asia/Tokyo \
    .venv/bin/python -m tests.web.capture_manual
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from tests.acceptance.virtual_two_camera import create_acceptance_environment

OUTPUT = Path(__file__).resolve().parents[2] / "docs/user/images/manual"


def capture() -> None:
    with tempfile.TemporaryDirectory(prefix="rr-manual-") as temporary:
        environment = create_acceptance_environment(
            Path(temporary),
        )
        server = make_server("127.0.0.1", 0, environment.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as driver:
                browser = driver.chromium.launch(args=["--no-sandbox"])
                context = browser.new_context(
                    viewport={"width": 1280, "height": 960},
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                    reduced_motion="reduce",
                )
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://localhost:{server.server_port}")
                expect(page.locator(".preview-frame img").first).to_have_js_property(
                    "naturalWidth", 320, timeout=20000
                )

                def shot(name: str, locator=None, *, full_page=False) -> None:
                    page.evaluate("document.fonts.ready")
                    if locator:
                        # Keep sticky navigation and the save bar outside the
                        # section being captured, without changing product CSS.
                        height = locator.bounding_box()["height"]
                        page.set_viewport_size(
                            {"width": 1280, "height": max(960, round(height) + 500)}
                        )
                        locator.evaluate(
                            "e => window.scrollTo(0, Math.max(0, "
                            "e.getBoundingClientRect().top + window.scrollY - 240))"
                        )
                    (locator or page).screenshot(
                        path=OUTPUT / f"{name}.png",
                        animations="disabled",
                        **({} if locator else {"full_page": full_page}),
                    )
                    if locator:
                        page.set_viewport_size({"width": 1280, "height": 960})

                shot("live-desktop")
                page.set_viewport_size({"width": 390, "height": 844})
                shot("live-mobile")
                page.set_viewport_size({"width": 1280, "height": 960})
                for _ in range(2):
                    time.sleep(1.5)
                    for button in page.get_by_role(
                        "button", name="録画を停止", exact=True
                    ).all():
                        button.click()
                    expect(page.get_by_role("button", name="録画を再開")).to_have_count(
                        2
                    )
                    for button in page.get_by_role(
                        "button", name="録画を再開", exact=True
                    ).all():
                        button.click()
                page.get_by_role("button", name="録画", exact=True).click()
                expect(page.locator("#recordings-time")).to_be_enabled(timeout=15000)
                page.set_viewport_size({"width": 390, "height": 844})
                shot("recordings-picker-simple")
                from datetime import datetime
                from zoneinfo import ZoneInfo

                data = page.request.get("/api/v1/recordings?limit=1").json()["items"][0]
                zone = page.request.get("/api/v1/status").json()["time"]["timezone"]
                value = datetime.fromtimestamp(
                    data["start_utc_ns"] / 1e9, ZoneInfo(zone)
                ).strftime("%H:%M")
                page.locator("#recordings-time").fill(value)
                expect(page.locator(".recording-playback video")).to_have_js_property(
                    "readyState", 4, timeout=15000
                )
                shot("recording-playback-simple")
                page.get_by_role("button", name="設定", exact=True).click()
                page.get_by_role("button", name="カメラ1", exact=True).wait_for()
                shot("settings-home-simple")
                for label, name in [
                    ("省電力モード", "settings-recording"),
                    ("ストレージ", "settings-storage"),
                    ("カメラ1", "settings-camera"),
                    ("日時", "settings-time"),
                    ("ソフトウェア更新", "settings-update"),
                ]:
                    page.get_by_role("button", name=label, exact=True).click()
                    shot(name)
                    page.get_by_role("button", name="設定に戻る", exact=True).click()
                page.get_by_role("button", name="パスワード", exact=True).click()
                page.get_by_text("ログイン保護", exact=True).click()
                assert not errors, errors
                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            environment.runtime.close()


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    capture()
    print(f"Screenshots: {OUTPUT}")
