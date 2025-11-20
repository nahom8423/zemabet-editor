"""
Lightweight helper server for editing Discord static messages via a web dashboard.

Run with:
    python web_dashboard/server.py

Then open http://localhost:5000/ to load the Tailwind-based editor template.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time
import logging
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "static_message.json"
CHANNEL_NAMES_PATH = PROJECT_ROOT / "channel_names.json"
DEFAULT_CONFIG = {"channels": {}, "allowed_helpers": []}
DISCORD_LIMIT = 2000
CONTINUATION_PREFIX = "-\n\n"
STATIC_MESSAGE_MARKER = "\u2063\u2060\u2063"
LEGACY_STATIC_MESSAGE_MARKERS = ("\u2063sm\u2063",)
STATIC_MESSAGE_MARKERS = (STATIC_MESSAGE_MARKER,) + LEGACY_STATIC_MESSAGE_MARKERS
STATIC_MESSAGE_CONTINUATION_PREFIX = "-\n\n"
MAX_HISTORY_SCAN = 250

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
app = Flask(__name__, static_folder=str(Path(__file__).parent), static_url_path="")
load_dotenv()

GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or os.getenv("GUILD_ID") or 970490174565928960)
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
COMMAND_LOG_CHANNEL_ID = os.getenv("COMMAND_LOG_CHANNEL_ID") or os.getenv("LOG_CHANNEL_ID")
CHANNEL_CACHE_TTL = int(os.getenv("CHANNEL_NAME_CACHE_TTL", "900"))
_CHANNEL_META_CACHE: dict[str, dict[str, str]] = {}
_CHANNEL_META_CACHE_TIME = 0.0
_BOT_USER_ID: str | None = None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))

    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(DEFAULT_CONFIG))

    data: Dict[str, Any] = {"channels": {}, "allowed_helpers": []}

    helpers: List[int] = []
    for helper in raw.get("allowed_helpers", []):
        coerced = _coerce_int(helper)
        if coerced is not None:
            helpers.append(coerced)
    data["allowed_helpers"] = helpers

    channels: Dict[str, Dict[str, Any]] = {}
    for key, entry in (raw.get("channels") or {}).items():
        try:
            channel_key = str(int(key))
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue

        msg_ids: List[int] = []
        for mid in entry.get("message_ids", []):
            coerced = _coerce_int(mid)
            if coerced is not None:
                msg_ids.append(coerced)

        channels[channel_key] = {
            "title": entry.get("title", ""),
            "content": entry.get("content", ""),
            "message_ids": msg_ids,
            "last_editor_id": entry.get("last_editor_id"),
            "last_updated": entry.get("last_updated"),
        }

    data["channels"] = channels
    return data


def _save_config(data: Dict[str, Any]) -> None:
    payload = {
        "channels": {},
        "allowed_helpers": data.get("allowed_helpers", []),
    }

    for key, entry in (data.get("channels") or {}).items():
        try:
            channel_key = str(int(key))
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        msg_ids: List[int] = []
        for mid in entry.get("message_ids", []):
            coerced = _coerce_int(mid)
            if coerced is not None:
                msg_ids.append(coerced)

        payload["channels"][channel_key] = {
            "title": entry.get("title", ""),
            "content": entry.get("content", ""),
            "message_ids": msg_ids,
            "last_editor_id": entry.get("last_editor_id"),
            "last_updated": entry.get("last_updated"),
        }

    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_channel_name_overrides() -> dict[str, dict[str, str]]:
    if not CHANNEL_NAMES_PATH.exists():
        return {}
    try:
        raw = json.loads(CHANNEL_NAMES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    overrides: dict[str, dict[str, str]] = {}
    for key, value in (raw or {}).items():
        try:
            channel_key = str(int(key))
        except (TypeError, ValueError):
            continue

        if isinstance(value, str):
            overrides[channel_key] = {"name": value.strip()}
            continue

        if isinstance(value, dict):
            entry: dict[str, str] = {}
            name = value.get("name")
            if isinstance(name, str) and name.strip():
                entry["name"] = name.strip()
            category = value.get("category")
            if isinstance(category, str) and category.strip():
                entry["category"] = category.strip()
            if entry:
                overrides[channel_key] = entry

    return overrides


def _fetch_channel_metadata_from_discord() -> dict[str, dict[str, str]]:
    if not BOT_TOKEN or not GUILD_ID:
        return {}
    try:
        response = requests.get(
            f"https://discord.com/api/v10/guilds/{GUILD_ID}/channels",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=10,
        )
    except requests.RequestException:
        return {}

    if response.status_code != 200:
        return {}

    category_lookup: dict[str, str] = {}
    try:
        payload = response.json()
    except ValueError:
        return {}

    for channel in payload or []:
        try:
            if int(channel.get("type")) == 4:
                channel_id = str(channel.get("id"))
                if channel_id:
                    category_lookup[channel_id] = channel.get("name") or ""
        except Exception:
            continue

    results: dict[str, dict[str, str]] = {}
    for channel in payload or []:
        try:
            channel_id = str(channel.get("id"))
        except Exception:
            continue
        if not channel_id:
            continue
        entry: dict[str, str] = {}
        name = channel.get("name")
        if name:
            entry["name"] = name
        parent_id = channel.get("parent_id")
        if parent_id:
            parent_name = category_lookup.get(str(parent_id))
            if parent_name:
                entry["category"] = parent_name
        if entry:
            results[channel_id] = entry

    return results


def _get_channel_metadata() -> dict[str, dict[str, str]]:
    global _CHANNEL_META_CACHE_TIME, _CHANNEL_META_CACHE
    now = time.time()
    should_refresh = now - _CHANNEL_META_CACHE_TIME > CHANNEL_CACHE_TTL or not _CHANNEL_META_CACHE
    if should_refresh:
        overrides = _load_channel_name_overrides()
        fetched = _fetch_channel_metadata_from_discord()
        combined = {**fetched, **overrides}
        if combined:
            _CHANNEL_META_CACHE = combined
            _CHANNEL_META_CACHE_TIME = now
    return _CHANNEL_META_CACHE.copy()


def _split_for_discord(text: str, *, marker_len: int = 0) -> List[str]:
    """Mirror the chunking logic used by the Discord bot for consistent previews."""
    segments: List[str] = []
    remaining = text or ""
    first_segment = True

    while remaining:
        prefix = "" if first_segment else CONTINUATION_PREFIX
        available = DISCORD_LIMIT - len(prefix) - marker_len
        if available <= 0:
            raise ValueError("Continuation prefix consumes all message space.")

        if len(remaining) <= available:
            segments.append(prefix + remaining)
            break

        cut = remaining.rfind("\n\n", 0, available)
        if cut == -1:
            cut = remaining.rfind("\n", 0, available)
        if cut == -1 or cut < 10:
            cut = available

        part = remaining[:cut].rstrip()
        if not part:
            part = remaining[:available]
            cut = available

        segments.append(prefix + part)
        remaining = remaining[cut:].lstrip("\n")
        first_segment = False

    return segments or [text.strip()]

def _bot_user_id() -> str | None:
    global _BOT_USER_ID
    if _BOT_USER_ID or not BOT_TOKEN:
        return _BOT_USER_ID
    try:
        resp = requests.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            _BOT_USER_ID = str(data.get("id")) if data.get("id") else None
    except requests.RequestException:
        return None
    return _BOT_USER_ID


def _has_marker(text: str | None) -> bool:
    return any(marker in (text or "") for marker in STATIC_MESSAGE_MARKERS)


def _fetch_recent_messages(channel_id: str, *, max_fetch: int = MAX_HISTORY_SCAN) -> List[dict]:
    """Pull a limited slice of history so we can find existing managed messages."""
    if not BOT_TOKEN:
        return []
    url_base = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}

    messages: List[dict] = []
    before: str | None = None
    remaining = max(1, max_fetch)

    while remaining > 0:
        limit = min(100, remaining)
        params = {"limit": limit}
        if before:
            params["before"] = before
        try:
            resp = requests.get(url_base, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                break
            batch = resp.json() or []
        except requests.RequestException:
            break

        if not batch:
            break

        messages.extend(batch)
        remaining -= len(batch)
        before = str(batch[-1].get("id"))
        if len(batch) < limit:
            break

    return messages


def _find_managed_messages(channel_id: str) -> List[str]:
    """
    Reconstruct managed message ids from channel history so the dashboard can edit in place.
    """
    bot_id = _bot_user_id()
    history = _fetch_recent_messages(channel_id)
    managed: List[dict] = []

    for msg in history:
        content = msg.get("content") or ""
        author = msg.get("author") or {}
        if bot_id and str(author.get("id")) != bot_id:
            continue
        if not _has_marker(content):
            continue
        managed.append(msg)

    managed.sort(key=lambda m: int(m.get("id", 0)))
    return [str(m.get("id")) for m in managed if m.get("id")]


@app.get("/")
def editor() -> Any:
    return send_from_directory(app.static_folder, "editor.html")


@app.get("/api/channels")
def list_channels() -> Any:
    config = _load_config()
    metadata = _get_channel_metadata()
    names = {cid: meta.get("name", "") for cid, meta in metadata.items()}
    payload = {
        "channels": config.get("channels", {}),
        "channel_names": names,
        "channel_meta": metadata,
    }
    return jsonify(payload)


@app.post("/api/preview")
def preview_segments() -> Any:
    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    combined = f"**{title[:256]}**\n\n{content}" if title else content
    segments = _split_for_discord(combined, marker_len=len(STATIC_MESSAGE_MARKER))
    return jsonify({"segments": segments, "count": len(segments)})


def _post_log_message(content: str) -> None:
    if not BOT_TOKEN or not COMMAND_LOG_CHANNEL_ID:
        return
    try:
        requests.post(
            f"https://discord.com/api/v10/channels/{COMMAND_LOG_CHANNEL_ID}/messages",
            headers={
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "content": content,
                "allowed_mentions": {"parse": []},
            },
            timeout=10,
        )
    except requests.RequestException:
        pass


def _publish_static_message(
    channel_id: str,
    segments: List[str],
    existing_ids: List[int] | List[str],
) -> List[str]:
    url_base = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    allowed_mentions = {"parse": []}

    # Prefer live discovery so we always target what the bot most recently posted
    normalized_existing = _find_managed_messages(channel_id)
    if not normalized_existing:
        normalized_existing = [str(mid) for mid in (existing_ids or []) if mid is not None]

    new_ids: List[str] = []
    obsolete: List[str] = []

    for idx, segment in enumerate(segments):
        payload = f"{segment}{STATIC_MESSAGE_MARKER}"
        message_id = normalized_existing[idx] if idx < len(normalized_existing) else None

        if message_id:
            try:
                resp = requests.patch(
                    f"{url_base}/{message_id}",
                    headers=headers,
                    json={"content": payload, "allowed_mentions": allowed_mentions, "embeds": []},
                    timeout=10,
                )
                if resp.status_code == 200:
                    new_ids.append(message_id)
                    continue
                logger.info(
                    "Patch of %s in channel %s returned %s: %s",
                    message_id,
                    channel_id,
                    resp.status_code,
                    resp.text,
                )
            except requests.RequestException as exc:
                logger.info(
                    "Patch of %s in channel %s errored: %s",
                    message_id,
                    channel_id,
                    exc,
                )
                pass
            except Exception as exc:  # pragma: no cover
                logger.info(
                    "Unexpected patch failure for %s in channel %s: %s",
                    message_id,
                    channel_id,
                    exc,
                )
                pass
            obsolete.append(message_id)

        resp = requests.post(
            url_base,
            headers=headers,
            json={"content": payload, "allowed_mentions": allowed_mentions},
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to post segment {idx + 1}: {resp.text}")
        data = resp.json()
        new_ids.append(str(data["id"]))

    # delete any leftover old messages
    leftover = normalized_existing[len(new_ids):]
    for extra_id in obsolete + leftover:
        try:
            requests.delete(f"{url_base}/{extra_id}", headers=headers, timeout=10)
        except requests.RequestException as exc:
            logger.info(
                "Delete of stale static message %s in channel %s failed: %s",
                extra_id,
                channel_id,
                exc,
            )
            continue

    if not new_ids:
        raise RuntimeError("No messages were published.")
    return new_ids


@app.post("/api/save")
def save_static_message() -> Any:
    payload = request.get_json(silent=True) or {}
    channel_id = payload.get("channelId") or payload.get("channel_id")
    if channel_id is None:
        abort(400, "Missing channelId")

    try:
        channel_key = str(int(channel_id))
    except (TypeError, ValueError):
        abort(400, "channelId must be numeric")

    title = (payload.get("title") or "").strip()
    content = (payload.get("content") or "").strip()
    if not content:
        abort(400, "content is required")

    config = _load_config()
    channels = config.setdefault("channels", {})
    entry = channels.get(channel_key, {})
    entry["title"] = title
    entry["content"] = content
    entry.setdefault("message_ids", [])

    editor_id = payload.get("editor_id")
    coerced_editor = _coerce_int(editor_id)
    if coerced_editor is not None:
        entry["last_editor_id"] = coerced_editor
    entry["last_updated"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

    channels[channel_key] = entry
    full_text = f"**{title[:256]}**\n\n{content}" if title else content
    segments = _split_for_discord(full_text, marker_len=len(STATIC_MESSAGE_MARKER))

    if not BOT_TOKEN:
        abort(500, "Bot token is not configured for publishing.")

    try:
        new_ids = _publish_static_message(
            channel_key,
            segments,
            entry.get("message_ids", []),
        )
    except Exception as exc:
        abort(500, f"Failed to publish static message: {exc}")

    entry["message_ids"] = new_ids
    channels[channel_key] = entry
    _save_config(config)

    actor = payload.get("actor") or "Dashboard"
    _post_log_message(
        f"📝 `{actor}` updated the static message in <#{channel_key}> "
        f"(segments: {len(segments)})."
    )

    return jsonify({"status": "ok", "channel_id": channel_key, "segment_count": len(segments)})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the static-message dashboard API.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "5000")))
    parser.add_argument("--debug", action="store_true", default=bool(os.getenv("FLASK_DEBUG")))
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=args.debug)
