"use strict";

const appRoot = document.getElementById("app");
const connectionBadge = document.getElementById("connection");
const topNavigation = document.getElementById("top-navigation");
const headerActions = document.getElementById("header-actions");
let csrfToken = "";
let statusTimer = null;
let liveDemandTimer = null;
let previewTimer = null;
let currentView = "live";
let viewGeneration = 0;
let dashboardActive = false;
let statusRequestGeneration = null;
let adminReauthenticationPromise = null;
let authenticationRefreshPromise = null;
const hlsPlayers = new WeakMap();
const previewRequests = new Map();
const previewObjectUrls = new Map();
const previewUpdatedAt = new Map();
const previewStartedAt = new Map();
let liveSettingsRequest = null;
let requestedCameraSettings = null;

function confirmAction(title, message, confirmText, danger = false) {
  if (document.querySelector(".action-confirmation")) return Promise.resolve(false);
  return new Promise((resolve) => {
    const dialog = element("dialog", undefined, "action-confirmation");
    const heading = element("h2", title);
    heading.id = "action-confirmation-title";
    dialog.setAttribute("aria-labelledby", heading.id);
    const description = element("p", message);
    const actions = element("div", undefined, "actions");
    const cancel = element("button", "キャンセル", "secondary");
    cancel.type = "button";
    cancel.autofocus = true;
    const confirm = element("button", confirmText, danger ? "danger-button" : "");
    confirm.type = "button";
    cancel.addEventListener("click", () => dialog.close("cancel"));
    confirm.addEventListener("click", () => dialog.close("confirmed"));
    actions.append(cancel, confirm);
    dialog.append(heading, description, actions);
    dialog.addEventListener("close", () => {
      const accepted = dialog.returnValue === "confirmed";
      dialog.remove();
      resolve(accepted);
    }, { once: true });
    document.body.append(dialog);
    dialog.showModal();
  });
}

function humanError(value, fallback = "操作できませんでした。もう一度お試しください。") {
  const code = typeof value === "string" ? value : value?.code || value?.message;
  const messages = {
    export_space_insufficient: "ZIPを作るための空き容量が足りません。録画を1件ずつダウンロードしてください。",
    export_yielded_to_recording_capacity: "録画用の空き容量を確保するため、ZIP作成を中止しました。録画を1件ずつダウンロードしてください。",
    export_worker_busy: "別のZIPを作成中です。完了してからもう一度お試しください。",
    export_job_limit: "保存待ちのZIPが多いため、今は作成できません。しばらくしてからお試しください。",
    export_not_ready: "ZIPを準備中です。少し待ってからダウンロードしてください。",
    recording_not_found: "この録画は見つかりません。一覧を更新してください。",
    volume_unavailable: "保存先が接続されていません。接続を確認してください。",
    recording_volume_unavailable: "保存先が接続されていません。接続を確認してください。",
    engine_unavailable: "録画サービスに接続できません。Piの電源と接続を確認してください。",
    operation_worker_busy: "別の設定変更を処理中です。完了してからお試しください。",
    revision_conflict: "別の画面で設定が変更されました。画面を読み直してから変更してください。",
    settings_apply_failed_rolled_back: "この設定で録画を開始できなかったため、変更前の設定に戻しました。別の録画品質を選んでください。",
    unsupported_camera_profile: "このカメラは指定した解像度・fpsに対応していません。別の録画品質を選んでください。",
    GitHubReleaseError: "更新情報を取得できませんでした。ネットワーク接続と、配布元が公開されているかを確認してください。",
    update_disk_space_insufficient: "更新に必要な空き容量が足りません。空き容量を確認してください。",
    update_candidate_changed: "更新内容が変更されました。もう一度更新を確認してください。",
  };
  return messages[code] || (typeof code === "string" && /[ぁ-んァ-ン一-龠]/.test(code) ? code : fallback);
}

function formValues(form) {
  return JSON.stringify(Array.from(form.querySelectorAll("input, select"), (control) =>
    [control.id, control.type === "checkbox" ? control.checked : control.value]));
}

function isCurrentView(generation, node) {
  return generation === viewGeneration && node.isConnected;
}

function stopViewResources(container) {
  if (statusTimer) clearInterval(statusTimer);
  if (liveDemandTimer) clearInterval(liveDemandTimer);
  if (previewTimer) clearInterval(previewTimer);
  statusTimer = null;
  liveDemandTimer = null;
  previewTimer = null;
  stopPreviewRequests();
  releaseLivePlayers(container);
}

function hasUnsavedSettings() {
  return Boolean(appRoot.querySelector('form[data-dirty="true"]'));
}

function lockSettingsOperation(form, back) {
  const controls = [...form.querySelectorAll("input, select, button"),
    ...topNavigation.querySelectorAll("button"),
    ...(headerActions?.querySelectorAll("button") || []), back];
  const previous = controls.map((control) => [control, control.disabled]);
  form.setAttribute("aria-busy", "true");
  for (const control of controls) control.disabled = true;
  return () => {
    for (const [control, disabled] of previous) control.disabled = disabled;
    form.removeAttribute("aria-busy");
  };
}

document.defaultView?.addEventListener("beforeunload", (event) => {
  if (hasUnsavedSettings() || appRoot.querySelector('form[aria-busy="true"]')) {
    event.preventDefault();
    event.returnValue = "";
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (currentView === "live") {
      stopPreviewRequests();
      void setPreviewDemand(false);
    }
  } else if (currentView === "live") {
    void refreshLiveDemand();
  }
});

async function api(path, options = {}) {
  const { _adminRetried = false, ...fetchOptions } = options;
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET" && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      headers,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (_error) {
    throw new Error("Piに接続できません。ネットワークを確認して、もう一度お試しください。");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = payload?.error?.message || humanError(payload?.error?.code);
    const error = new Error(message);
    error.code = payload?.error?.code;
    error.status = response.status;
    if (response.status === 428 && error.code === "reauthentication_required" && !_adminRetried) {
      await requestAdminReauthentication();
      return api(path, { ...options, _adminRetried: true });
    }
    if (response.status === 428 && error.code === "administrator_setup_required") {
      await refresh();
    }
    if (response.status === 401 && error.code === "authentication_required"
      && path !== "/api/v1/auth/status") {
      await refresh();
    }
    throw error;
  }
  return payload;
}

function newRequestId() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID().replaceAll("-", "");
  }
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function showMessage(container, message, kind = "") {
  let node = container.querySelector(".message");
  if (!node) {
    node = element("p", "", "message");
    container.append(node);
  }
  node.textContent = message;
  node.className = `message ${kind}`.trim();
}

function formField(form, id, labelText, type, autocomplete, required = true) {
  const wrap = element("div", undefined, "field");
  const label = element("label", labelText);
  label.htmlFor = id;
  const input = document.createElement("input");
  input.id = id;
  input.name = id;
  input.type = type;
  input.autocomplete = autocomplete;
  input.required = required;
  if (type === "password") input.minLength = 8;
  wrap.append(label, input);
  form.append(wrap);
  return input;
}

function renderLogin() {
  dashboardActive = false;
  const card = element("section", undefined, "card");
  card.append(element("h2", "ログイン"));
  const form = document.createElement("form");
  const password = formField(form, "password", "ログイン用パスワード", "password", "current-password");
  const submit = element("button", "ログイン");
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      await api("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify({ password: password.value }),
      });
      await refresh();
    } catch (error) {
      showMessage(card, error.message, "error");
    } finally {
      submit.disabled = false;
      password.value = "";
    }
  });
  card.append(form);
  appRoot.replaceChildren(card);
}

function requestAdminReauthentication() {
  if (!adminReauthenticationPromise) {
    adminReauthenticationPromise = openAdminReauthenticationDialog().finally(() => {
      adminReauthenticationPromise = null;
    });
  }
  return adminReauthenticationPromise;
}

function openAdminReauthenticationDialog() {
  return new Promise((resolve, reject) => {
    const dialog = document.createElement("dialog");
    dialog.className = "auth-dialog";
    const form = document.createElement("form");
    form.method = "dialog";
    form.append(element("h2", "管理者の確認"));
    form.append(element("p", "この操作には管理者用パスワードが必要です。"));
    const password = formField(form, "admin-reauth-password", "管理者用パスワード", "password", "current-password");
    const message = element("p", "", "message");
    const actions = element("div", undefined, "actions");
    const cancel = element("button", "キャンセル", "secondary");
    cancel.type = "button";
    const submit = element("button", "確認して続ける");
    submit.type = "submit";
    actions.append(cancel, submit);
    form.append(message, actions);
    dialog.append(form);
    document.body.append(dialog);
    const finish = (error) => {
      dialog.close();
      dialog.remove();
      if (error) reject(error);
      else resolve();
    };
    cancel.addEventListener("click", () => finish(new Error("操作をキャンセルしました")));
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      finish(new Error("操作をキャンセルしました"));
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await api("/api/v1/auth/admin/reauthenticate", {
          method: "POST",
          body: JSON.stringify({ password: password.value }),
          _adminRetried: true,
        });
        finish();
      } catch (error) {
        message.textContent = error.message;
        message.className = "message error";
        submit.disabled = false;
        password.value = "";
        password.focus();
      }
    });
    dialog.showModal();
    password.focus();
  });
}

function renderDashboard(authenticationStatus) {
  dashboardActive = true;
  const shell = element("div");
  const message = element("div", "録画状態を読み込んでいます。", "message status-message");
  const nav = element("nav", undefined, "actions");
  const viewRoot = element("section");
  const tabs = [
    ["live", "ライブ"],
    ["recordings", "録画"],
    ["settings", "設定"],
  ];
  for (const [name, label] of tabs) {
    const button = element("button", label, name === currentView ? "" : "secondary");
    button.type = "button";
    button.dataset.view = name;
    if (name === currentView) button.setAttribute("aria-current", "page");
    button.addEventListener("click", async () => {
      if (name === currentView) return;
      if (appRoot.querySelector('form[aria-busy="true"]')) return;
      if (hasUnsavedSettings()
        && !await confirmAction("変更を保存せずに移動しますか？", "変更した設定は保存されません。", "保存せずに移動", true)) return;
      if (currentView === "live" && name !== "live") void setPreviewDemand(false);
      currentView = name;
      for (const tab of nav.querySelectorAll("button")) {
        const selected = tab.dataset.view === name;
        tab.className = selected ? "" : "secondary";
        if (selected) tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
      }
      showView();
    });
    nav.append(button);
  }
  topNavigation?.replaceChildren(nav);
  headerActions?.replaceChildren();
  if (authenticationStatus.login_protection_enabled) {
    const logout = element("button", "ログアウト", "secondary");
    logout.addEventListener("click", async () => {
      if (logout.disabled || appRoot.querySelector('form[aria-busy="true"]')) return;
      logout.disabled = true;
      try {
        if (hasUnsavedSettings()
          && !await confirmAction("変更を保存せずにログアウトしますか？", "変更した設定は保存されません。", "保存せずにログアウト", true)) return;
        await setPreviewDemand(false);
        await api("/api/v1/auth/logout", { method: "POST", body: "{}" });
        await refresh();
      } catch (error) {
        showMessage(shell, error.message, "error");
      } finally {
        logout.disabled = false;
      }
    });
    headerActions?.append(logout);
  }
  shell.append(message, viewRoot);
  appRoot.replaceChildren(shell);
  function showView() {
    viewGeneration += 1;
    stopViewResources(viewRoot);
    viewRoot.replaceChildren();
    window.scrollTo({ top: 0, behavior: "auto" });
    if (currentView === "live") {
      liveSettingsRequest = api("/api/v1/settings").catch(() => null);
      const grid = element("section", undefined, "grid camera-grid");
      viewRoot.append(message, grid);
      void refreshLiveDemand();
      liveDemandTimer = setInterval(() => {
        if (!document.hidden) void refreshLiveDemand();
      }, 3000);
      void refreshEngineStatus(message, grid);
      previewTimer = setInterval(refreshVisiblePreviews, 1000);
      statusTimer = setInterval(() => void refreshEngineStatus(message, grid), 2000);
    } else if (currentView === "recordings") {
      viewRoot.append(message);
      void renderRecordings(viewRoot);
    } else {
      viewRoot.append(message);
      void renderSettings(viewRoot, authenticationStatus);
    }
  }
  showView();
}

async function refreshLiveDemand() {
  if (document.hidden || currentView !== "live" || !dashboardActive) return;
  await setPreviewDemand(true);
}

async function setPreviewDemand(active) {
  const missingCameras = new Set(
    [...document.querySelectorAll("[data-camera-id]")]
      .filter((card) => card.dataset.cameraMissing === "true")
      .map((card) => Number(card.dataset.cameraId)),
  );
  const targetIds = active
    ? [1, 2].filter((cameraId) => !missingCameras.has(cameraId))
    : [1, 2];
  const requests = targetIds.map((cameraId) => api(
    `/api/v1/cameras/${cameraId}/preview/demand`,
    { method: "POST", body: JSON.stringify({ active }) },
  ));
  if (!active) {
    stopPreviewRequests();
    const releases = [1, 2].map((cameraId) => api(
      `/api/v1/live/${cameraId}/demand`,
      { method: "POST", body: JSON.stringify({ active: false }) },
    ));
    await Promise.allSettled([...requests, ...releases]);
    return;
  }
  await Promise.allSettled(requests);
}

