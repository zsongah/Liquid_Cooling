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
  'updateConfigActions','markDirty','applySceneJson','discardSceneJson','isAnalysisScene',
  'relationLevels','parallelEdgeOffsets','renderAnalysis','clearAnalysis','runHydraulicAnalysis','renderBasicForm',
  'renderAssetForm','updateRunOptions','renderForecast','forecastPayload'];
const startup = '  initialize();\n})();';
assert.ok(source.includes(startup), 'VM harness must remove only app startup');
function harness(fetchImpl) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, value:'', innerHTML:'', textContent:'', disabled:false, hidden:false,
      style:{}, dataset:{}, classList:{toggle(){}}, listeners:{}, options:[],selectedOptions:[],
      querySelectorAll(){return [];},
      addEventListener(type,callback){this.listeners[type]=callback;}
    });
    return elements.get(id);
  }
  const context = vm.createContext({document:{getElementById:element},setTimeout,clearTimeout,AbortController,fetch:fetchImpl||(()=>{throw new Error('Unexpected network call');})});
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


function setupAnalysis(h,status='published',id='analysis-config'){
  const scene={schema_version:'0.2',scene_id:'hydraulic-scene',assets:[{id:'CDU_X',kind:'cdu'},{id:'RACK_X',kind:'rack',parent_id:'CDU_X'}],hydraulics:{junctions:[{id:'j0',circuit_id:'c1'},{id:'j1',circuit_id:'c1'}],elements:[{id:'e1',from_junction:'j0',to_junction:'j1',kind:'pipe'}]},branches:[{id:'branch1'}],control_domains:[{id:'domain1',member_asset_ids:['CDU_X','RACK_X'],circuit_ids:['c1'],branch_ids:['branch1']}]};
  const record={id,name:'Hydraulic configuration',revision:2,status,scene,validation:{valid:true}};
  Object.assign(h.state,{topologyConfig:record,topologyConfigId:id,topologyLoading:false,analysisDomainId:'domain1',config:record,scene,original:scene,originalName:record.name,topology:{nodes:scene.assets,edges:[]},selectedAsset:'CDU_X'});
  h.element('topology-source').value='config';return record;
}
function analysisResult(){return {source:'model_estimate',hardware_writes:false,domain_id:'domain1',
  readiness:{status:'ready',reasons:[]},solver:{status:'converged',reason:null,iterations:4,residuals:{continuity_kg_s:1e-8}},
  branches:[{id:'branch1',asset_ref:'RACK_X',estimated_flow_kg_s:2,flow_interval_kg_s:[1.8,2.2],min_flow_kg_s:1.5,max_flow_kg_s:3,status:'satisfied',reason:'within_declared_model_bounds',interval_kind:'conservative_model_bounds',field_safety_verified:false}],
  identifiability:{status:'not_required',reason:'known_parameters'},validation:{status:'out_of_scope',reason:'missing_branch_measurements',field_validated:false}};}

test('schema 0.2 JSON applies without inventing legacy devices or CDU controls',()=>{
  const h=harness();setupDraft(h);const record=setupAnalysis(h,'draft');h.state.jsonPending=true;
  h.applySceneJson(JSON.stringify(record.scene));
  assert.equal(h.isAnalysisScene(h.state.scene),true);assert.equal('devices' in h.state.scene,false);
  assert.equal(h.element('basic-edit-fields').disabled,true);assert.equal(h.element('asset-edit-fields').disabled,true);
  assert.equal(h.element('save-config').disabled,false);
  h.renderBasicForm();h.renderAssetForm();
  assert.match(h.element('config-form').innerHTML,/高级 JSON/);
  assert.match(h.element('asset-form').innerHTML,/不写入现场/);
  assert.doesNotMatch(h.element('config-form').innerHTML,/<input|<select/);
});

test('relationship layout supports deep ownership and finite hydraulic cycles',()=>{
  const h=harness(),nodes=Array.from({length:120},(_,i)=>({id:'n'+i,kind:'manifold',...(i?{parent_id:'n'+(i-1)}:{})}));
  const levels=h.relationLevels(nodes,[]);assert.equal(levels.size,120);assert.equal(levels.get('n119'),119);
  const cycle=h.relationLevels([{id:'a'},{id:'b'},{id:'c'},{id:'island'}],[{source:'a',target:'b',kind:'fluid'},{source:'b',target:'c',kind:'fluid'},{source:'c',target:'a',kind:'fluid'}]);
  assert.equal(cycle.size,4);assert.ok([...cycle.values()].every(Number.isFinite));assert.ok(Math.max(...cycle.values())<4);
});

test('analysis configuration hides execution actions and never exposes previous CDU telemetry',()=>{
  const h=harness();setupRuns(h);const record=setupAnalysis(h);
  h.state.runConfig=record;h.updateRunOptions();assert.equal(h.element('start-run').disabled,true);
  assert.equal(h.element('start-run').hidden,true);assert.equal(h.element('run-mode').disabled,true);
  assert.match(h.element('run-permission-note').textContent,/只读水力分析/);
  assert.equal(h.assetRecord('CDU_A'),null);h.renderMetrics(h.state.topology);h.renderDevice();h.renderQuickPanels();
  assert.doesNotMatch(h.element('overview-metrics').innerHTML,/9.75|1.25/);
  assert.match(h.element('device-detail').innerHTML,/未接入实测/);
  assert.match(h.element('topology-quick-panels').innerHTML,/尚未实现/);
});

