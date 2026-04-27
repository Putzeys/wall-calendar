import json
import logging
import os
from datetime import datetime, timedelta
from io import BytesIO

import requests
from dotenv import load_dotenv
from flask import Flask, Response, redirect, render_template_string, request
from PIL import Image, ImageDraw, ImageFont
from google.auth.transport.requests import Request as GAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("wall")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

CAL_COLORS = ["#1e3a5f", "#5f1e3a", "#3a5f1e", "#5f3a1e", "#3a1e5f", "#1e5f5f"]
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def parse_cal_entry(entry: str) -> tuple[str, str, str | None]:
    """Parse 'account::id|name', 'id|name', 'account::id', or 'id'.

    Account defaults to 'main' when omitted. Name override is None when
    not provided (falls back to Google calendarList summary).
    """
    s = entry.strip()
    if "::" in s:
        acc, rest = s.split("::", 1)
    else:
        acc, rest = "main", s
    if "|" in rest:
        cid, name = rest.split("|", 1)
    else:
        cid, name = rest, None
    return acc.strip(), cid.strip(), (name.strip() if name else None)


CALENDAR_ENTRIES = [
    parse_cal_entry(e)
    for e in os.getenv("CALENDAR_IDS", "primary").split(",")
    if e.strip()
]
WRITE_ACCOUNT, WRITE_CAL_ID, _ = parse_cal_entry(
    os.getenv("WRITE_CALENDAR", "primary")
)


def token_path(account: str) -> str:
    return "token.json" if account == "main" else f"token_{account}.json"


_services: dict[str, object] = {}


def calendar_service(account: str = "main"):
    if account in _services:
        return _services[account]
    path = token_path(account)
    if not os.path.exists(path):
        hint = "" if account == "main" else f" {account}"
        raise RuntimeError(
            f"{path} ausente — rode `python auth.py{hint}` primeiro."
        )
    creds = Credentials.from_authorized_user_file(path, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GAuthRequest())
        with open(path, "w") as f:
            f.write(creds.to_json())
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    _services[account] = svc
    return svc


app = Flask(__name__)

_cal_names_cache: dict[str, str] = {}


def cal_name(account: str, cal_id: str) -> str:
    key = f"{account}::{cal_id}"
    if key in _cal_names_cache:
        return _cal_names_cache[key]
    try:
        info = calendar_service(account).calendarList().get(calendarId=cal_id).execute()
        name = info.get("summaryOverride") or info.get("summary") or cal_id
    except Exception:
        name = cal_id.split("@")[0]
    _cal_names_cache[key] = name
    return name


def hidden_cals(req) -> set[str]:
    """Cookie stores account::id keys."""
    raw = req.cookies.get("hidden", "")
    return {c for c in raw.split(",") if c}


SYS_PROMPT = """Você converte frases em PT-BR para JSON de evento de calendário.

Hoje é {today}. Fuso horário America/Sao_Paulo.

Responda APENAS com JSON neste formato:
{{"title": "string", "start": "YYYY-MM-DDTHH:MM:SS", "duration_min": 60}}

Regras:
- Sem hora explícita use 09:00.
- Sem duração explícita use 60 minutos.
- "manhã" = 09:00, "tarde" = 14:00, "noite" = 20:00.
- "café da manhã" / "café" = 08:00; "almoço" = 12:00; "jantar" = 20:00.
- "amanhã" = hoje+1; "depois de amanhã" = hoje+2.
- "sexta que vem" / "próxima sexta" = próxima ocorrência do dia da semana.
- Title: preserve a frase original, removendo APENAS palavras de tempo
  ("hoje", "amanhã", horários como "20h", "às 12h30", dias da semana,
  "que vem"). Mantenha pessoas, locais, contexto. Sem ponto final.

Exemplos:
"jantar com Esther hoje" → title "jantar com Esther"
"reunião com Pedro amanhã 14h sobre TCC" → title "reunião com Pedro sobre TCC"
"academia sexta 7h" → title "academia"
"buscar Sofia na escola às 17h" → title "buscar Sofia na escola"
"""


