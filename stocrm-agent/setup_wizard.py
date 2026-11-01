"""Мастер установки STOCRM-агента: спрашивает ключи и сам заполняет .env.

Запускается из setup.bat (Windows) или setup.sh (macOS/Linux), но можно и напрямую:
    python setup_wizard.py

Что делает:
  1. создаёт виртуальное окружение .venv;
  2. ставит зависимости из requirements.txt;
  3. задаёт по одному вопросу про каждый ключ (Enter = оставить как есть);
  4. записывает ответы в .env, сохраняя комментарии шаблона;
  5. запускает самопроверку `doctor`.

Вся работа с текстом — в Python, а не в .bat: так не бывает проблем с кодировкой
русских букв в командной строке Windows.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
VENV_PYTHON = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")

LINE = "=" * 62


# ------------------------------------------------------------------ вывод
def say(text: str = "") -> None:
    """print(), устойчивый к консоли без юникода (Windows cp866/cp1251)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        for symbol, replacement in {"—": "-", "«": '"', "»": '"', "→": "->", "…": "..."}.items():
            text = text.replace(symbol, replacement)
        text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(text, flush=True)


def ask(
    question: str,
    default: str = "",
    secret: bool = False,
    optional: bool = False,
    empty_hint: str = "",
) -> str:
    """Спрашивает значение. Пустой ввод = значение по умолчанию (или текущее).

    optional=True — пустой ввод допустим (необязательное поле, например прокси).
    """
    shown = default
    if secret and default:
        shown = default[:4] + "…" if len(default) > 6 else "задано"
    if not shown and optional:
        shown = empty_hint
    hint = f" [{shown}]" if shown else ""
    while True:
        try:
            answer = input(f"{question}{hint}: ").strip()
        except EOFError:
            if default or optional:
                return default
            say("")
            say("[ОШИБКА] Консоль закрылась до конца ввода — настройки не сохранены.")
            say("Запустите setup.bat заново (окно должно оставаться открытым).")
            raise SystemExit(1) from None
        if answer:
            return answer
        if default or optional:
            return default
        say("  ↳ это поле нужно заполнить, попробуйте ещё раз")


