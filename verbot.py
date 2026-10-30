#!/usr/bin/env python3
"""Verbot: a small, local-first assistant with safe desktop app launching and window closing."""

from __future__ import annotations

import getpass
import configparser
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen2.5:1.5b"
MAX_RESPONSE_TOKENS = 1024
MAX_HISTORY_MESSAGES = 6
RESET = "\033[0m"
CYAN = "\033[96m"
PURPLE = "\033[95m"
MUTED = "\033[2m"
GREEN = "\033[92m"


def colorize(text: str, color: str) -> str:
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        return f"{color}{text}{RESET}"
    return text


def terminal_width() -> int:
    columns = shutil.get_terminal_size(fallback=(80, 24)).columns
    return max(18, min(84, columns - 2))


def print_card(title: str, lines: list[str], accent: str = CYAN) -> None:
    """Render a compact, terminal-width-aware card without extra packages."""
    width = terminal_width()
    title = title[: max(4, width - 6)]
    content_width = width - 4
    top_label = f"╭─ {title} "
    top = top_label + "─" * max(1, width - len(top_label) - 1) + "╮"
    print(colorize(top, accent))
    for line in lines:
        paragraphs = line.splitlines() or [""]
        for paragraph in paragraphs:
            wrapped = textwrap.wrap(paragraph, width=content_width, break_long_words=True,
                                    break_on_hyphens=False) or [""]
            for part in wrapped:
                print(f"│ {part:<{content_width}} │")
    print(colorize("╰" + "─" * (width - 2) + "╯", accent))


def config_dir() -> Path:
    """Return a per-user config directory on Windows and Unix-like systems."""
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "verbot"


def read_secret() -> str:
    """Read the optional bearer token from the environment or private config."""
    env_token = os.environ.get("VERBOT_API_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        return (config_dir() / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_secret(token: str | None) -> None:
    path = config_dir() / "token"
    if token is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".verbot-token-", delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            temp_file.write(token.strip() + "\n")
        if os.name != "nt":
            os.chmod(temp_name, 0o600)
            os.chmod(path.parent, 0o700)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def load_system_prompt() -> str:
    path = Path(__file__).resolve().with_name("system_prompt.txt")
    try:
        prompt = path.read_text(encoding="utf-8").strip()
    except OSError:
        prompt = "You are Verbot, a concise and capable professional AI assistant."
    memory_path = config_dir() / "memory.txt"
    try:
        memory = memory_path.read_text(encoding="utf-8").strip()
    except OSError:
        memory = ""
    if memory:
        prompt += "\n\nUser preferences saved for future conversations:\n" + memory
    return prompt


def update_memory(note: str) -> None:
    note = " ".join(note.split())
    if not note:
        return
    path = config_dir() / "memory.txt"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    previous = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    entries = [line for line in previous.splitlines() if line.strip()]
    entry = "- " + note.strip()
    if entry not in entries:
        entries.append(entry)
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)


APP_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "browser": {"linux": ("xdg-open",), "windows": ("msedge", "chrome", "firefox"), "darwin": ("open",)},
    "firefox": {"linux": ("firefox",), "windows": ("firefox",), "darwin": ("firefox",)},
    "chrome": {"linux": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"), "windows": ("chrome",), "darwin": ("Google Chrome",)},
    "calculator": {"linux": ("gnome-calculator", "kcalc", "mate-calc", "galculator", "xcalc"), "windows": ("calc",), "darwin": ("Calculator",)},
    "files": {"linux": ("xdg-open",), "windows": ("explorer",), "darwin": ("open",)},
    "terminal": {"linux": ("x-terminal-emulator", "kgx", "gnome-terminal", "konsole", "xfce4-terminal", "mate-terminal", "kitty", "alacritty"), "windows": ("wt", "powershell"), "darwin": ("open",)},
    "text editor": {"linux": ("gedit", "xed", "mousepad", "kate", "code"), "windows": ("notepad", "code"), "darwin": ("TextEdit",)},
    "notepad": {"linux": ("gedit", "xed", "mousepad", "kate"), "windows": ("notepad",), "darwin": ("TextEdit",)},
    "vscode": {"linux": ("code",), "windows": ("code",), "darwin": ("code",)},
    "telegram": {"linux": ("telegram-desktop", "telegram"), "windows": ("telegram",), "darwin": ("Telegram",)},
}