def parse_event(text: str) -> dict:
    today = datetime.now().strftime("%Y-%m-%d %A")
    payload = {
        "model": OLLAMA_MODEL,
        "format": "json",
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.1},
        "messages": [
            {"role": "system", "content": SYS_PROMPT.format(today=today)},
            {"role": "user", "content": text},
        ],
    }
    log.info("ollama parse: %r", text)
    r = requests.post(OLLAMA_URL, json=payload, timeout=60)
    r.raise_for_status()
    raw = r.json()["message"]["content"]
    p = json.loads(raw)
    if not isinstance(p, dict):
        raise ValueError(f"esperava objeto, veio: {raw}")
    for k in ("title", "start", "duration_min"):
        if k not in p:
            raise ValueError(f"campo ausente: {k}")
    datetime.fromisoformat(p["start"])
    int(p["duration_min"])
    p["title"] = str(p["title"]).strip()
    log.info("parsed: %s", p)
    return p


def fetch_events(start, end):
    out = []
    for i, (acc, cal_id, _name) in enumerate(CALENDAR_ENTRIES):
        try:
            cal = calendar_service(acc)
            items = (
                cal.events()
                .list(
                    calendarId=cal_id,
                    singleEvents=True,
                    orderBy="startTime",
                    timeMin=start.isoformat() + "Z",
                    timeMax=end.isoformat() + "Z",
                )
                .execute()
                .get("items", [])
            )
        except (HttpError, RuntimeError) as ex:
            log.warning("falha lendo %s::%s: %s", acc, cal_id, ex)
            continue
        color = CAL_COLORS[i % len(CAL_COLORS)]
        for e in items:
            e["_color"] = color
            e["_cal"] = cal_id
            e["_account"] = acc
            e["_key"] = f"{acc}::{cal_id}"
            out.append(e)
    return out


def day_label(d: datetime, today: datetime) -> str:
    delta = (d.date() - today.date()).days
    if delta == 0:
        return "hoje"
    if delta == 1:
        return "amanhã"
    return f"{DIAS[d.weekday()]} {d.day:02d}/{d.month:02d}"


# ---------- templates ----------

