"use strict";

// Execute the production renderer with controlled API promises. This is a DOM
// state unit test, not evidence about a native browser's date/time picker.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

class Node {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.parent = null;
    this.listeners = new Map();
    this.attributes = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this._value = "";
    this._text = "";
  }
  get isConnected() { return this.connected || Boolean(this.parent?.isConnected); }
  get value() {
    if (this.tagName !== "select") return this._value;
    return this.children.find((node) => node.value === this._value)?.value
      ?? this.children[0]?.value ?? "";
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(...nodes) {
    for (const node of nodes) { node.parent = this; this.children.push(node); }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = []; this._text = "";
    if (this.tagName === "select") this._value = "";
    this.append(...nodes);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === "src") this.src = ""; }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    this.listeners.set(name, [...callbacks, callback]);
  }
  fire(name) {
    const event = { target: this, preventDefault() {} };
    for (const callback of this.listeners.get(name) || []) callback(event);
    this[`on${name}`]?.(event);
  }
  closest(selector) {
    for (let node = this; node; node = node.parent) {
      if (node.className.split(" ").includes(selector.slice(1))) return node;
    }
    return null;
  }
  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) { const found = child.find(predicate); if (found) return found; }
    return null;
  }
  querySelectorAll() { return []; }
  pause() { this.paused = true; }
  load() {}
  play() { this.paused = false; return Promise.resolve(); }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const ns = (value) => Date.parse(value) * 1e6;
const clip = (day, camera = 1) => ({
  recording_id: `${day}-cam${camera}`, camera_id: camera, state: "finalized", part_index: 1,
  start_utc_ns: ns(`${day}T00:00:00Z`), end_utc_ns: ns(`${day}T00:10:00Z`),
});

async function screen(options = {}) {
  const root = new Node("main"); root.connected = true;
  const document = {
    getElementById: () => root, createElement: (tag) => new Node(tag),
    addEventListener() {}, defaultView: { addEventListener() {} },
  };
  const context = vm.createContext({
    document, Headers, URLSearchParams, fetch: () => new Promise(() => {}),
  });
  const source = process.env.RECORDER_UI_SOURCE || path.join(__dirname, "../../src/raspi_recorder/static/app.js");
  vm.runInContext(fs.readFileSync(source, "utf8"), context);
  const routes = new Map();
  let metadataFailure = options.failMetadataOnce;
  context.api = async (url) => {
    if (url === "/api/v1/status") {
      if (metadataFailure) { metadataFailure = false; throw new Error("metadata failed"); }
      return {time: {timezone: "Asia/Tokyo", utc_ns: ns("2026-10-26T00:00:00Z")}};
    }
    const query = new URL(url, "http://fixture").searchParams;
    if (!query.has("from_utc_ns")) return {items: [clip("2026-10-26")], next_before_sequence: null};
    const day = new Date(Number(query.get("from_utc_ns")) / 1e6 + 9 * 3600_000).toISOString().slice(0, 10);
    if (routes.has(day)) return routes.get(day)(query);
    return {items: ["2026-10-25", "2026-10-26"].includes(day) ? [clip(day)] : [], next_before_sequence: null};
  };
  await context.renderRecordings(root);
  const byId = (id) => root.find((node) => node.id === id);
  return {
    root, context, routes, date: byId("recordings-date"), time: byId("recordings-time"),
    camera: byId("recordings-camera"),
    playback: root.find((node) => node.className === "recording-playback"),
    player: root.find((node) => node.tagName === "video"),
    retry: root.find((node) => node.tagName === "button" && node.textContent === "読み込み直す"),
    async change(node, value) { node.value = value; node.fire("change"); await tick(); },
  };
}

test("an empty date keeps the time field usable and preserves the chosen time", async () => {
  const s = await screen();
  await s.change(s.time, "09:04");
  await s.change(s.date, "2026-10-24");
  assert.equal(s.time.disabled, false);
  assert.equal(s.time.value, "09:04");
  assert.equal(s.playback.hidden, true);
  assert.match(s.root.textContent, /この日の録画はありません/);
  await s.change(s.time, "11:00");
  assert.equal(s.time.disabled, false);
  assert.match(s.root.textContent, /この日の録画はありません/);
});

test("recorded date -> empty date -> recorded date reopens the preserved time", async () => {
  const s = await screen();
  await s.change(s.time, "09:04");
  await s.change(s.date, "2026-10-24");
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.value, "09:04");
  assert.equal(s.time.disabled, false);
  assert.equal(s.playback.hidden, false);
  assert.match(s.player.src, /2026-10-25-cam1/);
  await s.change(s.date, "2026-10-26");
  assert.match(s.player.src, /2026-10-26-cam1/);
});

