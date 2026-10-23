"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {queued:"排队中",context:"整理上下文",loading:"等待模型",thinking:"思考中",generating:"生成回复",cleaning:"整理回复",sending:"发送中",sent:"已发送",skipped:"已跳过",error:"处理失败",cancelled:"已取消"};
const stepIndex = {queued:0,context:1,loading:2,thinking:2,generating:3,cleaning:3,sending:4,sent:5};
let state = null, selectedTrace = null, selectedPerson = null, tab = "thinking", failed = false;
const stamp = (seconds, date=false) => seconds ? new Date(seconds*1000).toLocaleString("zh-CN", date ? {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"} : {hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}) : "—";
function duration(seconds) { seconds = Math.max(0, Math.floor(seconds || 0)); return seconds >= 3600 ? `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分` : seconds >= 60 ? `${Math.floor(seconds/60)} 分 ${seconds%60} 秒` : `${seconds} 秒`; }
function text(id, value) { const element=$(id), next=String(value ?? ""); if(element.textContent!==next) element.textContent=next; }
function html(id, value) { if($(id).innerHTML!==value) $(id).innerHTML=value; }
function classToggle(id, name, enabled) { $(id).classList.toggle(name, Boolean(enabled)); }
function currentTrace() { return state?.traces.find(t=>t.id===selectedTrace) || state?.traces.find(t=>t.id===state.current_id) || state?.traces[0]; }

function render() {
  if(!state) return;
  const config=state.config, profiles=Object.entries(state.profiles), online=state.connected && !state.stopping;
  text("subtitle", `${config.group_name || "目标群"} · ${config.group_id || ""}　/　${state.bot_name}的本地观察窗口`);
  text("connection", failed ? "管理台离线" : online ? "运行中 · 实时同步" : state.bot_running ? "QQ 等待连接" : "机器人已停止");
  classToggle("connection","offline",failed || !online);
  // 保留状态点，不通过群聊正文构造 HTML。
  if(!$("connection").querySelector("i")) $("connection").prepend(document.createElement("i"));
  html("onebot", `${online ? "已连接" : "未连接"}<span class="mini-dot ${online?"":"muted"}"></span>`);
  text("group-name", `${config.group_name || "目标群"} · ${online?"正在监听群消息":"等待 NapCat 连接"}`);
  text("model-name", config.model || "未设置模型");
  const model=state.ollama;
  text("model-state", model.state==="offline" ? "Ollama 离线 · 检查本地服务" : model.state==="checking" ? "正在检查 Ollama…" : `${model.loaded?"已加载":"服务在线 · 模型待加载"} · ${Math.round((config.num_ctx || 4096)/1024)}K 上下文 · ${config.think?"思考开启":"思考关闭"}`);
  text("queue-count",state.queue_size);
  text("run-count",`本次收到 ${state.received} 条 · 已完成回复 ${state.sent} 条`);
  text("people-count",profiles.length);
  text("pool-count",`${profiles.reduce((n,[,p])=>n+p.pool.length,0)} 条记忆 · 每人最多 ${state.pool_capacity} 条`);
  text("uptime",`本次运行 ${duration(state.now-state.started_at)}`);
  renderTask(); renderTraces(); renderPeople();
  if(typeof renderManagement === "function") renderManagement(state);
  const event=state.events[0]; text("last-event",event ? `${stamp(event.time)} · ${event.text}` : "正在监听目标群…");
}

function renderTask() {
  const task=currentTrace(), active=task?.status==="active";
  text("task-status", task ? labels[task.stage] || task.stage : "等待消息");
  classToggle("task-status","bad",task?.status==="error");
  text("target-avatar", task?.nick?.slice(0,1) || "鲸"); text("target-user",task?.nick || "下一位聊天的群友");
  text("target-time",stamp(task?.queued_at)); text("target-mode",task ? ({must:"点名回复",chime:"自然插话",say:"管理员指令"}[task.mode] || task.mode) : "监听中");
  text("target-text",task?.input || "鲸鱼正在等群里的下一条消息。");
  const index=stepIndex[task?.stage] ?? (task?.steps ? Math.max(...task.steps.map(s=>stepIndex[s.stage] ?? 0)) : -1);
  document.querySelectorAll(".pipeline>div").forEach((node,i)=>{node.classList.toggle("done",i<index);node.classList.toggle("current",i===index);});
  text("elapsed",task?.started_at ? `${duration((task.ended_at || state.now)-task.started_at)}${task.attempt>1?" · 重试 1 次":""}` : task ? "正在排队" : "等待新任务");
  classToggle("stream-dot","active",active && task.stage==="thinking");
  let body="", empty=false;
  if(tab==="thinking") {
    text("trace-caption","THINKING STREAM / 模型原始思考");
    body=task?.thinking || (task?.stage==="loading" ? "正在加载上下文，等待第一个思考片段…" : !task ? "收到消息后，这里会逐步展示模型的思考内容。" : task.stage==="queued" ? "消息正在等待前一个任务完成…" : "本次尚未收到思考内容；管理员指令无需模型思考。");
    empty=!task?.thinking;
  } else if(tab==="prompt") {
    text("trace-caption","CONTEXT / 本次实际输入（模型内置人设另计）");
    body=task?.prompt || "任务开始后，这里会显示送入模型的群聊背景与当前消息。";empty=!task?.prompt;
  } else {
    text("trace-caption","EXECUTION TIMELINE / 真实执行时间");
    body=task?.steps.map(s=>`${stamp(s.time)}   ${labels[s.stage] || s.stage}${s.note?"\n           "+s.note:""}`).join("\n\n") || "还没有执行记录。";empty=!task;
  }
  const pre=$("trace-text"), nearBottom=pre.scrollHeight-pre.scrollTop-pre.clientHeight<45;
  text("trace-text",body); classToggle("trace-text","empty-text",empty);
  if(nearBottom && active) pre.scrollTop=pre.scrollHeight;
  const reply=task?.answer || task?.draft;
  text("answer-text",reply || "还没有新的回复"); classToggle("answer-text","empty-text",!reply);
  text("answer-state", task?.status==="sent" ? "✓ OneBot 已确认发送" : task?.status==="skipped" ? "本次未向群聊发送" : task?.status==="error" ? "任务失败 · 请查看执行时间线" : task?.status==="cancelled" ? "任务取消 · 未确认发送" : task?.draft ? "正在生成，尚未发送" : "仅最终文本会发送到 QQ");
  text("context-info",`上下文 ${Math.round((state.config.num_ctx || 4096)/1024)}K${task?.context_messages!==undefined?` · ${task.context_messages} 条记录 / ${task.context_people} 位群友`:" · 等待任务"}`);
  const metrics=task?.metrics;
  text("token-info",metrics ? `输入 ${metrics.prompt_eval_count ?? "—"} / 输出 ${metrics.eval_count ?? "—"} tokens` : task?.thinking ? `已接收 ${task.thinking.length} 个思考字符` : "等待模型输出");
}

function renderTraces() {
  text("trace-count",state.traces.length);
  const selected=currentTrace()?.id;
  html("traces",state.traces.length ? state.traces.map(t=>`<button class="trace-item ${t.id===selected?"selected":""}" data-trace="${esc(t.id)}" aria-label="查看 ${esc(t.nick)} 的处理过程"><div class="trace-row"><span class="status-dot ${esc(t.status)}"></span><strong>${esc(t.nick)}</strong><time>${stamp(t.queued_at)}</time></div><p>${esc(t.input || "管理员指令")}</p><div class="trace-item-foot"><span>${esc(labels[t.stage] || t.stage)}</span><span>${t.started_at?duration((t.ended_at || state.now)-t.started_at):"等待中"}</span></div></button>`).join("") : '<div class="list-empty">还没有任务记录<br><small>新群消息触发回复后会出现在这里</small></div>');
}

function renderPeople() {
  const query=$("people-search").value.trim().toLowerCase();
  const all=Object.entries(state.profiles).sort((a,b)=>(b[1].last || 0)-(a[1].last || 0));
  const filtered=all.filter(([uid,p])=>`${uid} ${p.nick} ${(p.aliases || []).join(" ")}`.toLowerCase().includes(query));
  if(!filtered.some(([uid])=>uid===selectedPerson)) selectedPerson=filtered[0]?.[0] || null;
  text("people-filter-count",`${filtered.length} / ${all.length}`);
  html("people-list",filtered.length ? filtered.map(([uid,p])=>`<button class="person-button ${uid===selectedPerson?"selected":""}" data-person="${esc(uid)}"><div class="avatar">${esc(p.nick.slice(0,1))}</div><div><strong>${esc(p.nick)}</strong><small>${esc(uid)}</small></div><span>${p.pool.length}</span></button>`).join("") : '<div class="list-empty">没有匹配的群友</div>');
  const person=state.profiles[selectedPerson];
  classToggle("person-empty","hidden",person);classToggle("person-content","hidden",!person);
  if(!person) return;
  text("person-avatar",person.nick.slice(0,1));text("person-name",person.nick);text("person-id",`QQ ${selectedPerson}`);
  text("person-capacity",`${person.pool.length} / ${state.pool_capacity}`);text("person-count",person.count);text("person-direct",person.to_bot);text("person-last",stamp(person.last,true));
  const notes=[person.aliases?.length?`曾用名：${person.aliases.join("、")}`:"",person.notes?.length?`备注：${person.notes.join("；")}`:""].filter(Boolean).join("\n");
  text("person-notes",notes);classToggle("person-notes","hidden",!notes);
  html("pool-messages",person.pool.length ? [...person.pool].reverse().map(([when,,message])=>`<div class="pool-message"><time>${stamp(when,true)}</time><div>${esc(message)}</div></div>`).join("") : '<div class="list-empty">消息池为空<br><small>图片、表情不进入个人消息池</small></div>');
}

document.querySelectorAll("[data-tab]").forEach(button=>button.addEventListener("click",()=>{
  tab=button.dataset.tab;document.querySelectorAll("[data-tab]").forEach(b=>{b.classList.toggle("selected",b===button);b.setAttribute("aria-selected",String(b===button));});if(state)renderTask();$("trace-text").scrollTop=0;
}));
$("traces").addEventListener("click",event=>{const item=event.target.closest("[data-trace]");if(item){selectedTrace=item.dataset.trace;renderTask();renderTraces();}});
$("people-list").addEventListener("click",event=>{const item=event.target.closest("[data-person]");if(item){selectedPerson=item.dataset.person;renderPeople();}});
$("people-search").addEventListener("input",()=>{if(state)renderPeople();});
$("follow-live").addEventListener("click",()=>{selectedTrace=null;if(state){renderTask();renderTraces();}});
document.querySelectorAll("[data-nav]").forEach(a=>a.addEventListener("click",()=>{document.querySelectorAll("[data-nav]").forEach(b=>b.classList.toggle("active",b===a));}));

async function poll() {
  try {
    const response=await fetch("/api/state",{cache:"no-store",signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error(`HTTP ${response.status}`);
    state=await response.json();failed=false;classToggle("offline","hidden",true);text("sync",`上次同步 ${stamp(Date.now()/1000)}`);render();
  } catch(error) {
    failed=true;classToggle("offline","hidden",false);text("sync","同步中断 · 自动重连");
    if(state)render();else {text("connection","仪表盘离线");classToggle("connection","offline",true);}
  } finally {setTimeout(poll,document.hidden?3000:900);}
}
poll();
