#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
echo "מפעיל launcher מתוקן ל־Termux (Puppeteer ללא הורדת Chromium)..."

if ! command -v node >/dev/null 2>&1; then
  echo "שגיאה: Node.js לא נמצא. התקן אותו מ־https://nodejs.org/ והפעל שוב."
  exit 1
fi

is_termux=0
if [ -n "${PREFIX:-}" ] && [[ "${PREFIX}" == */com.termux/* ]]; then
  is_termux=1
fi

if [ ! -f ".npm-install-complete" ] || [ ! -d "node_modules" ] || ! node -e 'require.resolve("dotenv"); require.resolve("whatsapp-web.js")' >/dev/null 2>&1; then
  echo "מתקין חבילות WhatsApp Web בפעם הראשונה או מעדכן תלויות..."
  # Puppeteer's bundled Chromium download is not supported on Termux. The bot
  # uses the system Chromium instead (installed with: pkg install chromium).
  PUPPETEER_SKIP_DOWNLOAD=true npm install --no-audit --no-fund --ignore-scripts
  touch .npm-install-complete
fi

if [ "$is_termux" -eq 1 ]; then
  if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
    echo "שגיאה: Chromium לא נמצא ב־Termux. התקן אותו עם: pkg update && pkg install chromium"
    exit 1
  fi
fi

python_override="${PYTHON_EXECUTABLE:-}"
if [ -z "$python_override" ]; then
  python_override="$(node -e 'require("dotenv").config({ path: ".env" }); process.stdout.write(process.env.PYTHON_EXECUTABLE || "")')"
fi
python_bin="${python_override:-$PWD/.venv/bin/python}"

if [ -z "$python_override" ]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "שגיאה: Python 3 לא נמצא. התקן Python 3 והפעל שוב."
    exit 1
  fi
  if [ ! -x "$python_bin" ]; then
    echo "יוצר סביבת Python מקומית..."
    python3 -m venv .venv
  fi
elif [[ "$python_bin" == */* ]] && [ ! -x "$python_bin" ]; then
  echo "שגיאה: PYTHON_EXECUTABLE לא מצביע לקובץ הרצה: $python_bin"
  exit 1
elif [[ "$python_bin" != */* ]] && ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "שגיאה: פקודת Python לא נמצאה: $python_bin"
  exit 1
fi

if ! "$python_bin" -c 'import httpx, bs4, dotenv' >/dev/null 2>&1; then
  echo "מתקין חבילות Python..."
  "$python_bin" -m pip install -r requirements.txt
fi

if [[ "${1:-}" == "--once" ]]; then
  exec npm start
fi

exec npm run dev
