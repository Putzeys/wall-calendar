import json
import logging
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, redirect, render_template_string, request
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
CALENDAR_IDS = [
    c.strip()
    for c in os.getenv("CALENDAR_IDS", "primary").split(",")
    if c.strip()
]
WRITE_CALENDAR = os.getenv("WRITE_CALENDAR", "primary")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))

CAL_COLORS = ["#1e3a5f", "#5f1e3a", "#3a5f1e", "#5f3a1e", "#3a1e5f", "#1e5f5f"]
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

app = Flask(__name__)

_cal_names_cache: dict[str, str] = {}


def cal_name(cal, cal_id: str) -> str:
    if cal_id in _cal_names_cache:
        return _cal_names_cache[cal_id]
    try:
        info = cal.calendarList().get(calendarId=cal_id).execute()
        name = info.get("summaryOverride") or info.get("summary") or cal_id
    except Exception:
        name = cal_id.split("@")[0]
    _cal_names_cache[cal_id] = name
    return name


def hidden_cals(req) -> set[str]:
    raw = req.cookies.get("hidden", "")
    return {c for c in raw.split(",") if c}


def calendar_service():
    if not os.path.exists("token.json"):
        raise RuntimeError(
            "token.json ausente — rode `python auth.py` primeiro."
        )
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(GAuthRequest())
        with open("token.json", "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


SYS_PROMPT = """Você converte frases em PT-BR para JSON de evento de calendário.

Hoje é {today}. Fuso horário America/Sao_Paulo.

Responda APENAS com JSON neste formato:
{{"title": "string", "start": "YYYY-MM-DDTHH:MM:SS", "duration_min": 60}}

Regras:
- Sem hora explícita use 09:00.
- Sem duração explícita use 60 minutos.
- "manhã" = 09:00, "tarde" = 14:00, "noite" = 20:00.
- "amanhã" = hoje+1; "depois de amanhã" = hoje+2.
- "sexta que vem" / "próxima sexta" = próxima ocorrência do dia da semana.
- Title curto, sem ponto final.
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


def fetch_events(cal, start, end):
    out = []
    for i, cal_id in enumerate(CALENDAR_IDS):
        try:
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
        except HttpError as ex:
            log.warning("falha lendo %s: %s", cal_id, ex)
            continue
        color = CAL_COLORS[i % len(CAL_COLORS)]
        for e in items:
            e["_color"] = color
            e["_cal"] = cal_id
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
  background:#2a6;color:#fff;font-size:16px;z-index:10}
.flash.err{background:#b33}
"""

WALL_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<meta http-equiv="refresh" content="300" id="autoref">
<title>Cozinha</title>
<style>
""" + BASE_CSS + """
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
form.bar{position:fixed;bottom:0;left:0;right:0;padding:10px;background:#000;
  display:-webkit-flex;display:flex;border-top:1px solid #333}
input.txt{-webkit-flex:1;flex:1;font-size:22px;padding:14px;background:#222;
  color:#fff;border:0;border-radius:4px}
button.add{font-size:22px;padding:14px 24px;margin-left:8px;background:#2a6;
  color:#fff;border:0;border-radius:4px;font-weight:bold}
</style></head><body>
{% if msg %}<div class="flash {{ 'err' if err else '' }}">{{ msg }}</div>{% endif %}
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
      <div class="ev{% if e.past %} past{% endif %}{% if e.allday %} allday{% endif %}"
           style="background:{{ e.color }}">
        <time>{{ e.time }}</time>{{ e.title }}
      </div>
    {% endfor %}
  </div>
{% endfor %}
</div>
<form class="bar" action="/events" method="post" autocomplete="off">
  <input class="txt" type="text" name="text" id="t"
         placeholder="Almoço com Cheila amanhã 12h"
         autocapitalize="sentences" autocorrect="on">
  <button class="add" type="submit">+</button>
</form>
<script>
(function(){
  var t=document.getElementById('t'), m=document.getElementById('autoref');
  if(!t||!m) return;
  t.addEventListener('focus',function(){
    if(m.parentNode) m.parentNode.removeChild(m);
    setTimeout(function(){ t.scrollIntoView(); },300);
  });
})();
</script>
</body></html>"""

CONFIRM_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
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
    <form action="/events/confirm" method="post" style="-webkit-flex:1;flex:1">
      <input type="hidden" name="title" value="{{ p.title }}">
      <input type="hidden" name="start" value="{{ p.start }}">
      <input type="hidden" name="duration_min" value="{{ p.duration_min }}">
      <button type="submit" class="ok">Confirmar</button>
    </form>
  </div>
</div>
</body></html>"""

# ---------- routes ----------


@app.route("/")
@app.route("/wall")
def wall():
    try:
        cal = calendar_service()
    except RuntimeError as ex:
        return f"<h1>Setup pendente</h1><p>{ex}</p>", 503

    hidden = hidden_cals(request)
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    items = fetch_events(cal, start, end)

    cal_meta = [
        {
            "id": cid,
            "name": cal_name(cal, cid),
            "color": CAL_COLORS[i % len(CAL_COLORS)],
            "hidden": cid in hidden,
        }
        for i, cid in enumerate(CALENDAR_IDS)
    ]

    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        key = d.strftime("%Y-%m-%d")
        all_day, timed = [], []
        for e in items:
            if e["_cal"] in hidden:
                continue
            dt_full = e["start"].get("dateTime", "") or e["start"].get("date", "")
            if dt_full[:10] != key:
                continue
            is_allday = not e["start"].get("dateTime")
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
            }
            (all_day if is_allday else timed).append(ev)
        days.append({
            "label": day_label(d, now),
            "today": d.date() == now.date(),
            "events": all_day + timed,
        })

    return render_template_string(
        WALL_HTML,
        days=days,
        cal_meta=cal_meta,
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.route("/toggle")
def toggle_cal():
    cal_id = request.args.get("cal", "")
    hidden = hidden_cals(request)
    if cal_id in hidden:
        hidden.discard(cal_id)
    elif cal_id in CALENDAR_IDS:
        hidden.add(cal_id)
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
    return render_template_string(
        CONFIRM_HTML,
        p=p,
        pretty=pretty,
        original=text,
        write_cal=WRITE_CALENDAR,
    )


@app.route("/events/confirm", methods=["POST"])
def confirm_step():
    try:
        title = request.form["title"].strip()
        start = datetime.fromisoformat(request.form["start"])
        dur = int(request.form["duration_min"])
        end = start + timedelta(minutes=dur)
        calendar_service().events().insert(
            calendarId=WRITE_CALENDAR,
            body={
                "summary": title,
                "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
                "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
            },
        ).execute()
        log.info("inseriu: %s @ %s", title, start)
        return redirect(f"/wall?msg=Criado: {title}")
    except Exception as ex:
        log.exception("falha inserindo")
        return redirect(f"/wall?msg=Erro ao criar: {ex}&err=1")


@app.route("/health")
def health():
    out = {"calendar": "ok", "ollama": "ok", "calendars": CALENDAR_IDS}
    try:
        calendar_service().calendarList().list(maxResults=1).execute()
    except Exception as ex:
        out["calendar"] = f"erro: {ex}"
    try:
        requests.get(OLLAMA_URL.replace("/api/chat", "/api/tags"), timeout=3)
    except Exception as ex:
        out["ollama"] = f"erro: {ex}"
    return out


@app.route("/calendars")
def list_calendars():
    """Helper: lista IDs disponíveis pra colocar em CALENDAR_IDS."""
    cals = calendar_service().calendarList().list().execute().get("items", [])
    rows = "".join(
        f"<tr><td><code>{c['id']}</code></td><td>{c.get('summary','')}</td></tr>"
        for c in cals
    )
    return (
        "<h1>Suas agendas</h1>"
        "<p>Copie os IDs desejados para <code>CALENDAR_IDS</code> no .env</p>"
        f"<table border=1 cellpadding=6>{rows}</table>"
    )


@app.route("/favicon.ico")
def favicon():
    return "", 204


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
