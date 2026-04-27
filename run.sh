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

if [ ! -f token.json ]; then
  echo "ERRO: token.json ausente. Rode: python auth.py"
  exit 1
fi

# Lê só OLLAMA_MODEL e OLLAMA_URL do .env via python-dotenv pra não cair em
# armadilhas de shell quoting (valores com | ::, etc).
MODEL=$(python -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('OLLAMA_MODEL') or 'qwen2.5:3b')")
URL=$(python -c "from dotenv import dotenv_values; print(dotenv_values('.env').get('OLLAMA_URL') or 'http://localhost:11434/api/chat')")
echo "aquecendo $MODEL..."
curl -s -X POST "$URL" -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"oi\"}],\"stream\":false,\"keep_alive\":\"30m\"}" > /dev/null || \
  echo "aviso: Ollama não respondeu (siga assim mesmo)"

exec python app.py