test('analysis renders model intervals, unknown validation and no field-safety claim',()=>{
  const h=harness();setupAnalysis(h);h.renderAnalysis();h.state.analysisResult=analysisResult();h.renderAnalysis();
  const result=h.element('analysis-result').innerHTML;
  assert.match(result,/模型估计/);assert.match(result,/数值收敛/);assert.match(result,/1.8 – 2.2/);
  assert.match(result,/本任务无需辨识/);assert.match(result,/当前不具备判定条件/);
  assert.match(result,/声明误差范围内的保守模型区间/);assert.match(result,/不能替代现场热安全验收/);
  assert.match(result,/最高 3/);assert.doesNotMatch(result,/现场安全已验证|设备运行正常/);
});

test('numerical failure and missing interval remain explicit rather than safe estimates',()=>{
  const h=harness();setupAnalysis(h);h.renderAnalysis();const data=analysisResult();
  data.solver={status:'failed',reason:'iteration_limit',iterations:30,residuals:{}};
  Object.assign(data.branches[0],{estimated_flow_kg_s:null,flow_interval_kg_s:null,status:'unknown',reason:'no_current_solution'});
  h.state.analysisResult=data;h.renderAnalysis();const result=h.element('analysis-result').innerHTML;
  assert.match(result,/数值求解失败/);assert.match(result,/未获得估计/);assert.match(result,/无有效区间/);assert.match(result,/无法判定/);
  assert.doesNotMatch(result,/<svg class="analysis-interval"/);
});

test('analysis refuses response lacking model-only no-write contract',()=>{
  const h=harness();setupAnalysis(h);h.renderAnalysis();h.state.analysisResult={...analysisResult(),hardware_writes:true};h.renderAnalysis();
  assert.match(h.element('analysis-result').innerHTML,/缺少只读模型来源声明/);
  assert.doesNotMatch(h.element('analysis-result').innerHTML,/branch1/);
});

test('configuration and domain changes clear previous analysis results',()=>{
  const h=harness();setupAnalysis(h);h.renderAnalysis();h.state.analysisResult=analysisResult();h.renderAnalysis();
  setupAnalysis(h,'published','different-config');h.renderAnalysis();assert.equal(h.state.analysisResult,null);
  assert.doesNotMatch(h.element('analysis-result').innerHTML,/1.8 – 2.2/);
  h.state.topologyConfig.scene.control_domains.push({id:'domain2',branch_ids:[]});h.state.analysisResult=analysisResult();
  h.state.analysisDomainId='domain2';h.renderAnalysis();assert.equal(h.state.analysisResult,null);
});

test('only published analysis configuration can request a read-only calculation',async()=>{
  const h=harness();setupAnalysis(h,'draft');h.renderAnalysis();assert.equal(h.element('run-analysis').disabled,true);
  await assert.rejects(h.runHydraulicAnalysis(),/已发布/);
});

test('analysis sends only analysis API with CSRF and ignores late response after config switch',async()=>{
  let release;const calls=[];const h=harness((url,options)=>{calls.push({url,options});return new Promise(resolve=>{release=resolve;});});
  setupAnalysis(h);h.state.session={csrf_token:'test-csrf'};h.renderAnalysis();const pending=h.runHydraulicAnalysis();
  assert.equal(calls.length,1);assert.equal(calls[0].url,'/api/configs/analysis-config/analysis');
  assert.equal(calls[0].options.headers['X-LC-CSRF'],'test-csrf');assert.equal(calls[0].options.method,'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body),{domain_id:'domain1'});
  setupAnalysis(h,'published','new-selection');h.renderAnalysis();
  release({ok:true,json:async()=>({...analysisResult(),config_id:'analysis-config',config_revision:2})});await pending;
  assert.equal(h.state.analysisResult,null);assert.equal(h.state.analysisLoading,false);
  assert.doesNotMatch(h.element('analysis-result').innerHTML,/1.8 – 2.2/);
});

test('analysis rejects a response attributed to another immutable configuration',async()=>{
  const h=harness(async()=>({ok:true,json:async()=>({...analysisResult(),config_id:'wrong-config',config_revision:2})}));setupAnalysis(h);h.renderAnalysis();
  await h.runHydraulicAnalysis();assert.equal(h.state.analysisResult,null);
  assert.match(h.element('analysis-result').innerHTML,/配置版本或控制域不匹配/);
});

test('forecast unsupported state clears previous data and blocks import for schema 0.2',()=>{
  const h=harness();setupAnalysis(h);h.state.forecastConfigId='analysis-config';
  h.state.forecastDescriptor={config_id:'analysis-config',status:'unsupported'};
  h.state.forecastState={config_id:'analysis-config',status:'unsupported',reason:'analysis_schema_forecast_unsupported'};
  h.element('forecast-chart').innerHTML='old fake available preview';h.renderForecast();
  assert.equal(h.element('forecast-import').disabled,true);assert.equal(h.element('forecast-validate').disabled,true);
  assert.equal(h.element('forecast-file').disabled,true);assert.equal(h.element('forecast-job').disabled,true);
  assert.match(h.element('forecast-chart').innerHTML,/尚未实现支路功率预测分配/);
  assert.doesNotMatch(h.element('forecast-chart').innerHTML,/old fake/);assert.equal(h.element('forecast-metrics').innerHTML,'');
  assert.throws(()=>h.forecastPayload(),/不支持功率预测分配/);
});


test('parallel hydraulic paths remain separate without changing source and target',()=>{
  const h=harness(),edges=[{id:'element:a',element_id:'a',source:'s',target:'r'},{id:'element:b',element_id:'b',source:'s',target:'r'},{id:'contains:asset',source:'parent',target:'child'}];
  const before=JSON.stringify(edges),offsets=h.parallelEdgeOffsets(edges);
  assert.notEqual(offsets.get('element:a'),offsets.get('element:b'));
  assert.equal(offsets.get('element:a')+offsets.get('element:b'),0);
  assert.equal(offsets.has('contains:asset'),false);assert.equal(JSON.stringify(edges),before);
});
