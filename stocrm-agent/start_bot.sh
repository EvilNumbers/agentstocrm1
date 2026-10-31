#!/usr/bin/env bash
# Запуск бота в один шаг (macOS / Linux). Первый запуск сам создаст окружение.
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "Окружение не найдено. Создаю..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  echo "[ОШИБКА] Нет файла .env с ключами."
  echo "Скопируйте .env.example в .env и заполните. Подробно: docs/SETUP_FREE.md"
  exit 1
fi

echo "Проверяю настройки..."
if ! .venv/bin/python -m stocrm_agent.cli doctor; then
  echo "Исправьте проблемы выше и запустите снова."
  exit 1
fi

echo "Запускаю бота. Остановить: Ctrl+C"
exec .venv/bin/python -m stocrm_agent.bot
