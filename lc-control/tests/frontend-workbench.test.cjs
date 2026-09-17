'use strict';
// Run explicitly: node --test tests/frontend-workbench.test.cjs
// No browser or production dependencies. The VM exposes the actual app's
// functions after replacing only its automatic startup call.
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const webRoot = path.join(__dirname, '..', 'lc_control', 'web');
const source = fs.readFileSync(path.join(webRoot, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(webRoot, 'index.html'), 'utf8');
const exposed = ['state','assetRecord','telemetryFor','selectedDomainStatus',
  'assetRecordLabel','renderMetrics','renderDevice','renderQuickPanels',
  'updateConfigActions','markDirty','applySceneJson','discardSceneJson'];
const startup = '  initialize();\n})();';
assert.ok(source.includes(startup), 'VM harness must remove only app startup');
function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, value:'', innerHTML:'', textContent:'', disabled:false, hidden:false,
      style:{}, dataset:{}, classList:{toggle(){}}, listeners:{},
      querySelectorAll(){return [];},
      addEventListener(type,callback){this.listeners[type]=callback;}
    });
    return elements.get(id);
  }
  const context = vm.createContext({document:{getElementById:element},setTimeout,clearTimeout});
  vm.runInContext(source.replace(startup, `  globalThis.workbench = {${exposed.join(',')}};\n})();`),context);
  return {...context.workbench, element};
}
function setupDraft(h) {
  Object.assign(h.state, {config:{id:'draft-1',name:'Draft',status:'draft',revision:1},
    originalName:'Draft', scene:{scene_id:'before',assets:[],control_domains:[],devices:{}},
    original:{scene_id:'before',assets:[],control_domains:[],devices:{}},
    dirty:false,jsonPending:false,validation:{valid:true}});
  h.element('scene-json').value=JSON.stringify(h.state.scene,null,2);
}
function reading(value,unit='kg/s'){return {value,unit,quality:'good',provenance:'test_record'};}
function sample(asset_id,flow,timestamp){return {asset_id,timestamp,operating_mode:'control',
  owner:'lc-control',observations:{'cdu.sec_flow':reading(flow)},setpoints:{},alarms:[]};}
function event(payload){return {at:payload.timestamp||0,payload};}
function setupRuns(h) {
  h.element('topology-source').value='run';
  const scene={control_domains:[{id:'DOMAIN_A',cdu_id:'CDU_A'},{id:'DOMAIN_B',cdu_id:'CDU_B'}],
    assets:[{id:'CDU_A',kind:'cdu'},{id:'CDU_B',kind:'cdu'}],devices:{}};
  const old=sample('CDU_A',1.25,10),newA=sample('CDU_A',9.75,90),other=sample('CDU_B',2.5,80);
  const oldDecision={asset_id:'CDU_A',timestamp:10,mode:'control',demand_flow_kg_s:1.25,
    target_flow_kg_s:1.25,limited_control_target:20,policy_quantity:'cdu.dp_sp'};
  const newerDecision={...oldDecision,timestamp:90,demand_flow_kg_s:9.75};
  Object.assign(h.state,{runId:'run-older',run:{id:'run-older',source:'synthetic_thermal',active:false,
    scene,job:{config_id:'config-shared',domain_id:'DOMAIN_A',status:'completed'},
    latest:{telemetry:event(old),decision:event(oldDecision)}},events:{},selectedAsset:'CDU_A',
    topology:{nodes:scene.assets,edges:[]},siteConfigId:'config-shared',
    site:{telemetry_by_asset:{CDU_A:newA,CDU_B:other},coverage:{configured_domains:2,observed_domains:2},
      domains:[{cdu_id:'CDU_A',domain_id:'DOMAIN_A',selected_run_id:'run-newer',source:'modbus_network_unverified',
        active:true,is_historical:false,measurement_timestamp:90,latest_decision:newerDecision},
      {cdu_id:'CDU_B',domain_id:'DOMAIN_B',selected_run_id:'run-other',source:'field',active:true,
        is_historical:false,measurement_timestamp:80,latest_decision:null}]},
    catalog:{configs:[],runs:[{id:'run-older'},{id:'run-newer'},{id:'run-other'}],jobs:[]}});
  return {old,newA,other,oldDecision};
}

test('pending advanced JSON locks both forms and cannot be overwritten by markDirty',()=>{
  const h=harness();setupDraft(h);
  const pending='{"unsaved_advanced_edit":true}';
  h.element('scene-json').value=pending;h.state.jsonPending=true;h.updateConfigActions();
  assert.equal(h.element('basic-edit-fields').disabled,true);
  assert.equal(h.element('asset-edit-fields').disabled,true);
  assert.equal(h.element('json-pending-notice').hidden,false);
  assert.equal(h.element('discard-json').disabled,false);
  assert.equal(h.element('save-config').disabled,true);
  assert.equal(h.element('publish-config').disabled,true);
  assert.throws(()=>h.markDirty(),/应用或放弃/);
  assert.equal(h.element('scene-json').value,pending);
  assert.equal(h.state.jsonPending,true);
  assert.equal(h.state.dirty,false);
  // Native fieldsets block every generated input/button without disabling tabs.
  assert.match(html,/<fieldset id="basic-edit-fields"[^>]*><div id="config-form">/);
  assert.match(html,/<fieldset id="asset-edit-fields"[^>]*><div id="asset-form">/);
  assert.doesNotMatch(html,/<button[^>]*data-config-tab[^>]*disabled/);
});

