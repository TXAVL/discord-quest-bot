"""Tự đồng bộ emoji động (GIF) trong thư mục emojis/ lên Application Emojis của bot."""
import os

import config
from logger import log

EMOJI_DIR = "emojis"
# tên file gif -> thuộc tính trong config; chỉ ghi đè khi .env chưa đặt emoji tùy chỉnh
EMOJI_MAP = {
    "loading": "EMOJI_LOADING",
    "success": "EMOJI_SUCCESS",
    "fail": "EMOJI_FAIL",
    "rocket": "EMOJI_ROCKET",
}


async def sync_application_emojis(bot):
    """Tải GIF còn thiếu lên bot rồi dùng chúng làm emoji mặc định trong giao diện."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
        for name, attr in EMOJI_MAP.items():
            if os.getenv(attr):
                continue  # đã cấu hình thủ công trong .env
            emoji = existing.get(name)
            if emoji is None:
                path = os.path.join(EMOJI_DIR, f"{name}.gif")
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as f:
                    emoji = await bot.create_application_emoji(name=name, image=f.read())
                log(f"Đã tải emoji động '{name}' lên bot", "ok")
            setattr(config, attr, str(emoji))
    except Exception as e:
        log(f"Không đồng bộ được emoji động: {e}", "warn")
