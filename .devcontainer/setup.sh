#!/bin/sh
# Codespace setup, run once from postCreateCommand.
#
# The Python install and the Ollama setup don't depend on each other, so they
# run at the same time; the setup takes as long as the slower one. Each line
# of output is labeled [python] or [ollama].
#
# The Python install uses uv (installed here first), which is several times
# faster than pip; `make install` falls back to pip without it.
#
# Set INSTALL_OLLAMA to false in devcontainer.json for a class using Option A
# (Anthropic + OpenAI), which never uses Ollama. A failed Ollama setup doesn't
# fail the Codespace: docs/student-guide.md's Troubleshooting has the manual
# steps. A failed Python install does.

set -u
status_dir=$(mktemp -d)

echo "── Installing uv ──────────────────────────────────────────────────"
python3 -m pip install --user --quiet --disable-pip-version-check uv \
  || echo "Couldn't install uv; make install will use pip."
export PATH="$HOME/.local/bin:$PATH"

run_labeled() {
  # run_labeled <label> <command...>: prefix each output line, keep the status.
  label=$1
  shift
  { "$@" 2>&1; echo $? > "$status_dir/$label"; } | sed -u "s/^/[$label] /"
}

run_labeled python make install &
if [ "${INSTALL_OLLAMA:-true}" = "true" ]; then
  run_labeled ollama sh .devcontainer/setup-ollama.sh &
else
  echo "Skipping Ollama (INSTALL_OLLAMA=${INSTALL_OLLAMA})."
fi
wait

python_status=$(cat "$status_dir/python" 2>/dev/null || echo 1)
ollama_status=$(cat "$status_dir/ollama" 2>/dev/null || echo skipped)
rm -rf "$status_dir"

if [ "$ollama_status" != "0" ] && [ "$ollama_status" != "skipped" ]; then
  echo "Ollama setup failed - see docs/student-guide.md Troubleshooting to install it manually. The rest of the app will still work once you do."
fi
if [ "$python_status" != "0" ]; then
  echo "Python install failed (make install). Run it again in the terminal to see the error."
  exit 1
fi
echo "── Setup complete ─────────────────────────────────────────────────"
