#!/usr/bin/env bash
# Установка в один шаг (macOS / Linux). Запуск:  ./setup.sh
# Вся логика — в setup_wizard.py (общая с Windows-версией).
set -u
cd "$(dirname "$0")"

if [ ! -f "setup_wizard.py" ]; then
  echo "[ОШИБКА] Нет setup_wizard.py — распакуйте архив в папку и запустите снова."
  read -r -p "Нажмите Enter, чтобы закрыть..." _
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ОШИБКА] Python не найден. Установите с https://www.python.org/downloads/"
  read -r -p "Нажмите Enter, чтобы закрыть..." _
  exit 1
fi

"$PY" setup_wizard.py
RC=$?

echo
read -r -p "Готово. Нажмите Enter, чтобы закрыть окно..." _
exit $RC
