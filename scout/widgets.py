"""Small Bot API adapter and compact private-chat Telegram Widgets.

The installed aiogram release does not model widget_query yet.  Polling stays in
aiogram; only that new update type is intercepted here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.types import Message, Update

from .service import JOB_STATUS_LABELS, job_text

log = logging.getLogger(__name__)


class SendWidget(TelegramMethod[Message]):
    __api_method__ = "sendWidget"
    __returning__ = Message
    chat_id: int
    html: str
    width: int
    height: int
    chat_data: str
    allow_queries: bool = True
    caption: str | None = None


class AnswerWidgetQuery(TelegramMethod[bool]):
    __api_method__ = "answerWidgetQuery"
    __returning__ = bool
    widget_query_id: str
    data: str | None = None


class EditMessageWidgetData(TelegramMethod[bool]):
    __api_method__ = "editMessageWidgetData"
    __returning__ = bool
    chat_id: int
    message_id: int
    chat_data: str


class WidgetDispatcher(Dispatcher):
    def __init__(self, widget_handler):
        super().__init__()
        self.widget_handler = widget_handler

    async def _process_update(self, bot: Bot, update: Update, call_answer: bool = True, **kwargs) -> bool:
        query = (update.model_extra or {}).get("widget_query")
        if isinstance(query, dict):
            try:
                await self.widget_handler(query)
            except Exception:
                log.exception("Widget query %s failed", query.get("id"))
            return True
        return await super()._process_update(bot, update, call_answer=call_answer, **kwargs)


STYLE = r"""<style>
*{box-sizing:border-box}body{margin:0;padding:14px;background:var(--tg-theme-bg-color,#fffaf5);color:var(--tg-theme-text-color,#18191b);font:14px -apple-system,BlinkMacSystemFont,sans-serif}
:root{--orange:#f36a26;--muted:#777b84;--card:#fff;--line:#ede8e2;--soft:#fff2e8;--green:#29825d}
body.dark{--card:#24252a;--line:#393a40;--soft:#3a2b23;--muted:#a6a7ad;--green:#6cc69e;background:#18191d;color:#f6f3ef}
.wrap{max-width:360px;margin:auto}.eyebrow{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--orange);margin-bottom:6px}
h1{font-size:23px;line-height:1.12;letter-spacing:-.6px;margin:0 0 8px}p{margin:0}.sub{color:var(--muted);line-height:1.38;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:16px;box-shadow:0 8px 24px rgba(42,25,9,.06);margin-top:11px;animation:enter .3s ease-out both}
.card:nth-child(3){animation-delay:.05s}.card:nth-child(4){animation-delay:.1s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:13px}.metric{margin:0;min-height:82px}.num{font-size:27px;font-weight:750;letter-spacing:-1px;display:block}.label{font-size:11px;color:var(--muted)}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px}.stack{display:flex;flex-direction:column;gap:7px}.badge{display:inline-block;background:var(--soft);color:var(--orange);padding:5px 8px;border-radius:9px;font-size:11px;font-weight:650;margin:4px 4px 0 0}
.score{width:64px;height:64px;flex:none}.score circle{fill:none;stroke:var(--line);stroke-width:7}.score .arc{stroke:var(--orange);stroke-linecap:round;stroke-dasharray:0 176;transform:rotate(-90deg);transform-origin:50% 50%;transition:stroke-dasharray .5s ease-out}.score text{fill:currentColor;font-size:16px;font-weight:750;text-anchor:middle;dominant-baseline:middle}
.budget{font-size:18px;font-weight:750;color:var(--orange)}.buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:13px}button{font:inherit;border:1px solid var(--line);border-radius:12px;padding:10px 8px;background:var(--card);color:inherit;font-weight:650;cursor:pointer;transition:transform .18s ease-out,background .18s ease-out,opacity .18s ease-out}button:active{transform:scale(.96)}button.primary{background:var(--orange);border-color:var(--orange);color:#fff}button.ghost{color:var(--muted)}button:disabled{opacity:.48}.full{grid-column:1/-1}
.source{padding:12px 0;border-bottom:1px solid var(--line)}.source:last-child{border:0}.source h2{font-size:14px;margin:0 0 4px;max-width:210px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.source .sub{font-size:11px}.switch{width:46px;height:28px;border-radius:20px;background:#d3d3d6;border:0;padding:3px;flex:none;transition:background .22s ease-out}.switch.on{background:var(--green)}.switch i{display:block;width:22px;height:22px;border-radius:50%;background:white;box-shadow:0 2px 5px #0002;transition:transform .22s ease-out}.switch.on i{transform:translateX(18px)}
.foot{display:flex;justify-content:space-between;align-items:center;margin-top:13px}.notice{min-height:19px;color:var(--green);font-size:11px}.danger{color:#c54836}.confirm{background:var(--soft);border-radius:14px;padding:12px;margin-top:9px}.confirm .buttons{margin-top:8px}
@keyframes enter{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:translateY(0)}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}
</style>"""

SCRIPT = r"""<script>
const W=window.TelegramWidget,root=document.getElementById('app');let S=null,busy=false,confirmId=0;
const e=x=>String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function read(x){try{return JSON.parse(x||'null')}catch(_){return null}}
function theme(){document.body.classList.toggle('dark',W&&W.colorScheme==='dark')}
function notice(t,bad){let n=document.getElementById('notice');if(n){n.textContent=t||'';n.className='notice'+(bad?' danger':'')}}
function render(){theme();if(!S){root.innerHTML='<div class="card">Открой виджет в Telegram</div>';return}
let h='<div class="wrap">';
if(S.k==='d'){h+='<div class="eyebrow">Freelance AI Scout</div><h1>Панель</h1><p class="sub">Показатели из базы · новые за 24 часа</p><div class="grid">';
let m=[['Новые за 24 ч',S.n],['Подходящие',S.m],['Сохранённые',S.s],['Откликнулся',S.a],['Активные источники',S.c]];
for(let x of m)h+='<div class="card metric"><span class="num" data-count="'+e(x[1])+'">'+e(x[1])+'</span><span class="label">'+e(x[0])+'</span></div>';
h+='</div><div class="card row"><div><b>Мониторинг</b><p class="sub">'+e(S.mon)+'</p></div><span class="badge">'+(S.running?'Идёт':S.c?'Активен':'Нет источников')+'</span></div><div class="buttons"><button class="primary full" data-a="d:r">Обновить</button></div>'}
if(S.k==='j'){h+='<div class="eyebrow">'+e(S.src)+'</div><h1>'+e(S.t||'Заказ #'+S.id)+'</h1><p class="sub">'+e(S.when)+' · '+e(S.cat)+'</p><div class="card row"><div class="stack"><span class="label">БЮДЖЕТ</span><span class="budget">'+e(S.b||'Не указан')+'</span><span class="sub">'+e(S.status)+'</span></div>';
if(S.score!=null){let v=Math.max(0,Math.min(100,Number(S.score)||0));h+='<svg class="score" viewBox="0 0 64 64"><circle cx="32" cy="32" r="28"/><circle class="arc" cx="32" cy="32" r="28" style="stroke-dasharray:'+Math.round(v*1.76)+' 176"/><text x="32" y="33">'+v+'</text></svg>'}h+='</div><div class="card"><p>'+e(S.d)+'</p><div style="margin-top:8px">';for(let tag of S.tags||[])h+='<span class="badge">'+e(tag)+'</span>';h+='</div></div><div class="buttons">';
if(S.url)h+='<button class="primary full" data-link="'+e(S.url)+'">Открыть оригинал ↗</button>';
h+='<button data-a="j:'+(S.saved?'v':'s')+':'+S.id+'">'+(S.saved?'Сохранено ✓':'Сохранить')+'</button><button data-a="j:'+(S.hidden?'u':'h')+':'+S.id+'">'+(S.hidden?'Вернуть':'Скрыть')+'</button><button data-a="j:a:'+S.id+'">ИИ-анализ</button><button data-a="j:d:'+S.id+'">Подготовить отклик</button></div>'}
if(S.k==='s'){h+='<div class="eyebrow">Freelance AI Scout</div><h1>Источники</h1><p class="sub">Переключение меняет реальный мониторинг</p><div class="card">';
for(let x of S.rows||[]){h+='<div class="source"><div class="row"><div><h2>'+e(x.t)+'</h2><p class="sub">'+(x.on?'Мониторинг включён':'На паузе')+' · '+e(x.f)+' найдено · '+e(x.m)+' подходящих</p></div><button class="switch '+(x.on?'on':'')+'" aria-label="Мониторинг" data-a="s:t:'+x.id+':'+S.p+'"><i></i></button></div><div class="buttons"><button data-a="s:i:'+x.id+':'+S.p+'">Подробнее</button><button class="ghost" data-a="s:x:'+x.id+':'+S.p+'">Убрать</button></div></div>'}
if(!(S.rows||[]).length)h+='<p class="sub">Источников пока нет</p>';h+='</div><div class="foot"><button data-a="s:p:'+(S.p-1)+'" '+(S.p<=1?'disabled':'')+'>← Назад</button><span class="sub">'+S.p+' / '+S.pages+'</span><button data-a="s:p:'+(S.p+1)+'" '+(S.p>=S.pages?'disabled':'')+'>Далее →</button></div>';
if(confirmId)h+='<div class="confirm"><b>Убрать источник?</b><p class="sub">Мониторинг остановится. История заказов сохранится.</p><div class="buttons"><button data-cancel="1">Отмена</button><button class="primary" data-a="s:y:'+confirmId+':'+S.p+'">Убрать</button></div></div>'}
h+='<div id="notice" class="notice"></div></div>';root.innerHTML=h;if(S.err)notice(S.err,true);if(S.k==='d'&&window.requestAnimationFrame&&(!window.matchMedia||!matchMedia('(prefers-reduced-motion: reduce)').matches)){let a=[...root.querySelectorAll('[data-count]')],start=0;function tick(t){if(!start)start=t;let p=Math.min(1,(t-start)/320);for(let x of a)x.textContent=Math.round(Number(x.dataset.count)*p);if(p<1)requestAnimationFrame(tick)}requestAnimationFrame(tick)}}
document.addEventListener('click',ev=>{let b=ev.target.closest('button');if(!b||b.disabled||busy)return;if(b.dataset.cancel){confirmId=0;render();return}if(b.dataset.link){if(W&&W.openLink)W.openLink(b.dataset.link);return}let a=b.dataset.a;if(!a||!W)return;busy=true;notice('Сохраняю…');W.request(a).then(r=>{busy=false;let v=read(r&&r.result);if(v&&v.k){S=v;confirmId=0;render()}else if(v&&v.confirm){confirmId=v.confirm;render()}else if(v&&v.wait){notice('Обновляю…')}else notice(v&&v.error||v&&v.ok||'Готово',!!(v&&v.error))}).catch(err=>{busy=false;notice('Не удалось выполнить действие. Повтори.',true)})});
if(W){S=read(W.chatData);W.onEvent('chatDataChanged',()=>{S=read(W.chatData);confirmId=0;render()});W.onEvent('themeChanged',theme)}render();
</script>"""


JOB_STYLE = r"""<style>
*{box-sizing:border-box}body{margin:0;padding:14px;background:#eaf4ff;color:#10203e;font:14px -apple-system,BlinkMacSystemFont,sans-serif}.wrap{max-width:340px;margin:auto}.top{display:flex;align-items:center;gap:10px;margin:0 2px 13px}.back{font-size:22px;color:#203c6c}.top b{font-size:17px;letter-spacing:-.4px}.card{padding:16px 15px 13px;border:1px solid #fff;border-radius:22px;background:linear-gradient(145deg,#ffffffd9,#f6fbffb8);box-shadow:0 12px 28px #4f8ce824,0 2px 5px #6d93c21a,inset 0 1px #fff;animation:rise .35s ease-out both}.title{font-size:18px;line-height:1.2;letter-spacing:-.45px;font-weight:750;margin:0 0 7px}.desc{font-size:12px;line-height:1.35;color:#425576;margin:0}.tags{display:flex;gap:6px;flex-wrap:wrap;margin:11px 0 13px}.tag{padding:5px 9px;border-radius:20px;background:#dceaff;color:#183767;font-size:11px;font-weight:600}.facts{display:flex;align-items:center;justify-content:space-between;min-height:65px}.budget{display:flex;align-items:center;gap:8px;padding:9px 10px;background:#ffffffa8;border:1px solid #fff;border-radius:12px;box-shadow:0 4px 8px #5f83bd1d;font-weight:750;font-size:14px}.budget svg{color:#286fe7}.score{width:62px;height:62px;filter:drop-shadow(0 4px 5px #277bff40)}.score circle{fill:none;stroke:#dce8fa;stroke-width:6}.score .arc{stroke:#1879ef;stroke-linecap:round;stroke-dasharray:0 176;transform:rotate(-90deg);transform-origin:50% 50%;transition:stroke-dasharray .5s ease-out}.score text{fill:#162a4c;font-size:14px;font-weight:750;text-anchor:middle;dominant-baseline:middle}.source{display:flex;align-items:center;gap:8px;border-top:1px solid #dae7f4;margin-top:12px;padding-top:11px;font-size:11px}.source-icon{color:#2685f9}.source b{display:block;font-size:11px}.source span{display:block;color:#7384a0;font-size:10px}.when{margin-left:auto;color:#71819a}.actions{display:grid;grid-template-columns:1fr 44px 44px;gap:7px;margin:10px 4px 0}.more{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin:8px 4px 0}button{font:inherit;cursor:pointer;transition:transform .18s,box-shadow .18s,background .18s}button:active{transform:scale(.94)}.primary{border:1px solid #2f94ff;border-radius:13px;background:linear-gradient(180deg,#5cb6ff,#0e6ded);color:white;font-weight:700;box-shadow:0 5px 11px #2388f954;min-height:44px}.icon{display:grid;place-items:center;min-height:44px;border:1px solid #fff;border-radius:13px;background:#ffffffc9;color:#1c4e95;box-shadow:0 4px 9px #447ac22e}.icon.on{color:#0868df;background:#ddecff}.icon svg{width:19px;height:19px}.minor{min-height:44px;border:1px solid #ffffffb8;border-radius:11px;background:#ffffff8c;color:#345378;font-size:11px;font-weight:650}.notice{min-height:16px;margin:7px 5px 0;color:#237454;font-size:11px}.notice.bad{color:#b94b44}@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important}}
</style>"""

JOB_SCRIPT = r"""<script>
const W=window.TelegramWidget,root=document.getElementById('app');let S=null,busy=false;
const e=x=>String(x==null?'':x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const read=x=>{try{return JSON.parse(x||'null')}catch(_){return null}};
const wallet='<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="15" rx="3"/><path d="M3 10h18M6 6V4h12"/><path d="M16 14h2"/></svg>';
const plane='<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21 3-7.5 18-3.5-7-7-3.5L21 3ZM10 14l11-11"/></svg>';
const mark='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M6 3h12v18l-6-4-6 4V3Z"/></svg>';
const eye='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 3l18 18M10.6 5.1A11 11 0 0 1 12 5c5 0 9 7 9 7a15 15 0 0 1-2.3 3M6.6 6.6C4.3 8 3 12 3 12s4 7 9 7c1 0 2-.3 2.9-.7"/><path d="M10 10a3 3 0 0 0 4 4"/></svg>';
function notice(t,bad){let n=document.getElementById('notice');if(n){n.textContent=t||'';n.className='notice'+(bad?' bad':'')}}
function render(){if(!S){root.innerHTML='<div class="card">Открой карточку в Telegram</div>';return}let v=Math.max(0,Math.min(100,Number(S.score)||0));let h='<div class="wrap"><div class="top"><span class="back">‹</span><b>Новый заказ</b></div><div class="card"><h1 class="title">'+e(S.t||'Заказ #'+S.id)+'</h1><p class="desc">'+e(S.d||'Описание заказа не указано')+'</p><div class="tags">';for(let t of S.tags||[])h+='<span class="tag">'+e(t)+'</span>';h+='</div><div class="facts"><div class="budget">'+wallet+e(S.b||'Бюджет не указан')+'</div>';
if(S.score!=null)h+='<svg class="score" viewBox="0 0 64 64"><circle cx="32" cy="32" r="28"/><circle class="arc" cx="32" cy="32" r="28" style="stroke-dasharray:'+Math.round(v*1.76)+' 176"/><text x="32" y="33">'+v+'%</text></svg>';h+='</div><div class="source"><span class="source-icon">'+plane+'</span><div><b>Источник</b><span>'+e(S.src)+'</span></div><span class="when">'+e(S.when)+'</span></div></div><div class="actions">';
if(S.url)h+='<button class="primary" data-link="'+e(S.url)+'">'+plane+' Подробнее</button>';else h+='<button class="primary" disabled>Подробнее</button>';
h+='<button class="icon '+(S.saved?'on':'')+'" aria-label="'+(S.saved?'Убрать из сохранённого':'Сохранить')+'" data-a="j:'+(S.saved?'v':'s')+':'+S.id+'">'+mark+'</button><button class="icon '+(S.hidden?'on':'')+'" aria-label="'+(S.hidden?'Вернуть заказ':'Скрыть заказ')+'" data-a="j:'+(S.hidden?'u':'h')+':'+S.id+'">'+eye+'</button></div><div class="more"><button class="minor" data-a="j:a:'+S.id+'">ИИ-анализ</button><button class="minor" data-a="j:d:'+S.id+'">Подготовить отклик</button></div><div id="notice" class="notice"></div></div>';root.innerHTML=h;if(S.err)notice(S.err,true)}
document.addEventListener('click',ev=>{let b=ev.target.closest('button');if(!b||b.disabled||busy)return;if(b.dataset.link){if(W&&W.openLink)W.openLink(b.dataset.link);return}let a=b.dataset.a;if(!a||!W)return;busy=true;notice('Сохраняю…');W.request(a).then(r=>{busy=false;let v=read(r&&r.result);if(v&&v.k){S=v;render()}else if(v&&v.wait){notice('Обновляю…')}else notice(v&&v.error||v&&v.ok||'Готово',!!(v&&v.error))}).catch(()=>{busy=false;notice('Не удалось выполнить действие. Повтори.',true)})});
if(W){S=read(W.chatData);W.onEvent('chatDataChanged',()=>{S=read(W.chatData);render()})}render();
</script>"""


def widget_html(kind: str = "d") -> str:
    style, script = (JOB_STYLE, JOB_SCRIPT) if kind == "j" else (STYLE, SCRIPT)
    return '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">' + style + '</head><body><div id="app"></div>' + script + '</body></html>'


def packed(state: dict) -> str:
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > 1024:
        raise ValueError("Widget state exceeds 1024 bytes")
    return data


class WidgetController:
    def __init__(self, service):
        self.service = service
        self.repo = service.repo
        self.bot = service.bot
        self.owner_id = service.owner_id
        self.pending_removals: dict[tuple[int, int], datetime] = {}

    def dashboard(self) -> dict:
        values = self.repo.widget_dashboard()
        started = self.service.last_monitor_started
        mon = (started.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("Последняя проверка %d.%m в %H:%M")
               if started else "Ожидает первой проверки")
        return {"k": "d", "n": values["new"], "m": values["matching"], "s": values["saved"],
                "a": values["applied"], "c": values["sources"], "mon": mon,
                "running": self.service.monitor_lock.locked()}

    def job(self, job_id: int) -> dict | None:
        job = self.repo.job(job_id)
        if job is None:
            return None
        analysis = self.repo.analysis(job_id)
        result = analysis.result if analysis else {}
        channel = self.repo.job_channel(job_id)
        original = self.repo.job_original(job_id) or ""
        link = self.repo.job_link(job_id)
        state = {"k": "j", "id": job_id, "t": (result.get("title") or job.title or "")[:90],
                "d": original.replace("\n", " ")[:170], "b": (str(result.get("budget") or "") + " " + str(result.get("currency") or "")).strip()[:65],
                "src": (channel.title if channel else "Источник")[:45],
                "when": job.created_at.astimezone(ZoneInfo("Asia/Yekaterinburg")).strftime("%d.%m %H:%M"),
                "cat": str(result.get("category") or "")[:35],
                "tags": [str(x)[:20] for x in (result.get("tools") or result.get("requirements") or [])[:3]],
                "score": job.score, "status": "Скрыт" if job.hidden else JOB_STATUS_LABELS.get(job.status, "Новый"),
                "saved": job.saved, "hidden": job.hidden,
                "url": link[:180] if link and link.startswith("https://t.me/") else None}
        while len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1024:
            if state["d"]:
                state["d"] = state["d"][:-20]
            elif state["t"]:
                state["t"] = state["t"][:-10]
            else:
                state["url"] = None
                break
        return state

    def sources(self, page: int = 1) -> dict:
        rows, total = self.repo.widget_sources(max(1, page))
        pages = max(1, (total + 1) // 2)
        page = min(max(1, page), pages)
        if page != 1 and not rows:
            rows, _ = self.repo.widget_sources(page)
        return {"k": "s", "p": page, "pages": pages, "rows": [
            {"id": c.id, "t": c.title[:44], "on": c.status == "approved", "f": found, "m": match}
            for c, found, match in rows]}

    async def send(self, kind: str, job_id: int | None = None, page: int = 1, fallback: bool = True) -> None:
        state = self.dashboard() if kind == "d" else self.sources(page) if kind == "s" else self.job(job_id or 0)
        if state is None:
            await self.bot.send_message(self.owner_id, "Заказ не найден")
            return
        try:
            await self.bot(SendWidget(chat_id=self.owner_id, html=widget_html(kind), chat_data=packed(state),
                                      width=340, height={"d": 570, "j": 580, "s": 600}[kind],
                                      caption={"d": "Панель Freelance AI Scout", "j": "Карточка заказа", "s": "Источники"}[kind]))
        except Exception:
            log.exception("Не удалось отправить Telegram Widget %s", kind)
            if not fallback:
                return
            if kind == "j":
                from .bot import job_keyboard
                job = self.repo.job(job_id or 0)
                analysis = self.repo.analysis(job_id or 0)
                if job and analysis:
                    await self.bot.send_message(self.owner_id, job_text(job, analysis, self.repo.job_link(job.id))[:4000],
                                                reply_markup=job_keyboard(job.id, self.repo.job_link(job.id), job.status))
            elif kind == "d":
                await self.bot.send_message(self.owner_id, "Виджет недоступен. Открой /stats и /status для показателей.")
            else:
                await self.bot.send_message(self.owner_id, "Виджет недоступен. Открой /sources для управления каналами.")

    async def handle(self, query: dict) -> None:
        query_id = query.get("id")
        if not isinstance(query_id, str):
            return
        user = query.get("from") or {}
        chat = query.get("chat") or {}
        message_id = query.get("message_id")
        if (user.get("id") != self.owner_id or chat.get("id") != self.owner_id
                or chat.get("type") != "private" or not isinstance(message_id, int)):
            await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"error": "Доступ запрещён"})))
            return
        data = query.get("data")
        parts = data.split(":") if isinstance(data, str) and len(data.encode("utf-8")) <= 64 else []
        answered = False
        job_id, page = 0, 1
        try:
            if parts == ["d", "r"]:
                state = self.dashboard()
                await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed(state)))
                answered = True
                await self._edit(message_id, state)
                return
            if len(parts) == 3 and parts[0] == "j" and parts[2].isdigit():
                job_id = int(parts[2])
                if self.repo.job(job_id) is None:
                    raise ValueError("Заказ не найден")
                if parts[1] in {"s", "v", "h", "u"}:
                    flag = "saved" if parts[1] in {"s", "v"} else "hidden"
                    value = parts[1] in {"s", "h"}
                    if getattr(self.repo.job(job_id), flag) == value:
                        await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed(self.job(job_id))))
                        return
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"wait": 1})))
                    answered = True
                    self.repo.set_widget_job_flag(job_id, flag, value)
                    await self._edit(message_id, self.job(job_id))
                    return
                if parts[1] in {"a", "d"}:
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed(
                        {"ok": "Готовлю ответ в чате…"})))
                    answered = True
                    if parts[1] == "a":
                        job = self.repo.job(job_id)
                        analysis = self.repo.analysis(job_id)
                        if analysis:
                            await self.bot.send_message(self.owner_id, job_text(job, analysis, self.repo.job_link(job_id))[:4000])
                        else:
                            await self.bot.send_message(self.owner_id, "ИИ-анализ для заказа пока недоступен")
                    else:
                        original = self.repo.job_original(job_id)
                        if original is None:
                            raise ValueError("Текст заказа не найден")
                        result = await self.service.ai.ask("draft", original)
                        await self.bot.send_message(self.owner_id, result["draft"][:4000])
                    return
            if len(parts) == 3 and parts[0] == "s" and parts[1] == "p" and parts[2].isdigit():
                page = int(parts[2])
                state = self.sources(page)
                await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed(state)))
                answered = True
                await self._edit(message_id, state)
                return
            if (len(parts) == 4 and parts[0] == "s" and parts[2].isdigit()
                    and parts[3].isdigit()):
                value, page = int(parts[2]), int(parts[3])
                channel = self.repo.get_channel(value)
                if channel is None or channel.status not in {"approved", "paused"}:
                    raise ValueError("Источник уже убран")
                if parts[1] == "i":
                    rows, _ = self.repo.widget_sources(page)
                    found, match = next(((f, m) for c, f, m in rows if c.id == value), (0, 0))
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed(
                        {"ok": "Подробности отправляю в чат…"})))
                    answered = True
                    identity = f"@{channel.username}" if channel.username else "Приватный канал"
                    await self.bot.send_message(self.owner_id, f"{channel.title}\n{identity}\nНайдено: {found} · Подходящих: {match}\n{(channel.description or '')[:2500]}")
                    return
                if parts[1] == "x":
                    self.pending_removals[(message_id, value)] = datetime.now(timezone.utc) + timedelta(minutes=2)
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"confirm": value})))
                    answered = True
                    return
                if parts[1] in {"t", "y"}:
                    if parts[1] == "y":
                        expiry = self.pending_removals.pop((message_id, value), None)
                        if expiry is None or expiry < datetime.now(timezone.utc):
                            raise ValueError("Подтверждение истекло")
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"wait": 1})))
                    answered = True
                    if parts[1] == "t":
                        if not await self.service.set_channel_monitoring(value, channel.status == "paused"):
                            raise ValueError("Статус уже изменился")
                    else:
                        async with self.service.monitor_lock:
                            self.repo.remove_source(value)
                    await self._edit(message_id, self.sources(page))
                    return
            raise ValueError("Неизвестное действие")
        except ValueError as exc:
            if answered:
                state = self.sources(page) if parts[0] == "s" else self.job(job_id) if parts[0] == "j" else self.dashboard()
                if state:
                    if state["k"] == "j":
                        state["d"] = state["d"][:50]
                    await self._edit(message_id, {**state, "err": str(exc)[:80]})
            else:
                await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"error": str(exc)})))
        except Exception:
            log.exception("Действие widget %s", data)
            try:
                if answered:
                    await self.bot.send_message(self.owner_id, "Не удалось выполнить действие виджета. Попробуй позже.")
                else:
                    await self.bot(AnswerWidgetQuery(widget_query_id=query_id, data=packed({"error": "Ошибка. Повтори позже"})))
            except Exception:
                pass

    async def _edit(self, message_id: int, state: dict | None) -> None:
        if state is None:
            return
        try:
            await self.bot(EditMessageWidgetData(chat_id=self.owner_id, message_id=message_id, chat_data=packed(state)))
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
