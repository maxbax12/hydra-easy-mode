"""
Alerts — push notifications (Telegram) with de-duplication.

`Alerter.raise_(key, text)` sends once per key until the key is cleared or its
text changes; `clear(key, text)` sends a single all-clear. Credentials are read
from the `telegram:` section of config/bot_config.yaml (bot_token, chat_id).
"""

import json
import os
import time
from typing import Dict, Optional

import requests


class Alerter:
    def __init__(self, logger, state_path: str = "state/alerts.json", config_path: str = "config/bot_config.yaml",
                 prefix: str = "🤖 Hydra MM"):
        # ALERTS_PREFIX tells two bots sharing one Telegram bot apart (e.g. "🤖 Hydra MM · easy")
        self.logger, self.state_path, self.prefix = logger, state_path, os.getenv("ALERTS_PREFIX") or prefix
        # A dedicated alerts bot (not the log bot in bot_config.yaml's telegram: section)
        self.token: Optional[str] = os.getenv("ALERTS_TELEGRAM_BOT_TOKEN") or None
        self.chat: Optional[str] = os.getenv("ALERTS_TELEGRAM_CHAT_ID") or None
        if not (self.token and self.chat):
            logger.warning("alerts: ALERTS_TELEGRAM_BOT_TOKEN / ALERTS_TELEGRAM_CHAT_ID not set — log only")
        self.digest_every: Optional[float] = None    # set -> send() only queues; flush() sends one message
        self._queue = []
        self._last_flush = time.time()
        self.active: Dict[str, str] = {}
        try:
            with open(state_path) as f:
                self.active = json.load(f)
        except Exception:
            pass

    def send(self, text: str):
        self.logger.warning(f"ALERT: {text}")
        if self.digest_every:
            self._queue.append(text)
            return
        self._post(text)

    def flush(self, header_fn=None, force: bool = False, body_fn=None):
        """Send everything queued since the last flush as ONE message (digest mode).
        body_fn(items) -> lines may condense the body (header_fn still sees every item)."""
        if not self.digest_every or (not force and time.time() - self._last_flush < self.digest_every):
            return
        items, self._queue = self._queue, []
        start = time.strftime("%H:%M", time.localtime(self._last_flush))
        self._last_flush = time.time()
        if not items:
            return
        head = header_fn(items) if header_fn else ""
        body = "\n".join(body_fn(items) if body_fn else items)
        if len(body) > 3500:                          # Telegram limit is 4096
            body = body[:3500] + "\n…"
        self._post(f"🕔 {start}–{time.strftime('%H:%M')}\n{head}{body}")

    def _refresh_creds(self):
        """Use the token / chat as .env has them now: Telegram pairing and a bot switch
        (`hydra-mm telegram`) change them while this runs."""
        try:
            from dotenv import dotenv_values
            if not os.path.exists(".env"):
                return
            env = dotenv_values(".env")
        except Exception:
            return
        self.token = env.get("ALERTS_TELEGRAM_BOT_TOKEN") or self.token
        self.chat = env.get("ALERTS_TELEGRAM_CHAT_ID") or self.chat

    def _post(self, text: str):
        self._refresh_creds()
        if not (self.token and self.chat):
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          data={"chat_id": self.chat, "text": f"{self.prefix}\n{text}"}, timeout=15)
        except Exception as e:
            self.logger.warning(f"alerts: telegram send failed: {e}")

    def raise_(self, key: str, text: str, once: bool = False):
        """Send `text` unless this alert is already active with the same text
        (once=True: already active at all, whatever the text now says)."""
        if self.active.get(key) == text or (once and key in self.active):
            return
        self.active[key] = text
        self._save()
        self.send(text)

    def clear(self, key: str, text: Optional[str] = None):
        if key not in self.active:
            return
        del self.active[key]
        self._save()
        if text:
            self.send(text)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path + ".tmp", "w") as f:
                json.dump(self.active, f)
            os.replace(self.state_path + ".tmp", self.state_path)
        except Exception:
            pass