test('explicit JSON apply unlocks forms and records the unsaved scene',()=>{
  const h=harness();setupDraft(h);h.state.jsonPending=true;
  h.applySceneJson(JSON.stringify({...h.state.scene,scene_id:'applied'}));
  assert.equal(h.state.scene.scene_id,'applied');
  assert.equal(h.state.dirty,true);assert.equal(h.state.jsonPending,false);
  assert.equal(h.element('basic-edit-fields').disabled,false);
  assert.equal(h.element('asset-edit-fields').disabled,false);
  assert.equal(h.element('json-pending-notice').hidden,true);
  assert.equal(h.element('discard-json').disabled,true);
  assert.equal(JSON.parse(h.element('scene-json').value).scene_id,'applied');
  assert.equal(h.element('publish-config').disabled,true);
});

test('failed JSON application preserves both pending text and previous scene',()=>{
  const h=harness();setupDraft(h);h.state.jsonPending=true;
  const scene=h.state.scene, text='{"assets":true}';h.element('scene-json').value=text;
  assert.throws(()=>h.applySceneJson(text),/对象数组/);
  assert.equal(h.state.scene,scene);assert.equal(h.state.jsonPending,true);
  assert.equal(h.element('scene-json').value,text);
  assert.throws(()=>h.applySceneJson('{'),/JSON|position|property/);
  assert.equal(h.state.scene,scene);
});

test('explicit discard removes only pending text, preserving earlier unsaved form edits',()=>{
  const h=harness();setupDraft(h);h.state.scene.scene_id='already-edited';h.state.dirty=true;
  h.state.jsonPending=true;h.element('scene-json').value='unapplied text';h.discardSceneJson();
  assert.equal(h.state.scene.scene_id,'already-edited');assert.equal(h.state.dirty,true);
  assert.equal(h.state.jsonPending,false);
  assert.equal(JSON.parse(h.element('scene-json').value).scene_id,'already-edited');
  assert.equal(h.element('basic-edit-fields').disabled,false);
  assert.equal(h.element('asset-edit-fields').disabled,false);
});

test('selected historical CDU uses its own telemetry, source and decision',()=>{
  const h=harness(),r=setupRuns(h),record=h.assetRecord('CDU_A');
  assert.equal(record.telemetry,r.old);assert.equal(record.latest_decision,r.oldDecision);
  assert.equal(record.selected_run_id,'run-older');assert.equal(record.source,'synthetic_thermal');
  assert.equal(record.is_historical,true);assert.equal(record.active,false);
  assert.equal(h.telemetryFor('CDU_A'),r.old);
  assert.equal(h.selectedDomainStatus('CDU_A').selected_run_id,'run-older');
  assert.match(h.assetRecordLabel(record),/所选运行.*older.*仿真数据.*历史.*10 s/);
});

test('no telemetry or decision in selected run never falls back to newer same-CDU data',()=>{
  const h=harness();setupRuns(h);h.state.run.latest={};
  const record=h.assetRecord('CDU_A');
  assert.equal(record.telemetry,null);assert.equal(record.latest_decision,null);
  assert.equal(record.selected_run_id,'run-older');assert.equal(h.telemetryFor('CDU_A'),null);
  h.renderQuickPanels();assert.match(h.element('topology-quick-panels').innerHTML,/暂无对应决策/);
});

test('other CDU keeps its independent site record with explicit run and timestamp',()=>{
  const h=harness(),r=setupRuns(h),record=h.assetRecord('CDU_B');
  assert.equal(record.telemetry,r.other);assert.equal(record.selected_run_id,'run-other');
  assert.match(h.assetRecordLabel(record),/配置各域最新记录.*other.*设备运行.*80 s/);
  assert.equal(record.is_historical,false);assert.equal(record.active,true);
});

test('config preview still uses latest-per-domain records and rejects stale config context',()=>{
  const h=harness(),r=setupRuns(h);h.element('topology-source').value='config';
  h.state.topologyConfig={id:'config-shared',scene:h.state.run.scene};
  assert.equal(h.telemetryFor('CDU_A'),r.newA);
  assert.equal(h.assetRecord('CDU_A').selected_run_id,'run-newer');
  h.state.topologyConfig.id='another-config';assert.equal(h.telemetryFor('CDU_A'),null);
});

test('selected metrics, device labels and quick-decision link agree on the older run',()=>{
  const h=harness();setupRuns(h);h.renderMetrics(h.state.topology);h.renderDevice();h.renderQuickPanels();
  const metrics=h.element('overview-metrics').innerHTML,device=h.element('device-detail').innerHTML;
  assert.match(metrics,/1.25/);assert.doesNotMatch(metrics,/9.75/);
  assert.match(metrics,/所选运行.*older/);
  assert.match(device,/历史采集记录/);assert.match(device,/仿真数据/);
  assert.match(device,/所选运行.*older.*10 s/);assert.doesNotMatch(device,/网络采集/);
  const quick=h.element('topology-quick-panels').innerHTML.split('所选设备的最近决策')[1];
  assert.match(quick,/1.25/);assert.doesNotMatch(quick,/9.75/);assert.match(quick,/所选运行.*older/);
  // The site's newer domain table remains clearly labelled as a separate view.
  assert.match(h.element('topology-quick-panels').innerHTML,/配置各域最新记录/);
  assert.match(h.element('topology-quick-panels').innerHTML,/90 s/);
});

test('wrong-asset payload is not displayed under selected CDU identity',()=>{
  const h=harness();setupRuns(h);
  h.state.run.latest.telemetry=event(sample('WRONG_CDU',99,10));
  h.state.run.latest.decision=event({asset_id:'WRONG_CDU',demand_flow_kg_s:99});
  assert.equal(h.telemetryFor('CDU_A'),null);assert.equal(h.assetRecord('CDU_A').latest_decision,null);
});
