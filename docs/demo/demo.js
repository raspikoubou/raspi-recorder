"use strict";

(() => {
  const base = new URL(".", document.currentScript.src);
  const realFetch = window.fetch.bind(window);
  const clone = (value) => structuredClone(value);
  const ns = (milliseconds) => Math.round(milliseconds * 1e6);
  const initialNow = Date.now();
  let clockOffset = 0;
  let authenticated = true;
  let adminRecentUntil = 0;
  let authRevision = 0;
  let loginEnabled = false;
  let adminEnabled = false;
  const passwords = { login: null, admin: null };
  const started = [true, true];
  let currentVersion = "0.1.43";
  let candidate = null;
  const operations = new Map();
  const requests = new Map();
  const profiles = [
    { width: 640, height: 480, fps: [10, 15] },
    { width: 1280, height: 720, fps: [10, 15, 30] },
    { width: 1920, height: 1080, fps: [15, 30] },
  ];
  const volumes = [
    { kind: "microSD", persistent_id: "sample-microsd", filesystem: "ext4", total_bytes: 64e9, available_bytes: 42e9 },
    { kind: "USB", persistent_id: "sample-usb", filesystem: "exfat", total_bytes: 128e9, available_bytes: 96e9 },
  ];
  let settings = {
    schema_version: 2, revision: 0,
    auto_delete_start_tenths_gb: 50, auto_recover_tenths_gb: 70,
    volume: { kind: "microSD", persistent_id: null },
    power_saving_enabled: false, recording_segment_minutes: 5,
    cameras: [1, 2].map((id) => ({
      camera_id: id, width: 1280, height: 720, fps: 15, quality: "standard",
      audio_enabled: false, overlay_enabled: true, overlay_corner: "top_left",
      overlay_size: "medium", usb_port_key: "sample-camera-" + id,
      video_interface_key: "sample-video-" + id, audio_interface_key: null,
    })),
  };

  const recordings = [];
  const day = new Date(initialNow + 9 * 3600e3).toISOString().slice(0, 10);
  for (let daysAgo = 0; daysAgo < 3; daysAgo++) {
    const midnight = Date.parse(day + "T00:00:00+09:00") - daysAgo * 86400e3;
    for (let camera = 1; camera <= 2; camera++) {
      for (let part = 0; part < 4; part++) {
        const start = midnight + 9 * 3600e3 + part * 5 * 60e3;
        recordings.push({
          recording_id: "sample-day-" + daysAgo + "-camera-" + camera + "-part-" + part,
          camera_id: camera, part_index: part + 1, state: "finalized",
          start_utc_ns: ns(start), end_utc_ns: ns(start + 15e3),
          sequence: recordings.length + 1,
        });
      }
    }
  }

  const time = () => ({
    utc_ns: ns(Date.now() + clockOffset), timezone: "Asia/Tokyo",
    helper_timezone: "Asia/Tokyo", helper_available: true,
    ntp_enabled: true, synchronized: true,
  });
  const auth = () => ({
    login_protection_enabled: loginEnabled, admin_protection_enabled: adminEnabled,
    login_password_configured: Boolean(passwords.login),
    admin_password_configured: Boolean(passwords.admin),
    authenticated, csrf_token: "browser-demo", revision: authRevision,
  });
  const response = (payload, status = 200) => new Response(JSON.stringify(payload), {
    status, headers: { "Content-Type": "application/json" },
  });
  const error = (message, status = 400, code = "invalid_request") =>
    response({ error: { message, code } }, status);
  const hashPassword = async (value) => {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
  };
  function enqueue(body, effect, result = {}) {
    if (body.request_id && requests.has(body.request_id)) {
      return response({ operation_id: requests.get(body.request_id) }, 202);
    }
    const id = crypto.randomUUID().replaceAll("-", "");
    operations.set(id, { at: performance.now(), effect, result, complete: false });
    if (body.request_id) requests.set(body.request_id, id);
    return response({ operation_id: id }, 202);
  }
  function operation(id) {
    const job = operations.get(id);
    if (!job) return error("操作が見つかりません。", 404);
    if (performance.now() - job.at < 450) return response({ state: "applying" });
    if (!job.complete) { job.effect(); job.complete = true; }
    return response({ state: "succeeded", result: clone(job.result) });
  }

  const videoSources = new Map();
  const posters = new Map();
  function sourceFor(camera) {
    if (!videoSources.has(camera)) {
      const video = document.createElement("video");
      video.muted = true;
      video.defaultMuted = true;
      video.loop = true;
      video.playsInline = true;
      video.preload = "auto";
      video.src = new URL("media/sample-camera-" + camera + ".mp4", base).href;
      videoSources.set(camera, video);
    }
    return videoSources.get(camera);
  }
  function pauseSources() { for (const video of videoSources.values()) video.pause(); }
  document.addEventListener("visibilitychange", () => { if (document.hidden) pauseSources(); });
  new MutationObserver(() => {
    if (!document.querySelector("[data-camera-id]")) pauseSources();
  }).observe(document.getElementById("app"), { childList: true, subtree: true });
  window.addEventListener("pagehide", pauseSources);

  async function preview(camera, signal) {
    const config = settings.cameras[camera - 1];
    const video = sourceFor(camera);
    if (!document.hidden && started[camera - 1] && config.usb_port_key) {
      void video.play().catch(() => {});
    } else {
      video.pause();
    }
    const canvas = document.createElement("canvas");
    canvas.width = settings.power_saving_enabled ? 640 : config.width;
    canvas.height = settings.power_saving_enabled ? 480 : config.height;
    const context = canvas.getContext("2d");
    if (video.readyState >= 2) {
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
    } else {
      if (!posters.has(camera)) {
        posters.set(camera, (async () => {
          const file = await realFetch(new URL("media/poster-camera-" + camera + ".jpg", base));
          return createImageBitmap(await file.blob());
        })());
      }
      context.drawImage(await posters.get(camera), 0, 0, canvas.width, canvas.height);
    }
    if (config.overlay_enabled) {
      const fontSize = { small: 16, medium: 24, large: 34 }[config.overlay_size] * canvas.width / 1280;
      context.font = "bold " + Math.max(12, fontSize) + "px sans-serif";
      const value = new Intl.DateTimeFormat("sv-SE", {
        timeZone: "Asia/Tokyo", dateStyle: "short", timeStyle: "medium",
      }).format(new Date(Date.now() + clockOffset));
      const right = config.overlay_corner.endsWith("right");
      const bottom = config.overlay_corner.startsWith("bottom");
      const x = right ? canvas.width - 12 : 12;
      const y = bottom ? canvas.height - 20 : Math.max(60, canvas.height * .14);
      context.textAlign = right ? "right" : "left";
      context.strokeStyle = "#102a36"; context.lineWidth = 4;
      context.strokeText(value, x, y);
      context.fillStyle = "white"; context.fillText(value, x, y);
    }
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", .86));
    return new Response(blob, { headers: { "Content-Type": "image/jpeg" } });
  }

  function validateSettings(next) {
    if (![5, 10, 15, 20, 30, 60].includes(next.recording_segment_minutes)) return "保存単位を選んでください。";
    if (!next.cameras || next.cameras.length !== 2) return "カメラの設定を確認してください。";
    const ports = next.cameras.map((camera) => camera.usb_port_key).filter(Boolean);
    if (new Set(ports).size !== ports.length) return "同じUSBカメラを重複して指定できません。";
    if (next.auto_recover_tenths_gb <= next.auto_delete_start_tenths_gb) return "回復目標は削除開始の空き容量より大きくしてください。";
    return null;
  }
  async function handle(url, method, body, signal) {
    const path = url.pathname.replace("/api/v1/", "");
    if (path === "auth/status") return response(auth());
    if (path === "auth/login") {
      if (!passwords.login || await hashPassword(body.password || "") !== passwords.login) {
        return error("パスワードが一致しません。", 401);
      }
      authenticated = true; return response(auth());
    }
    if (path === "auth/logout") { authenticated = false; adminRecentUntil = 0; return response({ accepted: true }); }
    if (path === "auth/admin/reauthenticate") {
      if (await hashPassword(body.password || "") !== passwords.admin) return error("パスワードが一致しません。", 401);
      adminRecentUntil = Date.now() + 600e3; return response(auth());
    }
    if (loginEnabled && !authenticated) return error("ログインしてください。", 401, "authentication_required");
    if (method !== "GET" && adminEnabled && adminRecentUntil < Date.now() && !/preview\/demand$/.test(path)
      && !/cameras\/[12]\/(start|stop)$/.test(path)) {
      return error("管理者の確認が必要です。", 428, "reauthentication_required");
    }
    if (path === "auth/protections") {
      if (body.expected_revision !== authRevision) return error("画面を読み直してください。", 409, "revision_conflict");
      const nextPasswords = { ...passwords };
      for (const kind of ["login", "admin"]) {
        const password = body[kind + "_password"];
        if (password !== undefined) {
          if (password.length < 8 || password !== body[kind + "_confirmation"]) return error("8文字以上の一致するパスワードを入力してください。");
          nextPasswords[kind] = await hashPassword(password);
        }
        if (body[kind + "_enabled"] && !nextPasswords[kind]) return error("パスワードを登録してください。");
      }
      Object.assign(passwords, nextPasswords);
      loginEnabled = body.login_enabled; adminEnabled = body.admin_enabled;
      authenticated = !loginEnabled;
      adminRecentUntil = 0; authRevision++;
      return response(auth());
    }
    if (path === "settings" && method === "GET") return response({ settings: clone(settings) });
    if (path === "settings" && method === "PATCH") {
      if (body.expected_revision !== settings.revision) return error("設定を読み直してください。", 409, "revision_conflict");
      const problem = validateSettings(body.settings);
      if (problem) return error(problem);
      const next = clone(body.settings);
      next.revision = settings.revision + 1;
      return enqueue(body, () => { settings = next; }, { revision: next.revision });
    }
    if (path === "capabilities") return response({
      volumes: clone(volumes), cameras: [1, 2].map((camera) => ({
        port_key: "sample-camera-" + camera, video_interface_key: "sample-video-" + camera,
        product: camera === 1 ? "サンプルカメラ（ライン全景）" : "サンプルカメラ（検査工程）",
        profiles: clone(profiles),
        audio_interfaces: [{ interface_key: "sample-microphone-" + camera, label: "サンプルマイク" }],
      })),
    });
    if (path === "status") return response({
      revision: settings.revision, service: "running", time: time(),
      volume: { ...clone(volumes.find((v) => v.kind === settings.volume.kind)), selected: true },
      cameras: settings.cameras.map((camera, index) => ({
        camera_id: index + 1, state: started[index] ? "continuous" : "stopped",
        desired_running: started[index], recording_active: started[index] && Boolean(camera.usb_port_key),
        blockers: [], audio: { enabled: camera.audio_enabled && !settings.power_saving_enabled },
      })),
    });
    const cameraRoute = /^cameras\/([12])\/(start|stop|preview\/demand|preview\.jpg)$/.exec(path);
    if (cameraRoute) {
      const camera = Number(cameraRoute[1]), action = cameraRoute[2];
      if (action === "preview.jpg") return preview(camera, signal);
      if (action === "start" || action === "stop") {
        started[camera - 1] = action === "start";
        if (action === "stop") videoSources.get(camera)?.pause();
      }
      if (action === "preview/demand" && body.active === false) videoSources.get(camera)?.pause();
      return response({ accepted: true });
    }
    if (path === "recordings") {
      const from = Number(url.searchParams.get("from_utc_ns") || 0);
      const to = Number(url.searchParams.get("to_utc_ns") || Infinity);
      const camera = Number(url.searchParams.get("camera_id") || 0);
      const limit = Number(url.searchParams.get("limit") || 500);
      const items = recordings.filter((r) => r.end_utc_ns >= from && r.start_utc_ns < to
        && (!camera || r.camera_id === camera)).sort((a, b) => b.start_utc_ns - a.start_utc_ns).slice(0, limit);
      return response({ items: clone(items), next_before_sequence: null });
    }
    if (path === "time/status") return response(time());
    if (path === "time" && method === "POST") {
      const milliseconds = Date.parse(body.datetime + "+09:00");
      if (!Number.isFinite(milliseconds)) return error("日時を確認してください。");
      return enqueue(body, () => { clockOffset = milliseconds - Date.now(); }, { time: { ...time(), utc_ns: ns(milliseconds) } });
    }
    if (path === "update/status") return response({ current_version: currentVersion, source_configured: true, candidate });
    if (path === "update/check") return enqueue(body, () => {
      candidate = currentVersion === "0.1.44" ? null : {
        version: "0.1.44", release_name: "サンプル更新",
        published_at: "2026-01-15", asset_size_bytes: 900000, free_bytes: 42e9,
        install_free_bytes: 42e9, privileged_state_free_bytes: 42e9, required_bytes: 3e6,
        enough_space: true, notes: "体験デモ内で更新操作の流れを確認できます。",
      };
    }, {});
    if (path === "update/apply") return enqueue(body, () => { currentVersion = "0.1.44"; candidate = null; });
    if (path.startsWith("operations/")) return operation(path.slice("operations/".length));
    return error("この操作は見つかりません。", 404, "not_found");
  }

  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/v1/")) return realFetch(input, options);
    if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const method = (options.method || (input instanceof Request && input.method) || "GET").toUpperCase();
    let body = {};
    try { body = options.body ? JSON.parse(options.body) : {}; }
    catch { return error("入力を確認してください。"); }
    return handle(url, method, body, options.signal);
  };

  function mediaUrl(value) {
    const url = new URL(value, location.href);
    if (url.origin !== location.origin) return null;
    const match = /^\/api\/v1\/recordings\/([^/]+)\/(content|download)$/.exec(url.pathname);
    if (!match) return null;
    const recording = recordings.find((item) => item.recording_id === decodeURIComponent(match[1]));
    return recording ? new URL("media/sample-camera-" + recording.camera_id + ".mp4", base).href : null;
  }
  for (const [prototype, property] of [[HTMLMediaElement.prototype, "src"], [HTMLAnchorElement.prototype, "href"]]) {
    const descriptor = Object.getOwnPropertyDescriptor(prototype, property);
    Object.defineProperty(prototype, property, {
      ...descriptor, set(value) { descriptor.set.call(this, mediaUrl(value) || value); },
    });
  }
  document.addEventListener("click", (event) => {
    const link = event.target.closest?.("a.recording-download");
    if (link?.href.startsWith(new URL("media/", base).href)) {
      link.download = new URL(link.href).pathname.split("/").pop();
    }
  }, true);
  document.getElementById("demo-reset").addEventListener("click", () => {
    if (confirm("デモを初期状態に戻しますか？")) {
      for (const key of ["pendingUpdateRequest", "pendingUpdateOperation", "pendingUpdateKind"]) {
        sessionStorage.removeItem("raspiRecorder." + key);
      }
      location.reload();
    }
  });
})();