function stopPreviewRequests() {
  for (const controller of previewRequests.values()) controller.abort();
  previewRequests.clear();
  for (const url of previewObjectUrls.values()) URL.revokeObjectURL(url);
  previewObjectUrls.clear();
  previewUpdatedAt.clear();
  previewStartedAt.clear();
}

function refreshVisiblePreviews() {
  if (document.hidden || currentView !== "live") return;
  for (const card of appRoot.querySelectorAll("[data-camera-id]")) {
    const cameraId = Number(card.dataset.cameraId);
    const image = card.querySelector(".preview-frame img");
    const label = card.querySelector(".preview-label");
    if (image && label && (cameraId === 1 || cameraId === 2)) {
      if (card.dataset.cameraUnconfigured === "true") continue;
      if (card.dataset.cameraMissing === "true") {
        image.hidden = true;
        label.textContent = "カメラが接続されていません";
        label.className = "preview-label stale";
        continue;
      }
      const updatedAt = previewUpdatedAt.get(cameraId);
      if (updatedAt && Date.now() - updatedAt > 5_000) {
        card.querySelector(".preview-frame")?.classList.add("stale-frame");
        label.textContent = `最新画像を取得できません。${Math.floor((Date.now() - updatedAt) / 1000)}秒前の画像を表示しています。`;
        label.className = "preview-label stale";
      }
      void refreshPreviewImage(cameraId, image, label);
    }
  }
}

function releaseLivePlayers(container) {
  for (const video of container.querySelectorAll("video")) {
    const player = hlsPlayers.get(video);
    if (player) player.destroy();
    hlsPlayers.delete(video);
    video.pause();
    video.removeAttribute("src");
    video.load();
    delete video.dataset.liveSource;
  }
}

function videoPresetValue(settings) {
  return `${settings.width}x${settings.height}@${settings.fps}:${settings.quality}`;
}

function videoPresetSettings(value) {
  const match = /^(\d+)x(\d+)@(\d+):(low|standard|high)$/.exec(value);
  return match ? { width: Number(match[1]), height: Number(match[2]), fps: Number(match[3]), quality: match[4] } : null;
}

function videoPresetOptions(port, saved) {
  const profiles = [];
  for (const profile of port?.profiles || []) {
    for (const fps of profile.fps || []) profiles.push({ width: profile.width, height: profile.height, fps });
  }
  const describe = (item) => `${item.width}×${item.height} / ${item.fps}fps`;
  const options = [];
  for (const [quality, label, width, height, fps] of [
    ["low", "低容量", 640, 480, 10],
    ["standard", "標準", 1280, 720, 15],
    ["high", "高画質", 1920, 1080, 30],
  ]) {
    const within = profiles.filter((item) => item.width <= width && item.height <= height && item.fps <= fps);
    within.sort((a, b) => b.width * b.height - a.width * a.height || b.fps - a.fps);
    const fallback = [...profiles].sort((a, b) => a.width * a.height - b.width * b.height || a.fps - b.fps)[0];
    const profile = within[0] || fallback;
    if (profile) options.push([videoPresetValue({ ...profile, quality }), `${label}（${describe(profile)}）`]);
  }
  if (saved && !options.some(([value]) => value === videoPresetValue(saved))) {
    options.push([videoPresetValue(saved), `現在の設定（${describe(saved)}・${{ low: "低容量", standard: "標準", high: "高画質" }[saved.quality]}）`]);
  }
  return options;
}

function storageUsage(volume) {
  const total = volume?.total_bytes;
  const available = volume?.available_bytes;
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(available) || available < 0) return null;
  const free = Math.min(available, total);
  return { total, free, used: total - free, percent: (total - free) / total * 100 };
}

function connectedStorageVolumes(volumes, current) {
  const key = (volume) => `${volume.kind}:${volume.persistent_id || ""}`;
  const available = new Map((volumes || []).filter((volume) => ["microSD", "USB"].includes(volume.kind)).map((volume) => [key(volume), { ...volume, selected: false }]));
  if (current) available.set(key(current), { ...available.get(key(current)), ...current, selected: true });
  return [...available.values()].sort((a, b) => Number(b.selected) - Number(a.selected)
    || a.kind.localeCompare(b.kind) || String(a.persistent_id).localeCompare(String(b.persistent_id)));
}

function storageMeters(volumes, current, selectionLabel = "録画先") {
  const stack = element("div", undefined, "storage-meters");
  const connected = connectedStorageVolumes(volumes, current);
  if (!connected.length) connected.push({});
  for (const volume of connected) stack.append(storageMeter(volume, selectionLabel));
  return stack;
}

function storageMeter(volume, selectionLabel = "録画先") {
  const card = element("div", undefined, "storage-meter");
  const usage = storageUsage(volume);
  const heading = element("div", undefined, "storage-meter-heading");
  heading.append(element("span", volume?.kind === "USB" ? "USBストレージ" : volume?.kind === "microSD" ? "microSD" : "ストレージ"),
    element("span", usage ? `${formatBytes(usage.total)}` : "容量を確認できません"));
  const state = element("span", volume?.selected ? selectionLabel : "接続済み", "storage-state");
  if (volume?.kind) heading.firstChild.append(state);
  const lowSpace = usage && usage.free <= (volume?.kind === "USB" ? 1 : 2) * 1_000_000_000;
  card.classList.toggle("low-space", Boolean(lowSpace));
  card.append(heading);
  if (!usage) return card;
  const track = element("div", undefined, "storage-meter-track");
  track.setAttribute("role", "meter");
  track.setAttribute("aria-label", "ストレージの使用量");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(usage.total));
  track.setAttribute("aria-valuenow", String(usage.used));
  track.setAttribute("aria-valuetext", `使用済み ${formatBytes(usage.used)}、空き ${formatBytes(usage.free)}、合計 ${formatBytes(usage.total)}`);
  const fill = element("span", undefined, "storage-meter-fill");
  fill.style.width = `${usage.percent}%`;
  track.append(fill);
  const labels = element("div", undefined, "storage-meter-labels");
  labels.append(element("span", `使用済み ${formatBytes(usage.used)}`), element("span", `空き ${formatBytes(usage.free)}`));
  card.append(track, labels);
  if (volume?.read_only || lowSpace) {
    card.append(element("p", [volume?.read_only ? "読み取り専用" : "", lowSpace ? "空き容量不足" : ""].filter(Boolean).join(" ・ "), "storage-note"));
  }
  return card;
}

function selectField(form, id, labelText, options, selectedValue) {
  const wrap = element("div", undefined, "field");
  const label = element("label", labelText);
  label.htmlFor = id;
  const select = document.createElement("select");
  select.id = id;
  select.name = id;
  const selected = String(selectedValue ?? "");
  if (selected && !options.some(([value]) => String(value) === selected)) {
    options = [...options, [selected, `${selected}（保存済み・現在未確認）`]];
  }
  for (const [value, text] of options) {
    const option = element("option", text);
    option.value = value;
    option.selected = value === String(selectedValue ?? "");
    select.append(option);
  }
  wrap.append(label, select);
  form.append(wrap);
  return select;
}

function checkboxField(form, id, labelText, checked) {
  const wrap = element("div", undefined, "field checkbox-field");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = id;
  input.name = id;
  input.checked = Boolean(checked);
  if (!id.includes("confirm")) {
    input.setAttribute("role", "switch");
    input.classList.add("settings-switch");
  }
  const label = element("label", labelText);
  label.htmlFor = id;
  wrap.append(input, label);
  form.append(wrap);
  return input;
}

