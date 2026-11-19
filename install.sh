#!/usr/bin/env bash
# Sets up OpenShorts next to the kit: clones it at the tested commit, applies the
# kit's patch (Claude backend + campaign brief), adds the GPU docker-compose
# override and creates openshorts/.env from the template. Safe to run again.
set -euo pipefail

OPENSHORTS_REPO="https://github.com/mutonby/openshorts.git"
OPENSHORTS_COMMIT="0069894"
ROOT="$(cd "$(dirname "$0")" && pwd)"
PATCH_DIR="$ROOT/patches/openshorts"
OS_DIR="$ROOT/openshorts"

for tool in git docker python3 ffmpeg; do
  command -v "$tool" >/dev/null || { echo "Missing: $tool (install it first)"; exit 1; }
done

if [ ! -d "$OS_DIR/.git" ]; then
  echo "Cloning OpenShorts ..."
  git clone "$OPENSHORTS_REPO" "$OS_DIR"
fi
cd "$OS_DIR"

if [ ! -f campaign_brief.py ]; then
  echo "Checking out OpenShorts $OPENSHORTS_COMMIT and applying the patch ..."
  git checkout -q -B stream-clipper-kit "$OPENSHORTS_COMMIT"
  git apply "$PATCH_DIR/openshorts.patch"
  cp "$PATCH_DIR/campaign_brief.py" .
else
  echo "OpenShorts already patched; skipping the patch."
fi

# GPU override: run as your own user, mount Claude Code read-only if it is installed.
CLAUDE_BIN="$(command -v claude || true)"
python3 - "$PATCH_DIR/docker-compose.override.yml" docker-compose.override.yml \
  "$(id -u):$(id -g)" "$CLAUDE_BIN" <<'EOF'
import sys
src, dst, user, claude = sys.argv[1:5]
text = open(src).read().replace('user: "1000:1000"', f'user: "{user}"')
if claude:
    text = text.replace("${HOME}/.local/bin/claude", claude)
else:
    lines = text.splitlines(keepends=True)
    out, skip = [], 0
    for line in lines:
        if line.strip() == "volumes:":
            skip = 3          # "volumes:", its comment and the claude mount
        if skip:
            skip -= 1
            continue
        out.append(line)
    text = "".join(out)
    print("Claude Code not found on this computer: the subscription option is off "
          "(use LLM_PROVIDER=anthropic or Gemini, or install Claude Code and run install.sh again).")
open(dst, "w").write(text)
EOF

if [ ! -f .env ]; then
  cp "$PATCH_DIR/env.example" .env
  echo "Created openshorts/.env: open it and choose how Claude is used."
fi

mkdir -p "$ROOT/footage" "$ROOT/clips" "$ROOT/secrets" "$ROOT/brand"
cat <<'MSG'

Done. Next:
  1. Edit openshorts/.env (Claude Pro/Max via Claude Code, or an API key).
  2. cd openshorts && docker compose up -d --build backend
  3. python3 kit/campaign_clip.py --help
MSG
