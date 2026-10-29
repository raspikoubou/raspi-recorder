"use strict";

// Production-renderer state tests with controlled promises; native browser
// behavior is checked separately using interaction_fixture.py and Chrome.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

class Node {
  constructor(tag = "div") {
    this.tagName = tag; this.children = []; this.parent = null;
    this.listeners = new Map(); this.attributes = {}; this.dataset = {};
    this.className = ""; this.hidden = false; this.disabled = false;
    this._value = null; this._text = "";
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(" "), ...names])].join(" "); },
      remove: (...names) => { this.className = this.className.split(" ").filter(x => !names.includes(x)).join(" "); },
      toggle: (name, on) => { on ? this.classList.add(name) : this.classList.remove(name); },
    };
  }
  get isConnected() { return this.connected || Boolean(this.parent?.isConnected); }
  get value() {
    if (this.tagName !== "select") return this._value ?? "";
    return this.children.find(n => n.value === this._value)?.value
      ?? this.children.find(n => n.selected)?.value ?? this.children[0]?.value ?? "";
  }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(""); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parent) node.parent.children = node.parent.children.filter(n => n !== node);
      node.parent = this; this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = []; this._text = "";
    if (this.tagName === "select") this._value = null;
    this.append(...nodes);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  async fire(name, target = this) {
    const event = { target, preventDefault() {} };
    await Promise.all([...(this.listeners.get(name) || []).map(fn => fn(event)), this[`on${name}`]?.(event)]);
  }
  matches(selector) {
    return selector.split(",").some(part => {
      const token = part.trim();
      const segments = token.split(/\s+/);
      if (segments.length > 1) {
        if (!this.matches(segments.pop())) return false;
        for (let p = this.parent; p; p = p.parent) if (p.matches(segments.join(" "))) return true;
        return false;
      }
      const tag = token.match(/^[a-z]+/);
      if (tag && this.tagName !== tag[0]) return false;
      const cls = token.match(/\.([\w-]+)/);
      if (cls && !this.className.split(" ").includes(cls[1])) return false;
      const id = token.match(/^#([\w-]+)/);
      if (id && this.id !== id[1]) return false;
      for (const [, name, value] of token.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
        const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        const actual = name.startsWith("data-") ? this.dataset[key] : this[name] ?? this.getAttribute(name);
        if (value === undefined ? actual == null : String(actual) !== value) return false;
      }
      return true;
    });
  }
  querySelectorAll(selector) {
    return this.children.flatMap(n => [...(n.matches(selector) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let n = this; n; n = n.parent) if (n.matches(selector)) return n;
    return null;
  }
  focus() {}
  click() { return this.fire("click"); }
  reportValidity() { return true; }
  contains(node) { for (let n = node; n; n = n.parent) if (n === this) return true; return false; }
  pause() {}
  load() {}
}

const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject; const promise = new Promise((yes,no) => { resolve = yes; reject = no; });
  return { resolve, reject, promise };
};
const settings = () => ({
  revision: 11, volume: {kind:"USB", persistent_id:"synthetic-usb"},
  auto_delete_start_tenths_gb:50, auto_recover_tenths_gb:70, power_saving_enabled:false,
  cameras:[1,2].map(camera_id => ({camera_id, width:1280,height:720,fps:15,quality:"standard",
    usb_port_key:camera_id === 1 ? "synthetic-camera" : null, video_interface_key:null,
    audio_enabled:false,audio_interface_key:null,overlay_enabled:true,overlay_corner:"bottom_left",overlay_size:"medium"})),
});

async function screen(options = {}) {
  const root = new Node("main"); root.connected = true;
  const nav = new Node("nav"); nav.append(...["ライブ","録画","設定"].map(text => { const n = new Node("button"); n.textContent = text; return n; }));
  const badge = new Node(); const actions = new Node();
  const logout = new Node("button"); logout.textContent="ログアウト"; actions.append(logout);
  const document = {
    createElement: tag => new Node(tag), addEventListener() {}, defaultView: {addEventListener() {}},
    getElementById: id => ({app:root,"top-navigation":nav,connection:badge,"header-actions":actions})[id] || root.querySelector(`#${id}`),
    querySelector: s => root.querySelector(s),
  };
  const storage = new Map(Object.entries(options.storage || {}));
  const context = vm.createContext({document, Headers, URLSearchParams, setTimeout,
    setInterval: () => 1, clearInterval() {},
    fetch: () => new Promise(() => {}), window:{scrollTo() {}},
    sessionStorage:{getItem: key => storage.get(key), setItem: (key,value) => storage.set(key,value), removeItem: key => storage.delete(key)},
    crypto: {randomUUID: () => "b".repeat(32)}});
  vm.runInContext(fs.readFileSync(process.env.RECORDER_UI_SOURCE || path.join(__dirname,"../../src/raspi_recorder/static/app.js"),"utf8"),context);
  const calls = []; const routes = new Map();
  const clock = {timezone:"Asia/Tokyo",helper_timezone:"Asia/Tokyo",utc_ns:1792992000000000000,
    helper_available:true,ntp_enabled:true,synchronized:true};
  context.api = async (url, args = {}) => {
    calls.push([url,args]);
    if (routes.has(url)) return routes.get(url)(args);
    if (url === options.fail) throw new Error("synthetic failure");
    if (url === "/api/v1/settings") return options.settingsPromise || {settings:settings()};
    if (url === "/api/v1/capabilities") return options.capabilities || {cameras:[],volumes:[]};
    if (url === "/api/v1/time/status") return clock;
    if (url === "/api/v1/update/status") return options.updateStatus || {source_configured:false,current_version:"0.1.34"};
    if (url === "/api/v1/status") return {revision:11,time:clock};
    throw new Error(`unexpected ${url}`);
  };
  const auth = {revision:0,login_protection_enabled:false,admin_protection_enabled:false};
  context.renderSettings(root,auth,options.page || null); await tick();
  const byId = id => root.querySelector(`#${id}`);
  const button = text => root.querySelectorAll("button").find(n => {
    for (let p=n;p;p=p.parent) if (p.hidden) return false;
    return n.textContent === text || n.getAttribute("aria-label") === text;
  });
  return {root,nav,actions,logout,context,calls,routes,byId,button,storage,auth,
    async change(node,value) { node.value=value; await node.fire("change"); await node.parent?.parent?.fire("change"); await root.querySelector(".settings-form")?.fire("change"); await tick(); }};
}

for (const [url,label] of [["/api/v1/time/status","日時"],["/api/v1/update/status","ソフトウェア更新"]]) {
  test(`${url} failure does not block camera settings; its page can retry`,async()=>{
    const s=await screen({fail:url});
    assert.ok(s.button("カメラ1"));
    await s.button(label).fire("click");
    assert.ok(s.button("読み込み直す"));
    s.routes.set(url,()=>url.includes("time")?{timezone:"Asia/Tokyo",utc_ns:1792992000000000000}:{});
    await s.button("読み込み直す").fire("click"); await tick();
    assert.doesNotMatch(s.root.textContent,/synthetic failure/);
  });
}

test("core settings failure offers retry and recovers",async()=>{
  const s=await screen({fail:"/api/v1/settings"});
  assert.ok(s.button("読み込み直す"));
  s.routes.set("/api/v1/settings",()=>({settings:settings()}));
  await s.button("読み込み直す").fire("click"); await tick();
  assert.ok(s.button("カメラ1"));
});

test("unavailable camera, quality, USB and microphone selections preserve stored values",async()=>{
  const s=await screen();
  assert.equal(s.byId("camera-1-port").value,"synthetic-camera");
  assert.equal(s.byId("camera-1-profile").value,"1280x720@15:standard");
  assert.equal(s.byId("settings-volume").value,"usb:synthetic-usb");
});

for (const failed of [false,true]) {
  test(`settings ${failed?"failure":"success"} locks navigation and restores controls`,async()=>{
    const s=await screen({page:"cleanup"}); const pending=deferred();
    s.routes.set("/api/v1/settings",()=>({operation_id:"test-operation"}));
    s.routes.set("/api/v1/operations/test-operation",()=>pending.promise);
    await s.change(s.byId("settings-recover"),"7.1");
    const form=s.root.querySelector(".settings-form"); const saving=form.fire("submit"); await tick();
    assert.equal(form.getAttribute("aria-busy"),"true");
    assert.ok(s.nav.children.every(n=>n.disabled));
    assert.equal(s.logout.disabled,true);
    assert.equal(s.byId("settings-recover").disabled,true);
    await form.fire("submit");
    assert.equal(s.calls.filter(([url,args])=>url==="/api/v1/settings" && args.method==="PATCH").length,1);
    pending.resolve(failed?{state:"failed",error:"unsupported_camera_profile",recovery:{state:"previous_settings_unchanged"}}:{state:"succeeded",result:{revision:12}});
    await saving;
    assert.equal(form.getAttribute("aria-busy"),null);
    assert.ok(s.nav.children.every(n=>!n.disabled));
    assert.equal(s.logout.disabled,false);
    assert.equal(s.byId("settings-recover").disabled,false);
    assert.equal(form.dataset.dirty,failed?"true":undefined);
  });
}

test("clock operation blocks editing, double submit and navigation until completion",async()=>{
  const s=await screen({page:"manual-time"}); const pending=deferred();
  s.context.confirmAction=async()=>true;
  s.routes.set("/api/v1/time",()=>({operation_id:"clock-operation"}));
  s.routes.set("/api/v1/operations/clock-operation",()=>pending.promise);
  s.byId("manual-time-confirm").checked=true;
  const form=s.byId("manual-time-datetime").closest("form");
  const changing=form.fire("submit"); await tick();
  assert.equal(s.byId("manual-time-datetime").disabled,true);
  assert.ok(s.nav.children.every(n=>n.disabled));
  const duplicate=form.fire("submit"); await tick();
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/time").length,1);
  pending.resolve({state:"succeeded"}); await Promise.all([changing,duplicate]);
  assert.equal(s.byId("manual-time-datetime").disabled,false);
  assert.ok(s.nav.children.every(n=>!n.disabled));
});

test("cancelling a clock change restores the previously disabled NTP option",async()=>{
  const s=await screen({page:"manual-time"});
  s.context.confirmAction=async()=>false;
  s.byId("manual-time-confirm").checked=true;
  await s.byId("manual-time-datetime").closest("form").fire("submit");
  assert.equal(s.byId("manual-time-datetime").disabled,false);
  assert.equal(s.byId("manual-time-ntp").querySelector('option[value="keep"]').disabled,true);
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/time").length,0);
});

test("live status polling cannot re-enable a pending stop or submit it twice",async()=>{
  const s=await screen(); const pending=deferred();
  const grid=new Node("section"); const message=new Node("p"); s.root.replaceChildren(message,grid);
  s.context.storageMeters=()=>new Node();
  s.context.refreshPreviewImage=async()=>{};
  s.routes.set("/api/v1/status",()=>({revision:11,cameras:[{camera_id:1,state:"continuous",desired_running:true,recording_active:true}]}));
  s.routes.set("/api/v1/cameras/1/stop",()=>pending.promise);
  await s.context.refreshEngineStatus(message,grid);
  const toggle=grid.querySelector(".camera-toggle");
  const stopping=toggle.fire("click"); await tick();
  await s.context.refreshEngineStatus(message,grid);
  assert.equal(toggle.disabled,true);
  const duplicate=toggle.fire("click"); await tick();
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/cameras/1/stop").length,1);
  pending.resolve({}); await Promise.all([stopping,duplicate]);
  assert.equal(toggle.disabled,false);
});

test("cancelling a recording settings change keeps edits and unlocks controls",async()=>{
  const s=await screen({page:"camera-1"});
  s.context.confirmAction=async()=>false;
  await s.change(s.byId("camera-1-port"),"");
  const form=s.root.querySelector(".settings-form");
  await form.fire("submit");
  assert.equal(form.dataset.dirty,"true");
  assert.equal(s.byId("camera-1-port").value,"");
  assert.ok(s.nav.children.every(n=>!n.disabled));
  assert.equal(s.calls.filter(([url,args])=>args.method==="PATCH").length,0);
});

test("a camera without usable profiles cannot silently save the previous camera",async()=>{
  const s=await screen({page:"camera-1",capabilities:{volumes:[],cameras:[{port_key:"empty-camera",profiles:[]}]}});
  s.context.confirmAction=async()=>true;
  s.routes.set("/api/v1/settings",()=>({operation_id:"unexpected"}));
  s.routes.set("/api/v1/operations/unexpected",()=>({state:"succeeded"}));
  await s.change(s.byId("camera-1-port"),"empty-camera");
  await s.root.querySelector(".settings-form").fire("submit");
  assert.match(s.root.textContent,/使える録画品質を確認できません/);
  assert.equal(s.calls.filter(([url,args])=>args.method==="PATCH").length,0);
  assert.equal(s.byId("camera-1-port").value,"empty-camera");
});

test("opening manual time refreshes an old snapshot without replacing a new edit",async()=>{
  const s=await screen({page:"camera-1"});
  const latest={timezone:"Asia/Tokyo",helper_timezone:"Asia/Tokyo",utc_ns:1792992300000000000,
    helper_available:true,ntp_enabled:true};
  s.routes.set("/api/v1/time/status",()=>latest);
  await s.button("設定に戻る").fire("click");
  await s.button("日時").fire("click"); await tick();
  assert.equal(s.byId("manual-time-datetime").value,"2026-10-26T14:25:00");
  const pending=deferred(); s.routes.set("/api/v1/time/status",()=>pending.promise);
  await s.button("日時を手動で設定").fire("click");
  const input=s.byId("manual-time-datetime"); input.value="2026-10-26T15:30:00";
  const submit=input.closest("form").querySelector('button[type="submit"]');
  assert.equal(submit.disabled,true);
  await input.closest("form").fire("input",input);
  pending.resolve({...latest,utc_ns:1792992360000000000}); await tick();
  assert.equal(input.value,"2026-10-26T15:30:00");
  assert.equal(input.closest("form").dataset.dirty,"true");
  assert.equal(submit.disabled,false);
});

test("a revision rejection preserves edits and restores navigation",async()=>{
  const s=await screen({page:"cleanup"});
  await s.change(s.byId("settings-recover"),"7.1");
  s.routes.set("/api/v1/settings",()=>{throw new Error("revision rejected");});
  const form=s.root.querySelector(".settings-form"); await form.fire("submit");
  assert.match(s.root.textContent,/revision rejected/);
  assert.equal(form.dataset.dirty,"true");
  assert.equal(s.byId("settings-recover").value,"7.1");
  assert.ok(s.nav.children.every(n=>!n.disabled));
});

for (const failed of [false,true]) {
  test(`a stale settings ${failed?"failure":"success"} cannot replace another view`,async()=>{
    const pending=deferred(); const s=await screen({settingsPromise:pending.promise});
    vm.runInContext("viewGeneration += 1",s.context);
    const next=new Node(); next.textContent="new screen"; s.root.replaceChildren(next);
    if (failed) pending.reject(new Error("old request failure"));
    else pending.resolve({settings:settings()});
    await tick();
    assert.equal(s.root.textContent,"new screen");
  });
}

test("a failed clock operation preserves the entered date and unlocks controls",async()=>{
  const s=await screen({page:"manual-time"});
  s.context.confirmAction=async()=>true;
  s.routes.set("/api/v1/time",()=>({operation_id:"clock-fail"}));
  s.routes.set("/api/v1/operations/clock-fail",()=>({state:"failed",error:"synthetic failure",recovery:{state:"clock_unchanged"}}));
  const input=s.byId("manual-time-datetime"); input.value="2026-10-26T15:30:00";
  s.byId("manual-time-confirm").checked=true;
  const form=input.closest("form"); await form.fire("input"); await form.fire("submit");
  assert.match(s.root.textContent,/synthetic failure/);
  assert.equal(input.value,"2026-10-26T15:30:00");
  assert.equal(input.disabled,false);
  assert.equal(form.dataset.dirty,"true");
  assert.ok(s.nav.children.every(n=>!n.disabled));
});

test("a failed fresh-clock read blocks applying stale time and can retry",async()=>{
  const s=await screen({page:"manual-time"});
  s.routes.set("/api/v1/time/status",()=>{throw new Error("fresh clock failed");});
  await s.button("日時に戻る").fire("click"); await tick();
  await s.button("日時を手動で設定").fire("click"); await tick();
  assert.equal(s.button("日時を適用").disabled,true);
  const retry=s.button("読み込み直す"); assert.ok(retry);
  s.routes.set("/api/v1/time/status",()=>({timezone:"Asia/Tokyo",helper_timezone:"Asia/Tokyo",
    utc_ns:1792992300000000000,helper_available:true,ntp_enabled:true}));
  await retry.fire("click"); await tick();
  assert.equal(s.button("日時を適用").disabled,false);
  assert.equal(s.byId("manual-time-datetime").value,"2026-10-26T14:25:00");
  assert.doesNotMatch(s.root.textContent,/fresh clock failed/);
});

test("recording backlog shows an actionable warning and clears after recovery",async()=>{
  const s=await screen(); const grid=new Node("section"); const message=new Node("p");
  s.root.replaceChildren(message,grid); s.context.storageMeters=()=>new Node();
  s.context.refreshPreviewImage=async()=>{};
  let delay=12_000_000_000;
  s.routes.set("/api/v1/status",()=>({revision:11,cameras:[{camera_id:1,state:"continuous",desired_running:true,recording_active:true,recording_delay_ns:delay}]}));
  await s.context.refreshEngineStatus(message,grid);
  assert.match(grid.textContent,/録画処理が追いついていません/);
  assert.equal(grid.querySelector(".camera-settings-link").hidden,false);
  delay=0; await s.context.refreshEngineStatus(message,grid);
  assert.equal(message.textContent,"");
  assert.doesNotMatch(grid.textContent,/録画処理が追いついていません/);
  assert.equal(grid.querySelector(".camera-settings-link").hidden,true);
});

test("a protection request locks editing and navigation and recovers on rejection",async()=>{
  const s=await screen({page:"password-login"}); const pending=deferred();
  s.routes.set("/api/v1/auth/protections",()=>pending.promise);
  const form=s.byId("login-protection-enabled").closest("form");
  const saving=form.fire("submit"); await tick();
  assert.ok(s.nav.children.every(n=>n.disabled));
  assert.equal(s.byId("login-protection-enabled").disabled,true);
  const duplicate=form.fire("submit"); await tick();
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/auth/protections").length,1);
  pending.reject(new Error("synthetic credential rejection")); await Promise.all([saving,duplicate]);
  assert.equal(s.byId("login-protection-enabled").disabled,false);
  assert.ok(s.nav.children.every(n=>!n.disabled));
});

test("leaving live while audio preparation is pending prevents a detached player",async()=>{
  const s=await screen(); const grid=new Node("section"); const message=new Node("p");
  s.root.replaceChildren(message,grid); s.context.storageMeters=()=>new Node();
  s.context.refreshPreviewImage=async()=>{};
  s.routes.set("/api/v1/status",()=>({revision:11,cameras:[{camera_id:1,state:"continuous",desired_running:true,recording_active:true,audio:{enabled:true},live:{available:true,generation:5}}]}));
  await s.context.refreshEngineStatus(message,grid);
  const pending=deferred();s.routes.set("/api/v1/live/1/demand",()=>pending.promise);
  const preparing=grid.querySelector(".camera-audio-button").fire("click");await tick();
  const statusReads=s.calls.filter(([url])=>url==="/api/v1/status").length;
  vm.runInContext('viewGeneration += 1; currentView = "settings"',s.context);
  const next=new Node();next.textContent="settings view";s.root.replaceChildren(next);
  pending.resolve({});await preparing;
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/status").length,statusReads);
  assert.equal(s.root.textContent,"settings view");
});

const updateCandidate = () => ({source_configured:true,current_version:"0.1.34",
  candidate:{version:"0.1.35",enough_space:true}});

test("checking updates blocks applying an old candidate and parallel result checks",async()=>{
  const s=await screen({page:"update",updateStatus:updateCandidate()});
  const pending=deferred();
  s.routes.set("/api/v1/update/check",()=>({operation_id:"c".repeat(32)}));
  s.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>pending.promise);
  s.routes.set("/api/v1/update/apply",()=>({operation_id:"d".repeat(32)}));
  s.routes.set(`/api/v1/operations/${"d".repeat(32)}`,()=>({state:"succeeded"}));
  const applying=s.button("この安定版を適用");
  s.byId("update-confirm").checked=true;
  const checking=s.button("更新を確認").fire("click"); await tick();
  assert.equal(applying.disabled,true);
  assert.equal(s.button("更新の結果を見る").disabled,true);
  await applying.closest("form").fire("submit");
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/update/apply").length,0);
  pending.resolve({state:"succeeded",result:{scope:"update_check"}}); await checking;
  assert.equal(s.button("更新を確認").disabled,false);
});