function numericField(form, id, labelText, value, min, max) {
  const input = formField(form, id, labelText, "number", "off");
  input.value = String(value);
  input.min = String(min);
  input.max = String(max);
  input.step = "1";
  return input;
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "不明";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1000 && unit < units.length - 1) {
    size /= 1000;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

const dateFormatters = new Map();

function formatterFor(timezone, options) {
  const key = `${timezone || "UTC"}:${JSON.stringify(options)}`;
  if (!dateFormatters.has(key)) {
    dateFormatters.set(key, new Intl.DateTimeFormat("ja-JP", {
      timeZone: timezone || "UTC", ...options,
    }));
  }
  return dateFormatters.get(key);
}

function zonedParts(timestampMs, timezone) {
  const parts = formatterFor(timezone, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(timestampMs));
  return Object.fromEntries(parts.filter((part) => part.type !== "literal")
    .map((part) => [part.type, Number(part.value)]));
}

function formatTimestamp(value, timezone) {
  if (!Number.isFinite(value)) return "時刻不明";
  const timestampMs = Math.trunc(value / 1_000_000);
  const label = formatterFor(timezone, {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(new Date(timestampMs));
  return timezone ? label : `${label} UTC（OSタイムゾーン未判定）`;
}

function localInputCandidates(value, timezone) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match || !timezone) return [];
  const wanted = match.slice(1, 6).map(Number);
  wanted.push(match[6] === undefined ? 0 : Number(match[6]));
  const naiveMs = Date.UTC(wanted[0], wanted[1] - 1, wanted[2], wanted[3], wanted[4], wanted[5]);
  const naive = new Date(naiveMs);
  if (naive.getUTCFullYear() !== wanted[0] || naive.getUTCMonth() !== wanted[1] - 1
    || naive.getUTCDate() !== wanted[2] || wanted[3] > 23 || wanted[4] > 59
    || wanted[5] > 59) return [];
  try {
    const offsets = new Set();
    for (let hours = -36; hours <= 36; hours += 3) {
      const probe = naiveMs + hours * 3_600_000;
      const actual = zonedParts(probe, timezone);
      const representedMs = Date.UTC(actual.year, actual.month - 1, actual.day,
        actual.hour, actual.minute, actual.second);
      offsets.add(representedMs - probe);
    }
    const candidates = new Set();
    for (const offset of offsets) {
      const candidate = naiveMs - offset;
      const resolved = zonedParts(candidate, timezone);
      if (resolved.year === wanted[0] && resolved.month === wanted[1]
        && resolved.day === wanted[2] && resolved.hour === wanted[3]
        && resolved.minute === wanted[4] && resolved.second === wanted[5]) {
        candidates.add(candidate);
      }
    }
    return [...candidates].sort((left, right) => left - right);
  } catch (_error) {
    return [];
  }
}

function localInputToUtcMs(value, timezone) {
  return localInputCandidates(value, timezone)[0] ?? null;
}

function utcNsToLocalInput(value, timezone) {
  if (!value || !timezone) return "";
  try {
    const timestampMs = Number(BigInt(String(value)) / 1_000_000n);
    if (!Number.isFinite(timestampMs)) return "";
    const parts = zonedParts(timestampMs, timezone);
    return `${String(parts.year).padStart(4, "0")}-${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")}T${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}:${String(parts.second).padStart(2, "0")}`;
  } catch (_error) {
    return "";
  }
}

function utcMsToNsString(value) {
  return String(BigInt(value) * 1_000_000n);
}

function dateKeyAt(timestampMs, timezone) {
  const parts = zonedParts(timestampMs, timezone);
  return `${String(parts.year).padStart(4, "0")}-${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")}`;
}

function shiftDateKey(value, days) {
  const [year, month, day] = value.split("-").map(Number);
  const shifted = new Date(Date.UTC(year, month - 1, day + days));
  return `${String(shifted.getUTCFullYear()).padStart(4, "0")}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}-${String(shifted.getUTCDate()).padStart(2, "0")}`;
}

function localMidnightNs(dateKey, timezone) {
  const ms = localInputToUtcMs(`${dateKey}T00:00`, timezone);
  return ms === null ? null : BigInt(ms) * 1_000_000n;
}

function recordingIntervals(item) {
  const spans = Array.isArray(item.spans) ? item.spans : [];
  const intervals = spans.filter((span) => Number.isFinite(span.start_utc_ns)
    && Number.isFinite(span.end_utc_ns)).map((span) => [span.start_utc_ns, span.end_utc_ns]);
  if (intervals.length) return intervals;
  if (Number.isFinite(item.start_utc_ns)) {
    return [[item.start_utc_ns, Number.isFinite(item.end_utc_ns) ? item.end_utc_ns : item.start_utc_ns]];
  }
  return [];
}

function recordingsOverlapDay(item, dayStartNs, dayEndNs) {
  const start = Number(dayStartNs);
  const end = Number(dayEndNs);
  return recordingIntervals(item).some(([first, last]) => Math.max(first, last) >= start
    && Math.min(first, last) < end);
}

function recordingTimeOption(item, selectedDate, timezone, includeCamera) {
  const format = (value) => {
    const instant = Number(value) / 1_000_000;
    if (!Number.isFinite(instant)) return "時刻不明";
    const sameDay = dateKeyAt(instant, timezone) === selectedDate;
    return formatterFor(timezone, {
      ...(sameDay ? {} : { month: "numeric", day: "numeric" }),
      hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    }).format(new Date(instant));
  };
  const end = item.end_utc_ns ? ` ～ ${format(item.end_utc_ns)}` : "";
  const camera = includeCamera ? ` ・ カメラ ${item.camera_id}` : "";
  const state = item.state === "abnormal" ? "（要確認）" : "";
  return `${format(item.start_utc_ns)}${end}${camera}${state}`;
}

function recordingAtTime(items, candidatesMs) {
  let best = null;
  for (const item of items) {
    for (const targetMs of candidatesMs) {
      const target = targetMs * 1_000_000;
      for (const [first, last] of recordingIntervals(item)) {
        const start = Math.min(first, last);
        const end = Math.max(first, last);
        // The time picker has minute precision. A recording starting at
        // 09:45:00.050 belongs to the requested 09:45 minute too.
        const exact = start < target + 60e9 && end >= target;
        const distance = exact ? 0 : Math.min(Math.abs(start - target), Math.abs(end - target));
        if (!best || distance < best.distance) best = { item, exact, distance };
      }
    }
  }
  return best;
}

async function renderRecordings(container) {
  const viewGen = viewGeneration;
  const panel = element("section", undefined, "recordings-browser");
  panel.append(element("h2", "録画"));
  const choices = element("div", undefined, "recording-choices");
  const date = formField(choices, "recordings-date", "日付", "date", "off", false);
  const time = formField(choices, "recordings-time", "時刻", "time", "off", false);
  time.step = "60";
  const cameraChoice = selectField(choices, "recordings-camera", "カメラ", [], "");
  cameraChoice.closest(".field").hidden = true;
  const availability = element("p", "", "muted recording-availability");
  choices.append(availability);
  date.disabled = true;
  const message = element("p", "録画を読み込んでいます。", "message");
  message.setAttribute("role", "status");
  const retry = element("button", "読み込み直す", "secondary");
  retry.type = "button";
  retry.hidden = true;
  const playback = element("section", undefined, "recording-playback");
  playback.setAttribute("aria-label", "録画再生");
  playback.hidden = true;
  const player = document.createElement("video");
  player.controls = true;
  player.playsInline = true;
  player.muted = true;
  player.preload = "metadata";
  player.setAttribute("aria-label", "選択した録画");
  const playbackMessage = element("p", "", "message error");
  const download = element("a", "ダウンロード", "button-link recording-download");
  const selectedTime = element("h3", "", "recording-selected-time");
  const navigation = element("nav", undefined, "recording-navigation");
  navigation.setAttribute("aria-label", "録画の移動");
  const previous = element("button", "前の録画", "secondary");
  const next = element("button", "次の録画", "secondary");
  previous.type = next.type = "button";
  navigation.append(previous, next);
  playback.append(selectedTime, player, playbackMessage, navigation, download);
  panel.append(choices, message, retry, playback);
  container.replaceChildren(panel);

  let timezone = null;
  let dayGeneration = 0;
  let dayState = "loading";
  let dayError = "";
  let recordings = new Map();
  let selectedId = null;
  const cameraRecordings = () => [...recordings.values()].filter((item) => String(item.camera_id) === cameraChoice.value);
  const current = () => isCurrentView(viewGen, panel);
  const stopPlayback = () => {
    player.pause();
    player.removeAttribute("src");
    player.load();
    playback.hidden = true;
    playbackMessage.textContent = "";
    download.removeAttribute("href");
    selectedId = null;
  };
  const clearDayChoices = () => {
    availability.textContent = "";
    cameraChoice.replaceChildren();
    cameraChoice.closest(".field").hidden = true;
  };
  const showRecording = (item) => {
    stopPlayback();
    selectedId = item.recording_id;
    selectedTime.textContent = recordingTimeOption(item, date.value, timezone, false);
    const items = cameraRecordings();
    const index = items.findIndex((entry) => entry.recording_id === selectedId);
    previous.disabled = index <= 0;
    next.disabled = index < 0 || index === items.length - 1;
    playback.hidden = false;
    download.href = `/api/v1/recordings/${encodeURIComponent(item.recording_id)}/download`;
    player.hidden = item.state !== "finalized";
    if (item.state !== "finalized") {
      playbackMessage.textContent = "正常に保存されていない録画です。ダウンロードして確認してください。";
      return;
    }
    player.src = `/api/v1/recordings/${encodeURIComponent(item.recording_id)}/content`;
    player.load();
    void player.play().catch(() => {});
  };
  const selectTime = () => {
    stopPlayback();
    if (dayState === "loading") {
      message.textContent = "録画を読み込んでいます。";
      return;
    }
    if (dayState === "error") {
      message.textContent = dayError;
      return;
    }
    if (dayState === "empty") {
      message.textContent = "この日の録画はありません。";
      return;
    }
    if (!date.value || !timezone) {
      message.textContent = "日付を選んでください。";
      return;
    }
    message.textContent = "";
    if (!time.value) return;
    const candidates = localInputCandidates(`${date.value}T${time.value}`, timezone);
    const match = recordingAtTime(cameraRecordings(), candidates);
    if (!match) {
      message.textContent = "この時刻の録画はありません。";
      return;
    }
    showRecording(match.item);
    if (!match.exact) message.textContent = "指定した時刻には録画がないため、最も近い録画を表示しています。";
  };
  const updateAvailability = () => {
    const items = cameraRecordings();
    const times = items.flatMap(recordingIntervals).flat();
    const first = times.reduce((value, ns) => Math.min(value, ns), Infinity);
    const last = times.reduce((value, ns) => Math.max(value, ns), -Infinity);
    const format = (ns) => formatterFor(timezone, {hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(ns / 1_000_000));
    availability.textContent = times.length ? `録画の範囲 ${format(first)}〜${format(last)}` : "";
  };
  for (const [button, direction] of [[previous, -1], [next, 1]]) {
    button.addEventListener("click", () => {
      const items = cameraRecordings();
      const index = items.findIndex((item) => item.recording_id === selectedId);
      const item = index < 0 ? null : items[index + direction];
      if (!item) return;
      time.value = utcNsToLocalInput(item.start_utc_ns, timezone).split("T")[1].slice(0, 5);
      message.textContent = "";
      showRecording(item);
    });
  }
  const loadDay = async () => {
    const generation = ++dayGeneration;
    dayState = date.value && timezone ? "loading" : "idle";
    dayError = "";
    stopPlayback();
    recordings.clear();
    retry.hidden = true;
    clearDayChoices();
    message.textContent = date.value ? "録画を読み込んでいます。" : "日付を選んでください。";
    message.className = "message";
    if (!date.value || !timezone) return;
    try {
      const first = localMidnightNs(date.value, timezone);
      const last = localMidnightNs(shiftDateKey(date.value, 1), timezone);
      if (first === null || last === null) throw new Error("日付を選び直してください。");
      const query = new URLSearchParams({
        limit: "500", from_utc_ns: String(first), to_utc_ns: String(last),
      });
      const items = [];
      const cursors = new Set();
      for (let page = 0; page < 100; page += 1) {
        const result = await api(`/api/v1/recordings?${query}`);
        if (!current() || generation !== dayGeneration) return;
        items.push(...(Array.isArray(result.items) ? result.items : []));
        const cursor = result.next_before_sequence;
        if (cursor === null || cursor === undefined) break;
        if (cursors.has(cursor) || page === 99) throw new Error("この日の録画をすべて読み込めませんでした。読み込み直してください。");
        cursors.add(cursor);
        query.set("before_sequence", String(cursor));
      }
      const available = [...new Map(items.filter((item) =>
        ["finalized", "abnormal"].includes(item.state)
        && recordingsOverlapDay(item, first, last)).map((item) => [item.recording_id, item])).values()];
      available.sort((left, right) => Number(left.start_utc_ns) - Number(right.start_utc_ns)
        || left.camera_id - right.camera_id || left.part_index - right.part_index);
      if (!available.length) {
        dayState = "empty";
        message.textContent = "この日の録画はありません。";
        return;
      }
      for (const item of available) {
        recordings.set(item.recording_id, item);
      }
      const cameras = [...new Set(available.map((item) => item.camera_id))].sort();
      cameraChoice.replaceChildren();
      for (const cameraId of cameras) {
        const option = element("option", `カメラ ${cameraId}`);
        option.value = String(cameraId);
        cameraChoice.append(option);
      }
      cameraChoice.closest(".field").hidden = cameras.length <= 1;
      dayState = "ready";
      updateAvailability();
      selectTime();
    } catch (error) {
      if (!current() || generation !== dayGeneration) return;
      dayState = "error";
      dayError = error.message;
      clearDayChoices();
      message.textContent = dayError;
      message.className = "message error";
      retry.hidden = false;
      retry.onclick = () => void loadDay();
    }
  };
  time.addEventListener("change", selectTime);
  cameraChoice.addEventListener("change", () => {
    stopPlayback();
    updateAvailability();
    selectTime();
  });
  player.addEventListener("error", () => {
    if (!playback.hidden) playbackMessage.textContent = "再生できませんでした。ダウンロードして確認してください。";
  });
  date.addEventListener("change", () => void loadDay());
  const initialize = async () => {
    dayState = "loading";
    dayError = "";
    retry.hidden = true;
    message.textContent = "録画を読み込んでいます。";
    message.className = "message";
    try {
      const [status, latest] = await Promise.all([
        api("/api/v1/status"), api("/api/v1/recordings?limit=1"),
      ]);
      if (!current()) return;
      timezone = status.time?.timezone;
      if (!timezone) throw new Error("録画の日時を確認できません。読み込み直してください。");
      const latestTime = latest.items?.[0]?.start_utc_ns || status.time.utc_ns;
      date.value = dateKeyAt(Number(latestTime) / 1_000_000, timezone);
      date.disabled = false;
      await loadDay();
    } catch (error) {
      if (!current()) return;
      dayState = "error";
      dayError = error.message;
      message.textContent = dayError;
      message.className = "message error";
      retry.hidden = false;
      retry.onclick = () => void initialize();
    }
  };
  await initialize();
}

function renderSettings(container, authenticationStatus, initialPage = null) {
  const viewGen = viewGeneration;
  const panel = element("section", undefined, "settings-browser");
  const heading = element("div", undefined, "settings-heading");
  const back = element("button", "‹ 設定", "settings-back");
  back.type = "button";
  back.setAttribute("aria-label", "設定に戻る");
  back.hidden = true;
  const title = element("h2", "設定");
  heading.append(back, title);
  panel.append(heading);
  const message = element("p", "設定を読み込んでいます。", "message");
  message.setAttribute("role", "status");
  panel.append(message);
  container.replaceChildren(panel);
  void (async () => {
    try {
      let timeLoadError = null;
      let updateLoadError = null;
      const [settingsResponse, capabilities, timeResponse, updateResponse, engineStatus] = await Promise.all([
        api("/api/v1/settings"),
        api("/api/v1/capabilities"),
        api("/api/v1/time/status").catch((error) => { timeLoadError = error; return null; }),
        api("/api/v1/update/status").catch((error) => { updateLoadError = error; return null; }),
        api("/api/v1/status").catch(() => null),
      ]);
      if (!isCurrentView(viewGen, panel)) return;
      message.textContent = "";
      message.className = "message";
      const current = settingsResponse.settings;
      const subpages = [];
      const settingsRows = [];
      let activeSettingsPage = null;
      let timeStatus = timeResponse || { ...engineStatus?.time, helper_available: false, ntp_enabled: null };
      let updateStatus = updateResponse || { source_configured: false };
      const form = document.createElement("form");
      form.className = "settings-form";
      form.hidden = true;
      const settingsSection = (id, label) => {
        const section = element("fieldset", undefined, "settings-section");
        section.id = id;
        section.append(element("legend", label));
        return section;
      };
      const recordingGroup = settingsSection("settings-recording", "省電力モード");
      const storageGroup = settingsSection("settings-storage", "保存先");
      const cameraGroup = settingsSection("settings-cameras", "カメラ");
      const powerSaving = checkboxField(
        recordingGroup,
        "settings-power-saving",
        "省電力モード",
        current.power_saving_enabled ?? false,
      );
      const powerNote = element("p", "画質を下げ、音声を止めて電力を抑えます。", "muted");
      recordingGroup.append(powerNote);
      const powerSavingSummary = element("div", undefined, "power-saving-summary");
      powerSavingSummary.setAttribute("aria-live", "polite");
      recordingGroup.append(powerSavingSummary);
      const volumeOptions = [["microSD", "microSD（自動選択）"]];
      for (const volume of capabilities.volumes || []) {
        if (String(volume.kind || "").toLowerCase() === "usb") volumeOptions.push([`usb:${volume.persistent_id}`, `USB ・ ${volume.filesystem} ・ ${volume.persistent_id}`]);
      }
      const selectedVolume = current.volume.persistent_id
        ? `usb:${current.volume.persistent_id}` : "microSD";
      const volume = selectField(storageGroup, "settings-volume", "保存先", volumeOptions, selectedVolume);
      const autoStart = numericField(storageGroup, "settings-delete-start", "自動削除を始める空き容量（GB）", current.auto_delete_start_tenths_gb / 10, 0.1, 100000);
      autoStart.step = "0.1";
      const autoRecover = numericField(storageGroup, "settings-recover", "自動削除後の回復目標（GB）", current.auto_recover_tenths_gb / 10, 0.1, 100000);
      autoRecover.step = "0.1";
      const capacityPreview = element("div");
      const updateCapacityPreview = () => {
        const candidate = (capabilities.volumes || []).find((item) => volume.value === "microSD" ? item.kind === "microSD" : `usb:${item.persistent_id}` === volume.value);
        capacityPreview.replaceChildren(storageMeters(capabilities.volumes, candidate || engineStatus?.volume, "選択中"));
      };
      volume.addEventListener("change", updateCapacityPreview);
      updateCapacityPreview();
      storageGroup.append(capacityPreview);
      const deletionOptions = settingsSection("settings-cleanup", "自動削除");
      storageGroup.append(settingsLink("自動削除", "cleanup", () => `空き ${autoStart.value} GB以下`));
      subpages.push({ id: "cleanup", label: "自動削除", node: deletionOptions, parent: "storage", editable: true });
      deletionOptions.append(
        element("p", "空き容量が少なくなると古い録画から削除します。", "muted"),
        autoStart.closest(".field"), autoRecover.closest(".field"),
        element("p", "空き容量がmicroSDで2GB、USBで1GBを下回ると録画を停止します。", "muted"),
      );

      const cameraControls = [];
      for (const cameraId of [1, 2]) {
        const config = current.cameras.find((item) => item.camera_id === cameraId);
        const card = element("fieldset", undefined, "camera-settings");
        card.append(element("legend", `カメラ ${cameraId}`));
        card.setAttribute("aria-label", `カメラ ${cameraId}`);
        cameraGroup.append(card);
        const portOptions = [["", "使用しない"]];
        for (const [index, port] of (capabilities.cameras || []).entries()) {
          portOptions.push([port.port_key, `${port.product || "USBカメラ"}（USB ${index + 1}）`]);
        }
        const port = selectField(card, `camera-${cameraId}-port`, "使用するカメラ", portOptions, config.usb_port_key);
        const activePort = (capabilities.cameras || []).find((item) => item.port_key === config.usb_port_key);
        const selectedProfile = videoPresetValue(config);
        const profile = selectField(card, `camera-${cameraId}-profile`, "録画品質", videoPresetOptions(activePort, config), selectedProfile);
        const audioInterface = selectField(
          card,
          `camera-${cameraId}-audio-interface`,
          "マイク（選択USBポート）",
          [["", "未選択・未確認"]],
          config.audio_interface_key,
        );
        const populateAudioInterfaces = (selectedPort, selectedKey) => {
          const choices = [["", "未選択・未確認"]];
          for (const device of selectedPort?.audio_interfaces || []) {
            choices.push([device.interface_key, device.label]);
          }
          if (selectedKey && !choices.some(([key]) => key === selectedKey)) {
            choices.push([selectedKey, "保存済みデバイス・現在未確認"]);
          }
          audioInterface.replaceChildren();
          for (const [key, label] of choices) {
            const option = element("option", label);
            option.value = key;
            option.selected = key === (selectedKey || "");
            audioInterface.append(option);
          }
        };
        populateAudioInterfaces(activePort, config.audio_interface_key);
        const audio = checkboxField(card, `camera-${cameraId}-audio`, "録画・ライブで音声を有効にする", config.audio_enabled);
        const audioNote = element("p", "マイクが使えない場合も、映像の録画は続けます。", "muted");
        const overlay = checkboxField(card, `camera-${cameraId}-overlay`, "映像に日時を表示", config.overlay_enabled);
        const corner = selectField(card, `camera-${cameraId}-corner`, "日時の位置", [["top_left", "左上"], ["top_right", "右上"], ["bottom_left", "左下"], ["bottom_right", "右下"]], config.overlay_corner);
        const size = selectField(card, `camera-${cameraId}-size`, "日時の大きさ", [["small", "小"], ["medium", "中"], ["large", "大"]], config.overlay_size);
        const cameraFields = element("div");
        cameraFields.append(...[profile, overlay, corner, size, audio, audioInterface].map((control) => control.closest(".field")), audioNote);
        card.append(cameraFields);
        const unused = element("p", "このカメラは使用していません。使う場合は上でカメラを選んでください。", "muted");
        card.append(unused);
        const updateCameraFields = () => {
          cameraFields.hidden = !port.value;
          unused.hidden = Boolean(port.value);
          corner.closest(".field").hidden = !overlay.checked;
          size.closest(".field").hidden = !overlay.checked;
        };
        updateCameraFields();
        overlay.addEventListener("change", updateCameraFields);
        cameraControls.push({ config, card, port, profile, audio, audioInterface, overlay, corner, size });
        port.addEventListener("change", () => {
          const nextPort = (capabilities.cameras || []).find((item) => item.port_key === port.value);
          profile.replaceChildren();
          const choices = videoPresetOptions(nextPort, null);
          for (const [value, label] of choices.length ? choices : [["", "対応する録画品質がありません"]]) {
            const option = element("option", label); option.value = value; profile.append(option);
          }
          const standard = choices.find(([value]) => value.endsWith(":standard"));
          if (standard) profile.value = standard[0];
          populateAudioInterfaces(nextPort, null);
          updateCameraFields();
          updatePowerSavingSummary();
        });
        profile.addEventListener("change", updatePowerSavingSummary);
        audio.addEventListener("change", updatePowerSavingSummary);
      }
      function updatePowerSavingSummary() {
        const enabled = powerSaving.checked;
        powerSavingSummary.hidden = !enabled;
        powerNote.hidden = !enabled;
        const items = cameraControls.filter((controls) => controls.port.value).map((controls) => {
          const base = videoPresetSettings(controls.profile.value) || controls.config;
          let effective = { ...base };
          let reduced = false;
          let profileUnavailable = false;
          if (enabled) {
            const port = (capabilities.cameras || []).find((item) => item.port_key === controls.port.value);
            const candidates = [];
            for (const profile of port?.profiles || []) {
              for (const fps of profile.fps || []) {
                if (profile.width <= base.width && profile.height <= base.height && fps <= base.fps
                  && (profile.width !== base.width || profile.height !== base.height || fps !== base.fps)) {
                  candidates.push({ width: profile.width, height: profile.height, fps });
                }
              }
            }
            candidates.sort((left, right) =>
              (left.width * left.height * left.fps) - (right.width * right.height * right.fps)
              || (left.width * left.height) - (right.width * right.height)
              || left.fps - right.fps || left.width - right.width || left.height - right.height);
            if (candidates.length) {
              effective = candidates[0];
              reduced = true;
            } else {
              profileUnavailable = !port;
            }
          }
          const effectiveProfileLabel = `${effective.width}×${effective.height} ${effective.fps}fps`;
          const explanation = !enabled
            ? `通常設定を使用（音声${controls.audio.checked ? "ON" : "OFF"}）`
            : `${reduced ? "省電力の画質で録画" : profileUnavailable ? "カメラを確認できないため元の画質を維持" : "変更できる画質がないため元の画質を維持"}、音声OFF`;
          return element("li", `カメラ${controls.config.camera_id}: ${effectiveProfileLabel} ・ ${explanation}`);
        });
        for (const controls of cameraControls) {
          const audioLabel = document.querySelector(`label[for="camera-${controls.config.camera_id}-audio"]`);
          if (audioLabel) {
            audioLabel.textContent = enabled
              ? "通常時に録画・ライブで音声を有効にする"
              : "録画・ライブで音声を有効にする";
          }
        }
        const label = document.querySelector('label[for="settings-power-saving"]');
        if (label) label.textContent = "省電力モード";
        powerSavingSummary.replaceChildren(
          element("p", enabled ? "省電力モード中の適用内容" : "現在の通常設定", "muted"),
        );
        const list = document.createElement("ul");
        list.append(...items);
        powerSavingSummary.append(list);
      }
      powerSaving.addEventListener("change", updatePowerSavingSummary);
      updatePowerSavingSummary();
      const save = element("button", "設定を保存");
      save.type = "submit";
      const saveBar = element("div", undefined, "save-bar");
      const saveFeedback = element("span", "", "message save-feedback");
      saveFeedback.setAttribute("role", "status");
      saveBar.append(save, saveFeedback);
      saveBar.hidden = true;
      form.append(recordingGroup, storageGroup, cameraGroup, deletionOptions);
      form.append(saveBar);
      let savedFormValues = formValues(form);
      save.disabled = true;
      const markSettingsDirty = () => {
        const dirty = formValues(form) !== savedFormValues;
        if (dirty) form.dataset.dirty = "true";
        else delete form.dataset.dirty;
        saveBar.classList.toggle("dirty", dirty);
        saveBar.hidden = !dirty;
        save.disabled = !dirty;
        saveFeedback.textContent = "";
      };
      const markSettingsClean = () => {
        savedFormValues = formValues(form);
        delete form.dataset.dirty;
        saveBar.classList.remove("dirty");
        saveBar.hidden = true;
        save.disabled = true;
      };
      form.addEventListener("input", markSettingsDirty);
      form.addEventListener("change", markSettingsDirty);
      panel.append(form);
      const securityPanel = element("section", undefined, "card settings-card");
      securityPanel.id = "settings-security";
      securityPanel.className = "settings-list";
      for (const protection of [
        {
          kind: "login",
          label: "ログイン保護",
          enabled: authenticationStatus.login_protection_enabled,
          configured: authenticationStatus.login_password_configured,
          explanation: "ONにすると、閲覧・ライブ表示・録画操作・再生にログインが必要です。",
        },
        {
          kind: "admin",
          label: "管理者保護",
          enabled: authenticationStatus.admin_protection_enabled,
          configured: authenticationStatus.admin_password_configured,
          explanation: "ONにすると、設定保存・削除・時刻変更・更新適用・認証変更に管理者確認が必要です。",
        },
      ]) {
        const passwordForm = document.createElement("form");
        passwordForm.className = "credential-form";
        passwordForm.append(element("h4", protection.label));
        const enabled = checkboxField(
          passwordForm,
          `${protection.kind}-protection-enabled`,
          `${protection.label}を有効にする`,
          protection.enabled,
        );
        passwordForm.append(element("p", protection.explanation));
        passwordForm.append(element(
          "p",
          `現在: ${protection.enabled ? "ON" : "OFF"} ・ パスワード: ${protection.configured ? "登録済み" : "未登録"}`,
          "muted",
        ));
        const nextPassword = formField(
          passwordForm,
          `${protection.kind}-password`,
          `新しい${protection.kind === "login" ? "ログイン用" : "管理者用"}パスワード（登録・変更時のみ）`,
          "password",
          "new-password",
          false,
        );
        const confirmation = formField(
          passwordForm,
          `${protection.kind}-password-confirmation`,
          "確認用パスワード",
          "password",
          "new-password",
          false,
        );
        const save = element("button", `${protection.label}を保存`);
        save.type = "submit";
        passwordForm.append(save);
        passwordForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          if (passwordForm.getAttribute("aria-busy") === "true") return;
          const unlock = lockSettingsOperation(passwordForm, back);
          try {
            const body = {
              login_enabled: protection.kind === "login"
                ? enabled.checked : authenticationStatus.login_protection_enabled,
              admin_enabled: protection.kind === "admin"
                ? enabled.checked : authenticationStatus.admin_protection_enabled,
              expected_revision: authenticationStatus.revision,
            };
            if (nextPassword.value || confirmation.value) {
              body[`${protection.kind}_password`] = nextPassword.value;
              body[`${protection.kind}_confirmation`] = confirmation.value;
            }
            await api("/api/v1/auth/protections", {
              method: "POST",
              body: JSON.stringify(body),
            });
            nextPassword.value = "";
            confirmation.value = "";
            await refresh();
          } catch (error) {
            showMessage(passwordForm, error.message, "error");
          } finally {
            unlock();
          }
        });
        passwordForm.classList.add("settings-card");
        securityPanel.append(settingsLink(protection.label, `password-${protection.kind}`, () => protection.enabled ? "オン" : "オフ"));
        subpages.push({ id: `password-${protection.kind}`, label: protection.label, node: passwordForm, parent: "security" });
        panel.append(passwordForm);
        const trackProtectionChanges = () => {
          const dirty = enabled.checked !== Boolean(protection.enabled)
            || Boolean(nextPassword.value || confirmation.value);
          if (dirty) passwordForm.dataset.dirty = "true";
          else delete passwordForm.dataset.dirty;
        };
        passwordForm.addEventListener("input", trackProtectionChanges);
        passwordForm.addEventListener("change", trackProtectionChanges);
      }
      panel.append(securityPanel);
      const clockPanel = element("section", undefined, "clock-settings");
      clockPanel.id = "settings-time";
      clockPanel.classList.add("settings-card");
      const clockMessage = element("p", "", "message");
      const currentTimeLabel = element("p", "");
      clockPanel.append(currentTimeLabel);
      const timeForm = document.createElement("form");
      const timeInput = formField(timeForm, "manual-time-datetime", "新しい日時", "datetime-local", "off");
      timeInput.step = "1";
      timeInput.min = "1970-01-01T00:00:00";
      timeInput.value = utcNsToLocalInput(timeStatus.utc_ns, timeStatus.timezone);
      let manualTimeEdited = false;
      let timePageLoading = false;
      let timePageFailed = false;
      const trackClockChanges = (event) => {
        if (event.target === timeInput) manualTimeEdited = true;
        timeForm.dataset.dirty = "true";
      };
      timeForm.addEventListener("input", trackClockChanges);
      timeForm.addEventListener("change", trackClockChanges);
      const timezoneLabel = element("p", timeStatus.timezone
        ? `タイムゾーン: ${timeStatus.timezone}`
        : "OSタイムゾーンを特定できません。");
      timeForm.append(timezoneLabel);
      const fold = selectField(timeForm, "manual-time-fold", "夏時間切替で重複する時刻", [
        ["0", "1回目（切替前）"], ["1", "2回目（切替後）"],
      ], "0");
      const foldField = fold.closest(".field");
      if (foldField) foldField.hidden = true;
      const ntpPolicy = selectField(timeForm, "manual-time-ntp", "自動時刻合わせ", [
        ["keep", "現在の設定を変えない"],
        ["pause_and_resume", "手動設定後も自動で合わせる"],
      ], timeStatus.ntp_enabled ? "pause_and_resume" : "keep");
      const ntpField = ntpPolicy.closest(".field");
      if (timeStatus.ntp_enabled === false) {
        ntpPolicy.value = "keep";
        ntpPolicy.disabled = true;
        if (ntpField) ntpField.hidden = true;
      } else if (timeStatus.ntp_enabled === true) {
        ntpPolicy.required = true;
        ntpPolicy.querySelector('option[value="keep"]').disabled = true;
      }
      const confirmClock = checkboxField(timeForm, "manual-time-confirm", "録画を一度区切り、時刻変更後に再開する", false);
      const setTime = element("button", "日時を適用");
      setTime.type = "submit";
      timeForm.append(setTime);
      const manualClock = element("section", undefined, "settings-card");
      manualClock.append(clockMessage, timeForm);
      const retryTime = element("button", "読み込み直す", "secondary");
      retryTime.type = "button";
      retryTime.hidden = true;
      retryTime.addEventListener("click", () => void refreshDisplayedTime(activeSettingsPage));
      manualClock.append(retryTime);
      clockPanel.append(settingsLink("日時を手動で設定", "manual-time", () => ""));
      subpages.push({ id: "manual-time", label: "日時を手動で設定", node: manualClock, parent: "time" });
      panel.append(clockPanel, manualClock);

      const updatePanel = element("section", undefined, "card");
      updatePanel.id = "settings-update";
      const releaseNotesPage = element("section", undefined, "settings-card");
      subpages.push({ id: "release-notes", label: "更新内容", node: releaseNotesPage, parent: "update" });
      panel.append(releaseNotesPage);
      updatePanel.classList.add("settings-card");
      const updateVersionLabel = element(
        "p",
        `現在のバージョン: ${updateStatus.current_version || "不明"}`,
      );
      updatePanel.append(updateVersionLabel);
      const updateMessage = element("p", "", "message");
      updateMessage.setAttribute("role", "status");
      const updateErrorDetails = element("section", undefined, "settings-card");
      const updateErrorLink = settingsLink("エラーの詳細", "update-error", () => "");
      updateErrorLink.hidden = true;
      subpages.push({ id: "update-error", label: "エラーの詳細", node: updateErrorDetails, parent: "update" });
      panel.append(updateErrorDetails);
      const updateErrorText = element("p");
      updateErrorDetails.append(updateErrorText);
      const showUpdateError = (error, operationId = null) => {
        updateMessage.textContent = humanError(error.message || error, "更新できませんでした。接続を確認して、もう一度お試しください。");
        updateMessage.className = "message error";
        updateErrorText.textContent = `${error.message || error}${operationId ? `（操作ID: ${operationId}）` : ""}`;
        updateErrorLink.hidden = false;
      };
      const updateCandidate = element("div");
      const updateActions = element("div", undefined, "actions");
      const checkUpdate = element("button", "更新を確認", "secondary");
      checkUpdate.type = "button";
      checkUpdate.disabled = !updateStatus.source_configured;
      const reviewUpdate = element("button", "更新の結果を見る", "secondary");
      reviewUpdate.type = "button";
      reviewUpdate.hidden = true;
      updateActions.append(checkUpdate, reviewUpdate);
      updatePanel.append(updateMessage, updateErrorLink, updateActions, updateCandidate);
      panel.append(updatePanel);

      let pendingUpdateOperationId = null;
      let pendingUpdateRequest = null;
      let pendingUpdateKind = null;
      let updateBusy = false;
      try {
        const savedRequest = sessionStorage.getItem("raspiRecorder.pendingUpdateRequest");
        if (savedRequest) {
          const value = JSON.parse(savedRequest);
          const validCheck = value?.kind === "check"
            && /^[0-9a-f]{32}$/.test(value.request_id || "");
          const validApply = value?.kind === "apply"
            && /^[0-9a-f]{32}$/.test(value.request_id || "")
            && /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/.test(value.version || "");
          if (validCheck || validApply) pendingUpdateRequest = value;
        }
        if (!pendingUpdateRequest) {
          const savedOperationId = sessionStorage.getItem("raspiRecorder.pendingUpdateOperation");
          if (/^[0-9a-f]{32}$/.test(savedOperationId || "")) {
            pendingUpdateOperationId = savedOperationId;
          }
        }
        if (pendingUpdateOperationId || pendingUpdateRequest) {
          const savedKind = sessionStorage.getItem("raspiRecorder.pendingUpdateKind");
          pendingUpdateKind = pendingUpdateRequest?.kind
            || (["check", "apply"].includes(savedKind) ? savedKind : null);
          reviewUpdate.hidden = false;
        } else {
          sessionStorage.removeItem("raspiRecorder.pendingUpdateOperation");
          sessionStorage.removeItem("raspiRecorder.pendingUpdateRequest");
          sessionStorage.removeItem("raspiRecorder.pendingUpdateKind");
        }
      } catch (_error) {
        // The current page can still track the operation if session storage is unavailable.
      }

      const hasPendingUpdate = () => Boolean(pendingUpdateOperationId || pendingUpdateRequest);
      const syncUpdateControls = () => {
        checkUpdate.disabled = updateBusy || !updateStatus.source_configured || hasPendingUpdate();
        reviewUpdate.disabled = updateBusy;
        for (const control of updateCandidate.querySelectorAll("input, button")) {
          control.disabled = updateBusy || hasPendingUpdate();
        }
      };

      const savePendingUpdateRequest = (request) => {
        pendingUpdateRequest = request;
        pendingUpdateKind = request.kind;
        pendingUpdateOperationId = null;
        reviewUpdate.hidden = false;
        try {
          sessionStorage.setItem("raspiRecorder.pendingUpdateRequest", JSON.stringify(request));
          sessionStorage.setItem("raspiRecorder.pendingUpdateKind", request.kind);
          sessionStorage.removeItem("raspiRecorder.pendingUpdateOperation");
        } catch (_error) {
          // Keep the request ID in this page's memory.
        }
      };

      const savePendingUpdateOperation = (operationId) => {
        pendingUpdateOperationId = operationId;
        pendingUpdateRequest = null;
        reviewUpdate.hidden = false;
        try {
          sessionStorage.setItem("raspiRecorder.pendingUpdateOperation", operationId);
          sessionStorage.removeItem("raspiRecorder.pendingUpdateRequest");
        } catch (_error) {
          // Keep the operation ID in this page's memory.
        }
      };

      const clearPendingUpdateOperation = () => {
        pendingUpdateOperationId = null;
        pendingUpdateRequest = null;
        pendingUpdateKind = null;
        reviewUpdate.hidden = true;
        try {
          sessionStorage.removeItem("raspiRecorder.pendingUpdateOperation");
          sessionStorage.removeItem("raspiRecorder.pendingUpdateRequest");
          sessionStorage.removeItem("raspiRecorder.pendingUpdateKind");
        } catch (_error) {
          // No persisted operation ID remains when storage is available.
        }
      };

      const retryPendingUpdateRequest = async () => {
        if (!pendingUpdateRequest) throw new Error("確認する更新要求がありません");
        const request = pendingUpdateRequest;
        const endpoint = request.kind === "check"
          ? "/api/v1/update/check"
          : "/api/v1/update/apply";
        const body = request.kind === "check"
          ? { request_id: request.request_id }
          : { request_id: request.request_id, version: request.version, confirmed: true };
        const accepted = await api(endpoint, {
          method: "POST",
          body: JSON.stringify(body),
        });
        if (!/^[0-9a-f]{32}$/.test(accepted.operation_id || "")) {
          throw new Error("更新操作IDを確認できません。同じ要求IDで再確認してください。");
        }
        savePendingUpdateOperation(accepted.operation_id);
        return accepted.operation_id;
      };

      const waitForUpdateOperation = async (operationId) => {
        let lastError = null;
        for (let attempt = 0; attempt < 2100; attempt += 1) {
          if (viewGen !== viewGeneration) {
            throw new Error("画面が切り替わったため、更新状態の確認を終了しました。");
          }
          try {
            const operation = await api(`/api/v1/operations/${encodeURIComponent(operationId)}`);
            if (["succeeded", "failed"].includes(operation.state)) return operation;
            updateMessage.textContent = pendingUpdateKind === "check"
              ? "更新があるか確認しています。録画は続いています。"
              : pendingUpdateKind !== "apply"
              ? "前回の処理結果を確認しています。"
              : operation.stage === "validating"
              ? "更新パッケージを取得・検証しています。録画は継続中です。"
              : "更新を適用中です。録画とWeb UIが一時停止する場合があります。";
            lastError = null;
          } catch (error) {
            lastError = error;
            updateMessage.textContent = "更新処理との接続を再試行しています。Piの電源を切らずにお待ちください。";
          }
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        const state = lastError
          ? "更新結果をまだ確認できません"
          : "更新処理は継続中です";
        throw new Error(`${state} (操作ID: ${operationId})。Piの電源を切らず、設定画面の「更新の結果を見る」から再確認してください。`);
      };

      const showUpdateCandidate = (status) => {
        updateStatus = status;
        updateVersionLabel.textContent = `現在のバージョン: ${status.current_version || "不明"}`;
        updateMessage.className = "message";
        updateErrorLink.hidden = true;
        updateCandidate.replaceChildren();
        const candidate = status.candidate;
        if (!status.source_configured) {
          updateMessage.textContent = "更新元の設定が不正です。管理者が配布元の設定を確認してください。";
          return;
        }
        if (!candidate) {
          updateMessage.textContent = status.message || "利用可能な安定版はありません。";
          return;
        }
        updateMessage.textContent = status.message || "更新候補があります。";
        const details = document.createElement("dl");
        details.className = "status-grid";
        for (const [label, value] of [
          ["候補", `${candidate.version} ・ ${candidate.release_name || "安定版"}`],
          ["公開日時", candidate.published_at || "不明"],
          ["配布サイズ", formatBytes(candidate.asset_size_bytes)],
          ["作業用の空き容量", formatBytes(candidate.free_bytes)],
          ["インストール先の空き容量", formatBytes(candidate.install_free_bytes)],
          ["復旧用データの空き容量", formatBytes(candidate.privileged_state_free_bytes)],
          ["各領域の必要量", formatBytes(candidate.required_bytes)],
        ]) {
          details.append(element("dt", label), element("dd", value));
        }
        updateCandidate.append(details);
        if (candidate.notes) {
          const text = element("p", candidate.notes);
          text.style.whiteSpace = "pre-wrap";
          releaseNotesPage.replaceChildren(text);
          updateCandidate.append(settingsLink("更新内容", "release-notes", () => ""));
        }
        if (candidate.enough_space !== true) return;

        const applyForm = document.createElement("form");
        applyForm.append(element("p", "更新中は録画とこの画面が一時停止します。完了するまでPiの電源を切らないでください。"));
        const confirmUpdate = checkboxField(
          applyForm,
          "update-confirm",
          "録画が一時停止し、適用に失敗した場合は旧版へ戻ることを確認しました",
          false,
        );
        const applyUpdate = element("button", "この安定版を適用");
        applyUpdate.type = "submit";
        applyForm.append(applyUpdate);
        applyForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          if (updateBusy || hasPendingUpdate()) return;
          if (!confirmUpdate.checked) {
            updateMessage.textContent = "適用前の確認が必要です。";
            updateMessage.className = "message error";
            return;
          }
          if (!applyForm.reportValidity()) return;
          updateBusy = true;
          syncUpdateControls();
          let operationId = null;
          try {
            savePendingUpdateRequest({
              kind: "apply",
              request_id: newRequestId(),
              version: candidate.version,
            });
            operationId = await retryPendingUpdateRequest();
            const operation = await waitForUpdateOperation(operationId);
            clearPendingUpdateOperation();
            if (operation.state !== "succeeded") {
              const state = operation.recovery?.state;
              const recoveryMessage = state === "previous_version_restored"
                ? "旧版への復帰を確認しました。"
                : state === "manual_recovery_required"
                  ? "自動復帰を確認できません。ローカル復旧が必要です。"
                  : "";
              throw new Error(`${operation.error || "更新に失敗しました"} ${recoveryMessage}`.trim());
            }
            const installedVersion = operation.result?.version || candidate.version;
            updateVersionLabel.textContent = `現在のバージョン: ${installedVersion}`;
            updateCandidate.replaceChildren();
            updateMessage.textContent = `更新が完了しました。現在の版: ${installedVersion}`;
            updateMessage.className = "message ok";
          } catch (error) {
            if (
              pendingUpdateRequest
              && Number.isInteger(error.status)
              && error.status >= 400
              && error.status < 500
            ) clearPendingUpdateOperation();
            if (operationId || hasPendingUpdate()) updateCandidate.replaceChildren();
            showUpdateError(error, operationId);
          } finally {
            updateBusy = false;
            syncUpdateControls();
          }
        });
        updateCandidate.append(applyForm);
      };

      showUpdateCandidate(updateStatus);
      if (hasPendingUpdate()) {
        updateCandidate.replaceChildren();
        checkUpdate.disabled = true;
        updateMessage.textContent = pendingUpdateOperationId
          ? pendingUpdateKind === "check"
            ? "前回の更新確認の結果を確認できます。"
            : "前回の更新結果を確認してください。"
          : "前回の更新結果を確認できていません。「更新の結果を見る」で確認してください。";
      }
      reviewUpdate.addEventListener("click", async () => {
        if (updateBusy || !hasPendingUpdate()) return;
        let terminal = false;
        updateBusy = true;
        syncUpdateControls();
        try {
          const operationId = pendingUpdateRequest
            ? await retryPendingUpdateRequest()
            : pendingUpdateOperationId;
          const operation = await waitForUpdateOperation(operationId);
          terminal = true;
          clearPendingUpdateOperation();
          if (operation.state !== "succeeded") {
            const recovery = operation.recovery?.state;
            const recoveryMessage = recovery === "previous_version_restored"
              ? "旧版への復帰を確認しました。"
              : recovery === "manual_recovery_required"
                ? "自動復帰を確認できません。ローカル復旧が必要です。"
                : "";
            throw new Error(`${operation.error || "更新に失敗しました"} ${recoveryMessage}`.trim());
          }
          if (operation.result?.scope === "update") {
            const installedVersion = operation.result.version || "不明";
            updateVersionLabel.textContent = `現在のバージョン: ${installedVersion}`;
            updateCandidate.replaceChildren();
            updateMessage.textContent = `更新が完了しました。現在の版: ${installedVersion}`;
            updateMessage.className = "message ok";
          } else {
            showUpdateCandidate(await api("/api/v1/update/status"));
          }
        } catch (error) {
          if (terminal) clearPendingUpdateOperation();
          else if (
            pendingUpdateRequest
            && Number.isInteger(error.status)
            && error.status >= 400
            && error.status < 500
          ) clearPendingUpdateOperation();
          showUpdateError(error);
        } finally {
          updateBusy = false;
          syncUpdateControls();
        }
      });
      checkUpdate.addEventListener("click", async () => {
        if (updateBusy || hasPendingUpdate() || !updateStatus.source_configured) return;
        updateBusy = true;
        syncUpdateControls();
        let operationId = null;
        const request = { kind: "check", request_id: newRequestId() };
        updateCandidate.replaceChildren();
        updateErrorLink.hidden = true;
        updateMessage.textContent = "更新があるか確認しています。";
        updateMessage.className = "message";
        try {
          savePendingUpdateRequest(request);
          operationId = await retryPendingUpdateRequest();
          const operation = await waitForUpdateOperation(operationId);
          clearPendingUpdateOperation();
          if (operation.state !== "succeeded") {
            clearPendingUpdateOperation();
            throw new Error(operation.error || "更新確認に失敗しました");
          }
          showUpdateCandidate(await api("/api/v1/update/status"));
        } catch (error) {
          if (
            pendingUpdateRequest
            && Number.isInteger(error.status)
            && error.status >= 400
            && error.status < 500
          ) clearPendingUpdateOperation();
          showUpdateError(error, operationId);
        } finally {
          updateBusy = false;
          syncUpdateControls();
        }
      });

      const updateTimeStatusLabel = () => {
        const syncLabel = timeStatus.synchronized === true
          ? "同期確認済み"
          : timeStatus.synchronized === false
            ? "同期未確認"
            : "同期状態不明";
        currentTimeLabel.textContent = `現在: ${formatTimestamp(timeStatus.utc_ns, timeStatus.timezone)} ・ ${syncLabel}`;
        const helperMatchesTimezone = timeStatus.timezone
          && timeStatus.helper_timezone === timeStatus.timezone;
        const available = timeStatus.helper_available && helperMatchesTimezone;
        let statusMessage;
        if (!timeStatus.helper_available) {
          statusMessage = "日時を変更する機能を利用できません。管理者に確認してください。";
        } else if (!helperMatchesTimezone) {
          statusMessage = "タイムゾーンの設定に不一致があります。管理者に確認してください。";
        } else if (timeStatus.ntp_enabled === true) {
          statusMessage = "ネットワークから自動で時刻を合わせます。通常は手動設定の必要はありません。";
        } else if (timeStatus.ntp_enabled === false) {
          statusMessage = "自動時刻合わせはOFFです。録画に表示する日時を確認してください。";
        } else {
          statusMessage = "自動時刻合わせの状態を確認できません。今は日時を変更できません。";
        }
        if (clockMessage.className === "message") clockMessage.textContent = timePageLoading
          ? "現在の日時を読み込んでいます。" : statusMessage;
        const busy = timeForm.getAttribute("aria-busy") === "true";
        timeInput.disabled = busy || !available;
        ntpPolicy.disabled = busy || !available || timeStatus.ntp_enabled === false
          || timeStatus.ntp_enabled === null;
        setTime.disabled = busy || timePageLoading || timePageFailed
          || !available || timeStatus.ntp_enabled === null;
      };
      const updateFoldChoices = () => {
        const candidates = localInputCandidates(timeInput.value, timeStatus.timezone);
        if (foldField) foldField.hidden = candidates.length !== 2;
        return candidates;
      };
      timeInput.addEventListener("input", updateFoldChoices);
      updateTimeStatusLabel();
      updateFoldChoices();

      timeForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (timeForm.getAttribute("aria-busy") === "true") return;
        const unlock = lockSettingsOperation(timeForm, back);
        clockMessage.className = "message";
        updateTimeStatusLabel();
        try {
          const candidates = updateFoldChoices();
          if (candidates.length === 0) {
            throw new Error("入力日時が存在しません。夏時間の切替で欠落する日時でないか確認してください。");
          }
          if (timeStatus.ntp_enabled === true && !ntpPolicy.value) {
            throw new Error("NTPの扱いを選択してください。");
          }
          if (!confirmClock.checked) {
            throw new Error("録画を区切って再開することを確認してください。");
          }
          const ntpLabel = timeStatus.ntp_enabled === true
            ? (ntpPolicy.value === "pause_and_resume" ? "NTPを一時停止して設定後に再有効化" : "NTPを有効のまま設定")
            : "NTP設定を変更せず設定";
          const foldLabel = candidates.length === 2
            ? ` (${Number(fold.value) === 0 ? "1回目" : "2回目"})` : "";
          if (!await confirmAction("日時を変更しますか？", `${timeInput.value}${foldLabel}（${timeStatus.timezone}）に変更します。録画を一度区切り、変更後に再開します。処理中は録画に空白ができます。${ntpLabel}。`, "日時を変更")) return;
          const accepted = await api("/api/v1/time", {
            method: "POST",
            body: JSON.stringify({
              expected_revision: current.revision,
              request_id: newRequestId(),
              datetime: timeInput.value,
              timezone: timeStatus.timezone,
              fold: Number(fold.value),
              ntp_policy: timeStatus.ntp_enabled === false ? "keep" : ntpPolicy.value,
              confirmed: true,
            }),
          });
          clockMessage.textContent = "日時を変更しています。録画の再開までお待ちください。";
          let operation;
          for (let attempt = 0; attempt < 120; attempt += 1) {
            if (viewGen !== viewGeneration) {
              throw new Error("画面が切り替わったため、時刻設定の結果確認を終了しました。");
            }
            operation = await api(`/api/v1/operations/${encodeURIComponent(accepted.operation_id)}`);
            if (["succeeded", "failed"].includes(operation.state)) break;
            await new Promise((resolve) => setTimeout(resolve, 1000));
          }
          if (operation?.state !== "succeeded") {
            const recovery = operation?.recovery || {};
            let recoveryMessage = "時計とカメラの状態を確認してください。";
            if (recovery.state === "recording_unchanged") {
              recoveryMessage = "時計と録画は変更されていません。現在の録画を継続しています。";
            } else if (recovery.state === "clock_unchanged") {
              if (recovery.failed_camera_ids?.length) {
                recoveryMessage = `時計は変更されていません。カメラ ${recovery.failed_camera_ids.join("・")} のMP4確定に失敗しました。`;
                if (recovery.resume_failed_camera_ids?.length) {
                  recoveryMessage += ` カメラ ${recovery.resume_failed_camera_ids.join("・")} の再開も確認できません。`;
                }
              } else {
                recoveryMessage = "時計は変更されていません。進行中MP4を確定し、変更前のカメラ状態を再照合しました。";
                if (recovery.resume_failed_camera_ids?.length) {
                  recoveryMessage += ` カメラ ${recovery.resume_failed_camera_ids.join("・")} の再開は確認できません。`;
                }
              }
            } else if (recovery.state === "clock_restored") {
              recoveryMessage = "時刻設定に失敗し、変更前相当の時刻へ戻しました。カメラ状態も確認してください。";
            } else if (recovery.state === "clock_state_uncertain") {
              recoveryMessage = "時刻が変更された可能性があります。OS時計とカメラ状態を確認してください。";
            } else if (recovery.state === "clock_set_camera_restart_failed") {
              recoveryMessage = `時計は変更されましたが、カメラ ${recovery.failed_camera_ids?.join("・") || "?"} の再開を確認できません。`;
            }
            if (recovery.ntp_state_uncertain) {
              recoveryMessage += " NTPの有効状態も確認してください。";
            }
            const operationError = operation?.error === "ntp_active_time_change_rejected"
              ? "NTPが動作中のため手動時刻変更を拒否しました。NTPを一時停止して再有効化する選択でやり直してください。"
              : operation?.error === "ntp_state_unknown"
                ? "NTPの状態を確認できないため、録画と時計を変更しませんでした。時刻同期状態を確認してからやり直してください。"
              : operation?.error || "操作結果を確認できません";
            throw new Error(`${operationError} ${recoveryMessage}`);
          }
          clockMessage.className = "message ok";
          clockMessage.textContent = "日時を変更しました。録画の状態を確認しました。";
          confirmClock.checked = false;
          try {
            timeStatus = await api("/api/v1/time/status");
            updateTimeStatusLabel();
            timeInput.value = utcNsToLocalInput(timeStatus.utc_ns, timeStatus.timezone);
            clockMessage.className = "message ok";
            clockMessage.textContent = "日時を変更しました。録画の状態を確認しました。";
          } catch (_statusError) {
            clockMessage.textContent = "日時を変更しました。現在の同期状態は取得できませんでした。";
          }
          manualTimeEdited = false;
          delete timeForm.dataset.dirty;
        } catch (error) {
          clockMessage.textContent = error.message;
          clockMessage.className = "message error";
        } finally {
          unlock();
          updateTimeStatusLabel();
        }
      });

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (save.disabled || form.getAttribute("aria-busy") === "true") return;
        const unlock = lockSettingsOperation(form, back);
        saveFeedback.textContent = "設定を保存しています。";
        saveFeedback.className = "message save-feedback";
        message.className = "message";
        try {
          const overlayChanged = cameraControls.some((controls) =>
            controls.config.overlay_enabled !== controls.overlay.checked
            || controls.config.overlay_corner !== controls.corner.value
            || controls.config.overlay_size !== controls.size.value
          );
          const cameras = cameraControls.map((controls) => {
            const next = { ...controls.config, usb_port_key: controls.port.value || null };
            const profile = videoPresetSettings(controls.profile.value);
            if (next.usb_port_key && !profile) {
              throw new Error(`カメラ${controls.config.camera_id}で使える録画品質を確認できません。カメラを選び直してください。`);
            }
            const selectedPort = (capabilities.cameras || []).find((item) => item.port_key === controls.port.value);
            next.video_interface_key = selectedPort?.video_interface_key
              || (next.usb_port_key === controls.config.usb_port_key
                ? controls.config.video_interface_key : null);
            next.audio_interface_key = controls.audioInterface.value || null;
            Object.assign(next, profile);
            next.audio_enabled = controls.audio.checked;
            next.overlay_enabled = controls.overlay.checked;
            next.overlay_corner = controls.corner.value;
            next.overlay_size = controls.size.value;
            return next;
          });
          let volumeKind = "microSD";
          let persistentId = null;
          if (volume.value.startsWith("usb:")) {
            volumeKind = "USB";
            persistentId = volume.value.slice(4);
          }
          const cameraRestartIds = cameraControls
            .filter(({ config, port, profile, audio, audioInterface }) => {
              const selected = videoPresetSettings(profile.value) || config;
              return config.width !== selected.width || config.height !== selected.height
                || config.fps !== selected.fps || config.quality !== selected.quality
                || config.audio_enabled !== audio.checked
                || config.audio_interface_key !== (audioInterface.value || null)
                || config.usb_port_key !== (port.value || null);
            }).map(({ config }) => config.camera_id);
          const volumeChanged = current.volume.kind !== volumeKind
            || current.volume.persistent_id !== persistentId;
          const powerSavingChanged = Boolean(current.power_saving_enabled) !== powerSaving.checked;
          const restartBoth = powerSavingChanged || volumeChanged;
          const restartIds = restartBoth ? [1, 2] : cameraRestartIds;
          if (restartIds.length > 0) {
            const target = restartIds.map((cameraId) => `カメラ${cameraId}`).join("・");
            const selectedStorage = (capabilities.volumes || []).find((item) => volume.value === "microSD" ? item.kind === "microSD" : `usb:${item.persistent_id}` === volume.value);
            const stopFloor = selectedStorage?.kind === "USB" ? 1 : 2;
            const changeWarning = selectedStorage && selectedStorage.available_bytes <= stopFloor * 1_000_000_000
              ? `選択した保存先は空き容量が不足しています。空きを${stopFloor}GB超に増やすまで録画は停止します。`
              : "現在の録画ファイルを保存してから設定を切り替えます。";
            if (!await confirmAction("録画を区切って設定を変更しますか？", `${target}の設定を変更します。${changeWarning}切り替え中は録画が一時停止します。`, "変更して録画を再開")) return;
          }
          const accepted = await api("/api/v1/settings", {
            method: "PATCH",
            body: JSON.stringify({
              expected_revision: current.revision,
              request_id: newRequestId(),
              settings: {
                ...current,
                power_saving_enabled: powerSaving.checked,
                auto_delete_start_tenths_gb: Math.round(Number(autoStart.value) * 10),
                auto_recover_tenths_gb: Math.round(Number(autoRecover.value) * 10),
                volume: { kind: volumeKind, persistent_id: persistentId },
                cameras,
              },
            }),
          });
          let operation;
          for (let attempt = 0; attempt < 120; attempt += 1) {
            if (viewGen !== viewGeneration) {
              throw new Error("画面が切り替わったため、設定適用の結果確認を終了しました。");
            }
            operation = await api(`/api/v1/operations/${encodeURIComponent(accepted.operation_id)}`);
            if (["succeeded", "failed"].includes(operation.state)) break;
            saveFeedback.textContent = {
              queued: "設定の変更を待っています。",
              validating: "カメラと保存先を確認しています。",
              finalizing: "録画ファイルを保存しています。",
              applying: "設定を変更しています。",
              starting: "カメラを起動しています。",
            }[operation.state] || "設定を保存しています。";
            await new Promise((resolve) => setTimeout(resolve, 1000));
          }
          if (operation?.state !== "succeeded") {
            const recoveryState = operation?.recovery?.state;
            const recoveryMessage = recoveryState === "previous_settings_unchanged"
              ? "設定は変更していません。"
              : recoveryState === "previous_settings_restored"
              ? (operation?.error === "settings_apply_failed_rolled_back" ? "" : "旧設定へ戻しました。")
              : recoveryState === "blocked"
                ? "自動復旧できず、対象カメラを停止しています。状態を確認してください。"
                : "復旧状態を確認してください。";
            throw new Error(`${humanError(operation?.error, "設定適用の結果を確認できません。")} ${recoveryMessage}`.trim());
          }
          if (Number.isInteger(operation.result?.revision)) {
            current.revision = operation.result.revision;
          }
          current.power_saving_enabled = powerSaving.checked;
          current.auto_delete_start_tenths_gb = Math.round(Number(autoStart.value) * 10);
          current.auto_recover_tenths_gb = Math.round(Number(autoRecover.value) * 10);
          current.volume = { kind: volumeKind, persistent_id: persistentId };
          current.cameras = cameras;
          cameraControls.forEach((controls, index) => {
            controls.config = cameras[index];
          });
          updatePowerSavingSummary();
          markSettingsClean();
          showSettingsPage(null);
          message.textContent = overlayChanged
            ? "設定を保存しました。日時表示は次のプレビュー画像から、録画は次のファイルから反映されます。"
            : "設定を保存しました。";
          if (current.power_saving_enabled) {
            message.textContent += " 省電力モードでは画質を下げ、音声を止めています。";
          }
          message.className = "message ok";
          saveFeedback.textContent = message.textContent;
          saveFeedback.className = "message save-feedback ok";
        } catch (error) {
          message.textContent = error.message;
          message.className = "message error";
          saveFeedback.textContent = error.message;
          saveFeedback.className = "message save-feedback error";
        } finally {
          unlock();
          save.disabled = form.dataset.dirty !== "true";
          if (saveFeedback.textContent === "設定を保存しています。") saveFeedback.textContent = "";
        }
      });
      const home = element("div", undefined, "settings-home");
      const recordingList = element("div", undefined, "settings-list");
      const systemList = element("div", undefined, "settings-list");
      home.append(recordingList, systemList);
      panel.append(home);
      const destinations = [];
      function settingsLink(label, id, summary) {
        const row = element("button", undefined, "settings-row");
        row.type = "button";
        row.setAttribute("aria-label", label);
        const value = element("span", summary(), "settings-row-value");
        value.id = `settings-value-${viewGen}-${settingsRows.length}`;
        row.setAttribute("aria-describedby", value.id);
        settingsRows.push({ row, summary });
        const chevron = element("span", "›", "settings-chevron");
        chevron.setAttribute("aria-hidden", "true");
        row.append(element("span", label, "settings-row-title"), value, chevron);
        row.addEventListener("click", () => showSettingsPage(id));
        return row;
      }
      const addDestination = (id, label, node, summary, group, camera = null, parent = null, editable = false) => {
        const row = group ? settingsLink(label, id, summary) : null;
        if (row) group.append(row);
        destinations.push({ id, label, node, summary, row, camera, parent, editable });
      };
      for (const controls of cameraControls) {
        addDestination(`camera-${controls.config.camera_id}`, `カメラ${controls.config.camera_id}`, controls.card,
          () => controls.config.usb_port_key ? `${controls.config.width}×${controls.config.height} / ${controls.config.fps}fps` : "未使用", recordingList, controls);
      }
      addDestination("storage", "ストレージ", storageGroup,
        () => current.volume.kind === "USB" ? "USB" : "microSD", recordingList);
      addDestination("recording", "省電力モード", recordingGroup,
        () => current.power_saving_enabled ? "オン" : "オフ", recordingList);
      addDestination("security", "パスワード", securityPanel,
        () => authenticationStatus.login_protection_enabled || authenticationStatus.admin_protection_enabled ? "設定済み" : "未設定", systemList);
      addDestination("time", "日時", clockPanel,
        () => timeLoadError ? "確認できません" : timeStatus.ntp_enabled ? "自動" : "手動", systemList);
      addDestination("update", "ソフトウェア更新", updatePanel,
        () => updateVersionLabel.textContent.replace("現在のバージョン: ", ""), systemList);
      for (const page of subpages) addDestination(page.id, page.label, page.node, () => "", null, null, page.parent, page.editable);

      const showLoadError = (node, error, page) => {
        const retry = element("button", "読み込み直す", "secondary");
        retry.type = "button";
        retry.addEventListener("click", () => renderSettings(container, authenticationStatus, page));
        node.replaceChildren(element("p", error.message, "message error"), retry);
      };
      if (timeLoadError) showLoadError(clockPanel, timeLoadError, "time");
      if (updateLoadError) showLoadError(updatePanel, updateLoadError, "update");

      let timePageRequest = 0;
      const refreshDisplayedTime = async (page) => {
        const request = ++timePageRequest;
        timePageLoading = true;
        timePageFailed = false;
        retryTime.hidden = true;
        clockMessage.className = "message";
        updateTimeStatusLabel();
        try {
          const latest = await api("/api/v1/time/status");
          if (!isCurrentView(viewGen, panel) || request !== timePageRequest
            || activeSettingsPage !== page) return;
          timeStatus = latest;
          updateTimeStatusLabel();
          if (!manualTimeEdited) {
            timeInput.value = utcNsToLocalInput(latest.utc_ns, latest.timezone);
            updateFoldChoices();
          }
        } catch (error) {
          if (!isCurrentView(viewGen, panel) || request !== timePageRequest
            || activeSettingsPage !== page) return;
          timePageFailed = true;
          retryTime.hidden = false;
          clockMessage.textContent = error.message;
          clockMessage.className = "message error";
        } finally {
          if (isCurrentView(viewGen, panel) && request === timePageRequest
            && activeSettingsPage === page) {
            timePageLoading = false;
            updateTimeStatusLabel();
          }
        }
      };

      function showSettingsPage(id) {
        for (const node of [recordingGroup, storageGroup, cameraGroup, ...destinations.map((entry) => entry.node)]) node.hidden = true;
        const destination = destinations.find((entry) => entry.id === id);
        activeSettingsPage = destination?.id || null;
        const editable = destination && (destination.editable || destination.camera || [recordingGroup, storageGroup].includes(destination.node));
        form.hidden = !editable;
        home.hidden = Boolean(destination);
        back.hidden = !destination;
        const parent = destinations.find((entry) => entry.id === destination?.parent);
        back.textContent = `‹ ${parent?.label || "設定"}`;
        back.setAttribute("aria-label", `${parent?.label || "設定"}に戻る`);
        title.textContent = destination ? destination.label : "設定";
        message.textContent = "";
        if (destination) {
          destination.node.hidden = false;
          if (destination.camera) cameraGroup.hidden = false;
        }
        for (const entry of settingsRows) entry.row.querySelector(".settings-row-value").textContent = entry.summary();
        if (["time", "manual-time"].includes(id) && !timeLoadError) void refreshDisplayedTime(id);
        window.scrollTo({ top: 0, behavior: "auto" });
      }
      back.addEventListener("click", async () => {
        if (panel.querySelector('form[aria-busy="true"]')) return;
        const parent = destinations.find((entry) => entry.id === activeSettingsPage)?.parent || null;
        if (hasUnsavedSettings()) {
          if (!await confirmAction("変更を保存せずに戻りますか？", "変更した設定は保存されません。", "保存せずに戻る", true)) return;
          renderSettings(container, authenticationStatus, parent);
          return;
        }
        showSettingsPage(parent);
      });
      if (requestedCameraSettings !== null) {
        const cameraId = requestedCameraSettings;
        requestedCameraSettings = null;
        showSettingsPage(`camera-${cameraId}`);
        document.getElementById(`camera-${cameraId}-port`)?.focus({ preventScroll: true });
      } else {
        showSettingsPage(initialPage);
      }
    } catch (error) {
      if (!isCurrentView(viewGen, panel)) return;
      message.textContent = error.message;
      message.className = "message error";
      const retry = element("button", "読み込み直す", "secondary");
      retry.type = "button";
      retry.addEventListener("click", () => renderSettings(container, authenticationStatus, initialPage));
      panel.append(retry);
    }
  })();
}

