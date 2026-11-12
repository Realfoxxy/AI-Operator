#!/usr/bin/env bash
# Orkestra kurulum yardımcısı (Linux / macOS)
#
# Ne yapar:
#   1. Python 3.11+ ve Node.js/npm varlığını denetler.
#   2. Eksik resmi CLI araçlarını (Claude Code, Gemini CLI, Copilot CLI) npm ile kurmayı ÖNERİR;
#      onay vermezsen hiçbir şey kurmaz.
#   3. Varsayılan çalışma klasörünü (../orkestra-calisma) oluşturur.
#   4. Testleri ve "doctor" denetimini çalıştırır.
# İnternetten betik indirip çalıştırmaz; Orkestra'nın kendisi hiçbir Python paketi gerektirmez.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  say "✖ Python 3.11 veya üstü bulunamadı. Kur: https://www.python.org/downloads/"
  exit 1
fi
say "✔ Python: $("$PY" --version 2>&1)"

HAVE_NPM=0
if command -v npm >/dev/null 2>&1; then
  HAVE_NPM=1
  say "✔ Node.js: $(node --version 2>/dev/null || echo '?') · npm: $(npm --version)"
else
  say "⚠ Node.js/npm bulunamadı. CLI araçları için Node.js LTS kur: https://nodejs.org/"
fi

missing=()
for pair in "claude:@anthropic-ai/claude-code" "gemini:@google/gemini-cli" "copilot:@github/copilot"; do
  cmd="${pair%%:*}"; pkg="${pair#*:}"
  if command -v "$cmd" >/dev/null 2>&1; then say "✔ $cmd kurulu"; else say "• $cmd kurulu değil ($pkg)"; missing+=("$pkg"); fi
done

if [ "${#missing[@]}" -gt 0 ] && [ "$HAVE_NPM" -eq 1 ] && [ -t 0 ]; then
  read -r -p "Eksik CLI araçları kurulsun mu? (npm install -g ${missing[*]}) [e/H] " ans
  case "$ans" in
    e|E|evet|Evet|y|Y|yes) npm install -g "${missing[@]}" ;;
    *) say "Atlandı. Daha sonra: npm install -g ${missing[*]}" ;;
  esac
fi

mkdir -p ../orkestra-calisma
say "✔ Çalışma klasörü: $(cd ../orkestra-calisma && pwd)"

say ""
say "Testler çalıştırılıyor…"
if "$PY" -m unittest -q; then say "✔ Testler geçti"; else say "✖ Bazı testler başarısız oldu"; fi

say ""
"$PY" -m orkestra doctor || true

cat <<EOF

Sonraki adımlar:
  1. Her CLI'ı bir kez açıp giriş yap:  claude   ·   gemini   ·   copilot  (içeride /login)
  2. Copilot'un çalışma klasörüne güvenmesi için:  cd ../orkestra-calisma && copilot
  3. Abonelik harcamadan dene:          $PY -m orkestra ui --mock
  4. Gerçek çalıştırma:                 $PY -m orkestra ui
EOF
