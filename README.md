# wall-calendar

Calendário de parede pra iPad antigo. Mostra a semana inteira do Google
Calendar e cria novos eventos por voz ou texto, com um LLM local fazendo o
parsing de linguagem natural.

## Objetivo

Reaproveitar um iPad 2 (preso no iOS 9) como display fixo na cozinha.
O dispositivo é praticamente inútil pra navegação moderna, mas serve muito
bem como tela burra renderizando HTML server-side.

A ideia: bater o olho na parede e ver a agenda compartilhada da família;
quando algo aparecer ("dentista quinta às 15h"), ditar pelo microfone do
teclado iOS e o evento entra no Google Calendar — sem app, sem conta, sem
SaaS de terceiros.

## Restrições de design

- **iOS 9 Safari**: sem `getUserMedia`, sem CSS Grid, sem fetch confiável,
  sem ES6. Tudo é renderizado no servidor; o cliente é HTML + flexbox + um
  punhado de ES5. Áudio entra pelo botão de microfone nativo do teclado
  iOS (ditado), não pela página.
- **Self-hosted**: sem dependência de SaaS além do próprio Google Calendar.
  O parsing de NL roda em Ollama local (`qwen2.5:3b` por padrão, ~2GB RAM).
- **Sem app, sem store**: PWA simples adicionada à tela inicial.

## Arquitetura

```
iPad 2 (Safari iOS 9)
   │  HTTP via Tailscale
   ▼
Flask /wall                 ← server-rendered, meta-refresh 5min
       /events  (POST)      ← passa frase pro Ollama
       /events/confirm      ← insere após review humano
       /calendars           ← helper: lista IDs disponíveis
       /health              ← status JSON
   │
   ├──► Ollama (qwen2.5:3b)        — parsing NL → JSON
   └──► Google Calendar API        — leitura multi-agenda + insert
```

Fluxo de criação:

1. Usuário toca no input, toca no mic do teclado iOS, dita.
2. POST `/events` com a frase bruta.
3. Servidor chama Ollama com `format: "json"` → `{title, start, duration_min}`.
4. Tela de **confirmação** mostra o JSON parseado em PT-BR, com a frase
   original ao lado pra detectar erro de transcrição.
5. POST `/events/confirm` insere via Google Calendar API.

A confirmação não é opcional: modelos pequenos + ditado podem errar
("amanhã" virou "manhã"), e um evento mudo no calendário compartilhado é
ruim.

## Stack

- Python 3.10+, Flask 3
- Ollama (modelo configurável via env)
- google-api-python-client (OAuth Desktop flow)
- Zero dependência JS no front

## Setup

### 1. Pré-requisitos no host

```bash
# Ollama + modelo
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b
```

### 2. Google Cloud

1. Console GCP → novo projeto → ativa **Google Calendar API**.
2. Credentials → Create Credentials → **OAuth client ID** → tipo
   **Desktop app** → baixa o JSON como `credentials.json` na raiz do
   projeto.

### 3. Projeto

```bash
git clone <repo> wall-calendar
cd wall-calendar

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# OAuth (uma vez, abre browser local)
python auth.py
# → gera token.json

cp .env.example .env
# edita CALENDAR_IDS e WRITE_CALENDAR depois (ver passo 4)

./run.sh
```

### 4. Descobrir IDs de agenda

Com o app rodando, abra `http://localhost:5000/calendars`. Copie os IDs
desejados pro `.env`:

```env
CALENDAR_IDS=primary,esposo@gmail.com,c_xxx@group.calendar.google.com
WRITE_CALENDAR=primary
```

Reinicie o app.

### 5. iPad

1. **Ajustes → Tela e Brilho → Auto-bloqueio: Nunca**, brilho ~40%.
2. Safari → `http://<host>:5000` (via Tailscale ou rede local).
3. Compartilhar → **Adicionar à Tela de Início** (vira fullscreen).
4. Abrir o ícone → **Acessibilidade → Acesso Guiado** (triplo-clique trava
   no app).
5. **Ajustes → Geral → Teclado → ativar Ditado.**

### 6. systemd (opcional)

```bash
sudo cp wall-calendar.service /etc/systemd/system/
# edita User= e WorkingDirectory= se necessário
sudo systemctl enable --now wall-calendar
journalctl -u wall-calendar -f
```

## Configuração

Variáveis em `.env`:

| Variável | Padrão | Descrição |
|---|---|---|
| `HOST` | `0.0.0.0` | bind do Flask |
| `PORT` | `5000` | |
| `TIMEZONE` | `America/Sao_Paulo` | usado nos events do Google |
| `OLLAMA_URL` | `http://localhost:11434/api/chat` | |
| `OLLAMA_MODEL` | `qwen2.5:3b` | qualquer modelo Ollama com suporte a `format:json` |
| `CALENDAR_IDS` | `primary` | vírgula separa, ordem define cor |
| `WRITE_CALENDAR` | `primary` | onde novos eventos vão parar |

## Endpoints

- `GET /` ou `/wall` — view da semana, atualiza a cada 5min
- `POST /events` — recebe `text`, parseia, mostra confirmação
- `POST /events/confirm` — recebe `title`, `start`, `duration_min`, insere
- `GET /calendars` — tabela com IDs disponíveis
- `GET /health` — JSON `{calendar, ollama, calendars}`

## Burn-in

iPad 2 tem LCD IPS, não OLED — burn-in permanente é raríssimo. Mitigações
de baixo custo já estão no app (brilho recomendado baixo, refresh
periódico que muda layout do dia atual). Pra uso 24/7 contínuo, alternar
tema claro/escuro de dia/noite é uma melhoria possível.

## Modelos alternativos

Caso `qwen2.5:3b` erre muito raciocínio temporal ("sexta que vem"):

- `qwen2.5:7b` — mesma família, ~5GB, melhora bem em datas relativas
- `llama3.2:3b` — alternativa similar em peso, PT-BR aceitável
- `gemma2:2b` — mais leve, PT-BR mais fraco

Troque via `OLLAMA_MODEL`. O system prompt já enumera regras temporais;
se trocar de modelo e errar, ajuste `SYS_PROMPT` em `app.py`.

## Roadmap

- [ ] Edição/remoção de eventos pelo iPad (formulário inline em cada card)
- [ ] Wake-word + mic always-on no servidor (whisper.cpp), dispensa o iPad
      pra criação
- [ ] Tema claro/escuro automático por horário
- [ ] Suporte a eventos recorrentes na linguagem natural

## Licença

MIT.
