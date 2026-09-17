/* LC Control browser workbench.
 * This client never connects directly to a controller. All mutations use the
 * same-origin API and its CSRF token; configuration saves do not execute jobs.
 * Data is displayed only when returned by the API. Missing sensor data stays
 * missing, and simulation / history are never promoted to field telemetry.
 */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const isNumber = (v) => typeof v === 'number' && Number.isFinite(v);
  const state = {session:null,catalog:{configs:[],runs:[],jobs:[]},run:null,runId:'',runLoading:null,events:{},config:null,scene:null,original:null,dirty:false,jsonPending:false,validation:null,domainId:'',selectedAsset:null,topology:null,layout:{},topologySource:'run',topologyConfig:null,topologyConfigId:'',topologySerial:0,topologyLoading:false,page:'topology',decisionId:'',follow:true,loadSerial:0,polling:false,configSerial:0,runConfig:null,runConfigSerial:0,zoom:'fit',site:null,siteConfigId:'',siteSerial:0,forecastDescriptor:null,forecastState:null,forecastConfigId:'',forecastSerial:0,analysisResult:null,analysisKey:"",analysisDomainId:"",analysisSerial:0,analysisLoading:false,analysisError:""};
  const labels = {'cdu.sec_supply_temp':'二次供液温度','cdu.sec_return_temp':'二次回液温度','cdu.sec_flow':'二次实际流量','cdu.sec_dp':'二次实际压差','cdu.liquid_load':'液侧热负荷','cdu.electric_power':'CDU 电功率','cdu.pump_power':'CDU 泵功率','cdu.sec_supply_temp_sp':'供液温度设定','cdu.dp_sp':'压差设定','cdu.sec_flow_sp':'流量设定','cdu.pump_speed_rel':'泵速设定'};
  const kindLabels = {hydraulic_junction:'水力连接点',facility:'设施',room:'机房',row:'机柜列',server:'服务器',facility_water:'设施水侧',room_air:'空气热汇',cdu:'CDU',rack:'机柜',node:'计算节点',manifold:'分配总管',pump:'泵',valve:'阀',sensor:'测点',chiller:'冷机',dry_cooler:'干冷器',cooling_tower:'冷却塔',heat_exchanger:'换热器'};
  const statusLabels = {temperature_dp:'温度＋压差',temperature_flow:'温度＋流量',local_auto:'设备本地自动',setpoint_confirmed:'设定值已回读',shadow:'影子计算，未写入',shadow_only:'影子计算，未写入',rejected:'网关拒绝',uncertain:'写入结果不确定',write_uncertain:'写入结果不确定',not_verified:'过程响应未验证',promoted:'候选参数已晋升',collecting:'正在积累合格样本',frozen:'校准冻结',candidate_only:'候选已通过，本轮不晋升',running:'运行中',starting:'启动中',stopping:'正在请求退出',stop_requested:'已请求退出',completed:'已完成',stopped:'已退出',failed:'运行失败',control:'闭环控制',monitor:'只读监测',paused:'已暂停',published:'已发布',draft:'草稿',template:'配置模板'};
  const warningLabels = {forecast_absent:'未提供预测',forecast_used:'使用有效负荷预测',forecast_rejected:'预测输入未通过校验',return_soft_limit_risk:'回液预测接近软限值',flow_capacity_saturated:'需求超过流量上限',control_capacity_saturated:'控制能力达到上限',control_below_minimum:'候选低于控制下限',feedback_command_clipped:'候选已限幅',control_rate_limited:'目标已限制单步变化',minimum_interval_not_met:'尚未达到最小动作间隔',inner_loop_not_settled:'内环尚未稳定',insufficient_excitation:'辨识激励不足',actuator_at_limit:'执行器处于边界',unsafe_or_unqualified_data:'数据或状态不满足校准条件',transient:'处于动态过程',slow_calibration_cadence:'等待校准节拍',requires_commissioning:'需要现场验收',simulation_only:'仅仿真验证',coordinator_required:'需要共享域协调器'};
  // 后端原因码保持稳定，界面单独提供中文说明。冒号后的资产、端口或字段
  // 标识原样保留；未知原因码也原样显示，避免将诊断信息隐藏成笼统提示。
  const validationLabels = {
    analysis_outputs_are_estimates_not_branch_telemetry:'分析输出是模型估计，不是支路实测。',
    pressure_boundary_is_not_a_cdu_setpoint:'压力边界是模型输入，不是 CDU 设定值。',
    schema_0_2_analysis_only_hardware_execution_blocked:'Schema 0.2 仅支持只读分析，不能写入硬件。',
    static_inspection_does_not_verify_hardware_or_site_safety:'本次只检查配置；尚未验证设备连通性或现场安全。',
    rack_and_node_assets_do_not_create_branch_measurements_or_models:'添加机柜和节点后，仍需单独接入测点和模型。',
    service_relations_are_not_verified_physical_pipes:'当前连线表示供冷服务关系，不代表已核验的实际管路。',
    physical_graph_is_display_and_validation_only_no_network_solver:'物理连接图用于展示与配置检查，当前不进行管网求解。',
    adapter_not_implemented:'当前版本尚未实现所选设备驱动。',
    policy_not_implemented:'当前版本尚未实现所选控制策略。',
    automatic_policy_not_configured:'未配置自动控制策略。',
    required_observations_missing_or_unit_mismatch:'控制所需测点缺失，或其单位不匹配。',
    no_controls_declared:'尚未声明可写控制量。',
    policy_control_not_advertised:'策略使用的控制量未包含在设备能力中。',
    shared_hydraulics_coordinator_not_implemented:'共享水路需要协调器，当前版本尚未实现。',
    scene_validation_failed:'场景配置未通过校验。',
    hardware_control_requires_runtime_commissioning_and_authorization:'设备闭环控制还需现场验收，并在服务端明确授权。',
    dedicated_fmu_master_required:'此 FMU 必须由专用仿真主程序统一推进，请使用独立实验入口。',
    scene_mapping_required:'场景必须是 JSON 对象。',
    malformed_topology_structure:'拓扑字段结构不完整或类型错误。',
    malformed_scene_structure:'场景字段结构错误',
    malformed_scene_projection:'无法根据当前场景生成拓扑与能力报告。',
    physical_topology_mapping_required:'物理拓扑必须是 JSON 对象。',
    nonfinite_number:'数值必须是有限数字',
    asset_id_and_kind_required:'每个资产都需要有效的 ID 和类型。',
    unknown_parent:'资产引用了不存在的上级资产',
    node_requires_rack_parent:'计算节点必须归属于一个机柜',
    asset_parent_cycle:'资产归属关系存在循环',
    duplicate_load_assignment_requires_coordinator:'同一机柜分配给多个域，需要明确共享关系与协调器',
    physical_topology_evidence_required:'请填写物理连接的证据类型：示意、现场声明或已核验。',
    verified_topology_evidence_reference_required:'标记为已核验的连接，必须提供核验证据引用。',
    physical_ports_required:'物理拓扑需要非空的端口列表。',
    physical_connections_required:'物理拓扑需要连接列表。',
    duplicate_or_invalid_port_id:'端口 ID 缺失、格式无效或重复。',
    unknown_port_asset:'端口所属资产不存在',
    invalid_port_side:'端口侧别须为一次侧或二次侧',
    invalid_port_direction:'端口用途须为供液或回液',
    port_loop_required:'端口缺少所属回路 ID',
    duplicate_or_invalid_connection_id:'连接 ID 缺失、格式无效或重复。',
    unknown_connection_port:'连接引用了不存在的端口',
    fluid_self_connection_not_supported:'当前不支持同一资产内部的流体自连接',
    duplicate_fluid_connection:'同一端口对存在重复流体连接',
    primary_secondary_fluid_cross_connection:'一次侧与二次侧不能通过流体连接直接相通',
    connection_loop_mismatch:'连接两端的回路 ID 不一致',
    supply_return_short_circuit:'连接两端的供回用途不一致，形成供回短接',
    supply_return_pair_required:'资产间需要分别声明供液与回液连接',
    unconnected_physical_port:'已声明的物理端口尚未连接',
    shared_secondary_connection_requires_coordinator:'多台 CDU 共用二次水路，需要共享域协调器',
    served_rack_not_connected_to_secondary:'服务机柜未接入该 CDU 的二次回路',
    declared_heat_sink_not_connected_to_primary:'所声明热汇未连接到 CDU 一次回路',
    measurement_asset_mismatch:'测点绑定的资产不存在或与所属设备不一致',
    measurement_port_mismatch:'测点绑定的端口不存在或归属不一致',
    measurement_side_mismatch:'测点侧别与绑定端口不一致',
    unknown_layout_asset:'布局引用了不存在的资产',
    invalid_layout_position:'资产布局坐标无效',
    unsupported_schema_version:'当前版本不支持此配置格式。',
    topology_version_required:'请填写拓扑版本。',
    duplicate_asset_id:'资产 ID 不可重复。',
    cdu_required:'场景至少需要一个 CDU 资产。',
    device_asset_mismatch:'CDU 资产与设备 Profile 未一一对应。',
    duplicate_domain_id:'控制域 ID 不可重复。',
    one_domain_per_cdu_required:'当前执行器要求每台 CDU 对应一个控制域。',
    owner_required:'控制域缺少控制者标识。',
    shared_hydraulics_unknown:'请明确此控制域是否共享水路。',
    unknown_heat_sink:'控制域引用的热汇不存在。',
    invalid_heat_sink_type:'热汇类型应为设施水侧或空气热汇。',
    unsupported_device_type:'当前版本不支持此设备类型。',
    heat_path_mismatch:'设备类型与热汇路径不匹配。',
    duplicate_served_rack:'同一控制域的服务机柜列表存在重复项。',
    unknown_rack:'控制域引用的机柜不存在或资产类型不正确。',
    invalid_control_bounds:'控制量上下限无效。',
    invalid_policy_flow_bounds:'策略最低流量必须小于最高流量。',
    invalid_policy_parameter:'策略参数缺失或无效',
    control_unit_mismatch:'控制量单位不匹配。',
    guard_point_or_unit_missing:'保护条件所需测点缺失或单位不匹配。'
  };
  function human(value) {
    const raw = String(value ?? '—');
    const lookup = key => statusLabels[key] || warningLabels[key] || validationLabels[key];
    if (lookup(raw)) return lookup(raw);
    // 兼容 validate_scene 将多个原因码用分号串联的旧记录。
    if (raw.includes(';')) return raw.split(';').map(human).join('；');
    const colon = raw.indexOf(':');
    if (colon > 0 && lookup(raw.slice(0, colon))) {
      return `${lookup(raw.slice(0, colon))}：${raw.slice(colon + 1)}`;
    }
    return raw;
  }

  // 所有设备操作经同源后端。浏览器不直连 Modbus，也不保存控制凭据。
  // 超时只表示 HTTP 等待结束；UI 不自动重试可能已经产生副作用的 POST。
  async function api(path, options = {}) {
    const headers = {'Accept':'application/json',...(options.headers||{})};
    if (options.method && options.method !== 'GET') {
      headers['Content-Type']='application/json';
      if (state.session?.csrf_token) headers['X-LC-CSRF']=state.session.csrf_token;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error?.message || data.error?.code || `请求失败 (${response.status})`);
      return data;
    } finally { clearTimeout(timer); }
  }
  const post = (path, body) => api(path,{method:'POST',body:JSON.stringify(body)});
  function errorMessage(error) { return error.name === 'AbortError' ? '服务响应超时，请检查边缘服务状态。' : error.message || String(error); }
  function setConnection(ok,error) {
    $('connection-dot').className=`status-dot ${ok?'good':'error'}`;
    $('connection-status').textContent=ok?'边缘服务已连接':'服务连接异常';
    if (error) {$('global-error').textContent=(ok?'':'服务连接异常，以下仅保留上次取得的记录；当前设备状态未知。 ')+errorMessage(error);$('global-error').hidden=false;}
    else {$('global-error').hidden=true;}
  }
  let toastTimer;
  function toast(message) {$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},4800);}
  async function action(button,fn) {
    if(button?.dataset.busy==='true')return;
    const old=button?.disabled;if(button){button.disabled=true;button.dataset.busy='true';}
    try {await fn();} catch(error) {toast(errorMessage(error));setConnection(true,error);}
    finally {if(button){button.disabled=old;delete button.dataset.busy;}updateConfigActions();if($('forecast-clear'))$('forecast-clear').disabled=!state.forecastState?.input;}
  }
  function confirmAction(title,message) {
    return new Promise(resolve=>{const dialog=$('confirm-dialog');$('confirm-title').textContent=title;$('confirm-message').textContent=message;dialog.returnValue='cancel';dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'),{once:true});dialog.showModal();});
  }
  function sourceInfo(source, active=false) {
    const s=String(source||'').toLowerCase();
    if (/simulation|simulated|synthetic|thermal_sim|fmu|mock/.test(s)) return {label:active?'仿真运行':'仿真数据',tone:'blue'};
    if (/unverified/.test(s)) return {label:active?'网络采集 · 来源待核验':'网络记录 · 来源待核验',tone:'warning'};
    if (/field|hardware|modbus/.test(s)) return {label:active?'设备运行':'设备记录',tone:'teal'};
    if (/history|historical|replay/.test(s)) return {label:'历史记录',tone:'neutral'};
    return {label:source ? String(source) : '未确认来源',tone:'neutral'};
  }
  function badge(text,tone='neutral') {return `<span class="pill ${esc(tone)}">${esc(text)}</span>`;}
  function format(v,digits=2) {return isNumber(v)?v.toLocaleString('zh-CN',{maximumFractionDigits:digits}):'—';}
  function readingValue(reading) {
    if (!reading || !isNumber(reading.value)) return {text:'缺测',unit:'',valid:false};
    if (reading.unit==='K') return {text:format(reading.value-273.15),unit:'°C',valid:true};
    if (['W_e','W_th','W'].includes(reading.unit)) return {text:format(reading.value/1000),unit:reading.unit==='W_th'?'kW 热':'kW',valid:true};
    return {text:format(reading.value),unit:reading.unit||'',valid:true};
  }
  function atLabel(at) {if(!isNumber(Number(at)))return '时间未提供';const n=Number(at);return n>1e9?new Date(n*1000).toLocaleString('zh-CN',{hour12:false}):`仿真 t = ${format(n,1)} s`;}
  function itemPayload(item) {return item?.payload||item||{};}
  function eventAt(item) {return Number(item?.at ?? itemPayload(item).timestamp ?? 0);}
  function eventId(item,index=0) {return String(item?.id??`${eventAt(item)}:${index}`);}
  function activeRun() {return !!(state.run?.active || ['running','starting','stopping'].includes(state.run?.job?.status));}
  function selectOptions(select,rows,current,placeholder='暂无可用项') {
    select.innerHTML=rows.length?rows.map(r=>`<option value="${esc(r.value)}">${esc(r.label)}</option>`).join(''):`<option value="">${esc(placeholder)}</option>`;
    if(rows.some(r=>String(r.value)===String(current)))select.value=current;
  }
  function choosePage(page) {
    if(!['topology','decisions','forecast','config','runs'].includes(page))page='topology';
    state.page=page;$('current-page-label').textContent=({topology:'系统总览',decisions:'控制追踪',forecast:'功率预测',config:'配置管理',runs:'运行与验证'})[page];document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id===`page-${page}`));document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
    if(location.hash!==`#${page}`)history.replaceState(null,'',`#${page}`);
    if(page==='runs')renderTrends();if(page==='forecast'&&state.session)action(null,()=>refreshForecast(true));
  }
  function shortRunId(id){const raw=String(id||'');let length=6;while(length<raw.length&&(state.catalog.runs||[]).some(r=>r.id!==id&&String(r.id).endsWith(raw.slice(-length))))length+=4;return raw.slice(-length);}
  function runLabel(run){
    const job=run.job||(state.catalog.jobs||[]).find(j=>j.run_id===run.id),source=sourceInfo(run.source,run.active);
    const domain=run.domain_id||job?.domain_id||'';
    return `[${domain?domain+' · ':''}#${shortRunId(run.id)}] ${run.label||run.id} · ${source.label}${run.active?' · 活动':''}`;
  }
  function forecastJobLabel(job){return `[${job.domain_id?job.domain_id+' · ':''}#${shortRunId(job.run_id||job.id)}] ${job.name||job.id} · ${human(job.status)} · ${format(job.elapsed_s,1)}s`;}

  const isAnalysisScene=scene=>scene?.schema_version==='0.2';
  function configLabel(config) {return `${config.name||config.scene_id||config.id} · ${human(config.status)}${config.revision?' · r'+config.revision:''}`;}

  // 目录刷新只更新列表，不覆盖正在编辑的草稿，更不会因轮询创建运行任务。
  async function refreshCatalog(initial=false) {
    state.catalog=await api('/api/catalog');state.catalog.configs||=[];state.catalog.runs||=[];state.catalog.jobs||=[];
    const runRows=state.catalog.runs.map(r=>({value:r.id,label:runLabel(r)}));
    if(!state.runId || !state.catalog.runs.some(r=>r.id===state.runId)) state.runId=state.catalog.runs.find(r=>r.active)?.id||state.catalog.runs[0]?.id||'';
    ['decision-run','trend-run'].forEach(id=>selectOptions($(id),runRows,state.runId,'暂无运行；可先启动热仿真'));
    const configRows=state.catalog.configs.map(c=>({value:c.id,label:configLabel(c)}));
    if(state.config&&!state.config.id)configRows.unshift({value:'',label:state.config.name+' · 本地未保存草稿'});
    selectOptions($('config-select'),configRows,state.config?.id,'暂无配置');
    updateTopologySelection();
    const prior=$('run-config').value;
    const published=state.catalog.configs.filter(c=>c.status==='published');
    selectOptions($('run-config'),published.map(c=>({value:c.id,label:configLabel(c)})),prior,'先在配置页发布一个配置');
    if($('run-config').value && (initial || $('run-config').value!==state.runConfig?.id))await loadRunConfig();
    renderJobs(state.catalog.jobs);refreshForecastSelectors();
    if(initial && !state.config && state.catalog.configs.length){const pick=state.catalog.configs.find(c=>/workbench_physical/i.test(c.id+' '+c.name))||state.catalog.configs.find(c=>/thermal.*liquid.*liquid/i.test(c.id+' '+c.name))||state.catalog.configs.find(c=>/thermal/i.test(c.id+' '+c.name))||state.catalog.configs[0];await loadConfig(pick.id);}
  }
  function updateTopologySelection() {
    const source=$('topology-source').value;
    const rows=source==='run'?state.catalog.runs.map(r=>({value:r.id,label:runLabel(r)})):state.catalog.configs.map(c=>({value:c.id,label:configLabel(c)}));
    const current=source==='run'?state.runId:state.topologyConfigId||state.topologyConfig?.id||state.config?.id;
    selectOptions($('topology-selection'),rows,current,source==='run'?'暂无运行数据':'暂无配置');
    if(source==='run'&&!rows.length&&state.catalog.configs.length){$('topology-source').value='config';state.topologySource='config';updateTopologySelection();}
  }
  async function loadRun(id=state.runId){
    const serial=++state.loadSerial;
    if(!id){state.run=null;state.runLoading=null;state.events={};renderLiveViews();return;}
    state.runId=id;
    // 切换对象立即撤下旧图。普通同会话轮询保留已有曲线，避免每三秒闪屏。
    if(state.run?.id!==id){
      state.runLoading=id;state.run=null;state.events={};state.decisionId='';
      if($('topology-source').value==='run'){state.site=null;state.siteConfigId='';}
      ['decision-run','trend-run'].forEach(x=>{$(x).value=id;});
      if($('topology-source').value==='run')$('topology-selection').value=id;
      renderLiveViews();
    }
    try{
      const results=await Promise.all([api(`/api/runs/${encodeURIComponent(id)}`),...['telemetry','decision','calibration','fault'].map(kind=>api(`/api/runs/${encodeURIComponent(id)}/events?kind=${kind}&limit=600&tail=1`))]);
      if(serial!==state.loadSerial)return;
      state.runId=id;state.run=results[0];state.runLoading=null;state.events={};
      ['telemetry','decision','calibration','fault'].forEach((kind,i)=>{state.events[kind]=(results[i+1].items||[]).slice().sort((a,b)=>eventAt(a)-eventAt(b));});
      ['decision-run','trend-run'].forEach(x=>{$(x).value=id;});if($('topology-source').value==='run')$('topology-selection').value=id;
      renderLiveViews();
    }catch(error){if(serial===state.loadSerial){state.runLoading=null;renderLiveViews();}throw error;}
  }
  function renderLiveViews() {
    const source=sourceInfo(state.run?.source,activeRun());$('source-badge').className=`pill ${source.tone}`;$('source-badge').textContent=state.runLoading?'正在加载运行':state.run?source.label:'未选择运行数据';
    $('refresh-time').textContent=state.runLoading?'正在获取所选运行…':`最近获取 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
    renderTopology();renderDecisionSelector();renderTrends();renderQuickPanels();
  }
  function latest(kind){return itemPayload(state.run?.latest?.[kind] || state.events[kind]?.at(-1));}
  function selectedScene(){return $('topology-source').value==='config'?(state.topologyConfig?.scene||(state.topologyConfigId===state.config?.id?state.scene:null)):(state.run?.scene||state.run?.session?.scene);}
  // 兼容旧配置：只有 served_racks 时生成服务关系，不把它升级成物理管路。
  function inferTopology(scene) {
    if(!scene)return {kind:'service_relations',nodes:[],edges:[],layout:{}};
    const nodes=(scene.assets||[]).map(a=>({...a,label:a.label||a.name||a.id}));const edges=[];
    for(const d of scene.control_domains||[]){if(d.heat_sink_id)edges.push({id:`${d.id}-sink`,source:d.heat_sink_id,target:d.cdu_id,kind:'heat_dependency',label:'热汇关系'});for(const r of d.served_racks||[])edges.push({id:`${d.id}-${r}`,source:d.cdu_id,target:r,kind:'serves',label:'供冷服务'});}
    return {kind:'service_relations',nodes,edges,layout:scene.extensions?.ui_layout||{}};
  }
  function currentTopology(){const topo=$('topology-source').value==='config'?state.topologyConfig?.validation?.topology:state.run?.topology;return topo?.nodes?topo:inferTopology(selectedScene());}
  function siteContextId(){return $('topology-source').value==='config'?state.topologyConfig?.id:state.run?.job?.config_id;}
  function selectedRunCduId(){
    const run=state.run;if(!run)return null;
    const sample=latest('telemetry'),decision=latest('decision'),domains=run.scene?.control_domains||[];
    const domainId=run.job?.domain_id||sample.domain_id||decision.domain_id;
    return domains.find(d=>d.id===domainId)?.cdu_id||sample.asset_id||decision.asset_id||(domains.length===1?domains[0].cdu_id:null);
  }
  // A run is an immutable source boundary for its own CDU. Missing readings in
  // that run must remain missing even if the site API knows a newer task.
  // Other CDUs may use independent site records; every consumer shares this
  // context so measurement, source badges and decision links cannot disagree.
  function assetRecord(assetId){
    if(!assetId||isAnalysisScene(selectedScene()))return null;
    if($('topology-source').value==='run'&&assetId===selectedRunCduId()){
      const sample=latest('telemetry'),decision=latest('decision'),active=activeRun();
      return {cdu_id:assetId,selected_run_id:state.run.id,source:state.run.source,active,is_historical:!active,record_origin:'selected_run',
        telemetry:sample.asset_id===assetId?sample:null,
        latest_decision:Object.keys(decision).length&&(!decision.asset_id||decision.asset_id===assetId)?decision:null};
    }
    if(!state.site||state.siteConfigId!==siteContextId())return null;
    const domain=state.site.domains?.find(d=>d.cdu_id===assetId);
    if(!domain)return null;
    const raw=state.site.telemetry_by_asset?.[assetId],sample=raw?itemPayload(raw):null;
    const decision=domain.latest_decision?itemPayload(domain.latest_decision):null;
    return {...domain,record_origin:'site',telemetry:sample?.asset_id===assetId?sample:null,
      latest_decision:decision&&(!decision.asset_id||decision.asset_id===assetId)?decision:null};
  }
  function telemetryFor(assetId){return assetRecord(assetId)?.telemetry||null;}
  function selectedDomainStatus(assetId){return assetRecord(assetId);}
  function assetRecordLabel(record){
    if(!record)return '无对应运行记录';
    const origin=record.record_origin==='selected_run'?'所选运行':'配置各域最新记录';
    const run=record.selected_run_id?` #${shortRunId(record.selected_run_id)}`:'';
    const timestamp=record.telemetry?.timestamp;
    return `${origin}${run} · ${sourceInfo(record.source,record.active).label}${record.is_historical?' · 历史':''} · ${isNumber(timestamp)?atLabel(timestamp):'尚无采样时间'}`;
  }
  async function refreshSite(){
    const id=siteContextId(),serial=++state.siteSerial;
    if(!id||isAnalysisScene(selectedScene())){state.site=null;state.siteConfigId='';return;}
    try{const data=await api(`/api/configs/${encodeURIComponent(id)}/site`);if(serial!==state.siteSerial||id!==siteContextId())return;state.site=data;state.siteConfigId=id;renderTopology();renderQuickPanels();}
    catch(error){if(serial===state.siteSerial){state.site=null;state.siteConfigId='';renderTopology();renderQuickPanels();}}
  }
  const iconPaths={
    topology:'M3 5h6v5H3z M15 14h6v5h-6z M3 14h6v5H3z M6 10v4 M6 12h12v2',
    decisions:'M4 5h10 M4 12h16 M4 19h10 M16 3l3 2-3 2 M17 10l3 2-3 2 M16 17l3 2-3 2',
    forecast:'M3 19V5 M3 19h18 M6 15l4-6 4 4 6-9',
    config:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M11 15v6',
    runs:'M3 12h4l3-7 4 14 3-7h4',
    cdu:'M5 3h14v18H5z M8 6h8v5H8z M8 15h3 M14 15h2 M8 18h3 M14 18h2',
    rack:'M5 2h14v20H5z M8 6h6 M16 6h1 M8 11h6 M16 11h1 M8 16h6 M16 16h1',
    node:'M5 5h14v14H5z M8 8h8v8H8z M8 2v3 M13 2v3 M8 19v3 M13 19v3 M2 8h3 M19 8h3 M2 13h3 M19 13h3',
    manifold:'M3 9h18v6H3z M6 4v5 M12 4v5 M18 4v5 M6 15v5 M12 15v5 M18 15v5',
    sink:'M5 5h14v14H5z M8 8h8v8H8z M12 1v4 M12 19v4 M1 12h4 M19 12h4',
    temp:'M10 4a2 2 0 0 1 4 0v10a4 4 0 1 1-4 0z M12 8v8',
    power:'M13 2 5 13h6l-1 9 9-13h-6z'
  };
  function icon(name){return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${iconPaths[name]||iconPaths.sink}"/></svg>`;}
  function renderMetrics(topo){
    if(isAnalysisScene(selectedScene())){
      const scene=selectedScene(),hydraulics=scene.hydraulics||{},rows=[['资产',scene.assets?.length||0,'配置声明'],['水力连接点',hydraulics.junctions?.length||0,'压力节点'],['水力元件',hydraulics.elements?.length||0,'管路 / 阀门 / 水阻'],['控制域',scene.control_domains?.length||0,'只读分析范围'],['支路实测','未接入','无现场或仿真遥测'],['执行能力','不写设备','不创建控制任务']];
      $('overview-metrics').innerHTML=rows.map(([label,value,note])=>`<div class="metric-card"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join('');return;
    }
    const candidate=topo.nodes.find(n=>n.id===state.selectedAsset&&n.kind==='cdu')||topo.nodes.find(n=>n.id===selectedRunCduId()&&$('topology-source').value==='run')||topo.nodes.find(n=>n.kind==='cdu');
    const record=candidate?assetRecord(candidate.id):null,sample=record?.telemetry,obs=sample?.observations||{};
    const flow=readingValue(obs['cdu.sec_flow']),dp=readingValue(obs['cdu.sec_dp']),ts=readingValue(obs['cdu.sec_supply_temp']),tr=readingValue(obs['cdu.sec_return_temp']),power=readingValue(obs['cdu.electric_power']);
    const note=candidate?`${candidate.id} · ${assetRecordLabel(record)}`:'未配置 CDU';
    const metrics=[['CDU / 机柜',`${topo.nodes.filter(n=>n.kind==='cdu').length} / ${topo.nodes.filter(n=>n.kind==='rack').length}`,'',`${topo.nodes.filter(n=>n.kind==='node').length} 个计算节点 · 配置声明`,'cdu'],['二次实际流量',flow.text,flow.unit,note,'runs'],['二次实际压差',dp.text,dp.unit,note,'config'],['供液温度',ts.text,ts.unit,note,'temp'],['回液温度',tr.text,tr.unit,note,'temp'],['CDU 电功率',power.text,power.unit,note+' · 不合计其他记录','power']];
    $('overview-metrics').innerHTML=metrics.map(([label,value,unit,foot,i])=>`<div class="metric-card"><div class="metric-label">${icon(i)}${esc(label)}</div><div class="metric-value">${esc(value)}<small>${esc(unit)}</small></div><div class="metric-note">${esc(foot)}</div></div>`).join('');
  }
  function renderTopology() {
    if(state.dragging||state.topologyLoading)return;
    if(state.runLoading&&$('topology-source').value==='run'){
      state.topology={kind:'service_relations',nodes:[],edges:[],layout:{}};renderMetrics(state.topology);
      $('topology-kind').textContent='正在加载';$('topology-card-title').textContent='正在读取所选运行';$('topology-explanation').textContent='新的运行数据到达前，不显示其他运行的连接与测量。';
      $('topology-canvas').innerHTML='<div class="empty-state"><h3>正在加载所选运行…</h3><p>等待场景、遥测与控制记录。</p></div>';
      $('device-detail').innerHTML='<div class="empty-state">等待所选运行数据</div>';return;
    }
    if($('topology-source').value==='config'){$('source-badge').className='pill neutral';$('source-badge').textContent=state.siteConfigId===siteContextId()&&state.site?.coverage?.observed_domains?'配置关联记录 · 分域来源':'配置预览 · 无遥测';}
    const topo=currentTopology();state.topology=topo;renderAnalysis();
    const physical=topo.kind==='physical';$('topology-kind').textContent=physical?'物理连接声明':'服务关系';$('topology-kind').className=`pill ${physical?'blue':'neutral'}`;
    $('topology-card-title').textContent=physical?'物理连接视图':'供冷服务关系';
    $('topology-explanation').textContent=isAnalysisScene(selectedScene())?'水力连接点 / 元件投影与资产归属；不是自动识别的现场管路':physical?`来自配置的端口连接 · 证据：${({illustrative:'示意配置',declared:'现场声明',verified:'附核验引用'})[topo.evidence]||topo.evidence||'未填写'}`:'由控制域生成；连线不表示已测绘的管路';
    $('save-layout').disabled=isAnalysisScene(selectedScene());$('legend-forward-label').textContent=isAnalysisScene(selectedScene())?'水力元件（配置方向）':'供液';$('legend-return-item').hidden=isAnalysisScene(selectedScene());
    $('topology-boundary').textContent=isAnalysisScene(selectedScene())?'资产归属与水力元件是两种关系。只读分析输出模型估计；未接入支路测点，不生成实测流量、温度或阀位。':physical?'端口连接来自配置声明，不代表水力求解或现场核验已通过。无机柜／节点测点时不生成其温度。':'当前配置只声明供冷服务与热汇关系，尚未提供完整实际管路。没有机柜／节点测点时不生成其温度。';
    const context=`${$('topology-source').value}:${$('topology-source').value==='run'?state.runId:state.topologyConfig?.id||state.config?.id}`;
    if(state.layoutContext!==context){state.layout=clone(topo.layout||{});state.layoutContext=context;state.selectedAsset=null;state.zoom='fit';$('layout-note').textContent=isAnalysisScene(selectedScene())?'拖动只影响本页布局；新版布局暂不保存':'拖动仅调整布局 · 可缩放或横向滚动';}
    if(!topo.nodes.length){renderMetrics(topo);$('topology-canvas').innerHTML='<div class="empty-state"><span class="empty-icon">◇</span><h3>尚无连接数据</h3><p>选择配置预览，或从运行与趋势页启动已发布的仿真配置。</p></div>';renderDevice();return;}
    if(!topo.nodes.some(n=>n.id===state.selectedAsset))state.selectedAsset=topo.nodes.find(n=>$('topology-source').value==='run'&&n.id===selectedRunCduId())?.id||topo.nodes.find(n=>n.kind==='cdu')?.id||topo.nodes[0].id;
    renderMetrics(topo);drawTopology();renderDevice();
  }
  // Layout follows declared relationships, never hard-coded equipment levels.
  // Each node is visited once; loops and return paths cannot create recursion
  // or an endless relaxation. These levels have no hydraulic solver meaning.
  function clearAnalysis(){
    state.analysisSerial++;state.analysisResult=null;state.analysisKey='';state.analysisError='';state.analysisLoading=false;
  }
  function analysisConfig(){
    return $('topology-source').value==='config'&&!state.topologyLoading&&state.topologyConfig?.id===state.topologyConfigId&&isAnalysisScene(state.topologyConfig.scene)?state.topologyConfig:null;
  }
  function analysisContextKey(){const c=analysisConfig();return c?`${c.id}:${c.revision}:${state.analysisDomainId}`:'';}
  function analysisStatus(value){
    const map={model_estimate:'模型估计',ready:'参数已就绪',not_ready:'参数未就绪',converged:'数值收敛',ok:'计算完成',success:'计算完成',solved:'计算完成',numerical_failed:'数值求解失败',numerical_failure:'数值求解失败',failed:'未通过',not_evaluated:'未评估',not_run:'尚未求解',satisfied:'区间满足限值',violated:'区间越限',indeterminate:'区间跨越限值',unknown:'无法判定',missing:'缺失',missing_parameters:'参数缺失',out_of_scope:'当前不具备判定条件',not_required:'本任务无需辨识',pass:'声明范围内通过',fail:'未通过',unsupported:'尚不支持',not_available:'不可用',static_hydraulics:'静态水力分析',conservative_model_bounds:'声明误差范围内的保守模型区间',passive_network_interval_contraction:'被动管网区间收缩',within_declared_model_bounds:'在声明的模型误差范围内满足限值',bounds_cross_limit:'区间跨越限值，或约束信息不足',limit_violated_for_declared_bounds:'声明区间整体越过限值',forward_parameters_given_not_identified:'前向计算使用给定参数，本轮未进行参数辨识',independent_branch_validation_data_required:'缺少独立支路验证数据',local_screen_passed_only:'仅局部灵敏度筛查通过，不证明全局唯一',local_linearized_at_declared_parameters:'声明参数附近的局部线性化',rank_deficient_at_declared_conditions:'声明工况下的灵敏度秩不足',ill_conditioned_sensitivity:'灵敏度病态，参数难以区分',parameter_interval_too_wide:'参数区间超过预设误差预算',declared_holdout_cases_passed:'声明的留出案例通过校核',holdout_error_or_ordering_failed:'留出误差或支路排序未通过',insufficient_independent_evidence:'独立验证证据不足',forward_model_not_ready:'前向模型参数或边界未就绪',no_current_solution:'当前没有有效的数值解',held_out:'独立留出验证',local:'局部',global:'全局',point_estimate_only:'仅点估计',sample_range:'有限样本范围',empirical_quantile:'经验分位区间',verified_enclosure:'经验证的包含区间'};
    return map[value]||human(value||'unknown');
  }
  function analysisReason(value){
    if(value===null||value===undefined)return '未提供';
    if(Array.isArray(value))return value.map(analysisReason).join('；');
    if(typeof value==='object')return analysisReason(value.message||value.reason||value.code||JSON.stringify(value));
    return analysisStatus(String(value));
  }
  function analysisGateSummary(value){
    if(!value)return '<p class="fine-print">未提供判定记录，不能视为通过。</p>';
    const labels={structural:'结构可辨识性',local_screen:'局部灵敏度筛查',practical:'实用可辨识性',holdout:'留出验证',branch_holdout:'支路留出验证',aggregate_holdout:'总量留出验证'};
    const entries=value.status?[[value.scope||'结果',value]]:Object.entries(value).filter(([,v])=>v&&typeof v==='object'&&!Array.isArray(v));
    if(!entries.length)return `<p class="fine-print">${esc(analysisReason(value))}</p>`;
    return entries.map(([key,gate])=>`<div class="reading-row"><span class="label">${esc(labels[key]||analysisStatus(key))}</span><span class="reading">${esc(analysisStatus(gate.status))}<small>${esc(gate.scope?'范围：'+analysisStatus(gate.scope):'')}${gate.reason||gate.reasons?'<br>'+esc(analysisReason(gate.reason||gate.reasons)):''}</small></span></div>`).join('');
  }
  function analysisIntervalSVG(branch,maxValue,minValue=0){
    const range=branch.flow_interval_kg_s,valid=Array.isArray(range)&&range.length===2&&range.every(isNumber)&&range[0]<=range[1];
    const estimate=branch.estimated_flow_kg_s,limit=branch.min_flow_kg_s,maximum=branch.max_flow_kg_s;if(!valid&&!isNumber(estimate))return '';
    const width=190,left=7,right=183,x=v=>left+(v-minValue)/Math.max(maxValue-minValue,.001)*(right-left);
    return `<svg class="analysis-interval" viewBox="0 0 ${width} 35" role="img" aria-label="模型流量${valid?'区间 '+format(range[0])+' 至 '+format(range[1]):'点估计 '+format(estimate)} kg/s；不是实际测量"><line x1="${left}" x2="${right}" y1="17" y2="17" stroke="#3c5667"/>${valid?`<line x1="${x(range[0])}" x2="${x(range[1])}" y1="17" y2="17" stroke="#76a8e5" stroke-width="6" opacity=".7"/><path d="M${x(range[0])} 11V23 M${x(range[1])} 11V23" stroke="#a2c9f3"/>`:''}${isNumber(estimate)?`<circle cx="${x(estimate)}" cy="17" r="3.5" fill="#d6eafa"/>`:''}${isNumber(limit)?`<path d="M${x(limit)} 3V31" stroke="#dfb26d" stroke-dasharray="3 2"/>`:''}${isNumber(maximum)?`<path d="M${x(maximum)} 3V31" stroke="#bd9bdf" stroke-dasharray="3 2"/>`:''}</svg>`;
  }
  function renderAnalysis(){
    const config=analysisConfig(),card=$('hydraulic-analysis');card.hidden=!config;
    if(!config){if(state.analysisKey||state.analysisResult||state.analysisLoading)clearAnalysis();return;}
    const domains=config.scene.control_domains||[];
    if(!domains.some(d=>d.id===state.analysisDomainId))state.analysisDomainId=domains[0]?.id||'';
    selectOptions($('analysis-domain'),domains.map(d=>({value:d.id,label:`${d.id} · ${d.branch_ids?.length||0} 个配置支路`})),state.analysisDomainId,'未声明控制域');
    const key=analysisContextKey();if(state.analysisKey&&state.analysisKey!==key)clearAnalysis();state.analysisKey=key;
    $('run-analysis').disabled=config.status!=='published'||!state.analysisDomainId||state.analysisLoading;
    $('run-analysis').textContent=state.analysisLoading?'正在求解…':'运行只读分析';
    $('analysis-scope').textContent=`${config.name||config.id} · r${config.revision}${config.status!=='published'?' · 请先复制/保存草稿并发布':' · 不读取现场点表，不创建任务'}`;
    if(state.analysisLoading){$('analysis-result').innerHTML='<div class="empty-state"><h3>正在计算所选配置与控制域</h3><p>旧结果已清除，完成后显示模型估计与判定依据。</p></div>';return;}
    if(state.analysisError){$('analysis-result').innerHTML=`<div class="notice error">${esc(state.analysisError)}</div>`;return;}
    const data=state.analysisResult;if(!data){$('analysis-result').innerHTML='<p class="fine-print">分析仅使用配置中给定的模型、参数和边界。没有参数或有效区间时会明确报告缺失与未知，不补造支路实测值。</p>';return;}
    if(data.source!=='model_estimate'||data.hardware_writes!==false){$('analysis-result').innerHTML='<div class="notice error">分析响应缺少只读模型来源声明，未展示为可信分析结果。</div>';return;}
    const solver=data.solver||{},branches=Array.isArray(data.branches)?data.branches:[],readiness=data.readiness||{},uncertainty=data.uncertainty||{};
    const values=branches.flatMap(b=>[b.estimated_flow_kg_s,b.min_flow_kg_s,b.max_flow_kg_s,...(Array.isArray(b.flow_interval_kg_s)?b.flow_interval_kg_s:[])]).filter(isNumber),scale=values.reduce((a,v)=>({min:Math.min(a.min,v),max:Math.max(a.max,v)}),{min:0,max:0});scale.max=Math.max(scale.max,.001)*1.05;
    const residuals=solver.residuals&&typeof solver.residuals==='object'?Object.entries(solver.residuals).map(([k,v])=>`${k}: ${isNumber(v)?format(v,7):analysisReason(v)}`).join('；'):analysisReason(solver.residuals);
    const facts=[['分析来源','模型估计','没有现场采集或设备写入'],['求解状态',solver.status==='failed'?'数值求解失败':analysisStatus(solver.status),analysisReason(solver.reason)],['数值迭代',isNumber(solver.iterations)?String(solver.iterations):'未提供',residuals],['参数就绪',analysisStatus(readiness.status||(readiness.ready===true?'ready':readiness.ready===false?'not_ready':'unknown')),analysisReason(readiness.reasons||readiness.reason)]];
    const intervalKind=uncertainty.interval_kind||data.interval_kind||[...new Set(branches.map(b=>b.interval_kind).filter(Boolean))].map(analysisStatus).join('、')||'未声明';
    $('analysis-result').innerHTML=`<div class="notice info">这些流量来自静态模型，不是设备实测或控制回执。约束结果只适用于声明的参数、边界与区间方法，不能替代现场热安全验收。</div><div class="analysis-facts">${facts.map(([label,value,note])=>`<div class="analysis-fact"><span class="section-label">${esc(label)}</span><strong>${esc(value)}</strong><p class="fine-print">${esc(note)}</p></div>`).join('')}</div><div class="table-wrap"><table class="analysis-table"><thead><tr><th>支路 / 关联资产</th><th>模型流量估计</th><th>估计区间 / kg/s</th><th>流量要求 / kg/s</th><th>约束判定</th></tr></thead><tbody>${branches.length?branches.map(b=>{const interval=b.flow_interval_kg_s,valid=Array.isArray(interval)&&interval.length===2&&interval.every(isNumber)&&interval[0]<=interval[1];return `<tr><td><strong>${esc(b.id)}</strong><div class="fine-print">${esc(b.asset_ref||'未关联资产')}</div></td><td>${isNumber(b.estimated_flow_kg_s)?format(b.estimated_flow_kg_s,4)+' kg/s':'未获得估计'}<div class="fine-print">模型值 · 非实测</div></td><td>${valid?format(interval[0],4)+' – '+format(interval[1],4):'无有效区间'}${analysisIntervalSVG(b,scale.max,scale.min)}</td><td>最低 ${isNumber(b.min_flow_kg_s)?format(b.min_flow_kg_s,4):'未配置'}<div class="fine-print">最高 ${isNumber(b.max_flow_kg_s)?format(b.max_flow_kg_s,4):'未配置'}</div></td><td>${badge(analysisStatus(b.status),b.status==='violated'?'error':b.status==='satisfied'?'blue':'neutral')}<div class="fine-print">${esc(analysisReason(b.reason))}</div></td></tr>`;}).join(''):'<tr><td colspan="5">没有可展示的支路结果；请检查就绪信息与求解原因。</td></tr>'}</tbody></table></div><p class="fine-print">蓝线：后端返回区间；圆点：模型估计；金色虚线：最低流量；紫色虚线：最高流量。区间类型：${esc(analysisStatus(intervalKind))}。有限 Monte Carlo 样本范围不等于严格包络；无区间时不宣称满足最低流量要求。</p><div class="analysis-gates"><section><h3>可辨识性 / 参数依据</h3>${analysisGateSummary(data.identifiability)}<p class="fine-print">局部灵敏度通过不证明参数全局唯一；给定参数的前向计算无需辨识检查。</p></section><section><h3>独立验证范围</h3>${analysisGateSummary(data.validation)}<p class="fine-print">输入数据校核不等于现场验收或控制授权。</p></section></div><details><summary>分析配置、完整结果与能力边界</summary><pre>${esc(JSON.stringify({config_id:config.id,revision:config.revision,domain_id:state.analysisDomainId,result:data},null,2))}</pre></details>`;
  }
  async function runHydraulicAnalysis(){
    const config=analysisConfig();if(!config||config.status!=='published'||!state.analysisDomainId)throw new Error('请选择已发布的 Schema 0.2 配置与控制域。');
    const serial=++state.analysisSerial,key=analysisContextKey();state.analysisKey=key;state.analysisResult=null;state.analysisError='';state.analysisLoading=true;renderAnalysis();
    try{const result=await post(`/api/configs/${encodeURIComponent(config.id)}/analysis`,{domain_id:state.analysisDomainId});if(serial!==state.analysisSerial||key!==analysisContextKey())return;if((result.config_id&&result.config_id!==config.id)||(result.config_revision!==undefined&&result.config_revision!==config.revision)||(result.domain_id&&result.domain_id!==state.analysisDomainId))throw new Error('分析结果的配置版本或控制域不匹配，已拒绝显示。');state.analysisResult=result;}
    catch(error){if(serial===state.analysisSerial&&key===analysisContextKey())state.analysisError=errorMessage(error);}
    finally{if(serial===state.analysisSerial&&key===analysisContextKey()){state.analysisLoading=false;renderAnalysis();}}
  }
  function relationLevels(nodes,edges){
    const ids=new Set(nodes.map(n=>n.id)),links=new Map(nodes.map(n=>[n.id,new Set()])),incoming=new Map(nodes.map(n=>[n.id,0]));
    const add=(a,b)=>{if(!ids.has(a)||!ids.has(b)||a===b||links.get(a).has(b))return;links.get(a).add(b);incoming.set(b,incoming.get(b)+1);};
    nodes.forEach(n=>{if(n.parent_id)add(n.parent_id,n.id);});
    edges.forEach(e=>{if(e.direction!=='return')add(e.source,e.target);});
    const levels=new Map(),walk=(roots)=>{const queue=[];for(const id of roots)if(!levels.has(id)){levels.set(id,0);queue.push(id);}for(let i=0;i<queue.length;i++){const id=queue[i];for(const child of links.get(id))if(!levels.has(child)){levels.set(child,levels.get(id)+1);queue.push(child);}}};
    walk(nodes.filter(n=>incoming.get(n.id)===0).map(n=>n.id));
    for(const n of nodes)if(!levels.has(n.id))walk([n.id]);
    return levels;
  }
  // 画布坐标仅用于可视化；拖拽不会更改端口连接或控制域分配。
  // 缺少显式坐标时按资产层级布局，复杂现场可保存独立的界面布局草稿。
  function parallelEdgeOffsets(edges){
    const groups=new Map(),offsets=new Map();
    for(const edge of edges){if(!edge.element_id)continue;const key=JSON.stringify([edge.source,edge.target].sort());if(!groups.has(key))groups.set(key,[]);groups.get(key).push(edge);}
    for(const group of groups.values()){const step=Math.min(18,60/Math.max(group.length-1,1));group.forEach((edge,i)=>offsets.set(edge.id,(i-(group.length-1)/2)*step));}
    return offsets;
  }
  function drawTopology(){
    if(!state.topology?.nodes?.length)return;
    const {nodes,edges}=state.topology,levelById=relationLevels(nodes,edges),levels=[...new Set(levelById.values())].sort((a,b)=>a-b),positions={};
    const nodeW=178,nodeH=94;
    for(const level of levels){const list=nodes.filter(n=>levelById.get(n.id)===level);list.forEach((n,i)=>{positions[n.id]=state.layout[n.id]||{x:35+levels.indexOf(level)*230,y:65+i*130};});}
    const left=Math.max(0,Math.min(...Object.values(positions).map(p=>p.x))-24),top=Math.max(0,Math.min(...Object.values(positions).map(p=>p.y))-47);
    const width=Math.max(500,...Object.values(positions).map(p=>p.x+nodeW+24-left)),height=Math.max(210,...Object.values(positions).map(p=>p.y+nodeH+26-top));
    state.drawPositions=positions;state.viewBox={width,height,left,top};
    const edgeOffsets=parallelEdgeOffsets(edges);
    const paths=edges.map(edge=>{const a=positions[edge.source],b=positions[edge.target];if(!a||!b)return '';const back=b.x<a.x,returned=edge.direction==='return',relation=state.topology.kind!=='physical'||edge.kind!=='fluid';const offset=edgeOffsets.get(edge.id)||0,x1=a.x+(back?0:nodeW),x2=b.x+(back?nodeW:0),y1=a.y+42+(returned?17:0)+offset,y2=b.y+42+(returned?17:0)+offset,mid=(x1+x2)/2;const label=relation?(edge.kind==='contains'?'归属':'关系'):edge.element_id?(edge.label||edge.element_kind||edge.element_id):(edge.side==='primary'?'一次':'二次')+(returned?'回':'供');return `<g><title>${esc(edge.element_id?`${edge.element_id} · ${edge.asset_ref||'未绑定资产'} · 配置方向`:(edge.label||label))}</title><path class="topology-edge ${relation?'relation':returned?'return-line':''}" d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}" marker-end="url(#arrow-${relation?'relation':returned?'return':'supply'})"/>${Math.abs(x2-x1)>55?`<text class="edge-label" x="${mid}" y="${(y1+y2)/2+(returned?15:-9)}" text-anchor="middle">${esc(label)}</text>`:''}</g>`;}).join('');
    const blocks=nodes.map(n=>{const p=positions[n.id],sample=telemetryFor(n.id),value=readingValue(sample?.observations?.['cdu.sec_flow']),label=n.label||n.id,symbol=iconPaths[n.kind]||iconPaths.sink;const sub=sample?(value.valid?`${value.text} ${value.unit}`:'有采集记录'):(n.kind==='node'?'未接入节点测点':'配置声明 · 无遥测');return `<g class="topology-node ${esc(n.kind)} ${n.id===state.selectedAsset?'selected':''}" transform="translate(${p.x},${p.y})" data-asset="${esc(n.id)}" role="button" tabindex="0" aria-label="查看 ${esc(label)}"><title>${esc(assetRecordLabel(assetRecord(n.id)))}</title><rect class="node-body" width="${nodeW}" height="${nodeH}" rx="7"/><g class="node-symbol" transform="translate(13,12)"><path d="${symbol}"/></g><circle class="node-status ${sample?'measured':''}" cx="${nodeW-13}" cy="19" r="3"/><text class="node-kind" x="46" y="27">${esc(kindLabels[n.kind]||n.kind)}</text><text class="node-name" x="14" y="53">${esc(label.length>12?label.slice(0,11)+'…':label)}</text><text class="node-value" x="14" y="77">${esc(sub)}</text></g>`;}).join('');
    const guides=levels.map(level=>{const list=nodes.filter(n=>levelById.get(n.id)===level),x=Math.min(...list.map(n=>positions[n.id].x));return `<text class="layer-label" x="${x}" y="${top+19}">关系层 ${level+1}</text>`;}).join('');
    const viewport=$('topology-canvas');viewport.style.height='';
    const baseHeight=viewport.clientHeight||495;if(state.zoom==='fit')viewport.style.height=Math.max(250,Math.min(baseHeight,Math.ceil((viewport.clientWidth||800)*height/width)+8))+'px';
    const fit=Math.min((viewport.clientWidth||800)/width,(viewport.clientHeight||495)/height),scale=state.zoom==='fit'?fit:Math.max(.78,fit)*state.zoom;
    const scrollLeft=viewport.scrollLeft,scrollTop=viewport.scrollTop;
    viewport.innerHTML=`<svg viewBox="${left} ${top} ${width} ${height}" style="width:${Math.max(width*scale,viewport.clientWidth)}px;height:${Math.max(height*scale,viewport.clientHeight)}px" role="img" aria-label="${state.topology.kind==='physical'?'物理连接声明':'服务关系'}，点击设备查看详情"><defs>${[['supply','#4fa58c'],['return','#9c8557'],['relation','#526f85']].map(([k,c])=>`<marker id="arrow-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="${c}"/></marker>`).join('')}</defs>${guides}${paths}${blocks}</svg>`;
    viewport.scrollLeft=scrollLeft;viewport.scrollTop=scrollTop;
    for(const node of viewport.querySelectorAll('[data-asset]')){node.addEventListener('pointerdown',startDrag);node.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();state.selectedAsset=node.dataset.asset;drawTopology();renderDevice();renderMetrics(state.topology);renderQuickPanels();}});}
  }
  function startDrag(event){if(event.button!==0)return;event.preventDefault();state.dragging=true;const group=event.currentTarget,svg=group.ownerSVGElement,asset=group.dataset.asset;state.selectedAsset=asset;renderDevice();renderMetrics(state.topology);renderQuickPanels();const matrix=svg.getScreenCTM();if(!matrix){state.dragging=false;return;}const transform=matrix.inverse();const start=new DOMPoint(event.clientX,event.clientY).matrixTransform(transform);const original={...state.drawPositions[asset]};let moved=false;group.setPointerCapture(event.pointerId);const move=e=>{const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(transform);const x=Math.max(12,original.x+p.x-start.x),y=Math.max(20,original.y+p.y-start.y);if(Math.abs(x-original.x)+Math.abs(y-original.y)>3)moved=true;state.layout[asset]={x:Math.round(x),y:Math.round(y)};group.setAttribute('transform',`translate(${x},${y})`);};const end=()=>{state.dragging=false;group.removeEventListener('pointermove',move);group.removeEventListener('pointerup',end);group.removeEventListener('pointercancel',end);if(moved)$('layout-note').textContent='布局有未保存调整；物理连接未改变';drawTopology();};group.addEventListener('pointermove',move);group.addEventListener('pointerup',end);group.addEventListener('pointercancel',end);}
  function readingRows(readings){return Object.entries(readings||{}).map(([key,r])=>{const v=readingValue(r);return `<div class="reading-row"><span class="label">${esc(labels[key]||key)}</span><span class="reading ${v.valid?'':'unavailable'}">${esc(v.text)} ${esc(v.unit)}<small>${esc(({good:'有效',stale_source:'源数据停滞',bad:'无效',unknown:'未知'})[r.quality]||r.quality||'质量未声明')} · ${esc(r.provenance||'来源未声明')}</small></span></div>`;}).join('');}
  function renderDevice(){
    const n=state.topology?.nodes.find(a=>a.id===state.selectedAsset);
    if(!n){$('device-detail').innerHTML='<div class="empty-state"><span class="empty-icon">◇</span><h3>选择设备查看详情</h3><p>实际测量、目标回读与可用控制能力。</p></div>';return;}
    if(isAnalysisScene(selectedScene())){
      const scene=selectedScene(),junction=(scene.hydraulics?.junctions||[]).find(j=>j.id===n.junction_id),domains=(scene.control_domains||[]).filter(d=>(d.member_asset_ids||[]).includes(n.id)||(junction&&(d.circuit_ids||[]).includes(junction.circuit_id)));
      $('device-detail').innerHTML=`<div class="device-title"><span class="asset-kind">${esc(kindLabels[n.kind]||n.kind)}</span><h2>${esc(n.label||n.id)}</h2><div class="badge-row">${badge('配置声明')}${badge('未接入实测')}</div><p class="fine-print">${esc(n.id)}${n.parent_id?' · 归属 '+esc(n.parent_id):''}</p></div><div class="device-section"><span class="section-label">分析范围</span><p>${esc(domains.map(d=>d.id).join('、')||'未归入控制域')}</p><p class="fine-print">连接点代表水力模型中的压力节点；资产归属不代表管路连接。点表引用不会自动产生观测。</p></div><div class="device-section"><span class="section-label">运行边界</span><p class="fine-print">此配置当前只用于静态水力分析，没有可用设备控制模式。模型估计在下方分析面板中独立展示，不冒充流量、温度或阀位实测。</p></div>`;return;
    }
    const scene=selectedScene()||{},profile=scene.devices?.[n.id],sample=telemetryFor(n.id),domain=selectedDomainStatus(n.id),observations=sample?.observations||{},setpoints=sample?.setpoints||{},q=profile?.policy?.control_quantity;
    const history=!!domain?.is_historical;
    const source=sourceInfo(domain?.source,domain?.active);
    const type=({liquid_to_liquid:'液—液 CDU',liquid_to_air:'液—风 CDU'})[profile?.type]||kindLabels[n.kind]||n.kind;
    $('device-detail').innerHTML=`<div class="device-title"><span class="asset-kind">${esc(type)}</span><h2>${esc(n.label||n.id)}</h2><div class="badge-row">${badge(sample?(history?'历史采集记录':'有采集记录'):'未接入测量',sample?'blue':'neutral')}${sample?badge(source.label,source.tone):profile?badge(profile.adapter):''}</div><p class="fine-print">${esc(n.id)}${n.parent_id?' · 归属 '+esc(n.parent_id):''}</p></div><div class="device-section"><span class="section-label">实际过程量</span>${Object.keys(observations).length?readingRows(observations):'<p class="fine-print">当前没有该设备测点。配置初始值不作为实际观测。</p>'}${sample?`<p class="fine-print">${esc(assetRecordLabel(domain))}<br>采样时模式 ${esc(human(sample.operating_mode))} · 采样时控制者 ${esc(sample.owner||'未声明')}${history?'<br>历史记录，不代表当前设备状态。':''}</p>`:''}</div><div class="device-section"><span class="section-label">控制能力与当前策略</span>${profile?Object.entries(profile.controls||{}).map(([key,c])=>`<div class="reading-row"><span class="label">${esc(labels[key]||key)}<small style="display:block;color:${key===q?'#6edabb':'#6c899b'}">${key===q?'算法配置使用':key==='cdu.sec_supply_temp_sp'?'本版策略保持，不自动优化':'接口声明，不代表算法使用'}</small></span><span class="reading">${format(c.minimum)}–${format(c.maximum)}<small>${esc(c.unit)} · 单步 ≤ ${format(c.max_step)}</small></span></div>`).join('')||'<p class="fine-print">未声明可写量，按只读能力接入。</p>':'<p class="fine-print">未配置独立控制接口。机柜／节点归属不产生支路控制能力。</p>'}</div><details class="device-section" ${Object.keys(setpoints).length?'open':''}><summary style="padding:0 0 7px">目标值回读</summary>${Object.keys(setpoints).length?readingRows(setpoints):'<p class="fine-print">暂无目标回读。</p>'}<p class="fine-print">设定值回读不代表温压流已到位。</p></details>${sample?.alarms?.length?`<div class="notice error">${esc(sample.alarms.join(' / '))}</div>`:''}`;
  }
  function renderQuickPanels(){
    if(isAnalysisScene(selectedScene())){$('topology-quick-panels').innerHTML='<div class="notice info">Schema 0.2：仅展示配置与只读分析。支路调度、阀门写控制和功率预测分配尚未实现；旧运行记录仍可在控制追踪与运行页单独查看。</div>';return;}
    const node=state.topology?.nodes.find(n=>n.id===state.selectedAsset),domain=selectedDomainStatus(node?.id);
    const d=domain?.latest_decision?itemPayload(domain.latest_decision):{};
    const q=d.policy_quantity||d.request?.quantity,unit=d.request?.unit||(q==='cdu.dp_sp'?'kPa':q==='cdu.sec_flow_sp'?'kg/s':'');
    const domains=state.siteConfigId===siteContextId()?state.site?.domains||[]:[];
    const operational=domains.length?`<div class="table-wrap"><table><thead><tr><th>控制域 / CDU</th><th>采集来源</th><th>执行状态</th><th></th></tr></thead><tbody>${domains.map(x=>{const src=sourceInfo(x.source,x.active);const status=x.control_active?'闭环任务运行':x.is_historical?'历史记录':x.active?human(x.last_runtime_mode||x.mode):'未运行';return `<tr><td>${esc(x.domain_id)}<div class="fine-print">${esc(x.cdu_id)}</div></td><td>${badge(src.label,src.tone)}<div class="fine-print">${esc(x.selected_run_id?'#'+shortRunId(x.selected_run_id)+' · ':'')}${esc(isNumber(x.measurement_timestamp)?atLabel(x.measurement_timestamp)+' · ':'')}${esc(({fresh:'采样有效',stale:'采样过期',historical:'历史采样',simulation_time:'仿真时钟',not_sampled:'尚未采样',invalid_timestamp:'时间戳无效'})[x.freshness_status]||x.freshness_status||'未采样')}</div></td><td>${badge(status,x.control_active?'teal':x.latest_fault||x.job_error?'warning':'neutral')}${x.required_action?`<div class="fine-print">${esc(human(x.required_action))}</div>`:''}</td><td>${x.selected_run_id?`<button class="button text small" data-domain-run="${esc(x.selected_run_id)}">查看</button>`:'—'}</td></tr>`;}).join('')}</tbody></table></div>`:'<div class="quick-body"><p class="fine-print">当前视图没有可关联的多域运行记录。发布配置并运行对应控制域后，显示各 CDU 的独立采集与任务状态。</p><div class="badge-row">'+(selectedScene()?.control_domains||[]).map(x=>badge(x.id)).join('')+'</div></div>';
    const trace=d.target_flow_kg_s!==undefined?`<div class="quick-flow"><div class="quick-step"><span>需求流量</span><strong>${format(d.demand_flow_kg_s)} <small>kg/s</small></strong></div><div class="quick-arrow">→</div><div class="quick-step"><span>约束后目标</span><strong>${format(d.limited_control_target)} <small>${esc(unit)}</small></strong></div><div class="quick-arrow">→</div><div class="quick-step"><span>执行回执</span><strong style="font-size:12px">${esc(d.receipt?human(d.receipt.status):'无新命令')}</strong></div></div><div class="quick-foot"><span>外部预测：${esc(human(d.forecast_status||'forecast_absent'))}</span><button class="button text small" id="quick-decisions">查看控制追踪 →</button></div>`:'<div class="empty-state" style="min-height:115px;padding:12px"><h3>暂无对应决策</h3><p>选择一个带有控制记录的运行会话，查看算法如何生成目标。</p></div>';
    $('topology-quick-panels').innerHTML=`<article class="card quick-panel"><div class="card-heading"><h2>配置各域最新记录</h2>${badge(domains.length?`${state.site.coverage?.observed_domains||0} / ${state.site.coverage?.configured_domains||domains.length} 域有记录`:'按独立域运行')}</div>${operational}</article><article class="card quick-panel"><div class="card-heading"><h2>所选设备的最近决策</h2>${badge(d.mode?(domain?.is_historical?'历史 · ':'')+human(d.mode):'无记录',d.mode==='control'?'blue':'neutral')}</div><div class="quick-body"><p class="fine-print">${esc(assetRecordLabel(domain))}</p>${trace}</div></article>`;
    $('quick-decisions')?.addEventListener('click',e=>action(e.currentTarget,async()=>{if(domain?.selected_run_id&&domain.selected_run_id!==state.runId)await loadRun(domain.selected_run_id);choosePage('decisions');}));
    $('topology-quick-panels').querySelectorAll('[data-domain-run]').forEach(b=>b.addEventListener('click',()=>action(b,async()=>{await loadRun(b.dataset.domainRun);$('topology-source').value='run';updateTopologySelection();renderLiveViews();choosePage('decisions');})));
  }

  function renderDecisionSelector(){if(state.runLoading){selectOptions($('decision-event'),[],'','正在加载决策记录…');$('decision-content').innerHTML='<div class="card empty-state">正在加载所选运行的控制记录…</div>';return;}const decisions=state.events.decision||[];if(state.follow||!decisions.some((e,i)=>eventId(e,i)===state.decisionId))state.decisionId=decisions.length?eventId(decisions.at(-1),decisions.length-1):'';selectOptions($('decision-event'),decisions.map((e,i)=>({value:eventId(e,i),label:`${atLabel(eventAt(e))} · ${human(itemPayload(e).receipt?.status||itemPayload(e).mode||'记录')}`})).reverse(),state.decisionId,'没有决策记录');renderDecision();}
  /* Cycle IDs are preferred. Old records only match a preceding sample from
   * the same session; a future telemetry sample is never used for a decision. */
  function matchingTelemetry(decisionEvent){const p=itemPayload(decisionEvent);const items=state.events.telemetry||[];if(p.cycle_id){const exact=items.find(e=>itemPayload(e).cycle_id===p.cycle_id&&(!p.session||itemPayload(e).session===p.session));if(exact)return exact;}return items.filter(e=>(!p.session||itemPayload(e).session===p.session)&&eventAt(e)<=eventAt(decisionEvent)).at(-1)||null;}
  function row(label,value,unit=''){return `<div class="reading-row"><span class="label">${esc(label)}</span><span class="reading">${esc(isNumber(value)?format(value):value??'—')} ${esc(unit)}</span></div>`;}
  function traceCard(index,title,content,note){return `<article class="card trace-card"><div class="trace-header"><span class="trace-index">${index}</span><h3>${esc(title)}</h3></div>${content}<div class="trace-note">${esc(note)}</div></article>`;}
  function renderDecision(){const events=state.events.decision||[];const event=events.find((e,i)=>eventId(e,i)===state.decisionId);if(!event){$('decision-content').innerHTML='<div class="card empty-state"><span class="empty-icon">⇢</span><h3>当前没有算法决策记录</h3><p>只读监测不产生决策。选择带有决策记录的历史会话，或以影子／控制模式运行热仿真。</p></div>';return;}const d=itemPayload(event),sampleEvent=matchingTelemetry(event),t=itemPayload(sampleEvent),obs=t.observations||{};const profile=state.run?.scene?.devices?.[d.asset_id||t.asset_id];const q=d.policy_quantity||d.request?.quantity||profile?.policy?.control_quantity;const unit=d.request?.unit||profile?.controls?.[q]?.unit||'';const receipt=d.receipt;const actualFlow=readingValue(obs['cdu.sec_flow']),actualDP=readingValue(obs['cdu.sec_dp']);const calibrationEvent=(state.events.calibration||[]).filter(e=>(!d.session||itemPayload(e).session===d.session)&&((d.cycle_id&&itemPayload(e).cycle_id===d.cycle_id)||eventAt(e)<=eventAt(event))).at(-1);const calibration=itemPayload(calibrationEvent);const match=p=>sampleEvent?(d.cycle_id&&t.cycle_id===d.cycle_id?'同一采集周期':'旧记录按同会话较早时间匹配'):'本次窗口内无对应观测';
    const stages=[traceCard('01','观测与质量',row('采集设备',t.asset_id||d.asset_id||'—')+row('实际流量',actualFlow.text,actualFlow.unit)+row('实际压差',actualDP.text,actualDP.unit)+row('报警',t.alarms?.length?t.alarms.join(' / '):sampleEvent?'记录中无报警':'未记录'),match()),traceCard('02','负荷与需求',row('观测液侧热负荷',obs['cdu.liquid_load']?.unit==='W_th'?(goodValue(obs['cdu.liquid_load'],v=>v/1000)??'缺测'):'缺测','kW 热')+row('计算需求热负荷',isNumber(d.load_w_th)?d.load_w_th/1000:null,'kW 热')+row('原始需求流量',d.demand_flow_kg_s,'kg/s')+row('边界内目标流量',d.target_flow_kg_s,'kg/s')+row('外部预测',human(d.forecast_status)),`观测负荷依据：${d.load_provenance||'未记录'}${d.forecast_status==='forecast_used'?'；计算需求已与预测峰值取大。':''}`),traceCard('03','模型与候选',row('控制量',labels[q]||q||'未记录')+row('模型平衡目标',d.equilibrium_control_target,unit)+row('反馈后候选',d.raw_control_target,unit)+row('模型版本',d.model_version),`水力增益 ${format(d.gain,5)}，指数 ${format(d.hydraulic_exponent,3)}；DP 反馈 gain ${format(d.dp_feedback_gain,3)}。`),traceCard('04','约束与最终目标',row('限幅后',d.bounded_control_target,unit)+row('限速后',d.limited_control_target,unit)+row('当前目标回读',t.setpoints?.[q]?.value,t.setpoints?.[q]?.unit||unit),d.warnings?.length?d.warnings.map(human).join('；'):'记录中无策略告警；网关仍会执行检查。'),traceCard('05','命令网关',row('会话模式',human(d.mode))+row('申请目标',d.request?.value,d.request?.unit||unit)+row('处理结果',receipt?human(receipt.status):'本周期无命令回执')+row('回读值',receipt?.readback,unit),receipt?.reasons?.length?receipt.reasons.map(human).join('；'):d.request?'查看网关回执确认执行结果。':'处于死区等情况可能无需生成新命令。'),traceCard('06','效果与校准',row('流量误差',d.flow_tracking_error_kg_s,'kg/s')+row('压差误差',d.dp_tracking_error_kpa,'kPa')+row('回液温度预测',isNumber(d.return_forecast_k)?d.return_forecast_k-273.15:null,'°C')+row('过程响应',human(receipt?.process_response||'not_verified'))+row('参数校准',human(calibration.status||'无对应记录')),'校准：'+human(calibration.reason||calibration.status||'无对应记录')+'。没有过程确认记录时，不把设定值回读标为动作效果已验证。')];
    const uncertain=receipt&&/uncertain/.test(receipt.status||'');$('decision-content').innerHTML=`<div class="card trace-banner"><div><span class="step-label">决策回放 · ${esc(atLabel(eventAt(event)))}</span><p>${esc(d.asset_id||t.asset_id||'')} ${d.domain_id?' / '+esc(d.domain_id):''} · ${esc(d.cycle_id?'周期 '+d.cycle_id:'历史记录，无周期 ID')}</p></div><div class="badge-row">${badge(human(d.mode),d.mode==='control'?'teal':'neutral')}${badge(receipt?human(receipt.status):'无新命令',uncertain?'warning':receipt?.status==='rejected'?'error':'neutral')}</div></div>${d.mode==='shadow'?'<div class="notice info">影子模式只计算和记录建议，不写入设备。</div>':''}${uncertain?'<div class="notice warning">写入结果不确定：不能视为确定失败，也不能据此自动重复发送。请依据网关审计与设备状态处理。</div>':''}<div class="trace-grid">${stages.join('')}<details class="card trace-wide"><summary>查看完整决策与关联观测记录</summary><pre>${esc(JSON.stringify({decision:d,telemetry:sampleEvent?t:null},null,2))}</pre></details></div>`;
  }

  // 已发布版本在 UI 中只读。修改从复制的新草稿开始，运行永远绑定其发布快照。
  const editable=()=>state.config&&state.config.status==='draft';
  function currentDomain(){return (state.scene?.control_domains||[]).find(d=>d.id===state.domainId)||state.scene?.control_domains?.[0];}
  function currentProfile(){const d=currentDomain();return d?state.scene?.devices?.[d.cdu_id]:null;}
  async function loadConfig(id){const serial=++state.configSerial;const record=await api(`/api/configs/${encodeURIComponent(id)}`);if(serial!==state.configSerial)return;state.config=record;state.scene=clone(record.scene);state.original=clone(record.scene);state.originalName=record.name;state.validation=record.validation;state.dirty=false;state.jsonPending=false;state.domainId=state.scene.control_domains?.[0]?.id||'';$('config-select').value=id;renderConfig();if($('topology-source').value==='config'&&(!state.topologyConfigId||state.topologyConfigId===record.id)){state.topologyConfig=record;state.topologyConfigId=record.id;updateTopologySelection();renderTopology();}}
  async function loadTopologyConfig(id){if(id!==state.topologyConfigId){clearAnalysis();state.topologyConfig=null;}const serial=++state.topologySerial;state.topologyConfigId=id;state.topologyLoading=true;renderAnalysis();$('topology-explanation').textContent='正在读取所选配置…';if(!id){state.topologyConfig=null;state.topologyLoading=false;renderTopology();return;}try{const record=await api(`/api/configs/${encodeURIComponent(id)}`);if(serial!==state.topologySerial||state.topologyConfigId!==id)return;state.topologyConfig=record;state.layoutContext=null;$('topology-selection').value=id;}finally{if(serial===state.topologySerial){state.topologyLoading=false;renderTopology();refreshSite();}}}
  function copyToDraft(){if(!state.config)return;if(state.jsonPending){toast('请先应用 JSON 修改后再复制，避免丢失未应用的内容。');return;}const source=state.config.id||state.config.source_config_id;state.config={...state.config,id:null,source_config_id:source,name:`${state.config.name||state.scene.scene_id} · 新草稿`,status:'draft',revision:0};state.dirty=true;renderConfig();toast('已创建本地草稿，修改后保存；原配置保持不变。');}
  function fieldHtml(id,label,value,type='number',extra=''){return `<div class="field"><label for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" type="${type}" value="${esc(value??'')}" ${type==='number'?'step="any"':''} ${extra} ${editable()?'':'disabled'}></div>`;}
  function renderConfig(){const record=state.config,scene=state.scene;if(!record||!scene){$('config-form').innerHTML='<div class="empty-state">暂无配置</div>';return;}$('config-status').textContent=human(record.status);$('config-status').className=`pill ${record.status==='published'?'teal':'neutral'}`;$('config-revision').textContent=record.id?`版本 r${record.revision} · ${record.id}`:'尚未保存';$('config-readonly').hidden=editable();$('config-readonly').textContent=record.status==='template'?(isAnalysisScene(scene)?'当前是 Schema 0.2 分析模板，尚未发布。复制为草稿后，通过高级 JSON 编辑、校验并发布，再运行只读分析。':'当前是配置模板，尚未发布。点击“复制为新草稿”后编辑、校验并发布，才能创建运行任务。'):record.status==='published'?'当前是已发布快照。点击“复制为新草稿”后编辑，正在运行的配置不受影响。':'当前配置为只读内容。复制为新草稿后再修改。';$('scene-json').value=JSON.stringify(scene,null,2);$('json-status').textContent='';$('scene-json').disabled=!editable();$('apply-json').disabled=!editable();renderBasicForm();renderAssetForm();renderValidation();updateConfigActions();}
  function renderBasicForm(){if(isAnalysisScene(state.scene)){$('config-form').innerHTML='<div class="notice info">Schema 0.2 使用通用资产、水力元件与控制域声明。当前版本只支持高级 JSON 编辑；旧 CDU 参数表单不适用于此配置。点表仅作为配置引用，不会访问设备。</div>';return;}const s=state.scene,d=currentDomain(),p=currentProfile(),policy=p?.policy;const controls=Object.keys(p?.controls||{}).filter(q=>['cdu.dp_sp','cdu.sec_flow_sp'].includes(q));const disabled=editable()?'':'disabled';let html=`<div class="form-section"><h3>场景与控制域</h3><div class="form-grid">${fieldHtml('edit-name','配置名称',state.config.name,'text')}${fieldHtml('edit-scene-id','场景 ID',s.scene_id,'text')}${fieldHtml('edit-topology-version','拓扑版本',s.topology_version,'text')}<div class="field"><label for="edit-domain">控制域</label><select id="edit-domain">${(s.control_domains||[]).map(a=>`<option value="${esc(a.id)}" ${a.id===d?.id?'selected':''}>${esc(a.id)}</option>`).join('')}</select></div></div><p class="fine-print">${d?`CDU: ${esc(d.cdu_id)} · ${esc(p?.adapter||'未绑定驱动')} · 服务 ${d.served_racks?.length||0} 个机柜`:'未配置控制域'}</p></div>`;
    if(policy)html+=`<div class="form-section"><h3>热需求与控制策略</h3><div class="form-grid"><div class="field"><label for="edit-control">控制量（仅显示设备已声明能力）</label><select id="edit-control" ${disabled}>${controls.map(q=>`<option value="${esc(q)}" ${q===policy.control_quantity?'selected':''}>${esc(labels[q]||q)}</option>`).join('')}</select></div>${fieldHtml('edit-delta','目标供回温差 / K',policy.target_delta_k)}${fieldHtml('edit-min-flow','最低目标流量 / kg/s',policy.minimum_flow_kg_s)}${fieldHtml('edit-max-flow','最高目标流量 / kg/s',policy.maximum_flow_kg_s)}${fieldHtml('edit-horizon','预测时间窗 / s',policy.horizon_s)}${fieldHtml('edit-ttl','命令有效期 / s',policy.command_ttl_s)}</div><p class="fine-print">切换控制量不会自动改变设备模式、接口语义或验收状态；不兼容设置会在校验时报告。</p></div>`;else html+='<div class="notice info">此设备没有自动策略。仅可运行其已具备的模式；高级 JSON 可查看完整配置。</div>';
    if(p?.connection)html+=`<div class="form-section"><h3>设备连接</h3><div class="form-grid">${fieldHtml('edit-host','主机 / IP',p.connection.host,'text')}${fieldHtml('edit-port','TCP 端口',p.connection.port)}${fieldHtml('edit-unit','Modbus Unit ID',p.connection.unit_id)}${fieldHtml('edit-timeout','通信超时 / s',p.connection.timeout_s)}</div><p class="fine-print">点表、字节序、控制权与 commissioning 信息在高级 JSON 中维护。保存配置不会访问该端点。</p></div>`;
    $('config-form').innerHTML=html;
    $('edit-domain')?.addEventListener('change',e=>{state.domainId=e.target.value;renderBasicForm();});
    const bindings={'edit-name':v=>{state.config.name=v;},'edit-scene-id':v=>{s.scene_id=v;},'edit-topology-version':v=>{s.topology_version=v;},'edit-control':v=>{policy.control_quantity=v;},'edit-delta':v=>{policy.target_delta_k=v;},'edit-min-flow':v=>{policy.minimum_flow_kg_s=v;},'edit-max-flow':v=>{policy.maximum_flow_kg_s=v;},'edit-horizon':v=>{policy.horizon_s=v;},'edit-ttl':v=>{policy.command_ttl_s=v;},'edit-host':v=>{p.connection.host=v;},'edit-port':v=>{p.connection.port=v;},'edit-unit':v=>{p.connection.unit_id=v;},'edit-timeout':v=>{p.connection.timeout_s=v;}};
    for(const [id,set]of Object.entries(bindings)){const el=$(id);el?.addEventListener('input',()=>{set(el.type==='number'?(el.value===''?null:Number(el.value)):el.value);markDirty();});}
  }
  function markDirty(){if(state.jsonPending)throw new Error('请先应用或放弃高级 JSON 修改。');state.dirty=true;state.validation=null;$('scene-json').value=JSON.stringify(state.scene,null,2);state.jsonPending=false;renderValidation();updateConfigActions();}
  function applySceneJson(text){
    const parsed=JSON.parse(text);
    if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw new Error('场景必须是 JSON 对象。');
    if(!Array.isArray(parsed.assets)||!Array.isArray(parsed.control_domains)||parsed.assets.some(x=>!x||typeof x!=='object'||Array.isArray(x))||parsed.control_domains.some(x=>!x||typeof x!=='object'||Array.isArray(x))||(!isAnalysisScene(parsed)&&(!parsed.devices||typeof parsed.devices!=='object'||Array.isArray(parsed.devices))))throw new Error('assets 和 control_domains 必须是对象数组；旧版 devices 必须是对象。原草稿尚未改变。');
    state.scene=parsed;state.domainId=parsed.control_domains?.[0]?.id||'';state.jsonPending=false;markDirty();
  }
  function discardSceneJson(){
    // Discard only unapplied text. Earlier applied/unsaved form changes remain.
    $('scene-json').value=JSON.stringify(state.scene,null,2);state.jsonPending=false;updateConfigActions();
  }
  function collectDiff(a,b,path='',out=[]){if(JSON.stringify(a)===JSON.stringify(b))return out;if(a&&b&&typeof a==='object'&&typeof b==='object'&&!Array.isArray(a)&&!Array.isArray(b)){for(const key of new Set([...Object.keys(a),...Object.keys(b)]))collectDiff(a[key],b[key],path?`${path}.${key}`:key,out);}else out.push(`${path}\n  ${JSON.stringify(a)??'(缺省)'} → ${JSON.stringify(b)??'(删除)'}`);return out;}
  function updateConfigActions(){const can=editable();$('basic-edit-fields').disabled=state.jsonPending||isAnalysisScene(state.scene);$('asset-edit-fields').disabled=state.jsonPending||isAnalysisScene(state.scene);$('json-pending-notice').hidden=!state.jsonPending;$('discard-json').disabled=!can||!state.jsonPending;$('save-config').disabled=!can||state.jsonPending;$('validate-config').disabled=!state.scene||state.jsonPending;$('publish-config').disabled=!can||!state.config?.id||state.dirty||state.jsonPending||!state.validation?.valid;$('dirty-state').textContent=state.jsonPending?'JSON 尚未应用':state.dirty?'有未保存修改 · 不影响当前运行':'没有未保存修改';$('dirty-state').style.color=state.dirty||state.jsonPending?'#a37120':'';const diff=state.scene?collectDiff(state.original||{},state.scene):[];if(state.config?.name!==state.originalName)diff.unshift(`配置名称\n  ${state.originalName||''} → ${state.config?.name||''}`);$('config-diff').textContent=diff.length?diff.slice(0,80).join('\n\n'):'暂无修改';}
  function renderValidation(){const v=state.validation;$('validation-pill').textContent=v?(v.valid?'结构校验通过':'存在配置错误'):'等待校验';$('validation-pill').className=`pill ${v?(v.valid?'teal':'error'):'neutral'}`;if(!v){$('validation-results').innerHTML='<p class="fine-print">修改后需重新校验。校验检查配置与已实现能力，不代替设备通信或现场验收。</p>';return;}const items=(v.errors||[]).map(x=>({tone:'error',text:x})).concat((v.warnings||[]).map(x=>({tone:'warning',text:x})));const caps=Array.isArray(v.capabilities)?v.capabilities:Object.values(v.capabilities||{});$('validation-results').innerHTML=(items.length?items.map(i=>`<div class="validation-item ${i.tone}"><span> ${i.tone==='error'?'×':'△'} </span><span>${esc(typeof i.text==='object'?i.text.message||i.text.reason||JSON.stringify(i.text):human(i.text))}</span></div>`).join(''):'<div class="validation-item good">✓ 配置结构与基础语义通过</div>')+caps.map(c=>`<div class="capability-item"><strong>${esc(c.domain_id||c.cdu_id||'控制域')}</strong><p>${esc(c.adapter||'')} · ${esc(c.policy_kind||'无自动策略')}</p><div class="badge-row">${(c.supported_analysis||[]).map(m=>badge(analysisStatus(m),'blue')).join('')}${(c.supported_modes||[]).map(m=>badge(human(m),'blue')).join('')||badge(isAnalysisScene(state.scene)?'不提供设备控制':human(c.control||'无可用模式'))}</div>${(c.reasons||[]).map(r=>`<p>${esc(human(r))}</p>`).join('')}${c.missing_observations?.length?`<p>缺少：${esc(c.missing_observations.join('、'))}</p>`:''}</div>`).join('');}
  async function validateConfig(){if(state.jsonPending)throw new Error('请先应用 JSON 修改。');state.validation=await post('/api/configs/validate',{scene:state.scene});renderValidation();updateConfigActions();return state.validation;}
  // 脱敏配置复制时携带 source_config_id；后端恢复原敏感字段，UI 不猜测凭据。
  async function saveConfig(){if(!editable())throw new Error('请先复制为草稿。');if(state.jsonPending)throw new Error('请先应用 JSON 修改。');const body={name:state.config.name,scene:state.scene};if(state.config.id){body.id=state.config.id;body.expected_revision=state.config.revision;}else if(state.config.source_config_id)body.source_config_id=state.config.source_config_id;const saved=await post('/api/configs/draft',body);state.config=saved;state.scene=clone(saved.scene);state.original=clone(saved.scene);state.originalName=saved.name;state.validation=saved.validation;state.dirty=false;state.jsonPending=false;await refreshCatalog();$('config-select').value=saved.id;renderConfig();toast('草稿已保存；运行配置没有改变。');return saved;}
  async function publishConfig(){if(state.dirty||state.jsonPending)throw new Error('请先保存草稿并校验。');if(!state.config.id)throw new Error('请先保存草稿。');const v=await validateConfig();if(!v.valid)throw new Error('存在配置错误，无法发布。');const yes=await confirmAction('发布当前配置版本？',`将发布“${state.config.name}” r${state.config.revision} 的已保存快照。发布不会创建任务，也不会写入设备。Schema 0.2 从系统总览运行只读分析，旧版从运行页选择任务。`);if(!yes)return;const published=await post(`/api/configs/${encodeURIComponent(state.config.id)}/publish`,{expected_revision:state.config.revision});await refreshCatalog();await loadConfig(published.id);await loadRunConfig(published.id);toast(isAnalysisScene(published.scene)?'分析配置已发布；可在系统总览选择该配置运行只读分析。':'配置已发布，可前往运行页选择该版本。');}
  function renderAssetForm(){if(isAnalysisScene(state.scene)){$('asset-form').innerHTML='<div class="notice info">请在高级 JSON 中维护 assets、hydraulics 和 control_domains。此版本不提供图上增删水力元件；归属树与水力回路分开校验。保存、发布与只读分析均不写入现场。</div>';return;}const scene=state.scene,assets=scene.assets||[],p=scene.physical_topology,disabled=editable()?'':'disabled';const options=(values,current)=>values.map(([v,label])=>`<option value="${esc(v)}" ${v===current?'selected':''}>${esc(label)}</option>`).join('');const assetOptions=[['','无归属'],...assets.map(a=>[a.id,a.id])];
    let html=`<h3>资产与归属</h3><p class="fine-print">资产归属不是液路连接。新增 CDU 还需设备 Profile 和控制域，全部完成后才能通过校验。</p><div class="table-wrap"><table><thead><tr><th>资产 ID</th><th>显示名称</th><th>类型</th><th>归属</th><th></th></tr></thead><tbody>${assets.map((a,i)=>`<tr><td>${esc(a.id)}</td><td><input data-asset-label="${i}" value="${esc(a.label||'')}" aria-label="${esc(a.id)} 显示名称" ${disabled}></td><td>${esc(kindLabels[a.kind]||a.kind)}</td><td><select data-asset-parent="${i}" aria-label="${esc(a.id)} 归属" ${disabled}>${options(assetOptions.filter(v=>v[0]!==a.id),a.parent_id||'')}</select></td><td><button class="button text small" data-remove-asset="${i}" ${disabled} aria-label="删除资产 ${esc(a.id)}">删除</button></td></tr>`).join('')}</tbody></table></div><div class="asset-add"><input id="asset-new-id" placeholder="新资产 ID" aria-label="新资产 ID" ${disabled}><select id="asset-new-kind" aria-label="新资产类型" ${disabled}>${options(Object.entries(kindLabels),'node')}</select><select id="asset-new-parent" aria-label="新资产归属" ${disabled}>${options(assetOptions,'')}</select><button id="add-asset" class="button secondary small" ${disabled}>新增资产</button></div>`;
    if(!p)html+='<div class="connection-edit"><h3>实际管路尚未声明</h3><p class="fine-print">当前只显示控制域服务关系。声明端口与供回连接后，才能显示物理连接图；尚未完整填写时草稿可以保存，但不能发布。</p><button id="enable-physical" class="button secondary" '+disabled+'>创建物理连接草稿</button></div>';
    else{html+=`<div class="connection-edit"><h3>物理端口与管路</h3><div class="form-grid"><div class="field"><label for="physical-evidence">连接证据</label><select id="physical-evidence" ${disabled}>${options([['illustrative','示意 / 仿真'],['declared','现场声明'],['verified','已核验（需证据引用）']],p.evidence)}</select></div>${fieldHtml('physical-reference','核验证据引用',p.evidence_reference||'','text')}</div><p class="fine-print">供液／回液代表管路用途。连接两端应属于相同侧别、回路和用途；供回连接需分别声明。界面不会自动推断实际管路。</p><div class="table-wrap"><table><thead><tr><th>端口 ID</th><th>资产</th><th>侧别</th><th>用途</th><th>回路 ID</th><th></th></tr></thead><tbody>${(p.ports||[]).map((port,i)=>`<tr><td>${esc(port.id)}</td><td><select data-port-index="${i}" data-port-key="asset_id" ${disabled}>${options(assets.map(a=>[a.id,a.id]),port.asset_id)}</select></td><td><select data-port-index="${i}" data-port-key="side" ${disabled}>${options([['primary','一次'],['secondary','二次']],port.side)}</select></td><td><select data-port-index="${i}" data-port-key="direction" ${disabled}>${options([['supply','供液'],['return','回液']],port.direction)}</select></td><td><input data-port-index="${i}" data-port-key="loop_id" value="${esc(port.loop_id)}" ${disabled}></td><td><button class="button text small" data-remove-port="${i}" ${disabled}>删除</button></td></tr>`).join('')}</tbody></table></div><div class="form-grid" style="margin-top:14px">${fieldHtml('port-new-id','新端口 ID','','text')}<div class="field"><label for="port-new-asset">所属资产</label><select id="port-new-asset" ${disabled}>${options(assets.map(a=>[a.id,a.id]),'')}</select></div><div class="field"><label for="port-new-side">侧别</label><select id="port-new-side" ${disabled}>${options([['primary','一次侧'],['secondary','二次侧']],'secondary')}</select></div><div class="field"><label for="port-new-direction">用途</label><select id="port-new-direction" ${disabled}>${options([['supply','供液'],['return','回液']],'supply')}</select></div>${fieldHtml('port-new-loop','回路 ID','','text')}<div class="field"><label>&nbsp;</label><button id="add-port" class="button secondary" ${disabled}>新增端口</button></div></div><h3 style="margin-top:25px">连接</h3><div class="table-wrap"><table><thead><tr><th>ID</th><th>起点端口</th><th>终点端口</th><th></th></tr></thead><tbody>${(p.connections||[]).map((c,i)=>`<tr><td>${esc(c.id)}</td><td>${esc(c.from_port)}</td><td>${esc(c.to_port)}</td><td><button class="button text small" data-remove-connection="${i}" ${disabled}>删除</button></td></tr>`).join('')}</tbody></table></div><div class="asset-add"><input id="connection-new-id" placeholder="连接 ID" aria-label="连接 ID" ${disabled}><select id="connection-from" aria-label="起点端口" ${disabled}>${options((p.ports||[]).map(x=>[x.id,x.id]),'')}</select><select id="connection-to" aria-label="终点端口" ${disabled}>${options((p.ports||[]).map(x=>[x.id,x.id]),'')}</select><button id="add-connection" class="button secondary small" ${disabled}>新增连接</button></div></div>`;}
    $('asset-form').innerHTML=html;
    $('asset-form').querySelectorAll('[data-asset-label]').forEach(el=>el.addEventListener('input',()=>{assets[Number(el.dataset.assetLabel)].label=el.value;markDirty();}));
    $('asset-form').querySelectorAll('[data-asset-parent]').forEach(el=>el.addEventListener('change',()=>{const a=assets[Number(el.dataset.assetParent)];if(el.value)a.parent_id=el.value;else delete a.parent_id;markDirty();}));
    $('asset-form').querySelectorAll('[data-remove-asset]').forEach(el=>el.addEventListener('click',()=>{assets.splice(Number(el.dataset.removeAsset),1);markDirty();renderAssetForm();toast('资产已从草稿移除。关联端口或服务关系需同步调整并校验。');}));
    $('add-asset')?.addEventListener('click',()=>{const id=$('asset-new-id').value.trim();if(!id||assets.some(a=>a.id===id)){toast('资产 ID 不能为空或重复。');return;}const a={id,kind:$('asset-new-kind').value};if($('asset-new-parent').value)a.parent_id=$('asset-new-parent').value;assets.push(a);markDirty();renderAssetForm();});
    $('enable-physical')?.addEventListener('click',()=>{scene.physical_topology={evidence:'declared',ports:[],connections:[],layout:clone(scene.extensions?.ui_layout||{})};markDirty();renderAssetForm();});
    $('physical-evidence')?.addEventListener('change',e=>{p.evidence=e.target.value;markDirty();});$('physical-reference')?.addEventListener('input',e=>{if(e.target.value)p.evidence_reference=e.target.value;else delete p.evidence_reference;markDirty();});
    $('asset-form').querySelectorAll('[data-port-key]').forEach(el=>el.addEventListener('change',()=>{p.ports[Number(el.dataset.portIndex)][el.dataset.portKey]=el.value;markDirty();}));
    for(const [selector,key,attr] of [['[data-remove-port]','ports','removePort'],['[data-remove-connection]','connections','removeConnection']])$('asset-form').querySelectorAll(selector).forEach(el=>el.addEventListener('click',()=>{p[key].splice(Number(el.dataset[attr]),1);markDirty();renderAssetForm();}));
    $('add-port')?.addEventListener('click',()=>{const id=$('port-new-id').value.trim(),loop=$('port-new-loop').value.trim();if(!id||!loop||p.ports.some(a=>a.id===id)){toast('端口 ID、回路 ID 必填，端口 ID 不可重复。');return;}p.ports.push({id,asset_id:$('port-new-asset').value,side:$('port-new-side').value,direction:$('port-new-direction').value,loop_id:loop});markDirty();renderAssetForm();});
    $('add-connection')?.addEventListener('click',()=>{const id=$('connection-new-id').value.trim(),from=$('connection-from').value,to=$('connection-to').value;if(!id||!from||!to||from===to||p.connections.some(c=>c.id===id)){toast('连接需唯一 ID 与两个不同的有效端口。');return;}p.connections.push({id,from_port:from,to_port:to});markDirty();renderAssetForm();});
  }
  async function saveLayout(){const scene=selectedScene();if(isAnalysisScene(scene))throw new Error('Schema 0.2 当前仅支持高级 JSON 配置，不保存图上布局。');if(!scene)throw new Error('没有可保存的场景。');if(state.dirty||state.jsonPending){if(!await confirmAction('另建布局草稿？','配置页有未保存内容。继续将使用当前图对应的场景新建草稿，配置页未保存内容将被替换。'))return;}const modified=clone(scene);if(modified.physical_topology)modified.physical_topology.layout=clone(state.layout);else{modified.extensions||={};modified.extensions.ui_layout=clone(state.layout);}const origin=$('topology-source').value==='config'?state.topologyConfig?.id:state.run?.job?.config_id;const body={name:`${scene.scene_id||'场景'} · 布局草稿`,scene:modified};if(origin)body.source_config_id=origin;const record=await post('/api/configs/draft',body);await refreshCatalog();await loadConfig(record.id);choosePage('config');toast('布局已另存为配置草稿；物理连接与运行配置未改变。');}

  async function loadRunConfig(id=$('run-config').value){const serial=++state.runConfigSerial;state.runConfig=null;updateRunOptions();if(!id){state.runConfig=null;selectOptions($('run-domain'),[],'','先发布配置');updateRunOptions();return;}const record=await api(`/api/configs/${encodeURIComponent(id)}`);if(serial!==state.runConfigSerial)return;state.runConfig=record;$('run-config').value=id;selectOptions($('run-domain'),(record.scene.control_domains||[]).map(d=>({value:d.id,label:isAnalysisScene(record.scene)?`${d.id} · 仅只读分析`:`${d.id} / ${d.cdu_id}`})),$('run-domain').value);updateRunOptions();}
  // 前端能力限制只改善交互。后端仍独立执行模式、控制权、验收和看门狗检查。
  function updateRunOptions(){if(isAnalysisScene(state.runConfig?.scene)){$('run-kind').disabled=true;$('run-mode').disabled=true;$('start-run').disabled=true;$('start-run').hidden=true;$('run-permission-note').textContent='Schema 0.2 仅支持系统总览中的只读水力分析，不能创建热仿真或设备控制任务。';return;}$('start-run').hidden=false;$('run-mode').disabled=false;const config=state.runConfig,d=config?.scene.control_domains?.find(x=>x.id===$('run-domain').value),p=config?.scene.devices?.[d?.cdu_id];const kind=p?.adapter==='thermal_sim'?'thermal_sim':p?.adapter==='modbus_tcp'?'modbus':null;if(kind)$('run-kind').value=kind;$('run-kind').disabled=true;const caps=config?.validation?.capabilities?.find(c=>c.domain_id===d?.id);const modes=caps?.supported_modes||[];const hardware=kind==='modbus';const commissioned=p?.commissioning;
    const serverAllows=!!state.session?.allow_hardware_control;
    for(const option of $('run-mode').options){const mode=option.value;option.disabled=!modes.includes(mode);if(mode==='control'&&hardware)option.disabled=!serverAllows||!modes.includes('shadow')||!p?.write_enabled||!['point_map_verified','limits_verified','local_fallback_tested','watchdog_tested','exclusive_control_tested'].every(key=>commissioned?.[key]===true)||!commissioned?.evidence_reference||!['mode','owner','alarm'].every(key=>key in (p?.points?.status||{}))||!['heartbeat','release'].every(key=>key in (p?.points?.commands||{}));}
    if($('run-mode').selectedOptions[0]?.disabled)$('run-mode').value=[...$('run-mode').options].find(o=>!o.disabled)?.value||'monitor';
    $('speed-field').hidden=hardware;$('run-speed').required=!hardware;$('start-run').disabled=!config||!kind||!d||!modes.length;
    $('run-permission-note').textContent=!kind?'此配置的执行载体不支持从工作台启动。FMU 请使用独立实验入口。':hardware?serverAllows?'现场写控制仍需设备 commissioning 完整验收；后端再次检查。':'服务未启用设备写控制，可按配置使用监测／影子模式。':'合成热仿真 · 不连接硬件；模型与限值来自所选仿真配置。';
  }
  async function startRun(event){event.preventDefault();const button=$('start-run');await action(button,async()=>{if(!state.runConfig||state.runConfig.id!==$('run-config').value||isAnalysisScene(state.runConfig.scene))throw new Error('当前配置不能从此入口创建控制任务，请使用只读分析。');const kind=$('run-kind').value,mode=$('run-mode').value;if(kind==='modbus'&&mode==='control'){if(!await confirmAction('启动设备闭环控制？','此操作将按已发布配置，通过安全网关向实际 Modbus 端点发送设定值。请确认现场控制权、点表和失联接管已经验收。'))return;}const job=await post('/api/jobs',{config_id:$('run-config').value,domain_id:$('run-domain').value,kind,mode,seconds:Number($('run-seconds').value),interval_s:Number($('run-interval').value),speed:kind==='thermal_sim'?Number($('run-speed').value):1});await refreshCatalog();if(job.run_id){state.runId=job.run_id;await loadRun(job.run_id);$('topology-source').value='run';updateTopologySelection();renderTopology();}toast('后台任务已创建。页面刷新不影响任务；进度见后台任务。');});updateRunOptions();}
  function renderJobs(jobs){if(!jobs?.length){$('jobs-list').innerHTML='<div class="empty-state" style="min-height:100px"><p>尚无工作台任务。已有实验记录可在下方选择查看。</p></div>';return;}$('jobs-list').innerHTML=`<div class="table-wrap"><table><thead><tr><th>任务 / 来源</th><th>状态 / 模式</th><th>进度</th><th>控制交还</th><th>操作</th></tr></thead><tbody>${jobs.slice(0,30).map(j=>{const active=j.active||['starting','running','stop_requested','stopping'].includes(j.status);const source=sourceInfo(j.source,active);return `<tr><td><div class="job-name">${esc(j.name||j.id)}</div>${badge(source.label,source.tone)}<div class="fine-print">${esc(j.domain_id||'')}</div></td><td>${badge(human(j.status),j.status==='failed'?'error':active?'teal':'neutral')}<div class="fine-print">${esc(human(j.runtime_mode||j.mode))}</div>${j.error?`<div class="fine-print" style="color:var(--red)">${esc(j.error.message||j.error.code||j.error)}</div>`:''}</td><td>${format(j.elapsed_s,1)} / ${format(j.seconds,1)} s<div class="fine-print">${format(j.cycles,0)} 个周期</div></td><td>${j.handoff_confirmed===true?'已确认本地接管':j.handoff_confirmed===false?'未确认，请核查':j.mode==='control'?'尚无交还记录':'未申请写控制'}</td><td><button class="button text small" data-view-run="${esc(j.run_id)}">查看</button>${active?`<button class="button danger small" data-stop-job="${esc(j.id)}" ${j.status==='stop_requested'?'disabled':''}>请求退出</button>`:''}</td></tr>`;}).join('')}</tbody></table></div>`;
    $('jobs-list').querySelectorAll('[data-view-run]').forEach(b=>b.addEventListener('click',()=>action(b,()=>loadRun(b.dataset.viewRun))));$('jobs-list').querySelectorAll('[data-stop-job]').forEach(b=>b.addEventListener('click',()=>action(b,async()=>{if(!await confirmAction('请求退出后台任务？','服务将结束本软件任务，控制模式下尝试交还本地控制。这个操作不是停泵命令；实际交还结果会记录并显示。'))return;await post(`/api/jobs/${encodeURIComponent(b.dataset.stopJob)}/stop`,{});const data=await api('/api/jobs');renderJobs(data.jobs||[]);toast('已请求退出，等待执行线程处理。');})));
  }
  /* Match each telemetry sample only to a same-cycle or preceding decision.
   * A setpoint readback is always a separate series from proposed targets. */
  function decisionForTelemetry(telemetryEvent){const t=itemPayload(telemetryEvent),decisions=state.events.decision||[];if(t.cycle_id){const exact=decisions.find(e=>itemPayload(e).cycle_id===t.cycle_id&&(!t.session||itemPayload(e).session===t.session));if(exact)return itemPayload(exact);}return itemPayload(decisions.filter(e=>(!t.session||itemPayload(e).session===t.session)&&eventAt(e)<=eventAt(telemetryEvent)).at(-1));}
  function goodValue(reading,transform=v=>v){return reading&&isNumber(reading.value)&&(reading.quality===undefined||reading.quality==='good')?transform(reading.value):null;}
  function renderTrends(){if(state.runLoading){$('trend-source').className='pill neutral';$('trend-source').textContent='正在加载';$('trend-count').textContent='';$('trend-charts').innerHTML='<div class="card empty-state" style="grid-column:1/-1"><h3>正在加载所选运行…</h3><p>加载完成后显示该运行的实际数据。</p></div>';$('run-summary').innerHTML='';return;}const items=state.events.telemetry||[];const source=sourceInfo(state.run?.source,activeRun());$('trend-source').className=`pill ${source.tone}`;$('trend-source').textContent=state.run?source.label:'尚无数据';$('trend-count').textContent=items.length?`显示最近 ${items.length} 个采集周期`:'暂无采集记录';if(!items.length){$('trend-charts').innerHTML='<div class="card empty-state" style="grid-column:1/-1"><span class="empty-icon">⌁</span><h3>等待采集记录</h3><p>启动仿真或选择已有运行后显示曲线。缺测不会用配置初值填充。</p></div>';$('run-summary').innerHTML='';return;}
    const seriesRows=items.map(e=>{const t=itemPayload(e),d=decisionForTelemetry(e);return {at:isNumber(t.timestamp)?t.timestamp:eventAt(e),event:e,t,d};});
    const colors=['#66dcbc','#7aa8f3','#dfb26d','#bd9bdf'];
    const definitions=[{title:'需求流量与实际流量',unit:'kg/s',note:'需求与目标来自同周期或较早决策；灰缺口代表缺测／质量不合格。',series:[['实际流量',r=>goodValue(r.t.observations?.['cdu.sec_flow'])],['边界内目标流量',r=>r.d.target_flow_kg_s],['原始需求流量',r=>r.d.demand_flow_kg_s]]},{title:'压差目标与实际压差',unit:'kPa',note:'目标回读与实际压差分别采集；候选目标不等于设备实际达到。',series:[['实际压差',r=>goodValue(r.t.observations?.['cdu.sec_dp'])],['目标回读',r=>goodValue(r.t.setpoints?.['cdu.dp_sp'])],['模型平衡目标',r=>(r.d.policy_quantity||r.d.request?.quantity||state.run?.scene?.devices?.[r.t.asset_id]?.policy?.control_quantity)==='cdu.dp_sp'?r.d.equilibrium_control_target:null]]},{title:'供回液温度',unit:'°C',note:'只显示 CDU 液体温度；不是机柜或芯片温度。',series:[['供液温度',r=>goodValue(r.t.observations?.['cdu.sec_supply_temp'],v=>v-273.15)],['回液温度',r=>goodValue(r.t.observations?.['cdu.sec_return_temp'],v=>v-273.15)],['回液预测',r=>isNumber(r.d.return_forecast_k)?r.d.return_forecast_k-273.15:null]]},{title:'功率与液侧热负荷',unit:'kW',note:'电功率与热负荷物理含义不同；此图不等于全站能耗或节能结论。',series:[['CDU 电功率',r=>goodValue(r.t.observations?.['cdu.electric_power'],v=>v/1000)],['液侧热负荷',r=>goodValue(r.t.observations?.['cdu.liquid_load'],v=>v/1000)]]}];
    $('trend-charts').innerHTML=definitions.map(def=>{const series=def.series.map(([name,fn],i)=>({name,color:colors[i],data:seriesRows.map(r=>({x:r.at,y:fn(r)}))}));return `<article class="card chart-card"><div class="chart-heading"><h3>${esc(def.title)}</h3><span class="muted">${esc(def.unit)}</span></div><div class="chart-legend">${series.map(s=>`<span style="--series-color:${s.color}">${esc(s.name)}</span>`).join('')}</div>${chartSVG(series,def.title,{nonnegativeAxis:def.unit==='kW'})}<div class="chart-note">${esc(def.note)}</div></article>`;}).join('');
    const faults=state.events.fault||[];const latestFault=itemPayload(faults.at(-1));const calibration=latest('calibration');$('run-summary').innerHTML=`<div class="card run-facts"><span>记录来源：${esc(state.run?.source||'未声明')}</span><span>会话模式：${esc(human(state.run?.session?.requested_mode))}</span><span>校准：${esc(human(calibration.status||'无记录'))}${calibration.reason?' / '+esc(human(calibration.reason)):''}</span><span>所示窗口故障：${faults.length}</span></div>${faults.length?`<div class="notice error"><strong>最近故障</strong><span>${esc(latestFault.reason||latestFault.error||'查看运行事件')}</span></div>`:''}<details class="card" style="margin-top:15px"><summary>查看运行、配置来源与最新事件</summary><pre>${esc(JSON.stringify({run_id:state.run?.id,source:state.run?.source,session:state.run?.session,job:state.run?.job,latest:state.run?.latest},null,2))}</pre></details>`;
  }
  function chartSVG(series,title,{nonnegativeAxis=false}={}){const points=series.flatMap(s=>s.data.filter(p=>isNumber(p.x)&&isNumber(p.y)));if(!points.length)return '<div class="empty-state" style="height:215px;min-height:215px">该记录没有对应的有效测量</div>';const W=560,H=210,L=52,R=18,T=16,B=33;const xmin=Math.min(...points.map(p=>p.x)),xmax=Math.max(...points.map(p=>p.x));let ymin=Math.min(...points.map(p=>p.y)),ymax=Math.max(...points.map(p=>p.y));const margin=Math.max((ymax-ymin)*.12,Math.abs(ymax)*.03,.05),dataMinimum=ymin;ymin-=margin;ymax+=margin;if(nonnegativeAxis&&dataMinimum>=0)ymin=Math.max(0,ymin);const X=x=>L+(x-xmin)/Math.max(xmax-xmin,1)*(W-L-R),Y=y=>H-B-(y-ymin)/(ymax-ymin)*(H-T-B);let grid='';for(let i=0;i<=4;i++){const y=ymin+(ymax-ymin)*i/4,py=Y(y);grid+=`<line class="chart-gridline" x1="${L}" x2="${W-R}" y1="${py}" y2="${py}"/><text class="chart-axis" x="${L-9}" y="${py+3}" text-anchor="end">${format(y,2)}</text>`;}for(let i=0;i<=4;i++){const x=xmin+(xmax-xmin)*i/4;const label=x>1e9?new Date(x*1000).toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit'}):format(x,0)+'s';grid+=`<text class="chart-axis" x="${X(x)}" y="${H-12}" text-anchor="middle">${esc(label)}</text>`;}
    const paths=series.map(s=>{let d='',pen=false;for(const p of s.data){if(!isNumber(p.x)||!isNumber(p.y)){pen=false;continue;}d+=`${pen?'L':'M'}${X(p.x).toFixed(2)},${Y(p.y).toFixed(2)} `;pen=true;}const valid=s.data.filter(p=>isNumber(p.x)&&isNumber(p.y));return `<path class="chart-path" stroke="${s.color}" d="${d}"/>${valid.length===1?`<circle cx="${X(valid[0].x)}" cy="${Y(valid[0].y)}" r="3" fill="${s.color}"/>`:''}`;}).join('');return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}，来自所选运行最近采集记录"><title>${esc(title)}</title>${grid}${paths}</svg>`;
  }

  // 外部预测只读展示与显式导入。ready 是当前时钟下的可用预览，只有
  // usage.used 才表明实际控制周期采用；导入不会创建任务或替换实际热负荷。
  function forecastUnsupported(){return state.forecastState?.status==='unsupported'||state.forecastDescriptor?.status==='unsupported';}
  function updateForecastAvailability(){const disabled=!state.forecastConfigId||forecastUnsupported()||state.forecastDescriptor?.config_id!==state.forecastConfigId;for(const id of ['forecast-import','forecast-validate','forecast-file','forecast-json'])$(id).disabled=disabled;$('forecast-job').disabled=forecastUnsupported();$('forecast-domain').disabled=forecastUnsupported();}
  function refreshForecastSelectors(){
    const configs=state.catalog.configs.filter(c=>c.status==='published');
    if(!configs.some(c=>c.id===state.forecastConfigId))state.forecastConfigId=configs.find(c=>c.id===state.run?.job?.config_id)?.id||configs[0]?.id||'';
    selectOptions($('forecast-config'),configs.map(c=>({value:c.id,label:configLabel(c)})),state.forecastConfigId,'先发布一个配置版本');
    const old=$('forecast-job').value,jobs=(state.catalog.jobs||[]).filter(j=>j.config_id===state.forecastConfigId);
    selectOptions($('forecast-job'),[{value:'',label:'预览 · 仿真起点 / 当前 Unix 时间'},...jobs.map(j=>({value:j.id,label:forecastJobLabel(j)}))],old);
    updateForecastAvailability();
  }
  async function refreshForecast(descriptorNeeded=false){
    const id=$('forecast-config').value||state.forecastConfigId,serial=++state.forecastSerial;
    if(!id){state.forecastDescriptor=null;state.forecastState=null;renderForecast();return;}
    const changed=id!==state.forecastConfigId;state.forecastConfigId=id;
    if(changed){state.forecastState=null;state.forecastDescriptor=null;refreshForecastSelectors();renderForecast();}
    const job=$('forecast-job').value,base=`/api/configs/${encodeURIComponent(id)}/forecast`;
    const descriptorPromise=descriptorNeeded||changed||state.forecastDescriptor?.config_id!==id?api(base+'/descriptor'):Promise.resolve(state.forecastDescriptor);
    const [descriptor,data]=await Promise.all([descriptorPromise,api(base+(job?'?job_id='+encodeURIComponent(job):''))]);
    if(serial!==state.forecastSerial||id!==state.forecastConfigId)return;
    state.forecastDescriptor=descriptor;state.forecastState=data;
    selectOptions($('forecast-domain'),(descriptor.domains||[]).map(d=>({value:d.id,label:`${d.id} / ${d.cdu_id||''}`})),$('forecast-domain').value,'没有控制域');
    renderForecast();
  }
  function forecastReason(reason){
    const map={ready:'可用预览',rejected:'未采用',absent:'尚无预测',used:'本周期已采用',not_used:'本周期未采用',validated_domain_feedforward:'已通过域覆盖与时间检查',participated_in_demand_maximum:'已参与当前热需求与预测峰值的取大计算',forecast_clock_mismatch:'预测与运行时钟不一致',forecast_issued_in_future:'预测发布时间晚于当前参考时刻',forecast_too_old:'预测超过允许数据年龄',forecast_not_yet_valid:'预测尚未进入有效期',forecast_expired:'预测已过期',forecast_does_not_cover_horizon:'预测未覆盖整个策略时间窗',incomplete_domain_rack_coverage:'未覆盖该域全部机柜',ambiguous_rack_domain:'机柜属于多个域，不能重复分配',forecast_above_policy_load_envelope:'预测负荷超过策略配置边界',forecast_does_not_cover_time:'预测样本未覆盖所需时刻',not_executed:'影子建议未写入',monitor_mode:'只读模式不采用预测控制',runtime_fault:'运行周期故障，未完成采用'};
    return map[reason]||human(reason);
  }
  function forecastEmpty(title,text){return `<div class="empty-state"><span class="empty-icon">⌁</span><h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;}
  // 编号可能来自旧记录；内容归属同时核对后端保存的输入哈希。任一侧缺少
  // 哈希时不能把新旧记录当作已验证一致。仅两侧都缺失时保留显式旧版兼容。
  function forecastUsageIdentity(usage,data){
    const id=data?.input?.forecast_id,currentHash=data?.input_sha256,recordHash=usage?.input_sha256;
    if(!id||usage?.forecast_id!==id)return {current:false,verified:false,legacy:false,note:usage?.forecast_id?'此记录对应其他／旧预测包':'此周期未关联预测包'};
    if(currentHash&&recordHash){
      const matches=currentHash===recordHash;
      return {current:matches,verified:matches,legacy:false,note:matches?'当前预测包 · 内容哈希一致':'历史记录 · 编号相同但内容哈希不同'};
    }
    if(!currentHash&&!recordHash)return {current:true,verified:false,legacy:true,note:'旧版记录：仅按编号关联，双方均未记录内容哈希'};
    return {current:false,verified:false,legacy:false,note:!recordHash?'历史记录：缺少内容哈希，无法核验是否对应当前包':'当前输入缺少内容哈希，无法核验记录归属'};
  }
  function renderForecast(){
    updateForecastAvailability();
    if(forecastUnsupported()){
      $('forecast-connection').textContent='当前配置不支持预测分配';$('forecast-connection').className='pill neutral';$('forecast-record-badge').textContent='Schema 0.2 · 只读分析';$('forecast-record-badge').className='pill neutral';$('forecast-usage-badge').textContent='无算法采用能力';$('forecast-usage-badge').className='pill neutral';$('forecast-clear').disabled=true;
      $('forecast-metrics').innerHTML='';$('forecast-racks').innerHTML='';$('forecast-chart').innerHTML=forecastEmpty('尚未实现支路功率预测分配','Schema 0.2 当前仅支持静态水力分析，不将上游功率分配到支路，也不创建设备控制任务。');$('forecast-usage').innerHTML=forecastEmpty('此配置没有预测采用记录','不能把旧版 CDU 运行或其他配置的预测结果归属于此分析配置。');$('forecast-contract').innerHTML='<p class="fine-print">当前配置的功率预测导入与分配接口尚不支持。只读分析入口位于系统总览，分析参数通过高级 JSON 维护。</p>';$('forecast-validation').textContent='分析配置不支持导入功率预测；输入不会发送到现场。';return;
    }
    const data=state.forecastState,descriptor=state.forecastDescriptor,input=data?.input,domain=data?.domains?.find(d=>d.domain_id===$('forecast-domain').value)||data?.domains?.[0],usage=(data?.usage||[]).filter(u=>!domain||u.domain_id===domain.domain_id),currentUsage=usage.filter(u=>forecastUsageIdentity(u,data).current);
    $('forecast-connection').textContent=input?input.source?.kind==='synthetic'?'合成测试预测':'外部预测已接入':'尚未接入';$('forecast-connection').className=`pill ${input?(input.source?.kind==='synthetic'?'blue':'teal'):'neutral'}`;
    $('forecast-record-badge').textContent=domain?forecastReason(domain.status):'无预测包';$('forecast-record-badge').className=`pill ${domain?.status==='ready'?'blue':domain?.status==='rejected'?'warning':'neutral'}`;
    $('forecast-clear').disabled=!input;
    const metrics=[['预测来源',input?.source?.name||'未接入','',input?.forecast_id||'等待上游预测包'],['时间基准',input?(input.clock_basis==='simulation'?'仿真时钟':'Unix 时间'):'—','',data?.reference?`参考 ${atLabel(data.reference.now)}`:'未选择运行参考'],['未来液侧峰值',isNumber(domain?.peak_liquid_w)?format(domain.peak_liquid_w/1000):'—','kW 热',domain?.horizon_s?`${domain.horizon_s}s 策略窗口内预测`:'当前没有可用预测窗口'],['机柜覆盖',`${domain?.racks?.length||0} / ${descriptor?.domains?.find(d=>d.id===domain?.domain_id)?.served_racks?.length||0}`,'',domain?forecastReason(domain.reason):'发布配置后按域检查']];
    $('forecast-metrics').innerHTML=metrics.map(([label,value,unit,note])=>`<div class="metric-card"><div class="metric-label">${esc(label)}</div><div class="metric-value" style="font-size:${label==='预测来源'?'17':'23'}px">${esc(value)}<small>${esc(unit)}</small></div><div class="metric-note">${esc(note)}</div></div>`).join('');
    if(domain?.series?.length){const series=[{name:'电功率预测',color:'#7aa8f3',data:domain.series.map(p=>({x:p.at,y:isNumber(p.electric_w)?p.electric_w/1000:null}))},{name:'液侧热负荷预测',color:'#65d9b9',data:domain.series.map(p=>({x:p.at,y:isNumber(p.liquid_w)?p.liquid_w/1000:null}))}];$('forecast-chart').innerHTML=`<div class="chart-legend">${series.map(s=>`<span style="--series-color:${s.color}">${s.name} / kW</span>`).join('')}</div>${chartSVG(series,'机柜电功率与液侧热负荷预测',{nonnegativeAxis:true})}<div class="chart-note">${input?.selection==='upper_bound'?'采用上界预测':'采用中心预测'} · 按液冷分担比例转换 · 当前预览不代表已经执行控制</div>`;}else $('forecast-chart').innerHTML=forecastEmpty(input?'此时刻没有可用预测':'等待预测输入',input?forecastReason(domain?.reason||'forecast_absent'):'选择已发布配置，并导入与其机柜或节点对应的功率时间序列。');
    $('forecast-racks').innerHTML=domain?.racks?.length?`<div class="table-wrap"><table><thead><tr><th>机柜</th><th>预测来源资产</th><th>液侧预测峰值</th><th>窗口采样点</th></tr></thead><tbody>${domain.racks.map(r=>`<tr><td class="forecast-rack-id">${esc(r.rack_id)}</td><td>${esc((r.source_assets||[]).join('、'))}</td><td>${format(r.peak_liquid_w/1000)} kW 热</td><td>${r.series?.length||0}</td></tr>`).join('')}</tbody></table></div>`:'';
    const used=currentUsage.some(u=>u.status==='used'&&forecastUsageIdentity(u,data).verified),legacyUsed=currentUsage.some(u=>u.status==='used'&&forecastUsageIdentity(u,data).legacy);$('forecast-usage-badge').textContent=used?'有实际采用记录':legacyUsed?'旧版采用记录 · 仅编号匹配':currentUsage.length?'已有周期记录':usage.length?'历史记录 / 归属待核验':'暂无运行采用记录';$('forecast-usage-badge').className=`pill ${used?'teal':legacyUsed?'blue':'neutral'}`;
    $('forecast-usage').innerHTML=usage.length?`<div class="table-wrap"><table><thead><tr><th>运行 / 时间</th><th>预测编号</th><th>采用结果</th><th>原因</th></tr></thead><tbody>${usage.map(u=>`<tr><td><button class="button text small" data-forecast-run="${esc(u.run_id)}">查看运行</button><div class="fine-print">${esc(atLabel(u.now))}</div></td><td>${esc(u.forecast_id||'无预测')}<div class="fine-print">${esc(forecastUsageIdentity(u,data).note)}</div></td><td>${badge(forecastReason(u.status),u.status==='used'?(forecastUsageIdentity(u,data).verified?'teal':'blue'):u.status==='rejected'?'warning':'neutral')}</td><td>${esc(forecastReason(u.reason))}<div class="fine-print">${esc(human(u.policy_status||''))}</div></td></tr>`).join('')}</tbody></table></div><div class="card-body"><p class="fine-print">“已采用”表示预测参与了热需求计算，不代表生成了新命令、过程已到位或能耗降低。</p></div>`:forecastEmpty('尚无采用记录','校验与导入仅提供输入。匹配配置的任务运行后，由实际控制周期记录是否采用。');
    $('forecast-usage').querySelectorAll('[data-forecast-run]').forEach(b=>b.addEventListener('click',()=>action(b,async()=>{await loadRun(b.dataset.forecastRun);choosePage('decisions');})));
    const base=state.forecastConfigId?`/api/configs/${state.forecastConfigId}/forecast`:'/api/configs/{config_id}/forecast';
    $('forecast-contract').innerHTML=`<span class="section-label">导入接口</span><code>POST ${esc(base)}<br>X-LC-CSRF: 当前会话令牌</code><p class="fine-print">仅接收已发布配置。导入后，匹配任务从后续周期读取；不会创建任务。</p><span class="section-label">必需字段</span><code>forecast_id · source · clock_basis<br>issued_at · valid_from · valid_until<br>max_age_s · unit="W_e" · selection<br>entries[{asset_id, liquid_fraction,<br>  samples[{at, power_w, upper_power_w?}]}]</code><p class="fine-print">simulation 使用每次运行起点为 0 的相对秒，source.kind 为 synthetic；unix 使用 Unix 秒，source.kind 为 external。手动测试请明确标为 synthetic。</p><span class="section-label">覆盖规则</span>${(descriptor?.notes||['先发布配置以读取与现场资产一致的接口契约。']).map(n=>`<p class="fine-print">${esc(n)}</p>`).join('')}<details><summary>可引用资产（${descriptor?.assets?.length||0}）</summary><div class="table-wrap"><table><tbody>${(descriptor?.assets||[]).map(a=>`<tr><td>${esc(a.id)}</td><td>${esc(kindLabels[a.kind]||a.kind)}</td><td>${esc(a.rack_id||'')}</td></tr>`).join('')}</tbody></table></div></details>`;
  }
  function forecastPayload(){if(forecastUnsupported())throw new Error('当前分析配置不支持功率预测分配。');if(!state.forecastConfigId)throw new Error('请先选择已发布配置。');const text=$('forecast-json').value.trim();if(!text)throw new Error('请粘贴或选择上游预测 JSON 文件。');const payload=JSON.parse(text);if(!payload||typeof payload!=='object'||Array.isArray(payload))throw new Error('预测包必须是 JSON 对象。');if($('forecast-job').value)payload.job_id=$('forecast-job').value;else delete payload.job_id;return payload;}
  function renderForecastValidation(result){const box=$('forecast-validation');const issues=(result.errors||[]).map(e=>e.message||e.code||e);const domains=result.domains||[];box.innerHTML=result.valid===false?`<div class="notice error">${issues.map(e=>esc(forecastReason(e))).join('<br>')}</div>`:`<div class="notice ${domains.some(d=>d.status==='rejected')?'warning':'info'}"><span>预测包结构校验通过；尚未保存。${domains.map(d=>`<br>${esc(d.domain_id)}：${esc(forecastReason(d.status))} · ${esc(forecastReason(d.reason))}`).join('')}</span></div>`;}
  function bindForecastEvents(){
    $('forecast-config').addEventListener('change',()=>action(null,()=>refreshForecast(true)));
    $('forecast-domain').addEventListener('change',renderForecast);
    $('forecast-job').addEventListener('change',()=>action(null,()=>refreshForecast(false)));
    $('forecast-refresh').addEventListener('click',e=>action(e.currentTarget,()=>refreshForecast(true)));
    $('forecast-file').addEventListener('change',e=>action(null,async()=>{const file=e.target.files?.[0];if(!file)return;if(file.size>2*1024*1024)throw new Error('预测 JSON 超过 2 MB，请精简时间序列后导入。');$('forecast-json').value=await file.text();$('forecast-validation').textContent='文件已载入编辑区，尚未校验或导入。';}));
    $('forecast-json').addEventListener('input',()=>{$('forecast-validation').textContent='内容已修改，尚未校验或导入。';});
    $('forecast-validate').addEventListener('click',e=>action(e.currentTarget,async()=>{const result=await post(`/api/configs/${encodeURIComponent(state.forecastConfigId)}/forecast/validate`,forecastPayload());renderForecastValidation(result);}));
    $('forecast-import').addEventListener('click',e=>action(e.currentTarget,async()=>{const payload=forecastPayload();const result=await post(`/api/configs/${encodeURIComponent(state.forecastConfigId)}/forecast/validate`,payload);renderForecastValidation(result);if(!result.valid)throw new Error('预测包未通过校验，未导入。');if(!await confirmAction('导入此预测包？','导入后，使用此发布配置的匹配任务会在后续周期读取。只读模式不会控制设备；无匹配任务时仅提供预览。'))return;state.forecastState=await post(`/api/configs/${encodeURIComponent(state.forecastConfigId)}/forecast`,payload);renderForecast();$('forecast-validation').textContent='预测已导入。是否采用请查看下方实际周期记录。';toast('预测已导入，没有新建运行任务。');}));
    $('forecast-clear').addEventListener('click',e=>action(e.currentTarget,async()=>{if(!await confirmAction('清除当前预测输入？','匹配任务从后续周期恢复仅使用原有观测反馈。此操作不会停止任务或发送停泵命令。'))return;await post(`/api/configs/${encodeURIComponent(state.forecastConfigId)}/forecast/clear`,{});await refreshForecast(false);toast('已清除预测输入，原有观测反馈保持运行。');}));
    $('zoom-in').addEventListener('click',()=>{state.zoom=Math.min(2.5,(state.zoom==='fit'?1:state.zoom)+.2);drawTopology();});
    $('zoom-out').addEventListener('click',()=>{state.zoom=Math.max(.5,(state.zoom==='fit'?1:state.zoom)-.2);drawTopology();});
    $('zoom-fit').addEventListener('click',()=>{state.zoom='fit';drawTopology();$('topology-canvas').scrollLeft=0;$('topology-canvas').scrollTop=0;});
  }

  async function editViewedConfig(){
    const id=$('topology-source').value==='config'?state.topologyConfig?.id:state.run?.job?.config_id;
    if(id&&id!==state.config?.id){
      if((state.dirty||state.jsonPending)&&!await confirmAction('切换到图中配置？','配置页有未保存修改。继续将加载当前图对应的配置。'))return;
      await loadConfig(id);
    }
    choosePage('config');
  }
  function bindEvents(){$('run-analysis').addEventListener('click',()=>runHydraulicAnalysis().catch(e=>toast(errorMessage(e))));$('analysis-domain').addEventListener('change',e=>{clearAnalysis();state.analysisDomainId=e.target.value;renderAnalysis();});document.querySelectorAll('[data-page]').forEach(b=>b.addEventListener('click',()=>choosePage(b.dataset.page)));window.addEventListener('hashchange',()=>choosePage(location.hash.slice(1)));$('go-config').addEventListener('click',()=>editViewedConfig().catch(e=>toast(errorMessage(e))));
    $('refresh-button').addEventListener('click',e=>action(e.currentTarget,async()=>{await refreshCatalog();await loadRun();setConnection(true);}));
    $('topology-source').addEventListener('change',()=>action(null,async()=>{state.topologySource=$('topology-source').value;clearAnalysis();renderAnalysis();updateTopologySelection();state.layoutContext=null;if(state.topologySource==='config')await loadTopologyConfig($('topology-selection').value);else await loadRun($('topology-selection').value);}));
    $('topology-selection').addEventListener('change',e=>action(null,()=> $('topology-source').value==='run'?loadRun(e.target.value):loadTopologyConfig(e.target.value)));
    $('reset-layout').addEventListener('click',()=>{state.layout={};drawTopology();$('layout-note').textContent='布局已重置；保存到新草稿后持久化';});$('save-layout').addEventListener('click',e=>action(e.currentTarget,saveLayout));
    ['decision-run','trend-run'].forEach(id=>$(id).addEventListener('change',e=>action(null,()=>loadRun(e.target.value))));$('follow-latest').addEventListener('change',e=>{state.follow=e.target.checked;renderDecisionSelector();});$('decision-event').addEventListener('change',e=>{state.follow=false;$('follow-latest').checked=false;state.decisionId=e.target.value;renderDecision();});
    $('config-select').addEventListener('change',async e=>{const next=e.target.value;if(state.dirty||state.jsonPending){if(!await confirmAction('切换配置？','当前草稿有未保存修改。继续将放弃这些修改；已运行的配置不会受影响。')){e.target.value=state.config?.id||'';return;}}await action(null,()=>loadConfig(next));});
    $('copy-config').addEventListener('click',copyToDraft);document.querySelectorAll('[data-config-tab]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('[data-config-tab]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.config-tab').forEach(x=>x.classList.toggle('active',x.id===`config-${b.dataset.configTab}`));}));
    $('scene-json').addEventListener('input',()=>{state.jsonPending=true;$('json-status').textContent='尚未应用';updateConfigActions();});$('apply-json').addEventListener('click',e=>action(e.currentTarget,async()=>{applySceneJson($('scene-json').value);renderBasicForm();renderAssetForm();$('json-status').textContent='已应用到本地草稿，尚未保存';}));$('discard-json').addEventListener('click',e=>action(e.currentTarget,async()=>{if(!state.jsonPending)return;if(!await confirmAction('放弃尚未应用的 JSON 修改？','仅撤销 JSON 编辑区尚未应用的内容，恢复为当前本地草稿。之前已修改的参数和已保存配置均不受影响。'))return;discardSceneJson();$('json-status').textContent='已放弃未应用的 JSON 修改，恢复为当前本地草稿';}));
    $('validate-config').addEventListener('click',e=>action(e.currentTarget,async()=>{const v=await validateConfig();toast(v.valid?'校验完成；请查看警告与可用模式。':'校验发现错误，请查看右侧。');}));$('save-config').addEventListener('click',e=>action(e.currentTarget,saveConfig));$('publish-config').addEventListener('click',e=>action(e.currentTarget,publishConfig));
    $('run-config').addEventListener('change',()=>action(null,()=>loadRunConfig()));$('run-domain').addEventListener('change',updateRunOptions);$('run-kind').addEventListener('change',updateRunOptions);$('start-run-form').addEventListener('submit',startRun);
    window.addEventListener('beforeunload',e=>{if(state.dirty||state.jsonPending){e.preventDefault();e.returnValue='';}});
  }
  // 轮询用互斥标记避免慢请求堆积；刷新页面仅重新订阅已有记录。
  async function poll(){if(state.polling)return;state.polling=true;try{await refreshCatalog();if(state.runId)await loadRun();else renderLiveViews();await refreshSite();if(state.page==='forecast')await refreshForecast(false);setConnection(true);}catch(error){setConnection(false,error);}finally{state.polling=false;}}
  async function initialize(){bindEvents();bindForecastEvents();document.querySelectorAll('[data-page]').forEach(b=>{const span=b.querySelector('.nav-icon');if(span)span.innerHTML=icon(b.dataset.page);});choosePage(location.hash.slice(1)||'topology');try{state.session=await api('/api/session');$('app-version').textContent=state.session.version?`v${state.session.version}`:'';await refreshCatalog(true);if($('topology-source').value==='config')await loadTopologyConfig($('topology-selection').value);await loadRun();await refreshSite();setConnection(true);}catch(error){setConnection(false,error);renderLiveViews();}setInterval(poll,3000);}
  initialize();
})();