test("an update apply cannot replace its request ID through repeated submit",async()=>{
  const s=await screen({page:"update",updateStatus:updateCandidate()}); const pending=deferred();
  s.routes.set("/api/v1/update/apply",()=>pending.promise);
  s.routes.set(`/api/v1/operations/${"d".repeat(32)}`,()=>({state:"succeeded",result:{scope:"update",version:"0.1.35"}}));
  s.byId("update-confirm").checked=true;
  const form=s.button("この安定版を適用").closest("form");
  const first=form.fire("submit"); await tick();
  assert.equal(s.byId("update-confirm").disabled,true);
  const duplicate=form.fire("submit"); await tick();
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/update/apply").length,1);
  pending.resolve({operation_id:"d".repeat(32)}); await Promise.all([first,duplicate]);
  assert.match(s.root.textContent,/更新が完了しました/);
});

test("update check progress remains a check after acceptance and survives reload",async()=>{
  const s=await screen({page:"update",updateStatus:updateCandidate()});
  const pending=deferred(); let reads=0;
  s.context.setTimeout=fn=>{fn();return 1;};
  s.routes.set("/api/v1/update/check",()=>({operation_id:"c".repeat(32)}));
  s.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>++reads===1?{state:"running",stage:"validating"}:pending.promise);
  const checking=s.button("更新を確認").fire("click"); await tick();
  assert.match(s.root.querySelector("#settings-update").textContent,/更新があるか確認しています/);
  assert.doesNotMatch(s.root.querySelector("#settings-update").textContent,/更新を適用中|更新パッケージ/);
  const restored=await screen({page:"update",updateStatus:updateCandidate(),storage:Object.fromEntries(s.storage)});
  restored.context.setTimeout=fn=>{fn();return 1;}; let restoredReads=0;
  restored.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>++restoredReads===1?{state:"running"}:pending.promise);
  const reviewing=restored.button("更新の結果を見る").fire("click"); await tick();
  assert.match(restored.root.querySelector("#settings-update").textContent,/更新があるか確認しています/);
  pending.resolve({state:"succeeded",result:{scope:"update_check"}}); await Promise.all([checking,reviewing]);
});

