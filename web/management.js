"use strict";
const managementNav = [
  ["controls", "⏻", "组件管理"], ["model-settings", "✧", "模型人设"],
  ["bot-settings", "⚙", "运行设置"], ["log-panel", "≡", "运行日志"], ["qq-login", "↗", "QQ 登录"]
];
document.querySelector(".rail nav").insertAdjacentHTML("beforeend", managementNav.map(([id,icon,name]) =>
  `<a href="#${id}" class="nav-item" data-open="${id}"><span>${icon}</span>${name}</a>`).join(""));

document.querySelector(".stats").insertAdjacentHTML("afterend", `
<section class="panel control-panel" id="controls" aria-labelledby="control-title">
 <div class="panel-head"><div><div class="eyebrow">CONTROL CENTER</div><h2 id="control-title">所有操作，都在这里</h2></div><span class="tag">管理台独立运行</span></div>
 <div class="control-primary"><button class="button primary" data-action="bot_start">启动机器人</button><button class="button" data-action="bot_stop">停止机器人</button><button class="button" data-action="bot_restart">重启机器人</button><button class="button danger" data-action="stack_stop">停止机器人与 NapCat</button><span id="bot-process">检查进程…</span></div>
 <div class="component-grid">
  <div class="component"><div><strong>Ollama 服务</strong><span id="ollama-service-status">检查中</span></div><div class="button-row"><button class="button small" data-action="ollama_start">启动服务</button><button class="button small" data-action="ollama_stop" data-confirm="停止 Ollama 会先停止聊天机器人，管理台和模型文件会保留。继续吗？">停止服务</button><button class="button small" data-action="model_load">加载模型</button><button class="button small" data-action="model_unload">卸载模型内存</button></div></div>
  <div class="component"><div><strong>NapCat / QQ</strong><span id="napcat-service-status">检查中</span></div><div class="button-row"><button class="button small" data-action="napcat_start">启动 NapCat</button><button class="button small" data-action="napcat_restart">重启 NapCat</button><button class="button small" data-action="napcat_stop">停止 NapCat</button><button class="button small" data-action="connection_check">检查群连接</button><a class="button small" href="#qq-login" data-open="qq-login">扫码登录 ↗</a></div></div>
 </div>
 <div id="operation-message" class="operation-message" role="status">停止机器人后，管理台仍然可用。关闭网页不会停止后台组件。</div>
</section>`);