def confirm(question: str, default: bool = True) -> bool:
    suffix = " [Д/н]" if default else " [д/Н]"
    try:
        answer = input(f"{question}{suffix}: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer[0] in "дудy1t"


# ------------------------------------------------------------ нормализация
def clean_domain(value: str) -> str:
    value = value.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    if value.lower().endswith(".stocrm.ru"):
        value = value[: -len(".stocrm.ru")]
    return value.strip()


def clean_ids(value: str) -> str:
    return ",".join(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


# ------------------------------------------------------------------- .env
def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    """Обновляет значения в .env, не трогая комментарии и порядок строк."""
    base = ENV_PATH if ENV_PATH.exists() else EXAMPLE_PATH
    text = base.read_text(encoding="utf-8-sig") if base.exists() else ""
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(f"{key}={value}", text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


# ------------------------------------------------------------- шаги работы
def ensure_venv(base_python: str) -> None:
    """Создаёт .venv нужной версией Python; пересоздаёт, если версия неподходящая."""
    if VENV_PYTHON.exists():
        current = venv_python_version()
        if version_ok(current):
            say(f"       окружение уже есть (Python {current[0]}.{current[1]}) — пропускаю")
            return
        say(f"       в окружении Python {current[0]}.{current[1]} — не подходит. "
            "Пересоздаю .venv (файл .env не тронется)")
        shutil.rmtree(ROOT / ".venv", ignore_errors=True)
    say("       создаю окружение — 1-2 минуты, окно не закрывайте")
    code = run_with_spinner([base_python, "-m", "venv", str(ROOT / ".venv")],
                            "создаю .venv")
    if code != 0 or not VENV_PYTHON.exists():
        say("")
        say("[ОШИБКА] Не удалось создать окружение.")
        say("Проверьте, что Python установлен полностью (нужен модуль venv).")
        raise SystemExit(1)


def reexec_in_venv() -> None:
    """Перезапускает мастер уже внутри .venv, чтобы зависимости были доступны."""
    if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
        return
    say("       запускаю мастер заново уже внутри .venv (шаги 1-2 пройдут быстро)")
    result = subprocess.run(
        [str(VENV_PYTHON), str(Path(__file__).resolve())],
        cwd=ROOT,
        env={**os.environ, "STOCRM_SETUP_REEXEC": "1"},
    )
    raise SystemExit(result.returncode)


REQUIRED_IMPORTS = ("aiogram", "httpx", "mcp", "requests", "aiohttp_socks", "socksio")
PIP_LOG = ROOT / "pip-install.log"

# Рабочий диапазон: 3.10 - 3.13. Всё, что новее, ещё не имеет готовых пакетов:
# pydantic-core/aiohttp собираются через PyO3, а он поддерживает не больше 3.13.
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 13)
RECOMMENDED = "3.13 или 3.12"


def python_version(executable: str | None = None) -> tuple[int, int, int]:
    """Версия интерпретатора: текущего или указанного файла python.exe."""
    if executable is None:
        return sys.version_info[:3]          # type: ignore[return-value]
    try:
        result = subprocess.run(
            [executable, "-c", "import sys;print('%d %d %d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=20,
        )
        major, minor, micro = result.stdout.split()
        return int(major), int(minor), int(micro)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0, 0


def version_ok(version: tuple[int, int, int]) -> bool:
    return MIN_PYTHON <= version[:2] <= MAX_PYTHON


def _candidate_paths() -> list[Path]:
    """Где в системе обычно лежат разные версии Python."""
    found: list[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        for base in (local / "Programs" / "Python", Path("C:/"), program_files / "Python"):
            if base.is_dir():
                for folder in sorted(base.glob("Python3*"), reverse=True):
                    exe = folder / "python.exe"
                    if exe.is_file():
                        found.append(exe)
    else:
        for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
            path = shutil.which(name)
            if path:
                found.append(Path(path))
    return found


def find_other_pythons() -> list[tuple[tuple[int, int, int], Path]]:
    """Все установленные Python, которые подходят по версии (кроме текущего)."""
    candidates: list[Path] = []

    if os.name == "nt":
        try:
            listed = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=20)
            for line in listed.stdout.splitlines():
                match = re.search(r"([A-Za-z]:\\[^\s].*python\.exe)", line)
                if match:
                    candidates.append(Path(match.group(1).strip()))
        except (OSError, subprocess.SubprocessError):
            pass

    candidates.extend(_candidate_paths())

    result: list[tuple[tuple[int, int, int], Path]] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        version = python_version(str(path))
        if version_ok(version):
            result.append((version, path))
    result.sort(reverse=True)                 # сначала самые новые из подходящих
    return result


def explain_version_problem(version: tuple[int, int, int]) -> None:
    say("")
    if version[:2] > MAX_PYTHON:
        say(f"[!] Установлен Python {version[0]}.{version[1]}, а для него ещё нет готовых "
            f"пакетов (pydantic-core, aiohttp).")
        say(f"    Сборка из исходников упирается в ограничение PyO3: «newer than PyO3's "
            f"maximum supported version ({MAX_PYTHON[0]}.{MAX_PYTHON[1]})».")
        say(f"    Нужен Python {RECOMMENDED} — он работает со всеми зависимостями.")
    else:
        say(f"[!] Установлен Python {version[0]}.{version[1]}, а нужен {MIN_PYTHON[0]}."
            f"{MIN_PYTHON[1]} или новее (лучше {RECOMMENDED}).")
    say("")
    say("  ЧТО СДЕЛАТЬ (2 минуты):")
    say("    1. Откройте https://www.python.org/downloads/release/")
    say(f"       и поставьте Python {RECOMMENDED} (Windows installer 64-bit).")
    say("       Галочка «Add python.exe to PATH» — обязательно.")
    say("    2. Запустите setup.bat снова — мастер сам найдёт новый Python.")
    say("")
    say("  Старую версию удалять не нужно: они живут рядом.")


def ensure_base_python() -> str | None:
    """Гарантирует подходящую базовую версию Python. Возвращает путь к ней или None."""
    version = python_version()
    if version_ok(version):
        return sys.executable

    explain_version_problem(version)

    others = find_other_pythons()
    if not others:
        say("  Подходящих версий на этом компьютере не нашлось.")
        return None

    say("  Нашлись подходящие версии:")
    for index, (candidate, path) in enumerate(others, 1):
        say(f"    {index}) Python {candidate[0]}.{candidate[1]}.{candidate[2]} — {path}")
    say("")
    best_version, best_path = others[0]
    say(f"  Буду использовать Python {best_version[0]}.{best_version[1]} "
        f"({best_path}).")
    return str(best_path)


def venv_python_version() -> tuple[int, int, int]:
    return python_version(str(VENV_PYTHON)) if VENV_PYTHON.exists() else (0, 0, 0)


def check_python() -> bool:
    """Проверка версии перед установкой пакетов (после создания окружения)."""
    version = python_version(str(VENV_PYTHON)) if VENV_PYTHON.exists() else python_version()
    say(f"      Python {version[0]}.{version[1]}.{version[2]}")
    if version_ok(version):
        return True
    explain_version_problem(version)
    return False


def deps_probe() -> tuple[bool, str]:
    """Проверяет импорты, версии требований и зависимости без сетевых запросов."""
    script = """
import importlib.metadata as metadata
from pathlib import Path
from pip._vendor.packaging.requirements import Requirement
for line in Path('requirements.txt').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    req = Requirement(line)
    if req.marker and not req.marker.evaluate():
        continue
    actual = metadata.version(req.name)
    if req.specifier and not req.specifier.contains(actual, prereleases=True):
        raise RuntimeError(f'{req.name}: установлено {actual}, требуется {req.specifier}')
"""
    probe = subprocess.run(
        [str(VENV_PYTHON), "-c", f"import {', '.join(REQUIRED_IMPORTS)}\n" + script],
        cwd=ROOT, capture_output=True, text=True,
    )
    message = (probe.stderr or probe.stdout or "").strip().splitlines()
    if probe.returncode != 0:
        return False, (message[-1] if message else "Проверка зависимостей не выполнена")
    check = subprocess.run([str(VENV_PYTHON), "-m", "pip", "check"], cwd=ROOT, capture_output=True, text=True)
    details = (check.stdout or check.stderr or "").strip().splitlines()
    return check.returncode == 0, (details[-1] if details and check.returncode else "")


def deps_ok() -> bool:
    return deps_probe()[0]


def pip_available() -> bool:
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "--version"], cwd=ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


SPINNER = "|/-\\"
QUIET_WARN = 15          # секунд без вывода — сообщаем, что не зависли
MAX_IDLE = 120           # секунд полной тишины — считаем, что соединение залипло
MAX_TOTAL = 1800         # секунд на одну попытку установки


def _safe(text: str) -> str:
    """Строка, которую гарантированно можно напечатать в любой консоли."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        for symbol, replacement in {"—": "-", "«": '"', "»": '"', "→": "->", "…": "...",
                                    "·": ".", "│": "|"}.items():
            text = text.replace(symbol, replacement)
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


LINE_WIDTH = 78          # шире не надо: консоль Windows по умолчанию 80 символов

# --------------------------------------------------------------- прогресс
# Полоса прогресса видна всегда: и между шагами, и внутри долгих операций.
TOTAL_STEPS = 5
STEP_NAMES = (
    "проверка версии Python",
    "виртуальное окружение .venv",
    "установка зависимостей",
    "ключи и файл .env",
    "проверка настроек",
)
_STEP: dict[str, Any] = {"index": 0, "name": "", "started": time.monotonic()}


def _unicode_ok() -> bool:
    """Красивые символы полосы печатаем только если консоль их понимает."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "█░".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _bar(index: int, total: int = TOTAL_STEPS, width: int = 14) -> str:
    """ASCII-полоса: [######--------] — читается в любой консоли Windows."""
    full, empty = ("█", "░") if _unicode_ok() else ("#", "-")
    filled = max(0, min(width, round(width * index / max(1, total))))
    return "[" + full * filled + empty * (width - filled) + "]"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}с"
    return f"{seconds // 60}м {seconds % 60:02d}с"


def start_step(name: str, index: int | None = None) -> None:
    """Печатает начало шага с полосой: [####------] 2/5 название."""
    _STEP["index"] = index if index is not None else _STEP["index"] + 1
    _STEP["name"] = name
    _STEP["started"] = time.monotonic()
    say("")
    say(f"{_bar(_STEP['index'])} шаг {_STEP['index']} из {TOTAL_STEPS}: {name}")


def finish_step(note: str = "") -> None:
    """Закрывает шаг: сколько заняло и что получилось."""
    elapsed = time.monotonic() - _STEP["started"]
    tail = f" — {note}" if note else ""
    say(f"       готово за {_duration(elapsed)}{tail}")


def step_prefix() -> str:
    """Короткий префикс для строки прогресса: '2/5 '."""
    index = _STEP["index"]
    return f"{index}/{TOTAL_STEPS} " if index else ""


def _status_line(text: str, ends_with: str = "") -> None:
    """Строка прогресса: перерисовывается на месте через \r."""
    text = _safe(step_prefix() + text)
    ends_with = _safe(ends_with)
    room = LINE_WIDTH - len(ends_with) - 1
    body = text[: max(10, room)].ljust(max(10, room))
    sys.stdout.write("\r" + body + (" " + ends_with if ends_with else ""))
    sys.stdout.flush()


def _clear_line() -> None:
    sys.stdout.write("\r" + " " * LINE_WIDTH + "\r")
    sys.stdout.flush()


def run_with_spinner(command: list[str], label: str, timeout: int = 900) -> int:
    """Запускает команду, показывая, что она работает (иначе выглядит как зависание)."""
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True, env=env)
    except OSError as exc:
        say(f"      не удалось запустить: {exc}")
        return 1
    started = time.monotonic()
    while proc.poll() is None:
        elapsed = time.monotonic() - started
        spinner = SPINNER[int(elapsed * 4) % len(SPINNER)]
        _status_line(f"  {spinner} [{elapsed:4.0f}с] {label}")
        if elapsed > timeout:
            proc.kill()
            _clear_line()
            say(f"      не уложилось в {timeout // 60} минут — прерываю")
            return 1
        time.sleep(0.25)
    _clear_line()
    return proc.returncode or 0


