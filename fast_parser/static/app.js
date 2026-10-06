"use strict";
const $ = id => document.getElementById(id);
const labels = {scheduled:"Предстоящий",live:"Идёт",paused:"Перерыв / пауза",finished:"Завершён",postponed:"Перенесён",cancelled:"Отменён",abandoned:"Прерван",unknown:"Статус уточняется"};
const verificationLabels={unverified:"Не верифицировано",partial:"Частично верифицировано",verified:"Верифицировано"};
let verificationCid=null;
function verificationText(c){return (c?.official_source?"Официальный источник · ":c?.collection_allowed===false?"Официальный сбор не подключен · ":"Неофициальный источник · ")+(verificationLabels[c?.coverage_verification?.status]||verificationLabels.unverified);}
function renderCoverage(){
  const c=competitions.find(c=>c.id===$("league").value),v=c?.coverage_verification;
  $("verification-open").disabled=!c;
  $("coverage-status").textContent=c?"Верификация лиги: "+verificationText(c):"Верификация: выберите лигу";
  $("coverage-status").className="coverage-"+(v?.status||"unverified");
  const checked=v?[v.fixtures?"расписание":null,v.results?"результаты":null,v.live?"live":null].filter(Boolean):[];
  $("coverage-description").textContent=c?`${checked.length?"Проверено: "+checked.join(", ")+". ":"Покрытие еще не сверено. "}${v?.scope?"Выборка: "+v.scope+". ":""}${v?.updated_at?"Пометка обновлена: "+fmt(v.updated_at)+". ":""}${v?.evidence?"Основание: "+v.evidence+". ":""}${v?.note?"Замечания: "+v.note+". ":""}Сбор: ${c.collection_state==="available"?"последняя выборка получена":c.collection_state==="degraded"?"ошибка источника":"ожидается проверка источника"}. Все лиги доступны независимо от пометки.`:"Все доступные лиги остаются в каталоге независимо от верификации.";
}
const periods = {first_half:"1-й тайм",half_time:"Перерыв",second_half:"2-й тайм",extra_first_half:"Доп. время · 1-й тайм",extra_break:"Перерыв доп. времени",extra_second_half:"Доп. время · 2-й тайм",penalties:"Пенальти"};
let settings, countries=[], competitions=[], items=[], nextCursor=null, selected=null, requestId=0, abort=null, refreshBusy=false;
let backendOnline=true;
let saved={}; try { saved=JSON.parse(localStorage.getItem("fast-parser-view")||"{}"); } catch (_) {}
const csrf = document.querySelector('meta[name="csrf-token"]').content;
function remember(){localStorage.setItem("fast-parser-view",JSON.stringify({country:$("country").value,league:$("league").value,match:selected}));}
function notice(message,error=false){$("notice").textContent=message;$("notice").classList.toggle("error",error);}
async function api(url,options={}) {
  const response=await fetch(url,{cache:"no-store",...options,headers:{"Content-Type":"application/json","X-CSRF-Token":csrf,...options.headers}});
  if(!response.ok){const body=await response.json().catch(()=>({}));const detail=body.detail;throw new Error(typeof detail==="string"?detail:Array.isArray(detail)?detail.map(d=>d.msg).join("; "):"Ошибка HTTP "+response.status);}
  return response.json();
}
function fmt(value){if(!value)return "Не предоставлено";return new Intl.DateTimeFormat("ru-RU",{timeZone:settings?.timezone||"Europe/Moscow",dateStyle:"short",timeStyle:"short"}).format(new Date(value));}
function score(m){return `${m.score_home??"—"} : ${m.score_away??"—"}`;}
function matchName(m){return `${m.home_team.name} — ${m.away_team.name}`;}
function expired(m){return m.expires_at&&Date.parse(m.expires_at)<=Date.now();}
function state(m){let s=labels[m.status]||m.status;if(periods[m.period])s+=" · "+periods[m.period];if(m.elapsed_minutes!=null)s+=` · ${m.elapsed_minutes}${m.added_minutes!=null?"+"+m.added_minutes:""}′`;else if(m.status==="live")s+=" · минута недоступна";if(m.status==="live"||m.status==="paused"){const maxAge=Math.max(240,2*(settings?.live_interval_seconds||120));if(!backendOnline||!m.observed_at||Date.now()-Date.parse(m.observed_at)>maxAge*1000)s="Последний статус: "+s+" · данные задержаны";}if(m.is_stale&&!(m.status==="live"||m.status==="paused"))s+=" · данные задержаны";return s;}
function options(select, rows, text, placeholder, desired=""){
  select.replaceChildren(new Option(placeholder,""));for(const r of rows)select.add(new Option(text(r),r.id));
  if(rows.some(r=>r.id===desired))select.value=desired;select.disabled=!rows.length;
}
function filterCountries(desired=$("country").value){const q=$("country-search").value.toLocaleLowerCase();options($("country"),countries.filter(c=>c.name.toLocaleLowerCase().includes(q)),c=>`${c.name} (${c.competition_count})`,"Выберите страну",desired);}
function filterLeagues(desired=$("league").value){const q=$("league-search").value.toLocaleLowerCase(),country=$("country").value;options($("league"),competitions.filter(c=>c.country===country&&c.name.toLocaleLowerCase().includes(q)),c=>`${c.name} · ${c.season} · ${c.official_source?c.source:"Неофициальный"} · ${verificationText(c)}`,"Выберите лигу",desired);}
function localDay(date=new Date()){return new Intl.DateTimeFormat("en-CA",{timeZone:settings.timezone,year:"numeric",month:"2-digit",day:"2-digit"}).format(date);}
function dayBoundary(value,next=false){
  // Convert local calendar midnight in the selected IANA timezone to UTC, including DST.
  const [y,m,d]=value.split("-").map(Number);const target=Date.UTC(y,m-1,d+(next?1:0));let guess=target;
  for(let i=0;i<3;i++){const parts=new Intl.DateTimeFormat("en-GB",{timeZone:settings.timezone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).formatToParts(new Date(guess));const p=Object.fromEntries(parts.map(x=>[x.type,x.value]));const wall=Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second);guess+=target-wall;}
  return new Date(guess).toISOString();
}
function clearMatches(message="Выберите лигу.") {items=[];selected=null;nextCursor=null;$("rows").replaceChildren();$("match").replaceChildren(new Option("Сначала выберите лигу",""));$("match").disabled=true;$("empty").textContent=message;$("empty").classList.remove("hidden");$("more").classList.add("hidden");$("count").textContent="";renderDetail();renderCoverage();}
function renderDetail(){
  const box=$("detail");box.replaceChildren();const tag=document.createElement("div");tag.className="eyebrow";tag.textContent="КАРТОЧКА МАТЧА";box.append(tag);
  const m=items.find(m=>m.id===selected&&!expired(m));const title=document.createElement("h2");title.textContent=m?matchName(m):"Выберите матч";box.append(title);if(!m)return;
  const big=document.createElement("div");big.className="big-score";big.textContent=score(m);box.append(big);
  const badge=document.createElement("span");badge.className="badge "+m.status;badge.textContent=state(m);box.append(badge);
  const comp=competitions.find(c=>c.id===m.competition_id);const dl=document.createElement("dl");dl.className="detail-list";
  const rows=[["Лига",comp?.name||m.competition_id],["Верификация покрытия лиги",verificationText(comp)],["Страна / сезон",`${comp?.country||"Не указана"} / ${comp?.season||"—"}`],["Начало",m.kickoff_at?fmt(m.kickoff_at):m.scheduled_date?m.scheduled_date+" · время уточняется":"Дата уточняется"],["Окончание",m.finished_at?fmt(m.finished_at):m.status==="finished"?"Время окончания не предоставлено":"Матч ещё не завершён"],["Завершение обнаружено",fmt(m.first_observed_finished_at)],["Последние данные",fmt(m.last_checked_at||m.observed_at)],["Актуальность",m.is_stale?"Данные задержаны; проверьте источник":"В пределах интервала проверки"],["Хранится до",fmt(m.expires_at)],["Источник",(m.official_source?m.source+" · официальный источник":m.source+" · неофициальный источник")+" · без независимой проверки"],["Страница источника",m.source_url||"Не предоставлена"],["Исходный статус",m.raw_status||"—"],["Тайм",periods[m.period]||(m.status==="finished"?"Завершён":"Не предоставлен")],["Основное время",`${m.regular_home??"—"} : ${m.regular_away??"—"}`],["Дополнительное время",`${m.extra_home??"—"} : ${m.extra_away??"—"}`],["Пенальти",`${m.penalties_home??"—"} : ${m.penalties_away??"—"}`]];
  for(const [label,value]of rows){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=value;dl.append(dt,dd);}box.append(dl);
  if(m.status==="live"||m.status==="paused"){const p=document.createElement("p");p.className="muted";p.textContent="Статус и минута — последнее наблюдение источника, не непрерывный таймер.";box.append(p);}
}
function render(){
  renderCoverage();
  items=items.filter(m=>!expired(m));items.sort((a,b)=>(a.kickoff_at||"9999").localeCompare(b.kickoff_at||"9999")||a.id.localeCompare(b.id));
  const q=$("match-search").value.toLocaleLowerCase();const visible=items.filter(m=>matchName(m).toLocaleLowerCase().includes(q));
  options($("match"),visible,m=>`${m.kickoff_at?fmt(m.kickoff_at):m.scheduled_date?m.scheduled_date+" · время уточняется":"Дата уточняется"} · ${matchName(m)} · ${score(m)} · ${state(m)}`,"Выберите матч",selected);
  if(!visible.some(m=>m.id===selected))selected=null;
  const body=$("rows");body.replaceChildren();for(const m of visible){const tr=document.createElement("tr");tr.tabIndex=0;tr.classList.toggle("selected",m.id===selected);tr.title=matchName(m);
    for(const [index,value]of [m.kickoff_at?fmt(m.kickoff_at):m.scheduled_date?m.scheduled_date+" · время уточняется":"Дата уточняется",m.home_team.name,score(m),m.away_team.name,state(m)].entries()){const td=document.createElement("td");td.textContent=value;if(index===2)td.className="score";if(index===4){td.replaceChildren();const b=document.createElement("span");b.className="badge "+m.status;b.textContent=value;td.append(b);}tr.append(td);}
    const choose=()=>{selected=m.id;remember();render();};tr.addEventListener("click",choose);tr.addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();choose();}});body.append(tr);
  }
  $("empty").classList.toggle("hidden",visible.length>0);$("empty").textContent=q?"Матчей по поиску нет.":"Матчей в выбранном диапазоне нет. Проверьте покрытие и настройки сбора.";$("count").textContent=`${visible.length} матчей${nextCursor?" · доступны ещё":""}`;$("more").classList.toggle("hidden",!nextCursor);renderDetail();
}
async function loadMatches(append=false,preservePages=false){
  const cid=$("league").value;if(!cid){clearMatches();return;}const current=++requestId;if(abort)abort.abort();abort=new AbortController();
  const p=new URLSearchParams({competition_id:cid,limit:"100"});if($("status").value)p.set("status",$("status").value);if($("date-from").value)p.set("date_from",dayBoundary($("date-from").value));if($("date-to").value)p.set("date_to",dayBoundary($("date-to").value,true));if(append&&nextCursor)p.set("cursor",nextCursor);
  try{const target=preservePages?Math.max(100,items.length):100;const data=await api("/api/v1/matches?"+p,{signal:abort.signal});while(!append&&data.next_cursor&&data.items.length<target){p.set("cursor",data.next_cursor);const page=await api("/api/v1/matches?"+p,{signal:abort.signal});data.items.push(...page.items);data.next_cursor=page.next_cursor;}if(current!==requestId||cid!==$("league").value)return;items=append?[...items,...data.items.filter(m=>!items.some(old=>old.id===m.id))]:data.items;nextCursor=data.next_cursor;render();const c=competitions.find(c=>c.id===cid);$("league-title").textContent=c?.name||"Матчи";$("sync").disabled=!c||c.collection_allowed===false||(!c.enabled&&cid.startsWith("ol:"))||(cid.startsWith("sky:")&&!settings.html_enabled);notice(c?.error?"Источник недоступен: "+c.error:c?.state==="unverified"?"Покрытие ещё не подтверждено. Включите лигу в настройках и дождитесь сбора.":"Показаны последние сохранённые данные. Обновление экрана не запрашивает сайты.",!!c?.error);remember();}
  catch(e){if(e.name!=="AbortError"&&current===requestId){notice(e.message,true);render();}}
}
async function loadCatalog(initial=false){
  const [co,le]=await Promise.all([api("/api/v1/countries"),api("/api/v1/competitions")]);countries=co.items;competitions=le.items;
  const oldCountry=initial?saved.country:$("country").value,oldLeague=initial?saved.league:$("league").value;
  filterCountries(oldCountry||countries.find(c=>c.name==="Германия")?.id||"");filterLeagues(oldLeague);
  if(!$("league").value&&$("country").value){const first=competitions.find(c=>c.country===$("country").value&&c.enabled)||competitions.find(c=>c.country===$("country").value);if(first)$("league").value=first.id;}
  if(initial)selected=saved.match||null;
}
async function diagnostics(){
  const data=await api("/api/v1/diagnostics");backendOnline=true;const age=data.worker_heartbeat?(Date.now()-Date.parse(data.worker_heartbeat))/1000:Infinity;
  $("connection").textContent=`Сервер доступен · ${age<90?"сбор работает":"worker ещё не запущен / данные задержаны"} · ${new Date().toLocaleTimeString("ru-RU")}`;
  const box=$("diagnostics");box.replaceChildren();const top=document.createElement("p");top.textContent=`Каталог: ${data.coverage.catalog} лиг · Успешная выборка: ${data.coverage.verified} · Включено OpenLigaDB: ${data.coverage.enabled_openliga} · Минимальный обход: ${data.plan.minimum_full_sweep_seconds} сек.`;box.append(top);const verified=document.createElement("p");const v=data.coverage.verification||{};verified.textContent=`Верификация лиг: ${v.verified||0} верифицировано · ${v.partial||0} частично · ${v.unverified||0} не верифицировано. Успешная выборка не означает верификацию покрытия.`;box.append(verified);const pending=document.createElement("p");pending.textContent=`Без результата более 24 часов: ${(data.unresolved_over_24h||[]).length}`;box.append(pending);
  for(const source of data.sources){const div=document.createElement("div");div.className="source";div.textContent=`${source.name}: ${source.state} · Последний успех: ${fmt(source.last_success)}${source.error?" · "+source.error:""}`;if(source.state==="blocked"){const b=document.createElement("button");b.textContent="Повторная проверка после проверки доступа";b.className="secondary";b.addEventListener("click",async()=>{try{const r=await api(`/api/v1/sources/${source.name}/reset`,{method:"POST"});notice(r.message);}catch(e){notice(e.message,true);}});div.append(document.createElement("br"),b);}box.append(div);}
}
function enabledList(){const box=$("enabled-leagues");const q=$("enabled-search").value.toLocaleLowerCase();for(const label of box.children)label.classList.toggle("hidden",!label.textContent.toLocaleLowerCase().includes(q));}
async function openSettings(){
  try{settings=await api("/api/v1/settings");const form=$("settings-form");for(const[key,value]of Object.entries(settings)){const input=form.elements.namedItem(key);if(!input)continue;if(input.type==="checkbox")input.checked=value;else input.value=Array.isArray(value)?value.join(", "):value??"";}
    const box=$("enabled-leagues");box.replaceChildren();for(const c of competitions.filter(c=>c.id.startsWith("ol:")&&!competitions.some(other=>other.shortcut===c.shortcut&&other.season>c.season))){const label=document.createElement("label");label.className="check";const cb=document.createElement("input");cb.type="checkbox";cb.value=c.shortcut;cb.checked=settings.enabled_leagues.includes(c.shortcut);const t=document.createElement("span");t.textContent=`${c.country} · ${c.name} · ${c.season}`;label.append(cb,t);box.append(label);}$("settings-message").textContent="";$("settings-dialog").showModal();
  }catch(e){notice(e.message,true);}
}
$("verification-open").addEventListener("click",()=>{
  const c=competitions.find(c=>c.id===$("league").value);if(!c)return;
  verificationCid=c.id;const form=$("verification-form"),v=c.coverage_verification||{status:"unverified"};
  for(const key of["status","fixtures","results","live","scope","evidence","note"]){const input=form.elements.namedItem(key);if(input.type==="checkbox")input.checked=!!v[key];else input.value=v[key]||"";}
  $("verification-league").textContent=`${c.name} · ${c.season} · ${c.id}`;$("verification-message").textContent="";$("verification-dialog").showModal();
});
$("verification-close").addEventListener("click",()=>$("verification-dialog").close());
$("verification-form").addEventListener("submit",async e=>{
  e.preventDefault();const payload={},form=e.target;
  for(const key of["status","fixtures","results","live","scope","evidence","note"]){const input=form.elements.namedItem(key);payload[key]=input.type==="checkbox"?input.checked:input.value;}
  try{const result=await api("/api/v1/competitions/"+encodeURIComponent(verificationCid)+"/verification",{method:"PUT",body:JSON.stringify(payload)});const c=competitions.find(c=>c.id===verificationCid);if(c)c.coverage_verification=result;$("verification-dialog").close();filterLeagues();render();await diagnostics();notice("Пометка верификации сохранена. Сбор и доступ к лиге продолжаются.");}catch(err){$("verification-message").textContent=err.message;}
});
$("settings-form").addEventListener("submit",async e=>{e.preventDefault();const form=e.target;const payload={...settings};for(const key of Object.keys(settings)){const input=form.elements.namedItem(key);if(!input)continue;if(input.type==="checkbox")payload[key]=input.checked;else if(input.type==="number")payload[key]=Number(input.value);else if(key==="active_windows")payload[key]=input.value.split(",").map(s=>s.trim()).filter(Boolean);else payload[key]=input.value||null;}payload.enabled_leagues=[...new Set([...$("enabled-leagues").querySelectorAll("input:checked")].map(c=>c.value))];try{const r=await api("/api/v1/settings",{method:"PUT",body:JSON.stringify(payload)});settings=payload;$("timezone-label").textContent=settings.timezone;$("settings-dialog").close();notice(r.message);await loadCatalog();await loadMatches();}catch(err){$("settings-message").textContent=err.message;}});
$("settings-open").addEventListener("click",openSettings);$("settings-close").addEventListener("click",()=>$("settings-dialog").close());$("enabled-search").addEventListener("input",enabledList);
$("country").addEventListener("change",()=>{++requestId;abort?.abort();filterLeagues("");clearMatches();$("sync").disabled=true;remember();});
$("league").addEventListener("change",()=>{clearMatches();loadMatches();});
$("country-search").addEventListener("input",()=>{++requestId;abort?.abort();filterCountries();filterLeagues("");clearMatches();$("sync").disabled=true;});$("league-search").addEventListener("input",()=>{filterLeagues();if(!$("league").value){++requestId;abort?.abort();clearMatches();$("sync").disabled=true;}});$("match-search").addEventListener("input",render);
$("match").addEventListener("change",()=>{selected=$("match").value;remember();render();});for(const id of["date-from","date-to","status"])$(id).addEventListener("change",()=>loadMatches());$("more").addEventListener("click",()=>loadMatches(true));$("refresh").addEventListener("click",()=>refresh());
$("sync").addEventListener("click",async()=>{try{const r=await api("/api/v1/sync/"+encodeURIComponent($("league").value),{method:"POST"});notice(r.message);}catch(e){notice(e.message,true);}});
async function refresh(){if(refreshBusy)return;refreshBusy=true;try{await loadCatalog();await loadMatches(false,true);await diagnostics();}catch(e){backendOnline=false;$("connection").textContent="Сервер недоступен · показаны последние данные";notice(e.message,true);render();}finally{refreshBusy=false;}}
document.addEventListener("visibilitychange",()=>{if(!document.hidden){render();refresh();}});
setInterval(()=>{if(!document.hidden)refresh();},15000);setInterval(()=>{if(items.some(expired)){notice("Срок хранения матча истёк: он удалён с экрана.");render();}},1000);
(async()=>{try{settings=await api("/api/v1/settings");$("timezone-label").textContent=settings.timezone;$("date-from").value=localDay(new Date(Date.now()-3*86400000));$("date-to").value=localDay(new Date(Date.now()+7*86400000));await loadCatalog(true);await loadMatches();await diagnostics();}catch(e){notice(e.message,true);}})();