const settingFields = [
 ["model","使用模型","text"], ["group_name","目标群名称","text"], ["group_id","目标群号","number",1,1e16],
 ["command_user_id","指令管理员 QQ","text"], ["num_ctx","上下文 tokens","number",2048,32768],
 ["num_predict","生成额度（含思考）","number",64,8192], ["model_timeout_seconds","模型超时（秒）","number",10,900],
 ["chime_queue_timeout_seconds","插话排队时限（秒）","number",1,300], ["chime_probability","插话概率（%）","number",0,100],
 ["chime_cooldown","插话冷却（秒）","number",0,3600], ["group_log_size","群记录条数","number",1,200],
 ["prompt_log_chars","记录字符上限","number",100,16000], ["onebot_url","OneBot 本机地址","text"],
 ["ollama_url","Ollama 本机地址","text"]
];
document.querySelector(".page-footer").insertAdjacentHTML("beforebegin", `
<details class="panel management-details" id="model-settings"><summary><span><span class="eyebrow">MODEL & PERSONA</span><strong>模型、人设与本地试聊</strong></span><span>编辑 / 构建 / 测试 ＋</span></summary>
 <div class="settings-body"><p class="help-text">编辑当前自定义模型的 Modelfile。仅保存文件不会更新模型，点击“保存并构建”后才生效。思考开关、模型选择和上下文在运行设置中调整。</p><div class="button-row"><button class="button small" id="reload-settings">重新读取设置</button><span id="model-file-status" class="help-text">尚未读取</span></div>
 <label class="field">Modelfile<textarea id="modelfile-editor" class="code-editor" spellcheck="false" rows="15" aria-label="模型 Modelfile"></textarea></label><div class="button-row"><button class="button" id="save-modelfile">仅保存文件</button><button class="button primary" id="build-model">保存并构建模型</button></div>
 <div class="test-chat"><h3>本地试聊</h3><p class="help-text">使用当前模型与运行参数；测试消息不会发到 QQ。</p><form id="test-form" class="test-input"><input id="test-message" maxlength="4000" placeholder="输入一条消息，检查回复效果" aria-label="模型测试消息" required><button class="button primary" type="submit">发送测试</button></form><div class="preview-grid"><div><span class="eyebrow">模型思考</span><pre id="test-thinking">等待测试</pre></div><div><span class="eyebrow">最终回复</span><pre id="test-answer">等待测试</pre></div></div></div></div></details>

<details class="panel management-details" id="bot-settings"><summary><span><span class="eyebrow">BOT SETTINGS</span><strong>群聊、回复策略与模型参数</strong></span><span>保存后自动应用 ＋</span></summary>
 <div class="settings-body"><p class="help-text">保存会重启正在运行的机器人；机器人停止时只保存。新 Token 留空会保留原值。</p><form id="settings-form"><div class="form-grid">
 ${settingFields.map(([name,label,type,min,max])=>`<label class="field">${label}<input name="${name}" type="${type}" ${min!==undefined?`min="${min}" max="${max}"`:""} ${name==="model"?'list="available-models"':""} required></label>`).join("")}
 <datalist id="available-models"></datalist><label class="field">新 OneBot Token<input name="onebot_token" type="password" autocomplete="new-password" placeholder="留空保留原值"></label><label class="field">忽略账号（逗号分隔）<input name="ignore_user_ids" placeholder="其他机器人的 QQ 号"></label></div>
 <div class="check-row"><label><input type="checkbox" name="think"> 开启模型思考</label><label><input type="checkbox" name="clear_onebot_token"> 清除已存 OneBot Token</label><span id="token-setting-status" class="help-text"></span></div><button class="button primary" type="submit">保存并应用运行设置</button></form></div></details>

<details class="panel management-details" id="log-panel"><summary><span><span class="eyebrow">LOGS & EXPORTS</span><strong>运行日志与历史导出</strong></span><span>查看 / 下载 ＋</span></summary><div class="settings-body"><div class="button-row"><label class="field inline">日志来源<select id="log-source"><option value="bot">机器人</option><option value="ollama">Ollama</option><option value="console">网页管理台</option><option value="napcat">NapCat</option></select></label><button class="button small" id="refresh-logs">刷新日志</button><a class="button small" href="/api/export" download>导出当前思考与回复</a></div><pre class="log-view" id="log-text">展开后读取日志</pre><h3>已归档的文本</h3><div id="export-files" class="export-files">尚未读取</div></div></details>

<details class="panel management-details" id="qq-login"><summary><span><span class="eyebrow">QQ LOGIN</span><strong>NapCat 登录与连接设置</strong></span><span>扫码授权 ＋</span></summary><div class="settings-body"><p class="help-text">先启动 NapCat，再在官方 WebUI 扫码授权。登录成功后，运行中的机器人会自动连接。若嵌入页面无法显示，可独立打开。</p><div class="button-row"><a class="button" href="http://127.0.0.1:6099" target="_blank" rel="noopener">独立打开 NapCat WebUI ↗</a><button class="button" id="reload-napcat">刷新登录面板</button><button class="button small" id="show-napcat-token">查看本机 WebUI Token</button><code id="napcat-token" class="hidden"></code></div><iframe id="napcat-frame" title="NapCat QQ 登录面板" data-src="http://127.0.0.1:6099" referrerpolicy="no-referrer"></iframe></div></details>
<dialog id="notes-dialog"><form id="notes-form"><h3 id="notes-title">群友备注</h3><p class="help-text">每行一条，最多 5 条，每条最多 40 字。</p><textarea id="notes-editor" rows="5" maxlength="204" aria-label="群友备注"></textarea><div class="button-row"><button type="button" class="button" id="cancel-notes">取消</button><button class="button primary" type="submit">保存备注</button></div></form></dialog><div class="toast hidden" id="toast" role="status"></div>`);
document.querySelector(".page-footer>span").textContent="鲸鱼管理台 / 关闭网页不影响后台运行";
document.querySelector("#activity .panel-description").insertAdjacentHTML("afterend",'<a class="text-button export-link" href="/api/export" download>导出消息、思考与回复 ↓</a>');
document.querySelector("#memory .panel-head").insertAdjacentHTML("beforeend",'<button class="button small danger" data-action="clear_context" data-confirm="清空全群上下文、所有个人消息池与当前任务记录？昵称、统计和备注会保留。">清空全部聊天记忆</button>');
$("person-notes").insertAdjacentHTML("afterend",'<div class="person-actions"><button class="button small" id="edit-notes">编辑备注</button><button class="button small danger" id="clear-person-pool">清空个人消息池</button></div>');