def _phase_of(line: str) -> str | None:
    """Превращает строку pip в понятное описание действия."""
    low = line.lower()
    if line.startswith("Collecting"):
        return None                      # покажем ниже, вместе с названием пакета
    if low.startswith("downloading"):
        return "скачиваю " + line.split()[1] if len(line.split()) > 1 else "скачиваю"
    if low.startswith("using cached"):
        # "Using cached name.whl (62 kB)" — берём имя файла, а не размер в скобках
        filename = next(
            (part.rsplit("/", 1)[-1] for part in line.split() if part.endswith((".whl", ".tar.gz", ".zip"))),
            "",
        )
        filename = re.sub(r"\.(whl|tar\.gz|zip)$", "", filename)
        if not filename:
            return "из кэша"
        return "из кэша " + re.split(r"-(?=\d)", filename, maxsplit=1)[0]
    if low.startswith("building wheel") or "building wheel for" in low:
        return "собираю из исходников (это долго)"
    if low.startswith("installing collected"):
        return "устанавливаю пакеты"
    if low.startswith(("error", "failed")):
        return "ошибка"
    return None


def run_pip(args: list[str]) -> tuple[int, str]:
    """Запускает pip с живой строкой прогресса. Возвращает (код, хвост вывода)."""
    command = [str(VENV_PYTHON), "-m", "pip", *args]
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PIP_PROGRESS_BAR": "off"}

    try:
        proc = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return 1, f"не удалось запустить pip: {exc}"

    from queue import Empty, Queue
    from threading import Thread

    lines: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            queue.put(raw.rstrip())
        queue.put(None)          # сигнал «поток закончился»

    queue: Queue[str | None] = Queue()
    Thread(target=reader, daemon=True).start()

    started = time.monotonic()
    last_output = started
    phase = "подключаюсь к PyPI"
    package = ""
    collected = downloaded = 0
    killed = ""

    while True:
        try:
            line = queue.get(timeout=0.5)
        except Empty:
            line = ""

        now = time.monotonic()
        elapsed = now - started
        idle = now - last_output

        if line is None:
            break
        if line:
            last_output = now
            lines.append(line)
            stripped = line.strip()
            if stripped.startswith("Collecting"):
                collected += 1
                parts = stripped.split()
                package = parts[1] if len(parts) > 1 else ""
                phase = f"получаю {package}"
            elif stripped.startswith("Downloading"):
                downloaded += 1
                phase = _phase_of(stripped) or phase
            else:
                phase = _phase_of(stripped) or phase
            # важные строки (ошибки, итог) печатаем отдельными строками
            if stripped.startswith(("ERROR", "error:", "Successfully", "WARNING")):
                _clear_line()
                say("      " + stripped[:120])
                continue

        spinner = SPINNER[int(elapsed * 4) % len(SPINNER)]
        ends = f"пк:{downloaded}"
        if idle >= QUIET_WARN:
            ends = f"тишина {idle:.0f}с | {ends}"
        _status_line(f"  {spinner} [{elapsed:4.0f}с] {phase}", ends_with=ends)

        if idle >= MAX_IDLE:
            killed = (f"pip молчал {MAX_IDLE} с — соединение залипло. "
                      "Попробуйте без VPN или с зеркалом:")
            break
        if elapsed >= MAX_TOTAL:
            killed = f"установка не уложилась в {MAX_TOTAL // 60} минут."
            break

    if killed:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        lines.append(killed)

    code = proc.wait()
    _clear_line()                                  # стираем строку прогресса

    output = "\n".join(lines)
    with PIP_LOG.open("a", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(command) + "\n" + output + "\n\n")

    tail = [line for line in output.splitlines() if line.strip()]
    seconds = time.monotonic() - started
    if collected or downloaded:
        tail.append(f"(pip: пакетов {collected}, скачиваний {downloaded}, "
                    f"времени {seconds:.0f} с)")
    return (1 if killed else code), "\n".join(tail[-12:])