async function refreshEngineStatus(message, grid) {
  const generation = viewGeneration;
  if (statusRequestGeneration === generation) return;
  statusRequestGeneration = generation;
  try {
    const [result, initialSettings, storageCapabilities] = await Promise.all([
      api("/api/v1/status"), liveSettingsRequest, api("/api/v1/capabilities").catch(() => null),
    ]);
    if (!isCurrentView(generation, grid)) return;
    let settingsResponse = initialSettings;
    if (!settingsResponse || settingsResponse.settings?.revision !== result.revision) {
      liveSettingsRequest = api("/api/v1/settings").catch(() => null);
      settingsResponse = await liveSettingsRequest;
      if (!isCurrentView(generation, grid)) return;
    }
    connectionBadge.textContent = "接続済み";
    connectionBadge.className = "badge ok";
    const warningLabels = {
      time_synchronization_not_verified: "時刻同期を確認できません",
      camera_1_audio_unavailable: "カメラ1の音声を取得できません",
      camera_2_audio_unavailable: "カメラ2の音声を取得できません",
      low_space: "保存先の空き容量が不足しています",
      selected_volume_unavailable_or_ambiguous: "保存先を確認できません",
      runtime_state_write_failed: "カメラ状態を保存できません",
    };
    const warningList = [
      ...(result.warnings || []),
      result.capacity_warning,
      result.capacity_error,
      result.volume_error,
      result.runtime_state_error,
    ].filter(Boolean);
    message.replaceChildren(storageMeters(storageCapabilities?.volumes, result.volume));
    if (warningList.length) message.append(element("p", warningList.map((item) => warningLabels[item] || "確認が必要な項目があります").join(" ・ "), "storage-warning"));
    const criticalWarningCodes = new Set([
      "low_space",
      "selected_volume_unavailable_or_ambiguous",
      "runtime_state_write_failed",
    ]);
    const hasCriticalWarning = warningList.some((item) => criticalWarningCodes.has(item))
      || Boolean(result.capacity_error || result.volume_error || result.runtime_state_error);
    if (warningList.length) {
      message.className = hasCriticalWarning ? "message status-message error" : "message status-message warn";
      connectionBadge.textContent = hasCriticalWarning ? "確認が必要" : "注意あり";
      connectionBadge.className = hasCriticalWarning ? "badge error" : "badge warn";
    } else {
      message.className = "message status-message";
    }
    const cameras = Array.isArray(result.cameras) ? result.cameras : [];
    const stateLabels = {
      stopped: "停止中", starting: "準備中", continuous: "録画中",
      finalizing: "録画を保存中", blocked: "録画を開始できません",
      error: "確認が必要です",
    };
    const blockerLabels = {
      camera_missing: "カメラが見つかりません", storage_missing: "保存先が使えません",
      low_space: "空き容量が不足しています", io_error: "機器との通信を確認してください",
      invalid_config: "設定を確認してください", overload: "処理負荷が高い状態です",
      update_in_progress: "更新の確認が完了するまで録画を待機しています",
    };
    for (const cameraId of [1, 2]) {
      const item = cameras.find((camera) => camera.camera_id === cameraId);
      const config = settingsResponse?.settings?.revision === result.revision
        ? settingsResponse.settings.cameras?.find((camera) => camera.camera_id === cameraId)
        : null;
      const unconfigured = Boolean(config && !config.usb_port_key);
      let card = grid.querySelector(`[data-camera-id="${cameraId}"]`);
      if (!card) {
        card = element("article", undefined, "card camera-card");
        card.dataset.cameraId = String(cameraId);
        const title = element("h2", `カメラ ${cameraId}`);
        const previewFrame = element("div", undefined, "preview-frame");
        const image = document.createElement("img");
        image.alt = `カメラ ${cameraId} の最新画像`;
        image.loading = "eager";
        const previewLabel = element("span", "画像を準備しています", "preview-label");
        previewFrame.append(image, previewLabel);
        const detail = element("div", undefined, "camera-status");
        detail.setAttribute("role", "status");
        const controls = element("div", undefined, "actions camera-actions");
        const audioButton = element("button", "音声付きライブを準備", "secondary camera-audio-button");
        audioButton.type = "button";
        audioButton.hidden = true;
        const audioVideo = document.createElement("video");
        audioVideo.controls = true;
        audioVideo.playsInline = true;
        audioVideo.preload = "none";
        audioVideo.className = "audio-live-video";
        audioVideo.hidden = true;
        audioVideo.setAttribute("aria-label", `カメラ ${cameraId} の音声付きライブ`);
        const toggle = element("button", "録画を停止", "camera-toggle");
        toggle.type = "button";
        const settingsButton = element("button", "設定を確認する", "secondary camera-settings-link");
        settingsButton.type = "button";
        settingsButton.hidden = true;
        settingsButton.addEventListener("click", () => {
          requestedCameraSettings = cameraId;
          topNavigation?.querySelector('[data-view="settings"]')?.click();
        });
        controls.append(toggle, settingsButton, audioButton);
        card.append(title, previewFrame, detail, controls, audioVideo);
        audioButton.addEventListener("click", async () => {
          if (audioButton.dataset.pending === "true" || card.dataset.statusUnavailable === "true") return;
          audioButton.dataset.pending = "true";
          audioButton.disabled = true;
          audioButton.textContent = "音声付きライブを準備しています";
          try {
            await api(`/api/v1/live/${cameraId}/demand`, {
              method: "POST", body: JSON.stringify({ active: true }),
            });
            if (!isCurrentView(generation, card)) {
              if (currentView !== "live") {
                void api(`/api/v1/live/${cameraId}/demand`, {
                  method: "POST", body: JSON.stringify({ active: false }),
                }).catch(() => {});
              }
              return;
            }
            let live = null;
            for (let attempt = 0; attempt < 20; attempt += 1) {
              await new Promise((resolve) => setTimeout(resolve, 500));
              if (!isCurrentView(generation, card)) return;
              const state = await api("/api/v1/status");
              if (!isCurrentView(generation, card)) return;
              live = state.cameras?.find((camera) => camera.camera_id === cameraId)?.live;
              if (live?.available && Number.isInteger(live.generation)) break;
            }
            if (!live?.available || !Number.isInteger(live.generation)) {
              throw new Error("音声付きライブを準備できませんでした。録画状態とカメラ接続を確認してください。");
            }
            const source = `/api/v1/live/${cameraId}/${live.generation}/index.m3u8`;
            const previousPlayer = hlsPlayers.get(audioVideo);
            if (previousPlayer) {
              previousPlayer.destroy();
              hlsPlayers.delete(audioVideo);
            }
            if (audioVideo.canPlayType("application/vnd.apple.mpegurl")) {
              audioVideo.src = source;
            } else if (window.Hls && window.Hls.isSupported()) {
              const player = new window.Hls({ enableWorker: true, lowLatencyMode: false });
              player.loadSource(source);
              player.attachMedia(audioVideo);
              hlsPlayers.set(audioVideo, player);
            } else {
              throw new Error("このブラウザは音声付きライブ再生に対応していません。");
            }
            audioVideo.hidden = false;
            audioButton.textContent = "音声付きライブを準備しました";
            showMessage(card, "下の再生ボタンを押すと音声を聴けます。", "ok");
          } catch (error) {
            audioButton.textContent = "音声付きライブを再試行";
            showMessage(card, error.message, "error");
          } finally {
            delete audioButton.dataset.pending;
            audioButton.disabled = card.dataset.statusUnavailable === "true";
          }
        });
      }
      const image = card.querySelector(".preview-frame img");
      const previewLabel = card.querySelector(".preview-label");
      const detail = card.querySelector(".camera-status");
      const toggle = card.querySelector(".camera-actions .camera-toggle");
      const settingsButton = card.querySelector(".camera-settings-link");
      const audioButton = card.querySelector(".camera-audio-button");
      const audioVideo = card.querySelector(".audio-live-video");
      delete card.dataset.statusUnavailable;
      const captureState = unconfigured ? "使用していません" : stateLabels[item?.state] || "カメラを確認できません";
      const blockerCodes = item?.blockers || [];
      const blockers = blockerCodes.map((blocker) => blockerLabels[blocker] || "設定を確認してください");
      const recordingOverloaded = item?.last_error === "FrameBufferFullError"
        || Number(item?.recording_delay_ns) > 10_000_000_000;
      card.dataset.cameraMissing = blockerCodes.includes("camera_missing") ? "true" : "false";
      card.dataset.cameraUnconfigured = String(unconfigured);
      card.classList.toggle("camera-unused", unconfigured);
      card.querySelector(".preview-frame").hidden = unconfigured;
      card.dataset.cameraState = item?.state || "";
      detail.replaceChildren();
      detail.append(element("p", captureState, `camera-state ${item?.recording_active ? "is-recording" : ""}`));
      if (!unconfigured && blockers.length) detail.append(element("p", blockers.join(" ・ "), "camera-problem"));
      if (!unconfigured && recordingOverloaded) {
        detail.append(element("p", "録画処理が追いついていません。録画品質を下げてください。", "camera-problem"));
      }
      if (!unconfigured && item?.desired_running && !item?.recording_active
        && !["starting", "finalizing"].includes(item?.state)) {
        detail.append(element("p", "問題が解消すると自動で再開します", "muted"));
      }
      audioButton.hidden = !item?.audio?.enabled;
      audioButton.disabled = audioButton.dataset.pending === "true";
      const cannotStart = unconfigured || !item || item.state === "blocked" || item.state === "error";
      toggle.hidden = cannotStart;
      settingsButton.hidden = !cannotStart && !recordingOverloaded;
      settingsButton.textContent = unconfigured ? "カメラを設定"
        : recordingOverloaded ? "録画品質を見直す" : "設定を確認する";
      if (!cannotStart) {
        toggle.textContent = item?.desired_running ? "録画を停止" : "録画を再開";
        toggle.className = item?.desired_running
          ? "camera-toggle danger-button"
          : "camera-toggle";
        toggle.disabled = toggle.dataset.pending === "true";
      }
      toggle.onclick = async () => {
        if (toggle.dataset.pending === "true" || card.dataset.statusUnavailable === "true") return;
        toggle.dataset.pending = "true";
        toggle.disabled = true;
        try {
          await api(`/api/v1/cameras/${cameraId}/${item?.desired_running ? "stop" : "start"}`, {
            method: "POST",
            body: "{}",
          });
          await refreshEngineStatus(message, grid);
        } catch (error) {
          showMessage(card, error.message, "error");
        } finally {
          delete toggle.dataset.pending;
          toggle.disabled = card.dataset.statusUnavailable === "true";
        }
      };
      if (!item?.desired_running) {
        const player = hlsPlayers.get(audioVideo);
        if (player) player.destroy();
        hlsPlayers.delete(audioVideo);
        audioVideo.pause();
        audioVideo.removeAttribute("src");
        audioVideo.load();
        audioVideo.hidden = true;
      }
      if (!unconfigured) void refreshPreviewImage(cameraId, image, previewLabel);
      if (!grid.contains(card)) grid.append(card);
    }
  } catch (error) {
    if (!isCurrentView(generation, grid)) return;
    connectionBadge.textContent = "録画サービスに接続できません";
    connectionBadge.className = "badge error";
    message.textContent = error.message;
    message.className = "message status-message error";
    for (const card of grid.querySelectorAll("[data-camera-id]")) {
      card.dataset.statusUnavailable = "true";
      if (card.dataset.cameraUnconfigured !== "true") {
        card.querySelector(".camera-status").replaceChildren(
          element("p", "録画状態を確認できません", "camera-state"),
          element("p", "接続が戻ると自動で確認します。", "muted"),
        );
      }
      card.querySelector(".camera-toggle").disabled = true;
      card.querySelector(".camera-audio-button").disabled = true;
    }
  } finally {
    if (statusRequestGeneration === generation) statusRequestGeneration = null;
  }
}

