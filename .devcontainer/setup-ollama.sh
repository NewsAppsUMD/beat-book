#!/bin/sh
# Installs Ollama and pulls the local embedding model used for topic
# clustering and citation matching. Run from postCreateCommand.
#
# Deliberately non-fatal from the caller's point of view (see
# devcontainer.json, which wraps this with `|| true`-equivalent handling):
# a hiccup here (flaky network, slow model pull) shouldn't break the whole
# Codespace, since `make install` already succeeded by the time this runs.
# Falls back to the manual steps in docs/student-guide.md's Troubleshooting
# section if anything here fails.

set -e

echo "── Installing zstd (required by the Ollama installer) ────────────"
if ! command -v zstd > /dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y zstd
fi

echo "── Installing Ollama ──────────────────────────────────────────────"
curl -fsSL https://ollama.com/install.sh | sh

echo "── Starting Ollama server ─────────────────────────────────────────"
nohup ollama serve > /tmp/ollama.log 2>&1 &

echo "── Waiting for Ollama to accept connections ───────────────────────"
i=0
until curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "Ollama server did not come up after 30s — see /tmp/ollama.log" >&2
    exit 1
  fi
  sleep 1
done

echo "── Pulling qwen3-embedding:0.6b ────────────────────────────────────"
ollama pull qwen3-embedding:0.6b

echo "── Ollama setup complete ──────────────────────────────────────────"