test("only one resumed update poll can own a pending operation",async()=>{
  const s=await screen({page:"update",updateStatus:updateCandidate(),storage:{"raspiRecorder.pendingUpdateOperation":"c".repeat(32)}});
  const pending=deferred();
  s.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>pending.promise);
  const review=s.button("更新の結果を見る"); const first=review.fire("click"); await tick();
  const duplicate=review.fire("click"); await tick();
  assert.equal(s.calls.filter(([url])=>url.startsWith("/api/v1/operations/")).length,1);
  pending.resolve({state:"failed",error:"synthetic update rejection"}); await Promise.all([first,duplicate]);
  assert.equal(review.hidden,true);
  assert.equal(s.button("更新を確認").disabled,false);
});

test("refreshed update-source state controls the next check and clears old errors",async()=>{
  const s=await screen({page:"update",updateStatus:updateCandidate()});
  s.routes.set("/api/v1/update/check",()=>({operation_id:"c".repeat(32)}));
  s.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>({state:"failed",error:"synthetic failure"}));
  await s.button("更新を確認").fire("click");
  assert.equal(s.button("エラーの詳細").hidden,false);
  s.routes.set(`/api/v1/operations/${"c".repeat(32)}`,()=>({state:"succeeded",result:{scope:"update_check"}}));
  s.routes.set("/api/v1/update/status",()=>({source_configured:false,current_version:"0.1.34"}));
  await s.button("更新を確認").fire("click");
  assert.equal(s.button("更新を確認").disabled,true);
  assert.equal(s.root.querySelectorAll("button").find(n=>n.getAttribute("aria-label")==="エラーの詳細").hidden,true);
});