APP_ALIASES = {
    "web": "browser", "internet": "browser", "calc": "calculator",
    "file manager": "files", "explorer": "files", "editor": "text editor",
    "vs code": "vscode", "visual studio code": "vscode", "code": "vscode",
    "google chrome": "chrome", "mozilla firefox": "firefox",
    "телега": "telegram", "телеграм": "telegram", "телеграмм": "telegram", "тг": "telegram",
}

LAUNCH_REQUEST = re.compile(
    r"^(?:(?:please\s+)?(?:can|could|would)\s+you\s+)?(?:please\s+)?"
    r"(?:open|launch|start|run|открой|запусти|запустить)\s+"
    r"(?:(?:the|my|программу|программы|приложение|приложения)\s+)?(.+?)"
    r"(?:\s+for\s+me)?[.!?]*$",
    re.IGNORECASE,
)

CLOSE_REQUEST = re.compile(
    r"^(?:(?:please\s+)?(?:close|quit|stop|kill)|закрой|закрои|выключи|заверши)\s+"
    r"(?:(?:the|окно|программу|приложение)\s+)?(.+?)[.!?]*$",
    re.IGNORECASE,
)


def extract_launch_targets(message: str) -> list[str] | None:
    match = LAUNCH_REQUEST.fullmatch(message.strip())
    if not match:
        return None
    raw_target = match.group(1).strip().strip("\"'")
    raw_target = re.sub(r"\s+(?:please|now|пожалуйста)$", "", raw_target, flags=re.IGNORECASE)
    parts = re.split(r"\s+(?:and|и|а также)\s+|\s*,\s*|\s+\+\s+", raw_target, flags=re.IGNORECASE)
    targets = []
    for part in parts:
        target = re.sub(r"^(?:open|launch|start|run|открой|запусти)\s+", "", part.strip(), flags=re.IGNORECASE)
        target = target.strip().strip("\"'")
        if target:
            targets.append(target)
    return targets if targets else None


def extract_launch_target(message: str) -> str | None:
    """Backward-compatible single-target helper."""
    targets = extract_launch_targets(message)
    return targets[0] if targets and len(targets) == 1 else None


def extract_close_target(message: str) -> str | None:
    match = CLOSE_REQUEST.fullmatch(message.strip())
    if not match:
        return None
    target = match.group(1).strip().strip("\"'")
    target = re.sub(r"\s+(?:for me|please|now|пожалуйста)$", "", target, flags=re.IGNORECASE)
    target = re.sub(r"\s+(?:app|application|программу|приложение)$", "", target, flags=re.IGNORECASE)
    return target or None


TELEGRAM_MESSAGE_REQUEST = re.compile(
    r"^(?:please\s+)?(?:open|launch|открой|запусти)\s+(?:telegram|телеграм(?:м)?|тг)"
    r"\s+(?:and\s+|и\s+)?(?:write|message|напиши|написать|сообщение)\s+(.+?)[.!?]*$",
    re.IGNORECASE,
)


