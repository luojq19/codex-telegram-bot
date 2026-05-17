#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_STATE_DIR = Path.home() / ".codex_telegram_bot"
DEFAULT_CODEX_BIN = "codex"
DEFAULT_WORKDIR = str(Path.cwd())
MAX_TELEGRAM_TEXT = 3900

state_lock = threading.Lock()
current_process: subprocess.Popen | None = None
current_chat_id: int | None = None
current_started_at: float | None = None
media_group_lock = threading.Lock()
media_groups: dict[str, dict] = {}


def main() -> int:
    load_env_file(Path(os.environ.get("TELEGRAM_CODEX_RELAY_ENV", DEFAULT_ENV_FILE)))

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "Missing TELEGRAM_BOT_TOKEN. Create ~/.codex/telegram_relay/telegram_relay.env first.",
            file=sys.stderr,
        )
        return 2

    allowed_ids = parse_allowed_ids(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    api = TelegramAPI(token)
    offset = load_offset()

    print("Telegram Codex relay is running.", flush=True)
    while True:
        try:
            updates = api.get_updates(offset=offset, timeout=50)
        except Exception as exc:
            print(f"poll failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)
            continue

        for update in updates:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            save_offset(offset)
            handle_update(api, update, allowed_ids)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def parse_allowed_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            pass
    return result


def load_offset() -> int:
    path = state_dir() / "offset.txt"
    try:
        return int(path.read_text().strip())
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    path = state_dir() / "offset.txt"
    path.write_text(str(offset))


def state_dir() -> Path:
    path = Path(os.environ.get("CODEX_RELAY_STATE_DIR", DEFAULT_STATE_DIR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def handle_update(api: "TelegramAPI", update: dict, allowed_ids: set[int]) -> None:
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    photos = message.get("photo") or []
    if chat_id is None or (not text and not photos):
        return

    if text.startswith("/start") or text.startswith("/help"):
        api.send_message(chat_id, help_text(), reply_markup=command_keyboard())
        return

    if text.startswith("/menu"):
        api.send_message(chat_id, "Command menu enabled.", reply_markup=command_keyboard())
        return

    if text.startswith("/id"):
        user = message.get("from") or {}
        api.send_message(
            chat_id,
            f"chat_id: {chat_id}\nuser: @{user.get('username', '')}\n\n"
            "Put this chat_id into TELEGRAM_ALLOWED_CHAT_IDS on the server.",
        )
        return

    if not allowed_ids:
        api.send_message(
            chat_id,
            "Relay is not armed yet. Send /id, then add your chat_id to "
            "~/.codex/telegram_relay/telegram_relay.env.",
        )
        return

    if int(chat_id) not in allowed_ids:
        api.send_message(chat_id, "This chat_id is not allowed. Send /id from the allowed phone.")
        return

    if text.startswith("/status"):
        api.send_message(chat_id, status_text())
        return

    if text.startswith("/history"):
        api.send_long_message(chat_id, history_text(int(chat_id), text))
        return

    if text.startswith("/sessions"):
        api.send_long_message(chat_id, sessions_text(text))
        return

    if text.startswith("/session"):
        api.send_message(chat_id, session_text(int(chat_id)))
        return

    if text.startswith("/resume"):
        session_id = text[len("/resume") :].strip()
        if not session_id:
            api.send_message(chat_id, "Usage: /resume <session_id>")
        else:
            save_chat_session(int(chat_id), session_id)
            api.send_message(chat_id, f"Telegram relay will resume Codex session:\n{session_id}")
        return

    if text.startswith("/new"):
        clear_chat_session(int(chat_id))
        prompt = text[len("/new") :].strip()
        if prompt:
            start_codex_thread(api, int(chat_id), prompt, force_new=True)
        else:
            api.send_message(chat_id, "Cleared the Telegram-bound Codex session. Next /codex starts fresh.")
        return

    if text.startswith("/cancel"):
        api.send_message(chat_id, cancel_current())
        return

    if photos:
        handle_photo_message(api, int(chat_id), text, photos, message)
        return

    if not text.startswith("/codex"):
        api.send_message(chat_id, "Send /codex <task> to run Codex. Use /help for commands.")
        return

    prompt = text[len("/codex") :].strip()
    if not prompt:
        api.send_message(chat_id, "Usage: /codex <task>")
        return

    start_codex_thread(api, int(chat_id), prompt)


def handle_photo_message(
    api: "TelegramAPI",
    chat_id: int,
    text: str,
    photos: list,
    message: dict,
) -> None:
    media_group_id = message.get("media_group_id")
    if media_group_id:
        collect_media_group(api, chat_id, str(media_group_id), text, photos)
        return

    prompt = parse_codex_prompt(text)
    if prompt is None:
        api.send_message(chat_id, "Send photos with a caption like: /codex describe this image")
        return

    start_codex_thread(api, chat_id, prompt, [largest_photo_file_id(photos)])


def collect_media_group(
    api: "TelegramAPI",
    chat_id: int,
    media_group_id: str,
    text: str,
    photos: list,
) -> None:
    with media_group_lock:
        group = media_groups.setdefault(
            media_group_id,
            {
                "api": api,
                "chat_id": chat_id,
                "prompt_text": "",
                "file_ids": [],
                "timer": None,
            },
        )
        if text and not group["prompt_text"]:
            group["prompt_text"] = text
        group["file_ids"].append(largest_photo_file_id(photos))
        if group["timer"] is not None:
            group["timer"].cancel()
        group["timer"] = threading.Timer(1.5, process_media_group, args=(media_group_id,))
        group["timer"].daemon = True
        group["timer"].start()


def process_media_group(media_group_id: str) -> None:
    with media_group_lock:
        group = media_groups.pop(media_group_id, None)
    if group is None:
        return

    api = group["api"]
    chat_id = group["chat_id"]
    prompt = parse_codex_prompt(group["prompt_text"])
    if prompt is None:
        api.send_message(chat_id, "Send photo groups with a caption like: /codex compare these images")
        return

    start_codex_thread(api, chat_id, prompt, group["file_ids"])


def parse_codex_prompt(text: str) -> str | None:
    if not text.startswith("/codex"):
        return None
    prompt = text[len("/codex") :].strip()
    return prompt or "Analyze the attached image."


def largest_photo_file_id(photos: list) -> str:
    if not photos:
        raise ValueError("photo message has no photo sizes")
    largest = max(photos, key=lambda item: int(item.get("file_size") or item.get("width", 0) * item.get("height", 0)))
    return str(largest["file_id"])


def start_codex_thread(
    api: "TelegramAPI",
    chat_id: int,
    prompt: str,
    photo_file_ids: list[str] | None = None,
    force_new: bool = False,
) -> None:
    with state_lock:
        if current_process is not None:
            api.send_message(chat_id, status_text())
            return

    thread = threading.Thread(
        target=run_codex_and_reply,
        args=(api, chat_id, prompt, force_new, photo_file_ids or []),
        daemon=True,
    )
    thread.start()


def help_text() -> str:
    return (
        "Telegram Codex relay\n\n"
        "/id - show your chat_id for allowlist setup\n"
        "/codex <task> - run Codex, resuming this Telegram chat's session when possible\n"
        "Send photos with a /codex caption to attach images to the task\n"
        "/new [task] - clear the saved session, optionally starting a fresh Codex task\n"
        "/session - show this Telegram chat's saved Codex session id\n"
        "/sessions [all|N] - list recent Codex sessions for copy/paste resume\n"
        "/history [all|N] - show recent user/Codex messages from the bound session\n"
        "/resume <session_id> - bind this Telegram chat to an existing Codex session\n"
        "/menu - show the Telegram reply keyboard\n"
        "/status - show whether Codex is running\n"
        "/cancel - terminate the current Codex subprocess\n\n"
        "Only allowlisted chat IDs can run /codex."
    )


def command_keyboard() -> dict:
    return {
        "keyboard": [
            ["/sessions", "/history"],
            ["/session", "/status"],
            ["/cancel", "/help"],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": True,
    }


def status_text() -> str:
    with state_lock:
        if current_process is None:
            return "Codex relay is idle."
        elapsed = int(time.time() - (current_started_at or time.time()))
        return f"Codex is running for {elapsed}s. Use /cancel to terminate it."


def session_text(chat_id: int) -> str:
    session_id = load_chat_session(chat_id)
    if not session_id:
        return "No Telegram-bound Codex session yet. Next /codex starts fresh."
    return f"Current Telegram-bound Codex session:\n{session_id}"


def sessions_text(command_text: str) -> str:
    limit = parse_sessions_limit(command_text)
    sessions = load_recent_sessions(limit)
    if not sessions:
        return "No Codex sessions found in ~/.codex/session_index.jsonl."

    lines = [f"Recent Codex sessions ({len(sessions)}):"]
    for item in sessions:
        updated_at = item.get("updated_at", "")
        thread_name = item.get("thread_name", "(untitled)")
        session_id = item.get("id", "")
        lines.append(f"\n{updated_at}\n{thread_name}\n/resume {session_id}")
    return "\n".join(lines)


def parse_sessions_limit(command_text: str) -> int:
    arg = command_text[len("/sessions") :].strip().lower()
    if arg == "all":
        return 50
    if arg.isdigit():
        return max(1, min(int(arg), 50))
    return 10


def load_recent_sessions(limit: int) -> list[dict]:
    path = Path.home() / ".codex" / "session_index.jsonl"
    sessions: list[dict] = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except Exception:
        return []

    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("id"):
            sessions.append(item)

    sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return sessions[:limit]


def history_text(chat_id: int, command_text: str) -> str:
    session_id = load_chat_session(chat_id)
    if not session_id:
        return "No Telegram-bound Codex session yet. Use /sessions, then /resume <session_id>."

    path = find_session_file(session_id)
    if path is None:
        return f"Could not find a local session file for:\n{session_id}"

    messages = load_session_messages(path)
    if not messages:
        return f"No user/Codex messages found in:\n{path}"

    limit = parse_history_limit(command_text)
    shown = messages[-limit:]
    lines = [
        f"History for {session_id}",
        f"Showing last {len(shown)} of {len(messages)} messages.",
    ]
    for message in shown:
        role = "You" if message["role"] == "user" else "Codex"
        text = truncate_text(clean_history_text(message["text"]), 1400)
        lines.append(f"\n{role}:\n{text}")
    return "\n".join(lines)


def parse_history_limit(command_text: str) -> int:
    arg = command_text[len("/history") :].strip().lower()
    if arg == "all":
        return 60
    if arg.isdigit():
        return max(1, min(int(arg), 60))
    return 8


def find_session_file(session_id: str) -> Path | None:
    roots = [
        Path.home() / ".codex" / "sessions",
        Path.home() / ".codex" / "archived_sessions",
    ]
    matches: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        matches.extend(root.rglob(f"*{session_id}*.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def load_session_messages(path: Path) -> list[dict[str, str]]:
    primary: list[dict[str, str]] = []
    fallback: list[dict[str, str]] = []

    for line in path.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if record.get("type") == "response_item":
            payload = record.get("payload") or {}
            if payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                text = extract_content_text(payload.get("content"))
                if text and not should_skip_history_message(payload["role"], text):
                    primary.append({"role": payload["role"], "text": text})

        if record.get("type") == "event_msg":
            payload = record.get("payload") or {}
            event_type = payload.get("type")
            if event_type == "user_message" and payload.get("message"):
                text = str(payload["message"])
                if not should_skip_history_message("user", text):
                    fallback.append({"role": "user", "text": text})
            elif event_type == "agent_message" and payload.get("message"):
                text = str(payload["message"])
                if not should_skip_history_message("assistant", text):
                    fallback.append({"role": "assistant", "text": text})

    return primary or fallback


def extract_content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(part.strip() for part in parts if part and part.strip()).strip()


def should_skip_history_message(role: str, text: str) -> bool:
    if role != "user":
        return False

    stripped = text.lstrip()
    noisy_prefixes = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<developer",
        "<system",
    )
    return stripped.startswith(noisy_prefixes)


def clean_history_text(text: str) -> str:
    text = text.strip()
    marker = "## My request for Codex:"
    if marker in text:
        text = text.split(marker, 1)[1].strip()
    return text


def truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n... truncated ..."


def session_path(chat_id: int) -> Path:
    return state_dir() / f"session_{chat_id}.txt"


def load_chat_session(chat_id: int) -> str:
    try:
        return session_path(chat_id).read_text().strip()
    except Exception:
        return ""


def save_chat_session(chat_id: int, session_id: str) -> None:
    session_path(chat_id).write_text(session_id.strip() + "\n")


def clear_chat_session(chat_id: int) -> None:
    try:
        session_path(chat_id).unlink()
    except FileNotFoundError:
        pass


def cancel_current() -> str:
    with state_lock:
        proc = current_process
    if proc is None:
        return "Codex relay is idle."
    try:
        proc.terminate()
        return "Sent terminate signal to the current Codex subprocess."
    except Exception as exc:
        return f"Could not terminate process: {exc}"


def run_codex_and_reply(
    api: "TelegramAPI",
    chat_id: int,
    prompt: str,
    force_new: bool = False,
    photo_file_ids: list[str] | None = None,
) -> None:
    global current_chat_id, current_process, current_started_at

    codex_bin = os.environ.get("CODEX_BIN", DEFAULT_CODEX_BIN)
    workdir = os.environ.get("CODEX_WORKDIR", DEFAULT_WORKDIR)
    sandbox = os.environ.get("CODEX_SANDBOX", "workspace-write")
    timeout_sec = int(os.environ.get("CODEX_RELAY_TIMEOUT_SEC", "3600"))
    session_id = "" if force_new else load_chat_session(chat_id)
    photo_file_ids = photo_file_ids or []

    with tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt", delete=False) as tmp:
        last_message_path = Path(tmp.name)
    image_paths: list[Path] = []

    try:
        if photo_file_ids:
            image_paths = download_telegram_images(api, photo_file_ids)
    except Exception as exc:
        api.send_message(chat_id, f"Could not download Telegram image(s): {exc}")
        try:
            last_message_path.unlink(missing_ok=True)
        except Exception:
            pass
        return

    if session_id:
        cmd = [
            codex_bin,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
            "--output-last-message",
            str(last_message_path),
        ]
        for image_path in image_paths:
            cmd.extend(["--image", str(image_path)])
        cmd.extend([session_id, prompt])
    else:
        cmd = [
            codex_bin,
            "exec",
            "--cd",
            workdir,
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(last_message_path),
        ]
        for image_path in image_paths:
            cmd.extend(["--image", str(image_path)])
        cmd.append(prompt)

    env = os.environ.copy()
    extra_path = os.environ.get("CODEX_EXTRA_PATH", "").strip()
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with state_lock:
            current_process = proc
            current_chat_id = chat_id
            current_started_at = time.time()

        try:
            stdout, stderr = proc.communicate(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.terminate()
            stdout, stderr = proc.communicate(timeout=20)
            api.send_message(chat_id, f"Codex timed out after {timeout_sec}s and was terminated.")
            return

        last_message = read_last_message(last_message_path)
        if last_message:
            final_text = last_message
        else:
            final_text = tail_text((stdout or "") + "\n" + (stderr or ""), 12000)

        new_session_id = extract_thread_id(stdout or "")
        if new_session_id:
            save_chat_session(chat_id, new_session_id)

        prefix = "Codex finished"
        if proc.returncode:
            prefix += f" with exit code {proc.returncode}"
        api.send_long_message(chat_id, f"{prefix}:\n\n{final_text.strip() or '(no output)'}")
    except Exception as exc:
        api.send_message(chat_id, f"Codex relay failed: {exc}")
    finally:
        try:
            last_message_path.unlink(missing_ok=True)
        except Exception:
            pass
        for image_path in image_paths:
            try:
                image_path.unlink(missing_ok=True)
            except Exception:
                pass
        with state_lock:
            current_process = None
            current_chat_id = None
            current_started_at = None


def download_telegram_images(api: "TelegramAPI", file_ids: list[str]) -> list[Path]:
    result: list[Path] = []
    image_dir = state_dir() / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    try:
        for index, file_id in enumerate(file_ids, start=1):
            file_info = api.get_file(file_id)
            file_path = file_info.get("file_path")
            if not file_path:
                raise RuntimeError(f"Telegram returned no file_path for image {index}")
            suffix = Path(str(file_path)).suffix or ".jpg"
            destination = image_dir / f"telegram-image-{int(time.time() * 1000)}-{index}{suffix}"
            api.download_file(str(file_path), destination)
            result.append(destination)
        return result
    except Exception:
        for path in result:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def read_last_message(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except Exception:
        return ""


def extract_thread_id(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = event.get("thread_id")
        if event.get("type") == "thread.started" and isinstance(thread_id, str):
            return thread_id
    return ""


def tail_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "... truncated ...\n" + text[-max_chars:]


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        data = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": json.dumps(["message", "edited_message"]),
            },
            timeout=timeout + 10,
        )
        return data.get("result", [])

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        params = {"chat_id": str(chat_id), "text": text}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        self.call("sendMessage", params, timeout=20)

    def send_long_message(self, chat_id: int, text: str) -> None:
        chunks = chunk_text(text, MAX_TELEGRAM_TEXT)
        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                chunk = f"[{index}/{len(chunks)}]\n{chunk}"
            self.send_message(chat_id, chunk)

    def get_file(self, file_id: str) -> dict:
        data = self.call("getFile", {"file_id": file_id}, timeout=20)
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Unexpected getFile response: {data}")
        return result

    def download_file(self, file_path: str, destination: Path) -> None:
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())

    def call(self, method: str, params: dict[str, str], timeout: int) -> dict:
        body = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(f"{self.base_url}/{method}", data=body)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram HTTP {exc.code}: {detail}") from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram API error: {payload}")
        return payload


def chunk_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        chunk = rest[:max_chars]
        split_at = chunk.rfind("\n")
        if split_at > max_chars // 2:
            chunk = chunk[:split_at]
        chunks.append(chunk)
        rest = rest[len(chunk) :].lstrip("\n")
    return chunks


def install_signal_handlers() -> None:
    def _handler(_signum: int, _frame: object) -> None:
        with state_lock:
            proc = current_process
        if proc is not None:
            proc.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


if __name__ == "__main__":
    install_signal_handlers()
    raise SystemExit(main())
