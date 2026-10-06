"""
Crash Notifier
==============

Sends Telegram alerts on bot crash (with docker log attachments).
The "logs" command listener now lives in the standalone telegram_bot.py script.
"""

import os
import subprocess
import requests


# Config (populated by init_crash_notifier)
_bot_token: str = ""
_chat_id: str = ""

DOCKER_CONTAINERS = ["hydra-hydra-app-1", "hydra2-hydra-app-1"]
DOCKER_LOG_TAIL = 20000


def init_crash_notifier(bot_token: str, chat_id: str):
    """Set Telegram credentials. Call once at startup."""
    global _bot_token, _chat_id
    _bot_token = bot_token
    _chat_id = chat_id


def _send_message(text: str):
    """Send a Telegram text message."""
    if not _bot_token or not _chat_id:
        return
    url = f"https://api.telegram.org/bot{_bot_token}/sendMessage"
    requests.post(url, data={"chat_id": _chat_id, "text": text}, timeout=15)


def _send_document(filename: str, content: str):
    """Upload a text file as a Telegram document."""
    if not _bot_token or not _chat_id:
        return
    url = f"https://api.telegram.org/bot{_bot_token}/sendDocument"
    files = {"document": (filename, content.encode("utf-8", errors="replace"))}
    requests.post(url, data={"chat_id": _chat_id}, files=files, timeout=30)


def _capture_docker_logs(tail: int = DOCKER_LOG_TAIL) -> dict:
    """Capture the last *tail* lines from each docker container."""
    logs = {}
    for container in DOCKER_CONTAINERS:
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(tail), container],
                capture_output=True, text=True, timeout=60,
            )
            logs[container] = result.stdout + result.stderr
        except Exception:
            logs[container] = "(failed to capture logs)"
    return logs


# ---------------------------------------------------------------------------
# Crash handler
# ---------------------------------------------------------------------------

def notify_and_exit(reason: str, exit_code: int = 1):
    """Capture docker logs, send Telegram alert + log files, then exit."""
    try:
        logs = _capture_docker_logs()
        _send_message(f"\U0001f6d1 Bot crashed: {reason}")
        for container, content in logs.items():
            _send_document(f"{container}.log", content)
    except Exception:
        pass  # Best-effort — never prevent exit

    os._exit(exit_code)