test("time can change during a pending date request and is applied to its result", async () => {
  const s = await screen(); const pending = deferred();
  s.routes.set("2026-10-25", () => pending.promise);
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.disabled, false);
  await s.change(s.time, "09:06");
  assert.match(s.root.textContent, /録画を読み込んでいます/);
  pending.resolve({items: [clip("2026-10-25")], next_before_sequence: null}); await tick();
  assert.equal(s.time.value, "09:06");
  assert.match(s.player.src, /2026-10-25-cam1/);
});

for (const staleError of [false, true]) {
  test(`a stale ${staleError ? "failure" : "success"} cannot overwrite the newer selected date`, async () => {
    const s = await screen(); const pending = deferred();
    await s.change(s.time, "09:03");
    s.routes.set("2026-10-25", () => pending.promise);
    await s.change(s.date, "2026-10-25");
    await s.change(s.date, "2026-10-26");
    if (staleError) pending.reject(new Error("old failure"));
    else pending.resolve({items: [], next_before_sequence: null});
    await tick();
    assert.equal(s.time.disabled, false);
    assert.equal(s.time.value, "09:03");
    assert.match(s.player.src, /2026-10-26-cam1/);
    assert.doesNotMatch(s.root.textContent, /old failure|この日の録画はありません/);
  });
}

test("a failed request keeps time editable and retry uses the retained value", async () => {
  const s = await screen();
  s.routes.set("2026-10-25", () => Promise.reject(new Error("connection failed")));
  await s.change(s.date, "2026-10-25");
  await s.change(s.time, "09:05");
  assert.equal(s.time.disabled, false);
  assert.match(s.root.textContent, /connection failed/);
  assert.equal(s.retry.hidden, false);
  s.routes.delete("2026-10-25"); s.retry.fire("click"); await tick();
  assert.equal(s.retry.hidden, true);
  assert.equal(s.time.value, "09:05");
  assert.match(s.player.src, /2026-10-25-cam1/);
});

test("clearing a date rejects pending results and re-entering a date recovers", async () => {
  const s = await screen(); const pending = deferred();
  await s.change(s.time, "09:05");
  s.routes.set("2026-10-25", () => pending.promise);
  await s.change(s.date, "2026-10-25");
  await s.change(s.date, "");
  pending.resolve({items: [clip("2026-10-25")], next_before_sequence: null}); await tick();
  assert.equal(s.playback.hidden, true);
  assert.equal(s.time.disabled, false);
  await s.change(s.date, "2026-10-26");
  assert.equal(s.time.value, "09:05");
  assert.match(s.player.src, /2026-10-26-cam1/);
});

test("the form stays editable while collecting additional API pages", async () => {
  const s = await screen(); const second = deferred();
  s.routes.set("2026-10-25", (query) => query.has("before_sequence") ? second.promise
    : {items: [clip("2026-10-25", 1)], next_before_sequence: 42});
  await s.change(s.date, "2026-10-25");
  await s.change(s.time, "09:02");
  assert.equal(s.time.disabled, false);
  second.resolve({items: [clip("2026-10-25", 2)], next_before_sequence: null}); await tick();
  assert.match(s.player.src, /2026-10-25-cam1/);
  await s.change(s.camera, "2");
  assert.match(s.player.src, /2026-10-25-cam2/);
});

test("a repeated pagination cursor is recoverable without disabling time", async () => {
  const s = await screen();
  s.routes.set("2026-10-25", () => ({items: [clip("2026-10-25")], next_before_sequence: 42}));
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.disabled, false);
  assert.equal(s.retry.hidden, false);
  assert.match(s.root.textContent, /すべて読み込めませんでした/);
});

test("leaving the recording view prevents a pending result from changing the new view", async () => {
  const s = await screen(); const pending = deferred();
  s.routes.set("2026-10-25", () => pending.promise);
  await s.change(s.date, "2026-10-25");
  vm.runInContext("viewGeneration += 1", s.context);
  const settings = new Node(); settings.textContent = "settings view";
  s.root.replaceChildren(settings);
  pending.resolve({items: [clip("2026-10-25")], next_before_sequence: null}); await tick();
  assert.equal(s.root.textContent, "settings view");
});

test("initial metadata failure remains visible while editing time and retry recovers", async () => {
  const s = await screen({failMetadataOnce: true});
  await s.change(s.time, "09:05");
  assert.equal(s.time.disabled, false);
  assert.match(s.root.textContent, /metadata failed/);
  assert.equal(s.retry.hidden, false);
  s.retry.fire("click"); await tick();
  assert.equal(s.retry.hidden, true);
  assert.equal(s.time.value, "09:05");
  assert.match(s.player.src, /2026-10-26-cam1/);
});