let settingsLoaded=false, settingsLoading=null, busy=false, previousOperation=null, toastTimer=null, logTimer=null;
function toast(message) { text("toast",message);classToggle("toast","hidden",false);clearTimeout(toastTimer);toastTimer=setTimeout(()=>classToggle("toast","hidden",true),5500); }
async function jsonFetch(path, options={}) {
  const response=await fetch(path,{cache:"no-store",signal:AbortSignal.timeout(15000),...options});
  const data=await response.json(); if(!response.ok)throw new Error(data.error || `请求失败 ${response.status}`);return data;
}
async function act(action,data={}) {
  if(!state?.csrf_token){toast("管理台尚未连接，请稍候");return;}
  try {
    const result=await jsonFetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf_token},body:JSON.stringify({action,data})});
    busy=true;state.operation=result.operation;renderManagement(state);return result;
  } catch(error){toast(error.message);return null;}
}
function renderManagement(s) {
  const op=s.operation;busy=op && ["queued","running"].includes(op.status);
  document.querySelectorAll("[data-action], #save-modelfile, #build-model, #settings-form button[type=submit], #test-form button").forEach(b=>b.disabled=Boolean(busy));
  text("bot-process",s.bot_running?"机器人进程运行中":"机器人已停止 · 页面保持在线");
  text("ollama-service-status",s.ollama.state==="online"?s.ollama.loaded?"在线 · 当前模型已加载":"在线 · 模型待加载":"服务未启动");
  text("napcat-service-status",s.connected?"容器运行 · QQ 已连接":s.services?.napcat==="running"?"容器运行 · 等待 QQ 登录":s.services?.docker?"容器已停止":"Docker 未启动");
  if(op) {
    text("operation-message",`${busy?"◌ ":op.status==="error"?"! ":"✓ "}${op.message}`);
    classToggle("operation-message","error",op.status==="error");
    if(!busy && previousOperation!==op.id){previousOperation=op.id;toast(op.message);}
  }
  if(s.preview){text("test-thinking",s.preview.thinking || (s.preview.status==="running"?"等待思考片段…":"等待测试"));text("test-answer",s.preview.answer || "等待最终回复");}
  html("available-models",(s.models || []).map(m=>`<option value="${esc(m.name)}"></option>`).join(""));
}

