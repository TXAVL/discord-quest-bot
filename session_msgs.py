import json
import os
from logger import log

TRACK_FILE = "session_messages.json"


def _load() -> list:
    try:
        with open(TRACK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list):
    try:
        with open(TRACK_FILE, "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception as e:
        log(f"Lỗi ghi {TRACK_FILE}: {e}", "warn")


def track(msg):
    """Ghi nhớ tin nhắn phiên (DM user/admin) để dọn khi bot tắt/khởi động lại."""
    if msg is None:
        return msg
    rows = _load()
    key = [msg.channel.id, msg.id]
    if key not in rows:
        rows.append(key)
        _save(rows)
    return msg


async def purge_tracked(bot) -> int:
    """Xóa toàn bộ tin nhắn phiên đã ghi nhớ (kể cả của lần chạy trước). Trả về số tin đã xóa."""
    rows = _load()
    _save([])
    deleted = 0
    for channel_id, message_id in rows:
        try:
            ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            msg = ch.get_partial_message(message_id)
            await msg.delete()
            deleted += 1
        except Exception:
            pass
    if rows:
        log(f"Đã dọn {deleted}/{len(rows)} tin nhắn phiên cũ trong DM", "ok")
    return deleted