test("failed status refresh removes stale recording success and recovers its controls",async()=>{
  const s=await screen(); const grid=new Node("section"); const message=new Node("p");
  s.root.replaceChildren(message,grid); s.context.storageMeters=()=>new Node(); s.context.refreshPreviewImage=async()=>{};
  const status={revision:11,cameras:[{camera_id:1,state:"continuous",desired_running:true,recording_active:true,audio:{enabled:true}}]};
  s.routes.set("/api/v1/status",()=>status); await s.context.refreshEngineStatus(message,grid);
  assert.match(grid.querySelector(".camera-state").textContent,/録画中/);
  s.routes.set("/api/v1/status",()=>{throw new Error("synthetic offline");});
  await s.context.refreshEngineStatus(message,grid);
  assert.doesNotMatch(grid.querySelector(".camera-state").textContent,/録画中/);
  assert.doesNotMatch(grid.querySelector(".camera-state").className,/is-recording/);
  assert.equal(grid.querySelector(".camera-toggle").disabled,true);
  assert.equal(grid.querySelector(".camera-audio-button").disabled,true);
  s.routes.set("/api/v1/status",()=>status); await s.context.refreshEngineStatus(message,grid);
  assert.match(grid.querySelector(".camera-state").textContent,/録画中/);
  assert.equal(grid.querySelector(".camera-toggle").disabled,false);
  assert.equal(grid.querySelector(".camera-audio-button").disabled,false);
});

