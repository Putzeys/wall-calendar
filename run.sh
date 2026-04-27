#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "criado .env — edite se necessário"
fi

# shellcheck disable=SC1091
set -a; . ./.env; set +a

if [ ! -f token.json ]; then
  echo "ERRO: token.json ausente. Rode: python auth.py"
  exit 1
fi

# pré-aquece o modelo no Ollama (carrega na VRAM em background)
MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
echo "aquecendo $MODEL..."
curl -s -X POST "$URL" -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"oi\"}],\"stream\":false,\"keep_alive\":\"30m\"}" > /dev/null || \
  echo "aviso: Ollama não respondeu (siga assim mesmo)"

exec python app.py
