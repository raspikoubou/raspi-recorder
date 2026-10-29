"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = vm.createContext({
  document: { getElementById: () => ({ querySelectorAll: () => [] }), addEventListener() {} },
  Headers, fetch: () => new Promise(() => {}),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../src/raspi_recorder/static/app.js'), 'utf8'), context);
assert.match(context.humanError('export_space_insufficient'), /1件ずつダウンロード/);
assert.match(context.humanError('GitHubReleaseError'), /配布元/);
assert.equal(context.humanError('PrivateImplementationError'), '操作できませんでした。もう一度お試しください。');
assert.equal(context.humanError({code:'engine_unavailable'}).includes('Pi'), true);
const select = {id:'corner', type:'select-one', value:'bottom_left'};
const checkbox = {id:'overlay', type:'checkbox', checked:true};
const form = {querySelectorAll: () => [select, checkbox]};
const baseline = context.formValues(form);
select.value = 'top_right';
assert.notEqual(context.formValues(form), baseline);
select.value = 'bottom_left';
assert.equal(context.formValues(form), baseline);
checkbox.checked = false;
assert.notEqual(context.formValues(form), baseline);
checkbox.checked = true;
assert.equal(context.formValues(form), baseline);
const ns = (stamp) => Date.parse(stamp) * 1_000_000;
const span = {start_utc_ns:ns('2026-10-25T00:00:00Z'), end_utc_ns:ns('2026-10-25T00:05:00Z'), camera_id:1, state:'finalized'};
assert.equal(context.recordingTimeOption(span,'2026-10-25','Asia/Tokyo',false),'09:00:00 ～ 09:05:00');
assert.match(context.recordingTimeOption({...span,camera_id:2},'2026-10-25','Asia/Tokyo',true),/カメラ 2/);
assert.match(context.recordingTimeOption({...span,state:'abnormal'},'2026-10-25','Asia/Tokyo',false),/要確認/);
const midnight = {...span,start_utc_ns:ns('2026-10-25T14:58:00Z'),end_utc_ns:ns('2026-10-25T15:03:00Z')};
assert.match(context.recordingTimeOption(midnight,'2026-10-26','Asia/Tokyo',false),/10\/25/);
const first=context.localMidnightNs('2026-10-26','Asia/Tokyo');
const last=context.localMidnightNs('2026-10-27','Asia/Tokyo');
assert.equal(context.recordingsOverlapDay(midnight,first,last),true);
assert.equal(context.recordingsOverlapDay(span,first,last),false);
assert.equal(context.recordingsOverlapDay({start_utc_ns:Number(first),end_utc_ns:null},first,last),true);
console.log('UI state, local time choices and day boundaries: 15 assertions passed');

assert.equal(context.storageUsage(null),null);
assert.equal(context.storageUsage({total_bytes:0,available_bytes:0}),null);
assert.equal(context.storageUsage({total_bytes:100,available_bytes:25}).percent,75);
assert.equal(context.storageUsage({total_bytes:100,available_bytes:200}).used,0);
assert.equal(context.storageUsage({total_bytes:100,available_bytes:-1}),null);
assert.equal(context.formatBytes(16_000_000_000),'16.0 GB');
const hardware={profiles:[{width:640,height:480,fps:[5,10]},{width:1280,height:720,fps:[15]}]};
const options=context.videoPresetOptions(hardware,{width:1280,height:720,fps:15,quality:'standard'});
assert.equal(options.length,3);
assert.equal(options[1][0],'1280x720@15:standard');
assert.match(options[2][1],/1280×720 \/ 15fps/);
assert.equal(context.videoPresetSettings(options[2][0]).quality,'high');
assert.equal(context.videoPresetSettings('broken'),null);
const custom=context.videoPresetOptions(hardware,{width:800,height:600,fps:20,quality:'standard'});
assert.equal(custom[3][0],'800x600@20:standard');
assert.match(custom[3][1],/現在の設定/);
console.log('Storage capacity and unified recording quality: 13 assertions passed');

const sd={kind:'microSD',persistent_id:'sd',total_bytes:16e9,available_bytes:6e9,selected:true};
const usb={kind:'USB',persistent_id:'usb',total_bytes:8e9,available_bytes:0.7e9,selected:false,read_only:true};
const volumes=context.connectedStorageVolumes([usb,sd],{...sd,available_bytes:5.9e9});
assert.equal(volumes.length,2);
assert.equal(volumes[0].kind,'microSD');
assert.equal(volumes[0].available_bytes,5.9e9);
assert.equal(volumes[1].read_only,true);
assert.equal(context.connectedStorageVolumes([sd,usb],usb)[0].kind,'USB');
assert.equal(context.connectedStorageVolumes(null,sd).length,1);
assert.equal(sd.available_bytes,6e9);
console.log('Connected volumes and current recording destination: 7 assertions passed');

const minute = Date.parse('2026-10-25T00:00:00Z');
const clips = Array.from({length: 2880}, (_, i) => ({
  recording_id: String(i), start_utc_ns: (minute + i*60_000 + 50)*1e6,
  end_utc_ns: (minute + (i+1)*60_000 - 50)*1e6,
}));
assert.equal(context.recordingAtTime(clips, [minute + 2000*60_000]).item.recording_id, '2000');
assert.equal(context.recordingAtTime(clips, [minute]).exact, true);
assert.equal(context.recordingAtTime([], [minute]), null);
assert.equal(context.recordingAtTime(clips, []), null);
const gap = context.recordingAtTime([clips[0], clips[10]], [minute + 8*60_000]);
assert.equal(gap.exact, false);
assert.equal(gap.item.recording_id, '10');
const crossing = {recording_id:'midnight', start_utc_ns:(minute-60_000)*1e6, end_utc_ns:(minute+60_000)*1e6};
assert.equal(context.recordingAtTime([crossing], [minute]).exact, true);
const jump={recording_id:'jump',spans:[{start_utc_ns:minute*1e6,end_utc_ns:(minute+60_000)*1e6},{start_utc_ns:(minute+3600_000)*1e6,end_utc_ns:(minute+3660_000)*1e6}]};
assert.equal(context.recordingAtTime([jump], [minute+1800_000]).exact, false);
assert.equal(context.recordingAtTime([jump], [minute+3600_000]).exact, true);
assert.match(context.humanError('settings_apply_failed_rolled_back'), /変更前の設定に戻しました/);
console.log('Time lookup, 2880 clips, missing intervals and clock jumps: 10 assertions passed');
