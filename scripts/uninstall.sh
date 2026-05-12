#!/usr/bin/env bash
#
# Remove Foundation: pipx package + optional shell-alias block + optional
# config/state dirs. Use this when `foundation uninstall` itself is broken.
#
# Usage:
#   scripts/uninstall.sh             # remove alias block + pipx package
#   scripts/uninstall.sh --purge     # also wipe ~/.config/foundation, etc.
#   scripts/uninstall.sh --keep-alias  # leave shell rc untouched

set -euo pipefail

PURGE=0
KEEP_ALIAS=0
for arg in "$@"; do
  case "${arg}" in
    --purge) PURGE=1 ;;
    --keep-alias) KEEP_ALIAS=1 ;;
    -h|--help)
      sed -n '2,9p' "$0"
      exit 0 ;;
    *) printf '[uninstall] unknown flag: %s\n' "${arg}" >&2; exit 2 ;;
  esac
done

info() { printf '[uninstall] %s\n' "$1"; }

# 1. Shell alias block — find marker fence and strip in place.
if [[ "${KEEP_ALIAS}" -eq 0 ]]; then
  python3 - <<'PYEOF' || true
import os, re
from pathlib import Path

MARKER_START = "# >>> foundation cli alias >>>"
MARKER_END = "# <<< foundation cli alias <<<"
BLOCK_RE = re.compile(
    rf"(?:^|\n){re.escape(MARKER_START)}.*?{re.escape(MARKER_END)}\n?",
    re.DOTALL,
)

home = Path.home()
candidates = [home / ".zshrc", home / ".bashrc", home / ".bash_profile",
              home / ".config" / "fish" / "config.fish"]
for rc in candidates:
    if not rc.exists():
        continue
    text = rc.read_text(encoding="utf-8")
    if not BLOCK_RE.search(text):
        continue
    backup = rc.with_suffix(rc.suffix + ".bak")
    backup.write_text(text, encoding="utf-8")
    rc.write_text(BLOCK_RE.sub("", text, count=1).replace("\n\n\n", "\n\n"),
                  encoding="utf-8")
    print(f"[uninstall] removed alias block from {rc} (backup: {backup})")
PYEOF
else
  info "Keeping shell-alias block (--keep-alias)."
fi

# 2. Optional purge of state dirs.
if [[ "${PURGE}" -eq 1 ]]; then
  for dir in \
      "${HOME}/.config/foundation" \
      "${HOME}/Library/Application Support/foundation" \
      "${HOME}/.local/share/foundation" \
      "${HOME}/.local/state/foundation"; do
    if [[ -d "${dir}" ]]; then
      info "rm -rf ${dir}"
      rm -rf -- "${dir}"
    fi
  done
fi

# 3. Package removal via pipx (preferred) or pip fallback.
if command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q foundation-cli; then
  info "pipx uninstall foundation-cli"
  pipx uninstall foundation-cli
elif command -v pip >/dev/null 2>&1 && pip show foundation-cli >/dev/null 2>&1; then
  info "pip uninstall -y foundation-cli"
  pip uninstall -y foundation-cli
else
  info "foundation-cli not found in pipx or pip — nothing to uninstall."
fi

info "✓ done."