def diagnose(tail: str) -> None:
    """Расшифровывает типичные сбои pip человеческим языком."""
    say("")
    say("-" * 62)
    say("  Что именно ответил pip (последние строки):")
    for line in tail.splitlines()[-12:]:
        say("   " + line)
    say("-" * 62)
    say("")
    low = tail.lower()
    if "no module named pip" in low or "не найден модуль pip" in low:
        say("  ПРИЧИНА: в окружении нет pip (частая история на Linux без пакета python3-venv).")
        say("  ЧТО ДЕЛАТЬ:")
        say("    sudo apt install python3-venv python3-pip    (Debian/Ubuntu)")
        say("    sudo dnf install python3-pip                 (Fedora)")
        say("  затем запустите setup еще раз.")
    elif "ssl" in low or "certificate" in low or "сертификат" in low:
        say("  ПРИЧИНА: подмена сертификата — антивирус, файрвол или корпоративный прокси.")
        say("  ЧТО ДЕЛАТЬ: проверьте дату, сертификаты системы и настройки корпоративного прокси.")
        say("  При необходимости попросите администратора настроить доверенный корневой сертификат.")
        say("  Мастер не отключает проверку TLS для установки пакетов.")
    elif "proxy" in low or "407" in low or "timed out" in low or "timeout" in low:
        say("  ПРИЧИНА: не проходит соединение — прокси, VPN или блокировка PyPI.")
        say("  ЧТО ДЕЛАТЬ: если в компании прокси, укажите его:")
        say("    set HTTPS_PROXY=http://user:pass@proxy:8080")
        say("  или включите/выключите VPN и повторите. Можно также использовать зеркало:")
        say("    set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple")
    elif "pyo3" in low or "failed-wheel-build-for-install" in low:
        say("  ПРИЧИНА: Python слишком новый — для него ещё нет готовых пакетов,")
        say("  а собрать из исходников не получается (PyO3 поддерживает не больше 3.13).")
        say(f"  ЧТО ДЕЛАТЬ: поставьте Python {RECOMMENDED} с python.org,")
        say("  удалите папку .venv и запустите setup.bat снова.")
    elif ("microsoft visual c++" in low or "failed building wheel" in low
          or "error: command" in low or "meson" in low or "build backend" in low):
        say("  ПРИЧИНА: пакет нужно собирать из исходников, а компилятора в системе нет.")
        say("  ЧАЩЕ ВСЕГО: 32-битный Python или самая новая версия, под которую нет готовых колес.")
        say("  ЧТО ДЕЛАТЬ (по порядку):")
        say("    1) поставьте Python 3.12 или 3.13 x64 с python.org, удалите папку .venv")
        say("       и запустите setup заново;")
        say("    2) если не помогло — Visual Studio Build Tools: https://aka.ms/vs/17/release/"
            "vs_BuildTools.exe (компонент «Сборка C++»).")
    elif "requires a different python" in low or "requires-python" in low:
        say("  ПРИЧИНА: пакету нужен более новый Python.")
        say("  ЧТО ДЕЛАТЬ: поставьте Python 3.12/3.13 с python.org, удалите папку .venv, запустите заново.")
    elif "no space left" in low or "not enough space" in low or "нет места" in low:
        say("  ПРИЧИНА: на диске нет места.")
        say("  ЧТО ДЕЛАТЬ: освободите 1-2 ГБ и повторите.")
    elif "connection reset" in low or "connection aborted" in low or "read timed out" in low:
        say("  ПРИЧИНА: соединение обрывается (нестабильная сеть, VPN, антивирус).")
        say("  ЧТО ДЕЛАТЬ: повторите запуск (мастер качает заново только недостающее),")
        say("  попробуйте без VPN или укажите зеркало:")
        say("    set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple")
    elif "no matching distribution" in low or "не удается найти" in low or "could not find" in low:
        say("  ПРИЧИНА: для установленной версии Python нет подходящего пакета.")
        say("  ЧАЩЕ ВСЕГО: Python слишком старый или это 32-битная сборка.")
        say("  ЧТО ДЕЛАТЬ: поставьте Python 3.12/3.13 x64 с python.org и запустите снова.")
    elif "permission" in low or "access is denied" in low or "отказано в доступе" in low:
        say("  ПРИЧИНА: нет прав на запись в папку проекта (или её держит антивирус).")
        say("  ЧТО ДЕЛАТЬ: перенесите папку, например, в C:\\stocrm-agent и повторите.")
    elif "winerror 10013" in low or "errno 13" in low or "errno 10061" in low:
        say("  ПРИЧИНА: соединение блокирует файрвол/антивирус.")
        say("  ЧТО ДЕЛАТЬ: разрешите python.exe доступ в сеть и повторите.")
    else:
        say("  Причина не распознана автоматически — пришлите строки выше (они же")
        say(f"  сохранены в файл {PIP_LOG.name}).")
    say("")