def close_application(app_name: str) -> str:
    """Ask the OS to gracefully stop exact, known app process names."""
    key = " ".join(app_name.lower().split()).strip(" .!?\"'")
    key = re.sub(r"^(?:the|программу|приложение)\s+", "", key, flags=re.IGNORECASE)
    key = APP_ALIASES.get(key, key)

    process_names = {
        "browser": ("firefox", "chrome", "chromium", "chromium-browser", "msedge"),
        "telegram": ("telegram-desktop", "telegram-deskto", "telegram"),
        "firefox": ("firefox",),
        "chrome": ("chrome", "google-chrome", "chromium", "chromium-browser"),
        "vscode": ("code",),
        "calculator": ("gnome-calculator", "gnome-calculato", "kcalc", "mate-calc", "galculator", "xcalc", "calc"),
    }.get(key)
    if not process_names:
        return f"I can’t safely identify a process for {app_name!r}. Supported close targets: Telegram, Firefox, Chrome, VS Code, calculator, browser."

    system = platform.system().lower()
    closed_count = 0
    for proc in process_names:
        try:
            if system == "windows":
                image_name = proc if proc.lower().endswith(".exe") else f"{proc}.exe"
                res = subprocess.run(["taskkill", "/im", image_name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     check=False, timeout=10)
            else:
                res = subprocess.run(["pkill", "-TERM", "-x", proc],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     check=False, timeout=10)
            if res.returncode == 0:
                closed_count += 1
        except (OSError, subprocess.TimeoutExpired):
            continue

    if closed_count:
        return f"Closed {app_name}."
    return f"{app_name} does not appear to be running."


def handle_telegram_message_request(message: str) -> str | None:
    match = TELEGRAM_MESSAGE_REQUEST.fullmatch(message.strip())
    if not match:
        return None
    result = launch_application("telegram")
    if result.startswith("Opened "):
        return result + " I did not send a message; select the recipient and send it in Telegram."
    return result


def windows_app_paths(key: str) -> tuple[str, ...]:
    roots = [os.environ.get(name, "") for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    suffixes = {
        "firefox": (r"Mozilla Firefox\firefox.exe",),
        "chrome": (r"Google\Chrome\Application\chrome.exe",),
        "vscode": (r"Programs\Microsoft VS Code\Code.exe", r"Microsoft VS Code\Code.exe"),
        "text editor": (r"Programs\Microsoft VS Code\Code.exe",),
        "telegram": (r"Telegram Desktop\Telegram.exe", r"Programs\Telegram Desktop\Telegram.exe"),
    }.get(key, ())
    return tuple(str(Path(root) / suffix) for root in roots if root for suffix in suffixes)


def _normalized_app_name(name: str) -> str:
    return "".join(char for char in name.casefold() if char.isalnum())


def linux_desktop_entries() -> list[Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(os.pathsep)
    roots = [data_home, *(Path(root) for root in data_dirs if root)]
    roots.extend((
        Path("/var/lib/flatpak/exports/share"),
        Path("/var/lib/snapd/desktop"),
        data_home / "flatpak/exports/share",
    ))
    entries: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        apps = root / "applications"
        if apps.is_dir():
            try:
                for entry in apps.rglob("*.desktop"):
                    if entry not in seen:
                        seen.add(entry)
                        entries.append(entry)
            except OSError:
                continue
    return entries


def launch_linux_desktop_app(requested: str) -> str | None:
    wanted = _normalized_app_name(requested)
    if not wanted:
        return None
    matches: list[tuple[bool, Path, configparser.SectionProxy]] = []
    for path in linux_desktop_entries():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as file:
                parser.read_file(file)
            entry = parser["Desktop Entry"]
        except (OSError, KeyError, configparser.Error):
            continue
        if entry.get("Type", "Application").casefold() != "application":
            continue
        try:
            hidden = entry.getboolean("Hidden", fallback=False)
            no_display = entry.getboolean("NoDisplay", fallback=False)
            terminal_app = entry.getboolean("Terminal", fallback=False)
            dbus_activatable = entry.getboolean("DBusActivatable", fallback=False)
        except ValueError:
            continue
        if hidden or no_display or terminal_app:
            continue
        names = [value.strip() for key, value in entry.items()
                 if key == "name" or key.startswith("name[") if value.strip()]
        if not names:
            continue
        normalized_names = [_normalized_app_name(name) for name in names]
        if any(normalized == wanted or (len(wanted) >= 4 and wanted in normalized)
               for normalized in normalized_names):
            matches.append((wanted not in normalized_names, path, entry))

    for _partial, path, entry in sorted(matches, key=lambda item: (item[0], str(item[1]))):
        display_name = entry.get("Name", path.stem)
        command = entry.get("Exec", "").strip()
        if not command and dbus_activatable:
            gtk_launch = shutil.which("gtk-launch")
            app_id = None
            for parent in path.parents:
                if parent.name == "applications":
                    app_id = path.relative_to(parent).as_posix().replace("/", "-")
                    if app_id.endswith(".desktop"):
                        app_id = app_id[:-8]
                    break
            if gtk_launch and app_id:
                try:
                    subprocess.Popen([gtk_launch, app_id], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                    return f"Opened {display_name}."
                except OSError:
                    pass
            continue
        try:
            args = shlex.split(command)
        except ValueError:
            continue
        if not args:
            continue
        expanded: list[str] = []
        icon = entry.get("Icon", "").strip()
        for arg in args:
            if arg == "%i":
                if icon:
                    expanded.extend(("--icon", icon))
                continue
            if arg in {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%v", "%m"}:
                continue
            arg = arg.replace("%%", "%").replace("%c", display_name).replace("%k", str(path))
            arg = re.sub(r"%[fFuUdDnNvm]", "", arg)
            expanded.append(arg)
        if not expanded:
            continue
        try_exec = entry.get("TryExec", "").strip()
        if try_exec and not shutil.which(try_exec) and not Path(try_exec).is_file():
            continue
        working_dir = entry.get("Path", "").strip() or None
        if working_dir and not Path(working_dir).is_dir():
            working_dir = None
        try:
            subprocess.Popen(expanded, cwd=working_dir, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return f"Opened {display_name}."
        except OSError:
            continue
    return None


def launch_windows_shortcut(requested: str) -> str | None:
    roots = [Path(os.environ[name]) / "Microsoft/Windows/Start Menu/Programs"
             for name in ("APPDATA", "PROGRAMDATA") if os.environ.get(name)]
    wanted = _normalized_app_name(requested)
    if not wanted:
        return None
    matches: list[tuple[bool, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.lnk"):
                name = _normalized_app_name(path.stem)
                if name == wanted or (len(wanted) >= 4 and wanted in name):
                    matches.append((name != wanted, path))
        except OSError:
            continue
    for _partial, path in sorted(matches, key=lambda item: (item[0], str(item[1]))):
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return f"Opened {path.stem}."
        except OSError:
            continue
    return None


def launch_application(requested: str) -> str:
    key = " ".join(requested.lower().split())
    key = APP_ALIASES.get(key, key)
    system = platform.system().lower()
    system_key = "windows" if system == "windows" else "darwin" if system == "darwin" else "linux"

    if system_key == "windows" and key in {"browser", "files"}:
        try:
            os.startfile("https://www.google.com" if key == "browser" else str(Path.home()))  # type: ignore[attr-defined]
            return f"Opened {key} with the Windows default app."
        except OSError as exc:
            return f"Could not launch {key}: {exc}"

    if system_key == "darwin":
        app_names = {
            "firefox": "Firefox", "chrome": "Google Chrome", "calculator": "Calculator",
            "files": "Finder", "terminal": "Terminal", "text editor": "TextEdit",
            "notepad": "TextEdit", "vscode": "Visual Studio Code", "telegram": "Telegram",
        }
        try:
            if key == "browser":
                args = ["open", "https://www.google.com"]
            elif key == "files":
                args = ["open", str(Path.home())]
            else:
                args = ["open", "-a", app_names.get(key, requested)]
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"Opened {requested}."
        except OSError as exc:
            return f"Could not open {requested}: {exc}"

    candidates = APP_CANDIDATES.get(key, {}).get(system_key, ())
    if not candidates and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", key):
        candidates = (key,)

    launch_errors: list[str] = []
    resolved = [(executable, shutil.which(executable)) for executable in candidates]
    if system_key == "windows":
        resolved.extend((path, path if Path(path).is_file() else None) for path in windows_app_paths(key))

    for executable, located in resolved:
        if located:
            try:
                args = [located]
                if executable == "xdg-open":
                    args.append("https://www.google.com" if key == "browser" else str(Path.home()))
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=(os.name != "nt"))
                return f"Opened {requested}."
            except OSError as exc:
                launch_errors.append(f"{executable}: {exc}")

    if system_key == "linux":
        result = launch_linux_desktop_app(requested)
        if result:
            return result
    if system_key == "windows":
        result = launch_windows_shortcut(requested)
        if result:
            return result

    if launch_errors:
        return f"Could not open {requested}: {'; '.join(launch_errors)}"
    return (f"I couldn’t find an installed app called {requested!r}. "
            "Try its menu name or executable name.")


class Verbot:
    def __init__(self) -> None:
        self.url = os.environ.get("VERBOT_OLLAMA_URL", DEFAULT_URL)
        self.model = os.environ.get("VERBOT_MODEL", DEFAULT_MODEL)
        self.system_prompt = load_system_prompt()
        self.last_token_count: int | None = None
        self.history: list[dict[str, str]] = []

    def _local_tags_url(self) -> str | None:
        try:
            parts = urlsplit(self.url)
            if parts.scheme not in {"http", "https"} or parts.hostname not in {"localhost", "127.0.0.1", "::1"} or parts.port != 11434:
                return None
            return urlunsplit((parts.scheme, parts.netloc, "/api/tags", "", ""))
        except ValueError:
            return None

    def _probe_ollama(self, tags_url: str) -> urllib.error.URLError | None:
        try:
            with urllib.request.urlopen(tags_url, timeout=1):
                return None
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            return exc if isinstance(exc, urllib.error.URLError) else None
        except OSError as exc:
            return urllib.error.URLError(exc)

    def ensure_ollama(self) -> str | None:
        tags_url = self._local_tags_url()
        if tags_url is None:
            return None
        error = self._probe_ollama(tags_url)
        if error is None:
            return None
        if not isinstance(error.reason, ConnectionRefusedError):
            return f"Could not check the local Ollama service: {error}."

        executable = shutil.which("ollama")
        if not executable:
            return "Ollama is not running and 'ollama' was not found on PATH."

        print(colorize("●", GREEN) + "  Ollama is stopped; starting it in the background...")
        options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            options["start_new_session"] = True
        try:
            subprocess.Popen([executable, "serve"], **options)
        except OSError as exc:
            return f"Could not start Ollama: {exc}."

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.3)
            if self._probe_ollama(tags_url) is None:
                return None
        return "Ollama did not become ready within 15 seconds."

    def ask(self, prompt: str) -> str:
        self.last_token_count = None
        startup_error = self.ensure_ollama()
        if startup_error:
            return startup_error
        try:
            parsed_url = urlsplit(self.url)
            _ = parsed_url.port
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or any(char.isspace() for char in self.url):
                return "VERBOT_OLLAMA_URL is not a valid HTTP URL. Check the URL and try again."
            is_generate_endpoint = parsed_url.path.rstrip("/").endswith("/api/generate")
        except ValueError:
            return "VERBOT_OLLAMA_URL is not a valid URL. Check the URL and try again."
        if is_generate_endpoint:
            transcript = "\n".join(
                f"{'User' if item['role'] == 'user' else 'Assistant'}: {item['content']}"
                for item in self.history[-MAX_HISTORY_MESSAGES:]
            )
            full_prompt = f"Conversation so far:\n{transcript}\n\nUser: {prompt}" if transcript else prompt
            payload = {
                "model": self.model, "system": self.system_prompt, "prompt": full_prompt,
                "stream": False,
                "options": {"num_predict": MAX_RESPONSE_TOKENS, "num_ctx": 8192},
            }
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *self.history[-MAX_HISTORY_MESSAGES:],
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"num_predict": MAX_RESPONSE_TOKENS, "num_ctx": 8192},
            }
        try:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        except (TypeError, ValueError) as exc:
            return f"Could not build the model request: {exc}. Check VERBOT_OLLAMA_URL."
        token = read_secret()
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                return "The model service returned an unexpected response. Check that VERBOT_OLLAMA_URL points to Ollama."
            self.last_token_count = result.get("eval_count")
            if is_generate_endpoint:
                answer = str(result.get("response", "")).strip()
            else:
                message = result.get("message") or {}
                answer = str(message.get("content", "")).strip() if isinstance(message, dict) else ""

            marker = "[MEMORY:"
            if marker in answer:
                before_marker, remainder = answer.split(marker, 1)
                closing_bracket = remainder.find("]")
                memory = remainder[:closing_bracket].strip() if closing_bracket >= 0 else ""
                if memory:
                    answer = before_marker
                    try:
                        update_memory(memory)
                        self.system_prompt = load_system_prompt()
                    except OSError:
                        answer += "\n\n(Verbot could not save that preference to disk.)"
            answer = answer.strip()
            if answer:
                self.history.extend((
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": answer},
                ))
                self.history = self.history[-MAX_HISTORY_MESSAGES:]
            return answer or "The model returned an empty reply. Try again or check the Ollama model logs."
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return "The model service rejected the configured token. Use /token set to update it or /token clear to remove it."
            if exc.code == 404:
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except OSError:
                    body = ""
                finally:
                    exc.close()
                lowered = body.lower()
                if "model" in lowered and ("not found" in lowered or "does not exist" in lowered):
                    return f"Ollama is running, but model '{self.model}' is not installed. Run `ollama pull {self.model}` and retry."
                return f"Ollama returned HTTP 404 for {self.url}; the endpoint may be wrong. Use /api/chat (default) or /api/generate."
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
            finally:
                exc.close()
            return f"Model service returned HTTP {exc.code}: {detail or exc.reason}."
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError):
                return f"Ollama is not running at {self.url}. Install Ollama and ensure the model '{self.model}' is available."
            return f"Could not connect to Ollama at {self.url}: {reason}."
        except TimeoutError:
            return "Ollama took too long to respond. Try again or use a smaller model."
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "Ollama returned invalid JSON. Check that VERBOT_OLLAMA_URL points to an Ollama API endpoint."

    def clear_history(self) -> None:
        self.history.clear()


def show_token_status() -> str:
    if os.environ.get("VERBOT_API_TOKEN", "").strip():
        location = "VERBOT_API_TOKEN environment variable"
    elif read_secret():
        location = "private user config file"
    else:
        location = "not configured"
    return f"API token: {location}. Token values are never displayed."


def print_help() -> None:
    print_card("QUICK COMMANDS", [
        "/launch <app>    Open an installed application",
        "/close <app>     Close a supported application",
        "/token status    Check token configuration",
        "/token set       Save a token securely",
        "/token clear     Remove the saved token",
        "/clear           Forget this chat’s conversation context",
        "/exit            Close Verbot",
        "You can also ask naturally, for example: “open Firefox”.",
    ], accent=PURPLE)


def handle_command(text: str, assistant: Verbot) -> str | None:
    command, _, argument = text.partition(" ")
    command = command.lower()
    argument = argument.strip()
    if command in {"/exit", "/quit"}:
        raise EOFError
    if command == "/help":
        print_help()
        return ""
    if command == "/launch":
        if not argument:
            return "Usage: /launch <app>"
        return "\n".join(launch_application(target) for target in (extract_launch_targets(f"open {argument}") or [argument]))
    if command == "/close":
        return close_application(argument) if argument else "Usage: /close <app>"
    if command == "/clear":
        assistant.clear_history()
        return "Conversation context cleared."
    if command == "/token":
        action = argument.lower()
        if action == "status":
            return show_token_status()
        if action == "set":
            value = getpass.getpass("  API token (hidden) › ").strip()
            if not value:
                return "No token entered; existing token was left unchanged."
            try:
                save_secret(value)
            except OSError as exc:
                return f"Could not save the token: {exc}"
            return "Token saved in the user config directory."
        if action == "clear":
            try:
                save_secret(None)
            except OSError as exc:
                return f"Could not remove the saved token: {exc}"
            return "Saved token removed. VERBOT_API_TOKEN, if set, still takes precedence."
        return "Usage: /token <status|set|clear>"
    return None


def main() -> int:
    assistant = Verbot()
    print_card("VERBOT", [
        "Your local AI workspace",
        f"Model  ·  {assistant.model}",
        "Ollama starts on demand",
        "Type /help for commands",
    ])
    while True:
        try:
            user_input = input(colorize("\n  You  › ", CYAN)).strip()
            if not user_input:
                continue

            telegram_result = handle_telegram_message_request(user_input)
            if telegram_result:
                print_card("VERBOT", [telegram_result], accent=GREEN)
                continue

            close_target = extract_close_target(user_input)
            if close_target:
                print_card("VERBOT", [close_application(close_target)], accent=GREEN)
                continue

            launch_targets = extract_launch_targets(user_input)
            if launch_targets:
                results = []
                for target in launch_targets:
                    res = launch_application(target)
                    results.append(res)
                print_card("VERBOT", results, accent=GREEN)
                continue

            if user_input.startswith("/"):
                result = handle_command(user_input, assistant)
                if result:
                    print_card("VERBOT", result.splitlines(), accent=GREEN)
                continue

            response = assistant.ask(user_input)
            print_card("VERBOT", [response], accent=PURPLE)
            if assistant.last_token_count is not None:
                print(colorize(f"  · {assistant.last_token_count} response tokens", MUTED))
        except (EOFError, KeyboardInterrupt):
            print(colorize("\n  Session closed. See you next time.", MUTED))
            return 0


if __name__ == "__main__":
    sys.exit(main())
