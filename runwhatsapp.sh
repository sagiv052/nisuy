#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v node >/dev/null 2>&1; then
  echo "שגיאה: Node.js לא נמצא. התקן אותו מ־https://nodejs.org/ והפעל שוב."
  exit 1
fi

if [ ! -d "node_modules" ] || ! node -e 'require.resolve("dotenv"); require.resolve("whatsapp-web.js")' >/dev/null 2>&1; then
  echo "מתקין חבילות WhatsApp Web בפעם הראשונה או מעדכן תלויות..."
  npm install --no-audit --no-fund
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

if ! "$python_bin" -c 'import httpx, bs4, dotenv, playwright' >/dev/null 2>&1; then
  echo "מתקין חבילות Python..."
  "$python_bin" -m pip install -r requirements.txt
fi

if [[ "${1:-}" == "--once" ]]; then
  exec npm start
fi

exec npm run dev