test("a stop response cannot unlock controls after a newer offline status",async()=>{
  const s=await screen(); const grid=new Node("section"); const message=new Node("p");
  s.root.replaceChildren(message,grid); s.context.storageMeters=()=>new Node(); s.context.refreshPreviewImage=async()=>{};
  s.routes.set("/api/v1/status",()=>({revision:11,cameras:[{camera_id:1,state:"continuous",desired_running:true,recording_active:true}]}));
  await s.context.refreshEngineStatus(message,grid);
  const pending=deferred();s.routes.set("/api/v1/cameras/1/stop",()=>pending.promise);
  const toggle=grid.querySelector(".camera-toggle");const stopping=toggle.fire("click");await tick();
  s.routes.set("/api/v1/status",()=>{throw new Error("synthetic offline");});
  await s.context.refreshEngineStatus(message,grid);
  pending.resolve({accepted:true});await stopping;
  assert.equal(toggle.disabled,true);
  await toggle.fire("click");
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/cameras/1/stop").length,1);
});

test("authentication loading failure has a working retry",async()=>{
  const s=await screen();s.routes.set("/api/v1/auth/status",()=>{throw new Error("synthetic auth status failure");});
  await s.context.refreshAuthentication();
  assert.ok(s.button("読み込み直す"));
  let retries=0;s.context.refresh=async()=>{retries++;};
  await s.button("読み込み直す").fire("click");assert.equal(retries,1);
});