async function loadSettings(force=false) {
  if(settingsLoaded && !force)return;
  if(settingsLoading)return settingsLoading;
  settingsLoading=(async()=>{
    try {
      const result=await jsonFetch("/api/settings"), form=$("settings-form");
      for(const [name] of settingFields){let value=result.config[name];if(name==="chime_probability")value=(value??1)*100;form.elements.namedItem(name).value=value ?? "";}
      form.elements.namedItem("ignore_user_ids").value=(result.config.ignore_user_ids || []).join(", ");
      form.elements.namedItem("think").checked=Boolean(result.config.think);
      form.elements.namedItem("onebot_token").value="";form.elements.namedItem("clear_onebot_token").checked=false;
      $("modelfile-editor").value=result.modelfile;settingsLoaded=true;
      text("model-file-status","已读取 models/deepseek.modelfile");text("token-setting-status",result.onebot_token_set?"已有 Token，网页不回显":"当前没有 OneBot Token");
    }catch(error){toast(error.message);}finally{settingsLoading=null;}
  })();return settingsLoading;
}
async function readLogs() {try{const result=await jsonFetch(`/api/logs?source=${encodeURIComponent($("log-source").value)}`);text("log-text",result.text);}catch(error){text("log-text",error.message);}}
async function readExports() {try{const result=await jsonFetch("/api/exports");html("export-files",result.files.map(f=>`<a href="/api/download?name=${encodeURIComponent(f.name)}" download>${esc(f.name)} <small>${Math.ceil(f.bytes/1024)} KB ↓</small></a>`).join("") || "暂无归档文本");}catch(error){text("export-files",error.message);}}
function openSection(id) {const node=$(id);if(node?.tagName==="DETAILS")node.open=true;}
document.addEventListener("click",event=>{
  const control=event.target.closest("[data-action]");
  if(control){if(control.dataset.confirm && !confirm(control.dataset.confirm))return;act(control.dataset.action);}
  const link=event.target.closest("[data-open]");if(link){openSection(link.dataset.open);document.querySelectorAll(".nav-item").forEach(n=>n.classList.toggle("active",n===link));}
});
for(const id of ["model-settings","bot-settings","log-panel","qq-login"]){$(id).addEventListener("toggle",()=>{
  if(!$(id).open)return;
  if(["model-settings","bot-settings"].includes(id))loadSettings();
  if(id==="log-panel"){readLogs();readExports();}
  if(id==="qq-login" && !$("napcat-frame").getAttribute("src"))$("napcat-frame").src=$("napcat-frame").dataset.src;
});}
$("reload-settings").addEventListener("click",()=>{if(confirm("重新读取会覆盖页面中尚未保存的编辑，继续吗？"))loadSettings(true);});
$("save-modelfile").addEventListener("click",()=>act("model_save",{modelfile:$("modelfile-editor").value}));
$("build-model").addEventListener("click",()=>act("model_build",{modelfile:$("modelfile-editor").value}));
$("settings-form").addEventListener("submit",async event=>{
  event.preventDefault();const form=event.currentTarget, config={};
  for(const [name,,type] of settingFields)config[name]=type==="number"?Number(form.elements.namedItem(name).value):form.elements.namedItem(name).value.trim();
  config.chime_probability/=100;config.think=form.elements.namedItem("think").checked;
  config.ignore_user_ids=form.elements.namedItem("ignore_user_ids").value.split(/[,，\s]+/).filter(Boolean);
  config.onebot_token=form.elements.namedItem("onebot_token").value;config.clear_onebot_token=form.elements.namedItem("clear_onebot_token").checked;
  await act("config_save",{config});
});
$("test-form").addEventListener("submit",event=>{event.preventDefault();act("model_test",{text:$("test-message").value.trim()});});
$("refresh-logs").addEventListener("click",readLogs);$("log-source").addEventListener("change",readLogs);
logTimer=setInterval(()=>{if(!document.hidden && $("log-panel").open)readLogs();},6000);
$("reload-napcat").addEventListener("click",()=>{$("napcat-frame").src=$("napcat-frame").dataset.src;});
$("show-napcat-token").addEventListener("click",async()=>{try{const r=await jsonFetch("/api/napcat-token");text("napcat-token",r.token);classToggle("napcat-token","hidden",false);}catch(error){toast(error.message);}});
$("edit-notes").addEventListener("click",()=>{const p=state?.profiles[selectedPerson];if(!p)return;text("notes-title",p.nick+"的备注");$("notes-editor").value=(p.notes || []).join("\n");$("notes-dialog").showModal();});
$("cancel-notes").addEventListener("click",()=>$("notes-dialog").close());
$("notes-form").addEventListener("submit",async event=>{event.preventDefault();const notes=$("notes-editor").value.split("\n").map(x=>x.trim()).filter(Boolean);if(notes.length>5 || notes.some(n=>n.length>40)){toast("最多 5 条备注，每条最多 40 字");return;}if(await act("save_notes",{uid:selectedPerson,notes}))$("notes-dialog").close();});
$("clear-person-pool").addEventListener("click",()=>{if(selectedPerson && confirm("清空此人的个人消息池？全群滚动记录仍保留；需要清空全部对话时请使用“清空全部聊天记忆”。"))act("clear_pool",{uid:selectedPerson});});
if(location.hash)openSection(location.hash.slice(1));
