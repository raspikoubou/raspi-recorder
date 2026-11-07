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


test("recording times are a select of available recording intervals", async () => {
  const s = await screen();
  assert.equal(s.time.tagName, "select");
  assert.equal(s.time.disabled, false);
  assert.equal(s.time.value, "");
  assert.match(s.time.textContent, /09:00:00.*09:10:00/);
  await s.change(s.time, "2026-10-26-cam1");
  assert.equal(s.playback.hidden, false);
  assert.match(s.player.src, /2026-10-26-cam1/);
});

test("an empty date disables the interval list and clears old playback", async () => {
  const s = await screen();
  await s.change(s.time, "2026-10-26-cam1");
  await s.change(s.date, "2026-10-24");
  assert.equal(s.time.disabled, true);
  assert.equal(s.time.value, "");
  assert.equal(s.playback.hidden, true);
  assert.match(s.root.textContent, /この日の録画はありません/);
});

test("returning from an empty date populates only the new day's intervals", async () => {
  const s = await screen();
  await s.change(s.date, "2026-10-24");
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.disabled, false);
  assert.equal(s.time.value, "");
  await s.change(s.time, "2026-10-25-cam1");
  assert.match(s.player.src, /2026-10-25-cam1/);
});

test("the interval list stays disabled until a pending date has loaded", async () => {
  const s = await screen(); const pending = deferred();
  s.routes.set("2026-10-25", () => pending.promise);
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.disabled, true);
  assert.equal(s.playback.hidden, true);
  pending.resolve({items: [clip("2026-10-25")], next_before_sequence: null}); await tick();
  assert.equal(s.time.disabled, false);
  await s.change(s.time, "2026-10-25-cam1");
  assert.match(s.player.src, /2026-10-25-cam1/);
});

for (const staleError of [false, true]) {
  test(`a stale ${staleError ? "failure" : "success"} cannot replace the selected date's intervals`, async () => {
    const s = await screen(); const pending = deferred();
    s.routes.set("2026-10-25", () => pending.promise);
    await s.change(s.date, "2026-10-25");
    await s.change(s.date, "2026-10-26");
    if (staleError) pending.reject(new Error("old failure"));
    else pending.resolve({items: [], next_before_sequence: null});
    await tick();
    assert.equal(s.time.disabled, false);
    assert.equal(s.time.children[1].value, "2026-10-26-cam1");
    assert.doesNotMatch(s.root.textContent, /old failure|この日の録画はありません/);
  });
}

test("a failed date can retry and restore its interval choices", async () => {
  const s = await screen();
  s.routes.set("2026-10-25", () => { throw new Error("date failed"); });
  await s.change(s.date, "2026-10-25");
  assert.equal(s.time.disabled, true);
  assert.equal(s.retry.hidden, false);
  s.routes.delete("2026-10-25");
  s.retry.fire("click"); await tick();
  assert.equal(s.time.disabled, false);
  assert.equal(s.time.children[1].value, "2026-10-25-cam1");
});

test("two parts within the same minute remain separately selectable", async () => {
  const s = await screen();
  const second = {...clip("2026-10-26"), recording_id:"second", start_utc_ns:ns("2026-10-26T00:00:30Z")};
  s.routes.set("2026-10-26", () => ({items:[clip("2026-10-26"),second],next_before_sequence:null}));
  await s.change(s.date,"2026-10-26");
  assert.equal(s.time.children.length,3);
  await s.change(s.time,"second");
  assert.match(s.player.src,/second/);
});

test("switching cameras lists only that camera's recordings", async () => {
  const s = await screen();
  s.routes.set("2026-10-26", () => ({items:[clip("2026-10-26",1),clip("2026-10-26",2)],next_before_sequence:null}));
  await s.change(s.date,"2026-10-26");
  await s.change(s.camera,"2");
  assert.equal(s.time.children.length,2);
  assert.equal(s.time.children[1].value,"2026-10-26-cam2");
  await s.change(s.time,"2026-10-26-cam2");
  assert.match(s.player.src,/cam2/);
});

test("initial metadata failure does not expose an editable time input", async () => {
  const s = await screen({failMetadataOnce:true});
  assert.equal(s.time.tagName,"select");
  assert.equal(s.time.disabled,true);
  s.retry.fire("click"); await tick(); await tick();
  assert.equal(s.time.disabled,false);
});