test("a stale authentication failure cannot replace a newer page",async()=>{
  const s=await screen();const pending=deferred();s.routes.set("/api/v1/auth/status",()=>pending.promise);
  const refreshing=s.context.refreshAuthentication();await tick();
  vm.runInContext("viewGeneration += 1",s.context);
  s.root.replaceChildren(new Node());s.root.children[0].textContent="newer view";
  pending.reject(new Error("stale auth failure"));await refreshing;
  assert.equal(s.root.textContent,"newer view");
});

test("logout respects unsaved edits, cancellation, pending saves and double clicks",async()=>{
  const s=await screen(); vm.runInContext('currentView="settings"',s.context);
  s.context.renderDashboard({...s.auth,login_protection_enabled:true});await tick();
  const form=s.root.querySelector(".settings-form");form.dataset.dirty="true";
  s.context.setPreviewDemand=async()=>{};s.context.refresh=async()=>{};
  s.routes.set("/api/v1/auth/logout",()=>({}));
  const logout=s.actions.querySelector("button");
  s.context.confirmAction=async()=>false;await logout.fire("click");
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/auth/logout").length,0);
  assert.equal(form.dataset.dirty,"true");assert.equal(logout.disabled,false);
  form.setAttribute("aria-busy","true");await logout.fire("click");
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/auth/logout").length,0);
  form.removeAttribute("aria-busy");const pending=deferred();s.context.confirmAction=()=>pending.promise;
  const first=logout.fire("click");await tick();const duplicate=logout.fire("click");await tick();
  pending.resolve(true);await Promise.all([first,duplicate]);
  assert.equal(s.calls.filter(([url])=>url==="/api/v1/auth/logout").length,1);
});