def install_deps() -> None:
    say("       ставлю пакеты из requirements.txt — строка ниже показывает, что происходит")
    if not check_python():
        raise SystemExit(1)
    if deps_ok():
        say("      всё уже установлено.")
        return

    if not pip_available():
        say("      pip в окружении не найден — пробую восстановить...")
        run_with_spinner([str(VENV_PYTHON), "-m", "ensurepip", "--upgrade"], "восстанавливаю pip")
        if not pip_available():
            diagnose("No module named pip")
            raise SystemExit(1)

    run_with_spinner([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip",
                      "--timeout", "30"], "обновляю pip")

    requirements = str(ROOT / "requirements.txt")
    attempts: list[tuple[str, list[str]]] = [
        ("обычная установка", ["install", "-r", requirements, "--timeout", "60", "--retries", "5"]),
        ("без кэша (лечит битые загрузки)",
         ["install", "-r", requirements, "--no-cache-dir", "--timeout", "60", "--retries", "5"]),
        ("через индекс PyPI напрямую",
         ["install", "-r", requirements, "--index-url", "https://pypi.org/simple",
          "--timeout", "60", "--retries", "5"]),
    ]

    # ошибки, которые повтором не лечатся: сразу показываем причину
    fatal = ("invalid requirement", "no matching distribution",
             "no module named pip", "не найден модуль",
             "failed-wheel-build-for-install", "pyo3", "newer than pyo3")

    tail = ""
    for index, (title, args) in enumerate(attempts, 1):
        say(f"      попытка {index} из {len(attempts)}: {title}...")
        code, tail = run_pip(args)
        ok, probe_error = deps_probe()
        if ok:
            say("      готово.")
            return
        if code == 0:
            say(f"      pip отработал, но модули не импортируются: {probe_error}")
            say("      пробую дальше...")
        else:
            say("      не вышло.")
            if any(marker in tail.lower() for marker in fatal):
                say("      повторять нет смысла — ошибка не сетевая.")
                break

    say("")
    say("[ОШИБКА] Не удалось установить или проверить зависимости.")
    diagnose(tail)
    say("Можно поставить вручную и посмотреть полный вывод:")
    if os.name == "nt":
        say("    .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    else:
        say("    .venv/bin/python -m pip install -r requirements.txt")
    say(f"Полный лог: {PIP_LOG}")
    raise SystemExit(1)


# Необязательный шаг: прокси для Telegram (нужен, если api.telegram.org недоступен)
def ask_proxy(current: dict[str, str]) -> str:
    """Необязательный вопрос: пустой Enter = прокси не нужен."""
    say("")
    say("— Прокси для Telegram (необязательно)")
    say("  Если api.telegram.org открывается в браузере или включён VPN — просто нажмите Enter.")
    say("  Формат: socks5://логин:пароль@хост:1080  или  http://хост:8080")
    if current.get("TG_PROXY_URL"):
        say("  Чтобы убрать уже сохранённый прокси, введите минус: -")
    value = ask(
        "  прокси",
        default=current.get("TG_PROXY_URL", ""),
        optional=True,
        empty_hint="Enter — без прокси",
    ).strip()
    if value in {"-", "нет", "no", "off", "-."}:
        return ""
    return value


FIELDS: list[tuple[str, str, str, Callable[[str], str] | None]] = [
    ("STOCRM_DOMAIN", "Домен панели STOCRM (префикс из адреса, например mycompany)", "мойсервис", clean_domain),
    ("STOCRM_SID", "Ключ STOCRM (Настройки → API Ключи)", "123_abc456", None),
    ("TG_BOT_TOKEN", "Токен бота от @BotFather (вида 7123456789:AAH...)", "", None),
    ("TG_ALLOWED_USERS", "Ваш Telegram ID от @userinfobot (кому можно пользоваться ботом)", "123456789", clean_ids),
    ("TG_ADMIN_USERS", "ID администратора для /settings (обычно тот же)", "123456789", clean_ids),
    ("LLM_API_KEY", "Ключ Gemini с aistudio.google.com/apikey (начинается на AIza)", "", None),
    ("STT_API_KEY", "Ключ Groq с console.groq.com (начинается на gsk)", "", None),
]


def collect(current: dict[str, str]) -> dict[str, str]:
    say("")
    say("  Отвечайте на вопросы и жмите Enter. Если значение уже есть в квадратных")
    say("  скобках — можно просто нажать Enter, чтобы оставить его.")
    say("  Подробно, где что взять: docs/INSTALL.md")
    say("")

    answers: dict[str, str] = {}
    total_questions = len(FIELDS)
    for number, (key, question, example, clean) in enumerate(FIELDS, start=1):
        default = current.get(key, "")
        if not default and key in ("TG_ALLOWED_USERS", "TG_ADMIN_USERS"):
            # админ по умолчанию — тот же ID, что уже указан в белом списке
            default = (
                current.get("TG_ALLOWED_USERS", "")
                or current.get("TG_ADMIN_USERS", "")
                or answers.get("TG_ALLOWED_USERS", "")
            )
        say(f"— [{number}/{total_questions}] {question}")
        if example:
            say(f"  пример: {example}")
        secret = key in ("STOCRM_SID", "TG_BOT_TOKEN", "LLM_API_KEY", "STT_API_KEY")
        value = ask("  значение", default=default, secret=secret)
        if clean:
            value = clean(value)
        answers[key] = value
        say("")

    # админ по умолчанию — тот же, что в белом списке
    if not answers.get("TG_ADMIN_USERS"):
        answers["TG_ADMIN_USERS"] = answers.get("TG_ALLOWED_USERS", "")
    if not answers.get("TG_ALLOWED_USERS"):        # без белого списка бот открыт всем
        answers["TG_ALLOWED_USERS"] = answers.get("TG_ADMIN_USERS", "")

    # живая проверка ключа Groq: без него голосовые не расшифровываются
    for attempt in range(2):
        key = answers.get("STT_API_KEY", "").strip()
        if not key:
            break
        ok, message = verify_stt_key(key)
        if ok:
            say(f"  ✅ {message}")
            break
        say(f"  ⚠️ {message}")
        if attempt == 1 or "нет связи" in message.lower():
            say("  Продолжаю установку — ключ можно исправить в .env позже.")
            break
        say("  Вставьте ключ ещё раз (Enter — оставить как есть).")
        answers["STT_API_KEY"] = ask("  ключ Groq", default=key, secret=True).strip()

    proxy = ask_proxy(current)
    if proxy:
        answers["TG_PROXY_URL"] = proxy
        answers["HTTPS_PROXY"] = proxy        # тот же прокси для Gemini/Groq/STOCRM
    elif current.get("TG_PROXY_URL"):
        # пользователь ввёл «-» — чистим сохранённый прокси, иначе он останется в .env
        answers["TG_PROXY_URL"] = ""
        answers["HTTPS_PROXY"] = ""

    answers["LLM_PROVIDER"] = "google"
    answers["STT_PROVIDER"] = "groq"
    answers["STT_LANGUAGE"] = current.get("STT_LANGUAGE") or "ru"
    answers["TG_CONFIRM_REQUIRED"] = "1"
    return answers


def verify_stt_key(key: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Проверяет ключ Groq живым запросом (короткий файл-тишина).

    Возвращает (успех, сообщение). Сеть/таймаут не считаем провалом ключа.
    """
    import io
    import wave

    try:
        import httpx
    except ImportError:
        return True, "проверку ключа пропустил (нет httpx)"

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 8000)

    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data={"model": "whisper-large-v3", "response_format": "json", "language": "ru"},
            files={"file": ("check.wav", buf.getvalue(), "audio/wav")},
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — сеть бывает любой
        return True, f"ключ проверить не смог (нет связи с Groq: {exc})"

    if resp.status_code in (401, 403):
        return False, "ключ Groq не принят — скопируйте заново на console.groq.com/keys"
    if resp.status_code == 400:
        return True, "ключ Groq принят (сервис ответил на тестовый файл)"
    if resp.status_code == 429:
        return True, "ключ Groq принят (сейчас действует лимит бесплатного тарифа)"
    if resp.status_code >= 500:
        return True, f"ключ проверить не смог (Groq ответил {resp.status_code})"
    if resp.status_code != 200:
        return False, f"Groq ответил HTTP {resp.status_code}: {resp.text[:150]}"
    return True, "ключ Groq принят — голосовые будут расшифровываться"


def validate(values: dict[str, str]) -> list[str]:
    problems = []
    if not values.get("STOCRM_DOMAIN"):
        problems.append("не заполнен STOCRM_DOMAIN (домен панели)")
    if not values.get("STOCRM_SID"):
        problems.append("не заполнен STOCRM_SID (ключ API)")
    token = values.get("TG_BOT_TOKEN", "")
    if ":" not in token:
        problems.append("TG_BOT_TOKEN не похож на токен (нужно вида 7123456789:AAH...)")
    users = values.get("TG_ALLOWED_USERS", "")
    if users and not all(part.strip().lstrip("-").isdigit() for part in users.split(",")):
        problems.append("TG_ALLOWED_USERS должен состоять из цифр (ID), можно через запятую")
    if not values.get("LLM_API_KEY"):
        problems.append("не заполнен LLM_API_KEY — без него агент не поймёт просьбу")
    if not values.get("STT_API_KEY"):
        problems.append("не заполнен STT_API_KEY — без него не будут работать голосовые")
    return problems


def run_doctor() -> int:
    say("")
    say("Проверяю настройки...")
    say("")
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "stocrm_agent.cli", "doctor"], cwd=ROOT,
    )
    return result.returncode


def main() -> int:
    try:
        return _main()
    except KeyboardInterrupt:
        say("")
        say("")
        say("Установка прервана (Ctrl+C). Файл .env не изменён —")
        say("запустите setup.bat снова, когда будет удобно.")
        return 130


def _main() -> int:
    say(LINE)
    say("  УСТАНОВКА STOCRM-АГЕНТА")
    say(LINE)
    say("")
    say(f"Папка проекта: {ROOT}")
    if os.getenv("STOCRM_SETUP_REEXEC"):
        say("(продолжаю установку уже внутри .venv)")
    say("")

    if not (ROOT / "requirements.txt").exists():
        say("[ОШИБКА] В папке нет requirements.txt — похоже, архив не распакован.")
        say("Распакуйте ZIP в папку (например C:\\stocrm-agent) и запустите setup.bat оттуда.")
        return 1

    start_step(STEP_NAMES[0], index=1)
    base_python = ensure_base_python()
    if base_python is None:
        say("")
        say("[ОШИБКА] Нет подходящей версии Python. Установите "
            f"{RECOMMENDED} и запустите setup.bat снова.")
        raise SystemExit(1)

    found = python_version(base_python)
    finish_step(f"подходит Python {found[0]}.{found[1]}")
    start_step(STEP_NAMES[1], index=2)
    ensure_venv(base_python)
    finish_step()
    reexec_in_venv()
    start_step(STEP_NAMES[2], index=3)
    install_deps()
    finish_step()
    start_step(STEP_NAMES[3], index=4)

    current = read_env(ENV_PATH)
    if current:
        say("")
        say("Файл .env уже существует — текущие значения показаны в [скобках].")

    answers = collect(current)
    problems = validate(answers)
    if problems:
        say("")
        say("[!] Проверьте перед сохранением:")
        for problem in problems:
            say(f"    - {problem}")
        if not confirm("Всё равно сохранить и продолжить?"):
            say("Ничего не изменено. Запустите setup.bat ещё раз, когда будут ключи.")
            return 1

    write_env(answers)
    say("")
    say(f"[OK] Ключи записаны в {ENV_PATH}")

    finish_step()
    start_step(STEP_NAMES[4], index=5)
    code = run_doctor()
    finish_step("всё в порядке" if code == 0 else "есть замечания")
    say("")
    say(LINE)
    if code == 0:
        say("  ГОТОВО. Теперь запустите start_bot.bat (Windows) или ./start_bot.sh")
    else:
        say("  Есть проблемы — исправьте то, что написано после стрелки ->,")
        say("  и запустите setup.bat снова. Ключи уже сохранены.")
    say(LINE)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("")
        say("Отменено. Ничего не изменено.")
        raise SystemExit(1) from None
