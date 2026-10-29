"""Chromiumで製品UIと実際の録画engineを検証する（物理カメラは不要）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from tests.acceptance.virtual_two_camera import create_acceptance_environment

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def recorder(tmp_path_factory):
    environment = create_acceptance_environment(
        tmp_path_factory.mktemp("browser-recorder")
    )
    server = make_server("127.0.0.1", 0, environment.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        environment.runtime.close()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as driver:
        if not Path(driver.chromium.executable_path).exists():
            pytest.skip("run python -m playwright install chromium first")
        instance = driver.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture
def page(browser, recorder):
    context = browser.new_context(
        base_url=recorder, viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(recorder)
    page.get_by_role("heading", name="カメラ 1", exact=True).wait_for()
    yield page
    context.close()
    assert not errors, errors


def open_settings(page, destination=None):
    page.get_by_role("button", name="設定", exact=True).click()
    page.get_by_role("button", name="カメラ1", exact=True).wait_for()
    if destination:
        page.get_by_role("button", name=destination, exact=True).click()


def open_storage(page):
    open_settings(page, "ストレージ")
    page.get_by_role("button", name="自動削除", exact=True).click()


def test_delayed_recordings_status_does_not_replace_a_new_settings_view(page):
    status = page.request.get("/api/v1/status").json()
    pending = []

    def delay_first(route):
        if not pending:
            pending.append(route)
        else:
            route.continue_()

    page.route("**/api/v1/status", delay_first)
    page.get_by_role("button", name="録画", exact=True).click()
    playwright.expect(
        page.get_by_role("button", name="録画", exact=True)
    ).to_have_attribute("aria-current", "page")
    open_settings(page)
    assert pending
    pending[0].fulfill(json=status)
    # Wait for the deliberately delayed callback before checking a negative.
    page.wait_for_timeout(100)
    playwright.expect(
        page.get_by_role("button", name="カメラ1", exact=True)
    ).to_be_visible()
    playwright.expect(page.locator("#recordings-date")).to_have_count(0)


def test_recording_screen_uses_a_native_time_input_instead_of_a_file_dropdown(page):
    page.get_by_role("button", name="録画", exact=True).click()
    page.locator("#recordings-date").wait_for()
    playwright.expect(page.get_by_label("日付", exact=True)).to_be_visible()
    playwright.expect(page.locator('#recordings-time[type="time"]')).to_be_visible()
    playwright.expect(page.locator("#recordings-time option")).to_have_count(0)
    playwright.expect(page.locator("main button:visible")).to_have_count(0)
    playwright.expect(page.locator("#recordings-display")).to_have_count(0)
    playwright.expect(page.locator(".recording-playback")).to_be_hidden()


def test_session_expiry_returns_to_login_without_reloading_the_page(page):
    def expired(route):
        route.fulfill(
            status=401,
            json={
                "error": {
                    "code": "authentication_required",
                    "message": "ログインしてください",
                }
            },
        )

    def authentication_status(route):
        response = route.fetch()
        status = response.json()
        status.update(login_protection_enabled=True, authenticated=False)
        route.fulfill(response=response, json=status)

    page.route("**/api/v1/auth/status", authentication_status)
    page.route("**/api/v1/settings", expired)
    page.get_by_role("button", name="設定", exact=True).click()
    playwright.expect(
        page.get_by_role("button", name="ログイン", exact=True)
    ).to_be_visible()
    playwright.expect(page.locator("#top-navigation button")).to_have_count(0)


def test_unavailable_devices_keep_saved_settings_on_apply(page):
    original = page.request.get("/api/v1/settings").json()["settings"]

    def unavailable(route):
        response = route.fetch()
        capabilities = response.json()
        capabilities["cameras"] = []
        capabilities["volumes"] = []
        route.fulfill(response=response, json=capabilities)

    page.route("**/api/v1/capabilities", unavailable)
    open_storage(page)
    assert page.locator("#settings-volume").input_value() == (
        f"usb:{original['volume']['persistent_id']}"
    )
    for camera in original["cameras"]:
        assert (
            page.locator(f"#camera-{camera['camera_id']}-port").input_value()
            == (camera["usb_port_key"])
        )
    page.locator("#settings-recover").fill(
        str(original["auto_recover_tenths_gb"] / 10 + 0.1)
    )
    page.get_by_role("button", name="設定を保存", exact=True).click()
    playwright.expect(page.locator(".message").first).to_have_text(
        "設定を保存しました。", timeout=15000
    )
    saved = page.request.get("/api/v1/settings").json()["settings"]
    assert saved["volume"] == original["volume"]
    assert saved["cameras"] == original["cameras"]


def test_unreported_profile_does_not_silently_select_another_profile(page):
    original = page.request.get("/api/v1/settings").json()["settings"]

    def changed_profiles(route):
        response = route.fetch()
        capabilities = response.json()
        for camera in capabilities["cameras"]:
            camera["profiles"] = [
                {"width": 640, "height": 480, "fps": [10], "input_format": "mjpeg"}
            ]
        route.fulfill(response=response, json=capabilities)

    page.route("**/api/v1/capabilities", changed_profiles)
    open_settings(page, "カメラ1")
    for camera in original["cameras"]:
        assert page.locator(f"#camera-{camera['camera_id']}-profile").input_value() == (
            f"{camera['width']}x{camera['height']}@{camera['fps']}:{camera['quality']}"
        )


def test_selected_microphone_is_sent_when_changing_usb_port(page):
    submitted = []

    def new_camera(route):
        response = route.fetch()
        capabilities = response.json()
        capabilities["cameras"].append(
            {
                "port_key": "replacement-camera",
                "video_interface_key": "replacement-camera:1.0",
                "device_path": "/dev/test-only",
                "profiles": [
                    {"width": 320, "height": 240, "fps": [5], "input_format": "mjpeg"}
                ],
                "audio_interfaces": [
                    {"interface_key": "replacement-microphone", "label": "Test mic"}
                ],
            }
        )
        route.fulfill(response=response, json=capabilities)

    def capture_settings(route):
        if route.request.method != "PATCH":
            route.continue_()
            return
        submitted.append(route.request.post_data_json)
        route.fulfill(
            status=422,
            json={"error": {"message": "test intercepted; no settings changed"}},
        )

    page.route("**/api/v1/capabilities", new_camera)
    page.route("**/api/v1/settings", capture_settings)
    open_settings(page, "カメラ1")
    page.locator("#camera-1-port").select_option("replacement-camera")
    page.locator("#camera-1-audio-interface").select_option("replacement-microphone")
    page.locator("#camera-1-audio").check()
    page.get_by_role("button", name="設定を保存", exact=True).click()
    page.get_by_role("button", name="変更して録画を再開", exact=True).click()
    playwright.expect(page.locator(".message").first).to_have_text(
        "test intercepted; no settings changed"
    )
    camera = submitted[0]["settings"]["cameras"][0]
    assert camera["audio_interface_key"] == "replacement-microphone"
    assert camera["video_interface_key"] == "replacement-camera:1.0"


def test_settings_cannot_be_edited_while_applying(page):
    completed = [False]

    def accept_settings(route):
        if route.request.method == "PATCH":
            route.fulfill(status=202, json={"operation_id": "browser-test-operation"})
        else:
            route.continue_()

    def operation(route):
        route.fulfill(json={"state": "succeeded" if completed[0] else "running"})

    page.route("**/api/v1/settings", accept_settings)
    page.route("**/api/v1/operations/browser-test-operation", operation)
    open_storage(page)
    page.locator("#settings-recover").fill("1.5")
    page.get_by_role("button", name="設定を保存", exact=True).click()
    playwright.expect(page.locator("#camera-1-profile")).to_be_disabled()
    playwright.expect(page.locator("#settings-volume")).to_be_disabled()
    playwright.expect(page.locator("#settings-recover")).to_be_disabled()
    completed[0] = True
    playwright.expect(page.locator("#settings-volume")).to_be_enabled(timeout=5000)


def test_unsaved_settings_survive_active_tab_and_cancelled_navigation(page):
    open_storage(page)
    page.locator("#settings-recover").fill("1.8")
    page.get_by_role("button", name="設定", exact=True).click()
    assert page.locator("#settings-recover").input_value() == "1.8"
    page.get_by_role("button", name="録画", exact=True).click()
    confirmation = page.get_by_role("dialog", name="変更を保存せずに移動しますか？")
    playwright.expect(confirmation).to_be_visible()
    confirmation.get_by_role("button", name="キャンセル", exact=True).click()
    assert page.locator("#settings-recover").input_value() == "1.8"


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_navigation_does_not_overlap_status_or_overflow(page, width):
    def show_logout(route):
        response = route.fetch()
        status = response.json()
        status["login_protection_enabled"] = True
        route.fulfill(response=response, json=status)

    page.route("**/api/v1/auth/status", show_logout)
    page.set_viewport_size({"width": width, "height": 900})
    page.reload()
    logout = page.get_by_role("button", name="ログアウト", exact=True)
    logout.wait_for()
    badge = page.locator("#connection").bounding_box()
    action = logout.bounding_box()
    assert (
        badge["x"] + badge["width"] <= action["x"]
        or action["x"] + action["width"] <= badge["x"]
        or badge["y"] + badge["height"] <= action["y"]
        or action["y"] + action["height"] <= badge["y"]
    ), {"badge": badge, "logout": action}
    tabs = page.locator("#top-navigation button")
    assert len({tabs.nth(index).bounding_box()["y"] for index in range(3)}) == 1
    for index in range(3):
        box = tabs.nth(index).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= action["x"]
    for name in ["設定", "録画", "ライブ"]:
        page.get_by_role("button", name=name, exact=True).click()
        if name == "設定":
            page.get_by_role("button", name="カメラ1", exact=True).wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_browser_selects_time_plays_and_downloads_recording(page, tmp_path):
    camera = page.locator('[data-camera-id="1"]')
    playwright.expect(camera.locator(".preview-frame img")).not_to_have_js_property(
        "naturalWidth", 0
    )
    # A preview can be ready before the recording writer has received frames.
    for _ in range(100):
        status = page.request.get("/api/v1/status").json()
        first = next(item for item in status["cameras"] if item["camera_id"] == 1)
        if first["recording_active"] and first["frames"] >= 5:
            break
        page.wait_for_timeout(200)
    else:
        raise AssertionError("camera 1 did not begin recording")
    camera.get_by_role("button", name="録画を停止", exact=True).click()
    playwright.expect(camera.get_by_role("button", name="録画を再開")).to_be_visible(
        timeout=30000
    )
    # The time field remains editable even on empty days; wait for an actual
    # finalized camera-1 recording instead of using its enabled state as proof.
    for _ in range(100):
        items = page.request.get("/api/v1/recordings?camera_id=1&limit=20").json()[
            "items"
        ]
        finalized = [item for item in items if item["state"] == "finalized"]
        if finalized:
            break
        page.wait_for_timeout(200)
    else:
        raise AssertionError("camera 1 did not finalize a recording")
    page.get_by_role("button", name="録画", exact=True).click()
    choice = page.locator("#recordings-time")
    playwright.expect(choice).to_be_enabled(timeout=15000)
    # A clock time opens the matching file without enumerating file choices.
    data = finalized[0]
    status = page.request.get("/api/v1/status").json()
    from datetime import datetime
    from zoneinfo import ZoneInfo

    instant = datetime.fromtimestamp(
        data["start_utc_ns"] / 1e9, ZoneInfo(status["time"]["timezone"])
    )
    choice.fill(instant.strftime("%H:%M"))
    player = page.locator(".recording-playback video")
    playwright.expect(player).not_to_have_js_property("currentTime", 0, timeout=15000)
    with page.expect_download() as download:
        page.get_by_role("link", name="ダウンロード", exact=True).click()
    media = tmp_path / "recording.mp4"
    download.value.save_as(media)
    assert media.stat().st_size > 0
    page.get_by_role("button", name="ライブ", exact=True).click()
    page.locator('[data-camera-id="1"]').get_by_role(
        "button", name="録画を再開", exact=True
    ).click()


def test_login_and_administrator_confirmation_with_real_authentication(page):
    open_settings(page, "パスワード")
    page.get_by_role("button", name="管理者保護", exact=True).click()
    page.locator("#admin-protection-enabled").check()
    page.locator("#admin-password").fill("browser-admin-secret")
    page.locator("#admin-password-confirmation").fill("browser-admin-secret")
    page.get_by_role("button", name="管理者保護を保存", exact=True).click()
    page.get_by_role("button", name="カメラ1", exact=True).wait_for()
    page.get_by_role("button", name="パスワード", exact=True).click()
    page.get_by_role("button", name="管理者保護", exact=True).click()
    playwright.expect(page.locator("#admin-protection-enabled")).to_be_checked()
    # Wait for refresh() to replace the old settings form after the save.
    playwright.expect(page.locator("#admin-password")).to_have_value("")
    page.get_by_role("button", name="パスワードに戻る", exact=True).click()
    page.get_by_role("button", name="ログイン保護", exact=True).click()
    page.locator("#login-protection-enabled").check()
    page.locator("#login-password").fill("browser-viewer-secret")
    page.locator("#login-password-confirmation").fill("browser-viewer-secret")
    page.get_by_role("button", name="ログイン保護を保存", exact=True).click()
    page.locator("#admin-reauth-password").fill("browser-admin-secret")
    page.get_by_role("button", name="確認して続ける", exact=True).click()
    page.locator("#password").fill("browser-viewer-secret")
    page.get_by_role("button", name="ログイン", exact=True).click()
    page.get_by_role("button", name="カメラ1", exact=True).wait_for()
    page.get_by_role("button", name="ストレージ", exact=True).click()
    page.get_by_role("button", name="自動削除", exact=True).click()
    page.locator("#settings-recover").fill("1.6")
    page.get_by_role("button", name="設定を保存", exact=True).click()
    page.locator("#admin-reauth-password").fill("browser-admin-secret")
    page.get_by_role("button", name="確認して続ける", exact=True).click()
    playwright.expect(page.locator(".settings-browser > .message")).to_have_text(
        "設定を保存しました。", timeout=15000
    )
    page.get_by_role("button", name="ログアウト", exact=True).click()
    playwright.expect(
        page.get_by_role("button", name="ログイン", exact=True)
    ).to_be_visible()


def test_reverting_a_setting_clears_dirty_state(page):
    open_settings(page, "カメラ1")
    field = page.locator("#camera-1-profile")
    original = field.input_value()
    field.select_option(field.locator("option").first.get_attribute("value"))
    save = page.get_by_role("button", name="設定を保存", exact=True)
    playwright.expect(save).to_be_enabled()
    field.select_option(original)
    playwright.expect(save).to_be_hidden()
    page.get_by_role("button", name="録画", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="録画")).to_be_visible()
    playwright.expect(page.get_by_role("dialog")).to_have_count(0)


def test_settings_home_has_no_edit_controls_or_explanation_wall(page):
    open_settings(page)
    playwright.expect(page.locator("main input:visible")).to_have_count(0)
    playwright.expect(page.locator("main select:visible")).to_have_count(0)
    playwright.expect(page.locator(".save-bar")).to_be_hidden()
    page.get_by_role("button", name="省電力モード", exact=True).click()
    playwright.expect(page.locator("#settings-power-saving")).to_be_visible()
    playwright.expect(page.locator("#settings-volume")).to_be_hidden()
    playwright.expect(page.locator("#camera-1-profile")).to_be_hidden()
    playwright.expect(page.locator(".power-saving-summary")).to_be_hidden()
    page.get_by_role("button", name="設定に戻る", exact=True).click()
    playwright.expect(
        page.get_by_role("button", name="カメラ1", exact=True)
    ).to_be_visible()


def test_unconfigured_camera_has_no_failed_preview(page):
    def unconfigured(route):
        response = route.fetch()
        data = response.json()
        data["settings"]["cameras"][1]["usb_port_key"] = None
        route.fulfill(response=response, json=data)

    page.route("**/api/v1/settings", unconfigured)
    page.reload()
    unused = page.locator('[data-camera-id="2"]')
    playwright.expect(
        unused.get_by_text("使用していません", exact=True)
    ).to_be_visible()
    playwright.expect(unused.locator(".preview-frame")).to_be_hidden()
    unused.get_by_role("button", name="カメラを設定").click()
    playwright.expect(page.locator("#camera-2-port")).to_be_focused(timeout=30000)