async function refreshPreviewImage(cameraId, image, label) {
  if (document.hidden || currentView !== "live" || previewRequests.has(cameraId)) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  previewRequests.set(cameraId, controller);
  if (!previewStartedAt.has(cameraId)) previewStartedAt.set(cameraId, Date.now());
  let pendingUrl = null;
  const isCurrentRequest = () => previewRequests.get(cameraId) === controller
    && image.isConnected && !document.hidden && currentView === "live" && dashboardActive;
  try {
    const response = await fetch(`/api/v1/cameras/${cameraId}/preview.jpg?at=${Date.now()}`, {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(response.status === 503 ? "画像を準備しています" : "画像を取得できません");
    const blob = await response.blob();
    if (!blob.type.startsWith("image/jpeg") || blob.size > 1_000_000) throw new Error("画像データを確認できません");
    if (!isCurrentRequest()) return;
    const url = URL.createObjectURL(blob);
    pendingUrl = url;
    const decodable = await new Promise((resolve) => {
      const probe = new Image();
      probe.addEventListener("load", () => resolve(true), { once: true });
      probe.addEventListener("error", () => resolve(false), { once: true });
      probe.src = url;
    });
    if (!decodable) {
      throw new Error("画像データを確認できません");
    }
    if (!isCurrentRequest()) return;
    const previous = previewObjectUrls.get(cameraId);
    image.src = url;
    image.hidden = false;
    image.closest(".preview-frame")?.classList.remove("stale-frame");
    previewObjectUrls.set(cameraId, url);
    pendingUrl = null;
    previewUpdatedAt.set(cameraId, Date.now());
    if (previous) URL.revokeObjectURL(previous);
    label.textContent = "最新画像 ・ 1秒間隔";
    label.className = "preview-label fresh";
  } catch (error) {
    if (error.name === "AbortError" || !isCurrentRequest()) return;
    const updatedAt = previewUpdatedAt.get(cameraId);
    const stale = !updatedAt || Date.now() - updatedAt > 5_000;
    if (stale && updatedAt) {
      image.closest(".preview-frame")?.classList.add("stale-frame");
      label.textContent = `最新画像を取得できません。${Math.floor((Date.now() - updatedAt) / 1000)}秒前の画像を表示しています。`;
    } else {
      label.textContent = stale && Date.now() - previewStartedAt.get(cameraId) >= 5_000
        ? "画像を取得できません。カメラの接続を確認してください。"
        : "画像を準備しています。";
      if (stale) image.hidden = true;
    }
    label.className = `preview-label ${stale ? "stale" : ""}`.trim();
  } finally {
    if (pendingUrl) URL.revokeObjectURL(pendingUrl);
    clearTimeout(timeout);
    if (previewRequests.get(cameraId) === controller) previewRequests.delete(cameraId);
  }
}

function refresh() {
  if (!authenticationRefreshPromise) {
    authenticationRefreshPromise = refreshAuthentication().finally(() => {
      authenticationRefreshPromise = null;
    });
  }
  return authenticationRefreshPromise;
}

async function refreshAuthentication() {
  viewGeneration += 1;
  const generation = viewGeneration;
  dashboardActive = false;
  stopViewResources(appRoot);
  try {
    const status = await api("/api/v1/auth/status");
    if (generation !== viewGeneration) return;
    csrfToken = status.csrf_token;
    topNavigation?.replaceChildren();
    headerActions?.replaceChildren();
    if (status.login_protection_enabled && !status.authenticated) renderLogin();
    else renderDashboard(status);
  } catch (error) {
    if (generation !== viewGeneration) return;
    connectionBadge.textContent = "画面の読み込みエラー";
    connectionBadge.className = "badge error";
    topNavigation?.replaceChildren();
    headerActions?.replaceChildren();
    const panel = element("section", undefined, "card");
    panel.append(element("p", error.message, "message error"));
    const retry = element("button", "読み込み直す", "secondary");
    retry.type = "button";
    retry.addEventListener("click", () => void refresh());
    panel.append(retry);
    appRoot.replaceChildren(panel);
  }
}

void refresh();
