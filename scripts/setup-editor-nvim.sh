#!/usr/bin/env bash
# Set up the optional [E] edit-mode enhancement: a flash-only neovim profile
# that lives at ~/.config/synclisten-nvim/, isolated from any everyday nvim
# configuration via NVIM_APPNAME.
#
# Idempotent: re-running is safe. If the destination already matches the repo
# files, nothing is overwritten. If it differs, the existing directory is
# backed up to ~/.config/synclisten-nvim.backup-<timestamp>/ first.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO_DIR/etc/nvim"
DST="${HOME}/.config/synclisten-nvim"
RC_LINE="export EDITOR='env NVIM_APPNAME=synclisten-nvim nvim'"

# ── 1. Preflight ────────────────────────────────────────────
if ! command -v nvim >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[setup] neovim is not installed. Install it first, then re-run this script.

  # Ubuntu / Debian / Pop!_OS (modern):
  curl -LO https://github.com/neovim/neovim/releases/latest/download/nvim-linux-x86_64.appimage
  chmod +x nvim-linux-x86_64.appimage
  sudo mv nvim-linux-x86_64.appimage /usr/local/bin/nvim
EOF
  exit 1
fi

NVIM_VER="$(nvim --version | head -n1 | awk '{print $2}' | sed 's/^v//; s/-.*//')"
echo "[setup] found nvim ${NVIM_VER}"

if [[ ! -d "$SRC" ]]; then
  echo "[setup] missing source dir: $SRC" >&2
  exit 1
fi

# ── 2. Sync files (with backup-on-divergence) ──────────────
mkdir -p "$DST/lua"

backup_taken=0
take_backup() {
  if [[ "$backup_taken" -eq 0 ]]; then
    local bkp="${DST}.backup-$(date +%Y%m%d-%H%M%S)"
    echo "[setup] existing config differs from repo; backing up to $bkp"
    cp -a "$DST" "$bkp"
    backup_taken=1
  fi
}

for rel in init.lua lua/pyinitial.lua lua/pyinitial_data.lua; do
  src_file="$SRC/$rel"
  dst_file="$DST/$rel"
  if [[ -f "$dst_file" ]] && cmp -s "$src_file" "$dst_file"; then
    continue
  fi
  if [[ -f "$dst_file" ]]; then
    take_backup
  fi
  install -m 0644 -D "$src_file" "$dst_file"
  echo "[setup] installed $rel"
done

# ── 3. Headless plugin pre-fetch ───────────────────────────
echo "[setup] pre-installing flash.nvim (headless)..."
env NVIM_APPNAME=synclisten-nvim nvim --headless "+Lazy! sync" +qa >/dev/null 2>&1 || {
  echo "[setup] headless sync exited non-zero; first interactive launch will retry." >&2
}

# Verify flash loads
if env NVIM_APPNAME=synclisten-nvim nvim --headless \
     -c "lua local ok, _ = pcall(require, 'flash'); os.exit(ok and 0 or 1)" \
     +qa >/dev/null 2>&1; then
  echo "[setup] flash.nvim ready"
else
  echo "[setup] warning: flash.nvim did not load cleanly. Try manual: env NVIM_APPNAME=synclisten-nvim nvim '+Lazy sync'" >&2
fi

# ── 4. Tell the user how to wire EDITOR ────────────────────
cat <<EOF

[setup] ✅ Done.

Add this line to your shell rc (~/.bashrc, ~/.zshrc, or ~/.config/fish/config.fish):

    ${RC_LINE}

Then reload it (e.g. \`source ~/.bashrc\`) and run SyncListen.
Inside the editor: press \`s\` then pinyin initials (e.g. \`zw\` for 中文/找位)
and Shift+Letter to jump to a labeled match.
EOF