BASE_CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font:18px -apple-system,Helvetica;margin:0;background:#111;color:#eee}
a{color:#9cf;text-decoration:none}
.flash{position:fixed;top:0;left:0;right:0;padding:10px;text-align:center;
  background:#2a6;color:#fff;font-size:16px;z-index:10;
  -webkit-transition:opacity .4s;transition:opacity .4s}
.flash.err{background:#b33}
.flash.fade{opacity:0}
.spin{display:inline-block;width:18px;height:18px;border:3px solid #fff;
  border-top-color:transparent;border-radius:50%;vertical-align:middle;
  -webkit-animation:sp .8s linear infinite;animation:sp .8s linear infinite}
@-webkit-keyframes sp{to{-webkit-transform:rotate(360deg)}}
@keyframes sp{to{transform:rotate(360deg)}}
button[disabled]{opacity:.6}
"""

WALL_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
{% if w == 0 %}<meta http-equiv="refresh" content="300; url=/wall" id="autoref">{% endif %}
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Cozinha">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="apple-touch-icon" sizes="152x152" href="/apple-touch-icon-152x152.png">
<link rel="apple-touch-icon" sizes="76x76" href="/apple-touch-icon-76x76.png">
<title>Cozinha</title>
<style>
""" + BASE_CSS + """
.navbar{display:-webkit-flex;display:flex;-webkit-align-items:center;
  align-items:center;-webkit-justify-content:space-between;
  justify-content:space-between;padding:8px 14px;background:#0a0a0a;
  border-bottom:1px solid #222;font-size:15px}
.navbar .nav{display:inline-block;padding:6px 18px;border-radius:6px;
  background:#222;color:#9cf;font-size:22px;font-weight:bold;line-height:1}
.navbar .range{color:#aaa}
.navbar .today{margin-left:10px;padding:4px 10px;border-radius:6px;
  background:#2a6;color:#fff;font-size:13px}
.filters{display:-webkit-flex;display:flex;padding:8px;background:#0a0a0a;
  border-bottom:1px solid #222;-webkit-flex-wrap:wrap;flex-wrap:wrap}
.filters a{display:inline-block;padding:6px 12px;margin:3px;border-radius:14px;
  font-size:13px;color:#fff;font-weight:500}
.filters a.off{background:#222 !important;color:#666;text-decoration:line-through}
.week{display:-webkit-flex;display:flex;padding-bottom:74px}
.day{-webkit-flex:1;flex:1;border-right:1px solid #333;padding:8px;min-height:75vh}
.day.today{background:#181818}
.day h2{font-size:13px;color:#888;margin:0 0 8px;text-transform:uppercase}
.day.today h2{color:#9cf}
.ev{padding:6px 8px;border-radius:4px;margin-bottom:4px;font-size:14px;color:#dfefff}
.ev time{display:block;color:#bcd;font-size:12px}
.ev.past{opacity:0.45}
.ev.allday{font-size:12px;padding:4px 8px;border-left:3px solid #fff;border-radius:2px}
.ev.allday time{display:none}
.ev.bday{background:transparent !important;color:#f9c66b;font-size:13px;
  padding:2px 4px;border:0;margin-bottom:2px}
.ev.bday time{display:none}
.ev.bday:before{content:"🎂 "}
form.bar{position:fixed;bottom:0;left:0;right:0;padding:10px;background:#000;
  display:-webkit-flex;display:flex;border-top:1px solid #333}
input.txt{-webkit-flex:1;flex:1;font-size:22px;padding:14px;background:#222;
  color:#fff;border:0;border-radius:4px}
button.add{font-size:22px;padding:14px 24px;margin-left:8px;background:#2a6;
  color:#fff;border:0;border-radius:4px;font-weight:bold}
</style></head><body>
{% if msg %}<div class="flash {{ 'err' if err else '' }}" id="flash">{{ msg }}</div>{% endif %}
<div class="navbar">
  <a href="/wall?w={{ w - 1 }}" class="nav">‹</a>
  <span class="range">{{ range_label }}{% if w != 0 %} <a href="/wall" class="today">↺ hoje</a>{% endif %}</span>
  <a href="/wall?w={{ w + 1 }}" class="nav">›</a>
</div>
<div class="filters">
{% for c in cal_meta %}
  <a href="/toggle?cal={{ c.id|urlencode }}"
     class="{{ 'off' if c.hidden else '' }}"
     style="background:{{ c.color }}">{{ c.name }}</a>
{% endfor %}
</div>
<div class="week">
{% for d in days %}
  <div class="day{% if d.today %} today{% endif %}">
    <h2>{{ d.label }}</h2>
    {% for e in d.events %}
      <div class="ev{% if e.past %} past{% endif %}{% if e.allday and not e.bday %} allday{% endif %}{% if e.bday %} bday{% endif %}"
           style="background:{{ e.color }}">
        <time>{{ e.time }}</time>{{ e.title }}
      </div>
    {% endfor %}
  </div>
{% endfor %}
</div>
<form class="bar" action="/events" method="post" autocomplete="off" id="addform">
  <input class="txt" type="text" name="text" id="t"
         placeholder="almoço com a esther amanhã 12h"
         autocapitalize="sentences" autocorrect="on">
  <button class="add" type="submit">+</button>
</form>
<script>
(function(){
  var t=document.getElementById('t'), m=document.getElementById('autoref');
  if(t&&m){
    t.addEventListener('focus',function(){
      if(m.parentNode) m.parentNode.removeChild(m);
      setTimeout(function(){ t.scrollIntoView(); },300);
    });
  }
  var fl=document.getElementById('flash');
  if(fl){
    var isErr=fl.className.indexOf('err')>=0;
    var hideAfter=isErr?6000:3000;
    setTimeout(function(){ fl.className+=' fade'; },hideAfter);
    setTimeout(function(){ if(fl.parentNode) fl.parentNode.removeChild(fl); },hideAfter+500);
    if(window.history&&window.history.replaceState){
      window.history.replaceState({},'',window.location.pathname);
    }
  }
  var forms=document.getElementsByTagName('form');
  for(var i=0;i<forms.length;i++){
    forms[i].addEventListener('submit',function(ev){
      var f=ev.currentTarget||this;
      setTimeout(function(){
        var btns=f.getElementsByTagName('button');
        for(var j=0;j<btns.length;j++){
          if(btns[j].type==='submit'){
            btns[j].disabled=true;
            btns[j].innerHTML='<span class="spin"></span>';
          }
        }
        var inp=f.querySelector('input[type=text]');
        if(inp) inp.disabled=true;
      },0);
    });
  }
})();
</script>
</body></html>"""

CONFIRM_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<title>Confirmar evento</title>
<style>
""" + BASE_CSS + """
.box{max-width:600px;margin:60px auto;padding:24px;background:#1b1b1b;border-radius:8px}
h1{font-size:18px;color:#888;text-transform:uppercase;margin:0 0 16px}
.title{font-size:32px;margin:0 0 12px}
.meta{font-size:18px;color:#9cf;margin:0 0 24px}
.original{font-size:14px;color:#666;margin:0 0 24px;font-style:italic}
.row{display:-webkit-flex;display:flex}
button{-webkit-flex:1;flex:1;font-size:22px;padding:18px;border:0;border-radius:4px;
  color:#fff;font-weight:bold;margin:0 4px}
.ok{background:#2a6}
.cancel{background:#444}
</style></head><body>
<div class="box">
  <h1>Confirmar?</h1>
  <p class="title">{{ p.title }}</p>
  <p class="meta">{{ pretty }} · {{ p.duration_min }} min · {{ write_cal }}</p>
  <p class="original">Você disse: "{{ original }}"</p>
  <div class="row">
    <form action="/wall" method="get" style="-webkit-flex:1;flex:1">
      <button type="submit" class="cancel">Cancelar</button>
    </form>
    <form action="/events/confirm" method="post" style="-webkit-flex:1;flex:1" id="cf">
      <input type="hidden" name="title" value="{{ p.title }}">
      <input type="hidden" name="start" value="{{ p.start }}">
      <input type="hidden" name="duration_min" value="{{ p.duration_min }}">
      <button type="submit" class="ok">Confirmar</button>
    </form>
  </div>
</div>
<script>
document.getElementById('cf').addEventListener('submit',function(){
  var b=this.querySelector('button');
  setTimeout(function(){ b.disabled=true; b.innerHTML='<span class="spin"></span>'; },0);
});
</script>
</body></html>"""

# ---------- routes ----------


@app.route("/")
@app.route("/wall")
def wall():
    try:
        for acc in {e[0] for e in CALENDAR_ENTRIES}:
            calendar_service(acc)
    except RuntimeError as ex:
        return f"<h1>Setup pendente</h1><p>{ex}</p>", 503

    hidden = hidden_cals(request)
    try:
        w = int(request.args.get("w", "0"))
    except ValueError:
        w = 0
    w = max(-52, min(52, w))
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start + timedelta(days=7 * w)
    end = start + timedelta(days=7)
    items = fetch_events(start, end)
    range_label = (
        f"{start.strftime('%d/%m')} — {(end - timedelta(days=1)).strftime('%d/%m')}"
    )

    cal_meta = []
    bday_keys = set()
    for i, (acc, cid, name_override) in enumerate(CALENDAR_ENTRIES):
        name = name_override or cal_name(acc, cid)
        key = f"{acc}::{cid}"
        is_bday = "aniversári" in name.lower() or "birthday" in name.lower()
        if is_bday:
            bday_keys.add(key)
        cal_meta.append({
            "id": key,
            "name": name,
            "color": CAL_COLORS[i % len(CAL_COLORS)],
            "hidden": key in hidden,
        })

    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        bdays, all_day, timed = [], [], []
        for e in items:
            if e["_key"] in hidden:
                continue
            dt_full = e["start"].get("dateTime", "") or e["start"].get("date", "")
            if dt_full[:10] != key:
                continue
            is_allday = not e["start"].get("dateTime")
            is_bday = e["_key"] in bday_keys
            time_str = "" if is_allday else e["start"]["dateTime"][11:16]
            past = bool(e["start"].get("dateTime")) and (
                datetime.fromisoformat(
                    e["start"]["dateTime"].replace("Z", "+00:00")
                ).replace(tzinfo=None) < now
            )
            ev = {
                "title": e.get("summary", "(sem título)"),
                "time": time_str,
                "color": e["_color"],
                "past": past,
                "allday": is_allday,
                "bday": is_bday,
            }
            if is_bday:
                bdays.append(ev)
            elif is_allday:
                all_day.append(ev)
            else:
                timed.append(ev)
        timed.sort(key=lambda x: x["time"])
        bdays.sort(key=lambda x: x["title"].lower())
        all_day.sort(key=lambda x: x["title"].lower())
        days.append({
            "label": day_label(d, now),
            "today": d.date() == now.date(),
            "events": bdays + all_day + timed,
        })

    return render_template_string(
        WALL_HTML,
        days=days,
        cal_meta=cal_meta,
        w=w,
        range_label=range_label,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.route("/toggle")
def toggle_cal():
    key = request.args.get("cal", "")
    valid_keys = {f"{a}::{c}" for a, c, _ in CALENDAR_ENTRIES}
    hidden = hidden_cals(request)
    if key in hidden:
        hidden.discard(key)
    elif key in valid_keys:
        hidden.add(key)
    resp = redirect("/wall")
    resp.set_cookie("hidden", ",".join(sorted(hidden)),
                    max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@app.route("/events", methods=["POST"])
def parse_step():
    text = request.form.get("text", "").strip()
    if not text:
        return redirect("/wall")
    try:
        p = parse_event(text)
    except requests.RequestException as ex:
        return redirect(f"/wall?msg=Ollama indisponível: {ex}&err=1")
    except (ValueError, KeyError, json.JSONDecodeError) as ex:
        return redirect(f"/wall?msg=Não entendi: {ex}&err=1")

    s = datetime.fromisoformat(p["start"])
    pretty = s.strftime("%a %d/%m às %H:%M")
    write_label = WRITE_CAL_ID if WRITE_ACCOUNT == "main" else f"{WRITE_ACCOUNT}::{WRITE_CAL_ID}"
    return render_template_string(
        CONFIRM_HTML,
        p=p,
        pretty=pretty,
        original=text,
        write_cal=write_label,
    )


@app.route("/events/confirm", methods=["POST"])
def confirm_step():
    try:
        title = request.form["title"].strip()
        start = datetime.fromisoformat(request.form["start"])
        dur = int(request.form["duration_min"])
        end = start + timedelta(minutes=dur)
        calendar_service(WRITE_ACCOUNT).events().insert(
            calendarId=WRITE_CAL_ID,
            body={
                "summary": title,
                "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
            },
        ).execute()
        log.info("inseriu: %s @ %s (acc=%s)", title, start, WRITE_ACCOUNT)
        return redirect(f"/wall?msg=Criado: {title}")
    except Exception as ex:
        log.exception("falha inserindo")
        return redirect(f"/wall?msg=Erro ao criar: {ex}&err=1")


@app.route("/health")
def health():
    out = {"ollama": "ok", "accounts": {}}
    accs = {e[0] for e in CALENDAR_ENTRIES} | {WRITE_ACCOUNT}
    for acc in accs:
        try:
            calendar_service(acc).calendarList().list(maxResults=1).execute()
            out["accounts"][acc] = "ok"
        except Exception as ex:
            out["accounts"][acc] = f"erro: {ex}"
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
    except Exception as ex:
        out["ollama"] = f"erro: {ex}"
    return out


@app.route("/calendars")
def list_calendars():
    """Lista IDs de todas as contas autenticadas."""
    rows = []
    accs = sorted({e[0] for e in CALENDAR_ENTRIES} | {WRITE_ACCOUNT, "main"})
    for acc in accs:
        try:
            cals = calendar_service(acc).calendarList().list().execute().get("items", [])
        except Exception as ex:
            rows.append(f"<tr><td colspan=4>conta <b>{acc}</b>: {ex}</td></tr>")
            continue
        for c in cals:
            rows.append(
                f"<tr><td><b>{acc}</b></td>"
                f"<td><code>{c['id']}</code></td>"
                f"<td>{c.get('summary','')}</td>"
                f"<td>{c.get('accessRole','')}</td></tr>"
            )
    return (
        "<h1>Agendas disponíveis</h1>"
        "<p>Sintaxe pro <code>CALENDAR_IDS</code>: "
        "<code>id|nome</code> ou <code>conta::id|nome</code>. "
        "Sem prefixo de conta usa <code>main</code> (token.json). "
        "Pra adicionar outra conta: <code>python auth.py LABEL</code>.</p>"
        f"<table border=1 cellpadding=6>"
        f"<tr><th>conta</th><th>id</th><th>nome</th><th>access</th></tr>"
        f"{''.join(rows)}</table>"
    )


@app.route("/whoami")
def whoami():
    """Conta(s) autenticada(s) e configuração atual."""
    out = {"accounts": {}}
    accs = {e[0] for e in CALENDAR_ENTRIES} | {WRITE_ACCOUNT}
    for acc in accs:
        try:
            cals = calendar_service(acc).calendarList().list().execute().get("items", [])
            primary = next((c for c in cals if c.get("primary")), None)
            out["accounts"][acc] = {
                "primary_id": primary["id"] if primary else None,
                "primary_summary": primary.get("summary") if primary else None,
                "token_file": token_path(acc),
            }
        except Exception as ex:
            out["accounts"][acc] = {"error": str(ex)}
    out["write"] = {"account": WRITE_ACCOUNT, "calendar": WRITE_CAL_ID}
    out["calendars"] = [
        {"account": a, "id": c, "name_override": n}
        for a, c, n in CALENDAR_ENTRIES
    ]
    return out


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/favicon.svg")
def favicon_svg():
    """Calendar tile with today's day number."""
    day = datetime.now().day
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="6" y="14" width="52" height="44" rx="6" fill="#fafafa"/>
  <rect x="6" y="14" width="52" height="14" rx="6" fill="#b53a3a"/>
  <rect x="6" y="22" width="52" height="6" fill="#b53a3a"/>
  <rect x="14" y="6" width="6" height="14" rx="2" fill="#444"/>
  <rect x="44" y="6" width="6" height="14" rx="2" fill="#444"/>
  <text x="32" y="52" font-family="-apple-system,Helvetica,sans-serif"
        font-size="28" font-weight="700" text-anchor="middle"
        fill="#222">{day}</text>
</svg>"""
    return svg, 200, {"Content-Type": "image/svg+xml; charset=utf-8",
                      "Cache-Control": "public, max-age=3600"}


_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]


def _load_font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_calendar_png(size: int = 180) -> bytes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 14
    body_top = int(size * 0.18)
    bar_bot = body_top + int(size * 0.22)
    radius = size // 12

    d.rounded_rectangle(
        (pad, body_top, size - pad, size - pad), radius=radius, fill="#fafafa"
    )
    d.rounded_rectangle(
        (pad, body_top, size - pad, bar_bot), radius=radius, fill="#b53a3a"
    )
    d.rectangle((pad, bar_bot - radius, size - pad, bar_bot), fill="#b53a3a")

    ring_w = max(4, size // 18)
    ring_h = int(size * 0.18)
    ring_y = int(size * 0.06)
    for cx in (size * 0.28, size * 0.72):
        d.rounded_rectangle(
            (cx - ring_w / 2, ring_y, cx + ring_w / 2, ring_y + ring_h),
            radius=ring_w // 2,
            fill="#444",
        )

    day = str(datetime.now().day)
    font = _load_font(int(size * 0.45))
    bbox = d.textbbox((0, 0), day, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = bar_bot + (size - pad - bar_bot - th) / 2 - bbox[1]
    d.text((tx, ty), day, fill="#222", font=font)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
@app.route("/apple-touch-icon-180x180.png")
def apple_icon_180():
    return Response(render_calendar_png(180), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/apple-touch-icon-152x152.png")
@app.route("/apple-touch-icon-152x152-precomposed.png")
def apple_icon_152():
    return Response(render_calendar_png(152), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/apple-touch-icon-76x76.png")
@app.route("/apple-touch-icon-76x76-precomposed.png")
def apple_icon_76():
    return Response(render_calendar_png(152), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
