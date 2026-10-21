"""txa_layout — giao diện dùng chung (Discord Components V2) cho toàn bộ Quest Bot.

Mọi màn hình đều theo một khuôn: một khung (Container) chứa tiêu đề, nội dung, menu chọn,
còn hàng nút điều hướng (Quay lại / Làm mới / Trợ giúp...) nằm ngay dưới khung.
Các màn hình điều hướng cho nhau bằng cách sửa chính tin nhắn hiện tại.
"""
import asyncio
import inspect
import time

import discord

import config
from limits_manager import (
    get_price_per_quest,
    set_price_per_quest,
    get_transaction_timeout,
    set_transaction_timeout,
)

MISSING = discord.utils.MISSING
ACCENT = discord.Colour.from_rgb(88, 101, 242)
GREEN = discord.Colour.from_rgb(87, 242, 135)
YELLOW = discord.Colour.from_rgb(254, 231, 92)
ORANGE = discord.Colour.from_rgb(250, 166, 26)
RED = discord.Colour.from_rgb(237, 66, 69)

PACKAGE_EMOJI = {1: "💎", 2: "🎫", 5: "🔥", 10: "👑", 20: "🚀", 50: "🏆"}


# ───────────────────────── Khối dựng chung ─────────────────────────

BOT_AVATAR = None   # ảnh đại diện bot, gán lúc khởi động (configure)
INVITE_URL = None   # link mời bot, gán lúc khởi động (configure)
QUEST_HOME_URL = "https://discord.com/quest-home"


def configure(avatar: str = None, invite: str = None):
    global BOT_AVATAR, INVITE_URL
    if avatar:
        BOT_AVATAR = avatar
    if invite:
        INVITE_URL = invite


def link_buttons(*which) -> list:
    """Các nút link dùng chung: 'invite' (mời bot), 'quest' (trang Quest), 'discord' (Discord Web)."""
    out = []
    for w in which:
        if w == "invite" and INVITE_URL:
            out.append(discord.ui.Button(label="Mời Bot", emoji="➕", style=discord.ButtonStyle.link, url=INVITE_URL))
        elif w == "quest":
            out.append(discord.ui.Button(label="Trang Quest", emoji="🎁", style=discord.ButtonStyle.link, url=QUEST_HOME_URL))
        elif w == "discord":
            out.append(discord.ui.Button(label="Discord Web", emoji="💬", style=discord.ButtonStyle.link, url="https://discord.com/app"))
    return out


def _with_hero(items: list) -> list:
    """Ghép tiêu đề '### ...' (+ dòng mô tả ngay sau nó) thành một Section có ảnh đại diện bot bên phải."""
    if not BOT_AVATAR or not items or not isinstance(items[0], str) or not items[0].startswith("### "):
        return items
    items = list(items)
    texts = [items[0]]
    for j in range(1, len(items)):
        it = items[j]
        if it is None:
            continue
        if isinstance(it, str) and it != "---" and not it.startswith("```") and len(it) < 600:
            texts.append(it)
            del items[j]
        break
    items[0] = discord.ui.Section(*texts, accessory=discord.ui.Thumbnail(BOT_AVATAR))
    return items


def box(*items, accent=ACCENT, hero: bool = True) -> discord.ui.Container:
    """Tạo khung nội dung. Chuỗi thường -> TextDisplay, "---" -> đường kẻ, None -> bỏ qua.
    Tiêu đề đầu khung tự có ảnh đại diện bot (hero=False để tắt)."""
    c = discord.ui.Container(accent_colour=accent)
    for it in (_with_hero(list(items)) if hero else items):
        if it is None:
            continue
        if isinstance(it, str):
            c.add_item(discord.ui.Separator() if it == "---" else discord.ui.TextDisplay(it))
        else:
            c.add_item(it)
    return c


def row_of(*items) -> discord.ui.ActionRow:
    r = discord.ui.ActionRow()
    for i in items:
        r.add_item(i)
    return r


def btn(label, emoji, callback, style=discord.ButtonStyle.secondary, disabled=False) -> discord.ui.Button:
    b = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    b.callback = callback
    return b


async def _resolve(back, interaction):
    """back(interaction) có thể trả về view hoặc (embed, view); có thể là coroutine."""
    result = back(interaction)
    if inspect.isawaitable(result):
        result = await result
    return result[1] if isinstance(result, tuple) else result


class TxaView(discord.ui.LayoutView):
    """Lớp nền cho mọi màn hình: kiểm tra quyền, nút điều hướng, quay lại."""
    admin_only = False

    def __init__(self, user_id: int, back=None, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.back = back

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.admin_only:
            if interaction.user.id not in config.ADMIN_IDS:
                await interaction.response.send_message("❌ Chỉ Admin được dùng bảng này.", ephemeral=True)
                return False
            return True
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bảng này không dành cho bạn.", ephemeral=True)
            return False
        return True

    def on_leave(self):
        """Hook chạy trước khi rời màn hình (ví dụ dừng vòng cập nhật realtime)."""

    async def go_back(self, interaction: discord.Interaction):
        self.on_leave()
        view = await _resolve(self.back, interaction)
        await interaction.response.edit_message(view=view)

    async def go(self, interaction: discord.Interaction, view: "TxaView"):
        """Chuyển sang màn hình khác bằng cách sửa chính tin nhắn này."""
        self.on_leave()
        await interaction.response.edit_message(view=view)

    def nav_row(self, refresh=None, help_text: str = None, extra=()) -> discord.ui.ActionRow:
        items = []
        if self.back:
            items.append(btn("Quay lại", "◀️", self._on_back_click))
        if refresh:
            items.append(btn("Làm mới", "🔄", refresh))
        for b in extra:
            items.append(b)
        if help_text:
            async def show_help(interaction: discord.Interaction, text=help_text):
                await interaction.response.send_message(text, ephemeral=True)
            items.append(btn("Trợ giúp", "❓", show_help))
        return row_of(*items)

    async def _on_back_click(self, interaction: discord.Interaction):
        await self.go_back(interaction)


class SimpleLayoutView(TxaView):
    """Màn hình đơn giản: tiêu đề + nội dung, tuỳ chọn ảnh đại diện, nút Quay lại/Làm mới."""

    def __init__(self, user_id, title, body, accent=ACCENT, back=None, refresh=None, thumb=None, footer=None, help_text=None):
        super().__init__(user_id, back)
        self.title, self.body, self.accent = title, body, accent
        self.refresh_cb, self.thumb, self.footer, self.help_text = refresh, thumb, footer, help_text
        self.build()

    def build(self):
        self.clear_items()
        head = f"### {self.title}\n{self.body}"
        if self.thumb:
            top = discord.ui.Section(head, accessory=discord.ui.Thumbnail(self.thumb))
        else:
            top = head
        self.add_item(box(top, f"-# {self.footer}" if self.footer else None, accent=self.accent))

        async def do_refresh(interaction: discord.Interaction):
            fresh = self.refresh_cb()
            if inspect.isawaitable(fresh):
                fresh = await fresh
            self.title, self.body = fresh
            self.build()
            await interaction.response.edit_message(view=self)

        nav = self.nav_row(refresh=do_refresh if self.refresh_cb else None, help_text=self.help_text)
        if nav.children:
            self.add_item(nav)


# ───────────────────────── Cài đặt (Config) ─────────────────────────

def _bool_values(on_desc: str, off_desc: str):
    return [("True", "Bật ✅", on_desc), ("False", "Tắt ❌", off_desc)]


def _settings():
    """Danh sách cài đặt theo từng nhóm. Mỗi mục: tiêu đề, mô tả, các giá trị, hàm đọc và hàm ghi."""
    def env_setter(key):
        return lambda v: config.update_env_variable(key, v)

    return {
        "sys": {
            "label": "Hệ thống", "emoji": "⚙️",
            "desc": "Chu kỳ quét, nhịp tim và các chế độ tự động của bot.",
            "items": [
                {
                    "key": "POLL_INTERVAL", "emoji": "🔄", "label": "Tần suất quét quest",
                    "desc": "Thời gian chờ giữa hai lần quét quest mới của mỗi tài khoản.",
                    "values": [("15", "15 giây", "Quét rất nhanh • tốn tài nguyên"), ("30", "30 giây", "Nhanh • hợp với đa số"),
                               ("60", "60 giây", "Cân bằng • khuyên dùng"), ("120", "120 giây", "Chậm • tiết kiệm"),
                               ("300", "300 giây", "Rất chậm • tiết kiệm tối đa")],
                    "custom": {"min": 5, "max": 3600, "unit": "giây"},
                    "get": lambda: str(config.POLL_INTERVAL), "set": env_setter("POLL_INTERVAL"),
                },
                {
                    "key": "HEARTBEAT_INTERVAL", "emoji": "💓", "label": "Chu kỳ heartbeat",
                    "desc": "Khoảng cách giữa các nhịp tim gửi lên để tính tiến độ nhiệm vụ.",
                    "values": [("10", "10 giây", "Nhịp dày • tiến độ mượt"), ("15", "15 giây", "Cân bằng • khuyên dùng"),
                               ("20", "20 giây", "Mặc định của Discord"), ("30", "30 giây", "Nhịp thưa • ít request")],
                    "custom": {"min": 5, "max": 300, "unit": "giây"},
                    "get": lambda: str(config.HEARTBEAT_INTERVAL), "set": env_setter("HEARTBEAT_INTERVAL"),
                },
                {
                    "key": "AUTO_ACCEPT", "emoji": "📥", "label": "Tự động nhận quest",
                    "desc": "Bot tự nhận quest mới ngay khi phát hiện.",
                    "values": _bool_values("Bot tự nhận quest mới", "Phải nhận quest thủ công"),
                    "get": lambda: str(bool(config.AUTO_ACCEPT)), "set": env_setter("AUTO_ACCEPT"),
                },
                {
                    "key": "LOG_PROGRESS", "emoji": "📝", "label": "Log từng nhịp tim",
                    "desc": "In chi tiết từng nhịp tim ra console của bot.",
                    "values": _bool_values("In từng nhịp tim ra console", "Console gọn hơn"),
                    "get": lambda: str(bool(config.LOG_PROGRESS)), "set": env_setter("LOG_PROGRESS"),
                },
                {
                    "key": "DEBUG", "emoji": "🐞", "label": "Chế độ debug",
                    "desc": "Bật log chi tiết để tìm lỗi.",
                    "values": _bool_values("Log chi tiết để tìm lỗi", "Log bình thường"),
                    "get": lambda: str(bool(config.DEBUG)), "set": env_setter("DEBUG"),
                },
            ],
        },
        "pay": {
            "label": "Quest & Thanh toán", "emoji": "💰",
            "desc": "Giá lượt, hạn hóa đơn QR và quyền của Admin.",
            "items": [
                {
                    "key": "price", "emoji": "💵", "label": "Đơn giá mỗi lượt",
                    "desc": "Đổi giá sẽ quay lại mức ưu đãi ngẫu nhiên của các gói.",
                    "values": [("5000", "5,000 VNĐ", "Giá mềm • thu hút người mua"), ("10000", "10,000 VNĐ", "Mức phổ biến • khuyên dùng"),
                               ("20000", "20,000 VNĐ", "Giá cao"), ("50000", "50,000 VNĐ", "Cao cấp")],
                    "custom": {"min": 1000, "max": 1000000, "unit": "VNĐ", "money": True},
                    "get": lambda: str(get_price_per_quest()), "set": lambda v: set_price_per_quest(int(v)),
                },
                {
                    "key": "timeout", "emoji": "⏳", "label": "Hạn hóa đơn QR",
                    "desc": "Sau thời gian này hóa đơn chưa thanh toán sẽ hết hạn.",
                    "values": [("3", "03 phút", "Ngắn • tránh treo đơn"), ("5", "05 phút", "Mặc định • khuyên dùng"),
                               ("10", "10 phút", "Thoải mái cho người chuyển chậm"), ("15", "15 phút", "Dài"),
                               ("30", "30 phút", "Rất dài • giữ đơn lâu")],
                    "custom": {"min": 1, "max": 120, "unit": "phút"},
                    "get": lambda: str(get_transaction_timeout() // 60),
                    "set": lambda v: set_transaction_timeout(int(v) * 60),
                },
                {
                    "key": "ADMIN_BYPASS_LIMIT", "emoji": "👑", "label": "Admin vô hạn lượt",
                    "desc": "Admin làm quest không bị trừ lượt.",
                    "values": _bool_values("Admin không bị trừ lượt", "Admin bị tính lượt như user thường"),
                    "get": lambda: str(bool(getattr(config, "ADMIN_BYPASS_LIMIT", True))),
                    "set": env_setter("ADMIN_BYPASS_LIMIT"),
                },
            ],
        },
    }


def _label_of(item, value: str) -> str:
    for v, label, _desc in item["values"]:
        if v == value:
            return label
    spec = item.get("custom")
    if spec and value.isdigit():
        return f"{int(value):,} {spec['unit']}" if spec.get("money") else f"{value} {spec['unit']}"
    return value


CUSTOM_VALUE = "__custom__"


def parse_number(text: str, allow_suffix: bool = True):
    """Đọc số người dùng nhập: hỗ trợ 15000, 15,000, 15.000, 15k, 1tr. Trả về int hoặc None."""
    t = text.strip().lower().replace(" ", "")
    mult = 1
    if not allow_suffix:
        pass
    elif t.endswith("k"):
        mult, t = 1000, t[:-1]
    elif t.endswith("tr"):
        mult, t = 1000000, t[:-2]
    t = t.replace(",", "").replace(".", "")
    if not t.isdigit():
        return None
    return int(t) * mult


class CustomValueModal(discord.ui.Modal):
    """Hộp nhập giá trị tùy chỉnh cho các mục số."""

    def __init__(self, layout, item):
        spec = item["custom"]
        super().__init__(title=f"Nhập {item['label']}"[:45])
        self.layout = layout
        self.item = item
        self.spec = spec
        self.example = "15000 hoặc 15k" if spec.get("money") else str(max(spec["min"], min(spec["max"], 10)))
        shown_min = f"{spec['min']:,}" if spec.get("money") else spec["min"]
        shown_max = f"{spec['max']:,}" if spec.get("money") else spec["max"]
        self.value_input = discord.ui.TextInput(
            label=f"{item['label']} ({spec['unit']})"[:45],
            placeholder=f"Từ {shown_min} đến {shown_max} {spec['unit']} (ví dụ {self.example})"[:100],
            default=item["get"](),
            min_length=1,
            max_length=12,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        number = parse_number(self.value_input.value, allow_suffix=bool(self.spec.get("money")))
        lo, hi, unit = self.spec["min"], self.spec["max"], self.spec["unit"]
        if number is None or not (lo <= number <= hi):
            await interaction.response.send_message(
                f"❌ Giá trị không hợp lệ. Hãy nhập một số từ **{lo:,}** đến **{hi:,}** {unit} (ví dụ `{self.example}`).",
                ephemeral=True
            )
            return
        self.item["set"](str(number))
        self.layout.note = f"✅ Đã đặt **{self.item['label']}** = **{_label_of(self.item, str(number))}** (nhập tay)"
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class GroupSelect(discord.ui.Select):
    def __init__(self, layout, groups):
        options = [
            discord.SelectOption(label=g["label"], value=k, emoji=g["emoji"], description=g["desc"][:100])
            for k, g in groups.items()
        ]
        super().__init__(placeholder="📂 Nhấn vào đây để chọn nhóm cài đặt", options=options)
        self.layout = layout

    async def callback(self, interaction: discord.Interaction):
        self.layout.page = self.values[0]
        self.layout.note = None
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class SettingSelect(discord.ui.Select):
    def __init__(self, layout, item):
        current = item["get"]()
        options = []
        known = {v for v, _, _ in item["values"]}
        if current not in known:
            options.append(discord.SelectOption(label=f"{current} (hiện tại)", value=current, description="Giá trị đang dùng (nhập tay)", default=True))
        for v, label, desc in item["values"]:
            options.append(discord.SelectOption(label=label, value=v, description=desc[:100], default=(v == current)))
        spec = item.get("custom")
        if spec:
            if spec.get("money"):
                hint = f"Tự gõ số tiền {spec['min']:,}–{spec['max']:,} (vd 15000 hoặc 15k)"
            else:
                hint = f"Tự gõ số {spec['unit']} từ {spec['min']} đến {spec['max']}"
            options.append(discord.SelectOption(label="Nhập tùy chỉnh...", value=CUSTOM_VALUE, emoji="✏️", description=hint[:100]))
        super().__init__(placeholder=f"{item['emoji']} Chọn giá trị mới", options=options[:25])
        self.layout = layout
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == CUSTOM_VALUE:
            await interaction.response.send_modal(CustomValueModal(self.layout, self.item))
            return
        self.item["set"](value)
        self.layout.note = f"✅ Đã đặt **{self.item['label']}** = **{_label_of(self.item, value)}**"
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class ConfigLayoutView(TxaView):
    admin_only = True

    def __init__(self, user_id: int, back=None):
        super().__init__(user_id, back)
        self.page = "home"
        self.note = None
        self.build()

    def _summary(self) -> str:
        on = lambda v: "Bật ✅" if v else "Tắt ❌"
        timeout = get_transaction_timeout()
        mapped = getattr(config, "SERVER_CHANNELS", None) or {}
        if mapped:
            shown = " ".join(f"<#{c}>" for c in list(mapped.values())[:4])
            channel = f"{len(mapped)} server — {shown}" + (" …" if len(mapped) > 4 else "")
        else:
            channel = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "Chưa cấu hình"
        return (
            f"📺 Kênh hoạt động: {channel}\n"
            f"👑 Admin IDs: `{', '.join(map(str, config.ADMIN_IDS))}`\n"
            f"🔑 SePay: {'Đã cấu hình ✅' if config.SEPAY_API_KEY else 'Chưa cấu hình ❌'}\n\n"
            f"**⚙️ Hệ thống**\n"
            f"🔄 Quét quest: `{config.POLL_INTERVAL:02d}s` • 💓 Heartbeat: `{config.HEARTBEAT_INTERVAL:02d}s`\n"
            f"📥 Tự nhận quest: {on(config.AUTO_ACCEPT)} • 📝 Log: {on(config.LOG_PROGRESS)} • 🐞 Debug: {on(config.DEBUG)}\n\n"
            f"**💰 Quest & Thanh toán**\n"
            f"💵 Đơn giá: `{get_price_per_quest():,} VNĐ`/lượt • ⏳ Hạn QR: `{timeout // 60:02d} phút`\n"
            f"👑 Admin vô hạn lượt: {on(getattr(config, 'ADMIN_BYPASS_LIMIT', True))}"
        )

    def build(self):
        self.clear_items()
        groups = _settings()
        items = []
        if self.page == "home" or self.page not in groups:
            self.page = "home"
            items += [f"### ⚙️ Cài đặt Chung {config.EMOJI_ROCKET}", self.note, self._summary(), "---", "**Chọn nhóm cài đặt**"]
            row = discord.ui.ActionRow()
            row.add_item(GroupSelect(self, groups))
            items += [row, "-# Chọn nhóm rồi chỉnh trực tiếp bằng menu. Thay đổi áp dụng ngay và được lưu lại."]
        else:
            group = groups[self.page]
            items += [f"### {group['emoji']} {group['label']}", group["desc"], self.note]
            for item in group["items"]:
                row = discord.ui.ActionRow()
                row.add_item(SettingSelect(self, item))
                items += ["---", f"**{item['emoji']} {item['label']}**", row, f"-# {item['desc']}"]
        self.add_item(box(*items))

        back_ok = self.page != "home" or self.back is not None
        self.add_item(row_of(
            btn("Quay lại", "◀️", self._on_back, disabled=not back_ok),
            btn("Làm mới", "🔄", self._on_refresh),
            btn("Trợ giúp", "❓", self._on_help),
        ))

    async def _on_back(self, interaction: discord.Interaction):
        if self.page != "home":
            self.page, self.note = "home", None
            self.build()
            await interaction.response.edit_message(view=self)
        else:
            await self.go_back(interaction)

    async def _on_refresh(self, interaction: discord.Interaction):
        self.note = None
        self.build()
        await interaction.response.edit_message(view=self)

    async def _on_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "ℹ️ **Cách dùng:** chọn một nhóm ở trang chính, rồi đổi giá trị bằng menu thả xuống của từng mục. "
            "Thay đổi áp dụng ngay lập tức và được lưu vào `.env` hoặc `user_limits.json`. "
            "Dùng **Quay lại** để về trang trước.",
            ephemeral=True
        )


# ───────────────────────── Trạng thái hệ thống (Status) ─────────────────────────

class StopSessionSelect(discord.ui.Select):
    def __init__(self, layout, bot):
        options = []
        for uid, info in list(config.ACTIVE_USERS.items())[:25]:
            completer = info.get("completer")
            curr = getattr(completer, "current_quest_info", None) if completer else None
            if curr:
                needed = curr.get("needed", 1)
                pct = int(curr.get("done", 0) / needed * 100) if needed else 0
                desc = f"Đang làm {str(curr.get('name', 'Quest'))[:30]} ({pct}%)"
            else:
                desc = "Đang quét nhiệm vụ"
            options.append(discord.SelectOption(
                label=f"{str(info.get('username', 'Unknown'))[:40]} ({uid})"[:100],
                value=str(uid), description=desc[:100], emoji="🛑"
            ))
        super().__init__(placeholder="🛑 Chọn phiên để dừng ngay lập tức", options=options)
        self.layout = layout
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        uid = int(self.values[0])
        info = config.ACTIVE_USERS.pop(uid, None)
        if info:
            try:
                await info["completer"].stop()
                if info.get("task"):
                    info["task"].cancel()
                user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
                if user:
                    await user.send("🛑 **Tiến trình làm quest của bạn đã bị dừng bởi quản trị viên.**")
            except Exception:
                pass
            try:
                await self.bot.update_control_panel()
            except Exception:
                pass
            self.layout.note = f"🛑 Đã dừng phiên của **{info.get('username')}** (`{uid}`)"
        else:
            self.layout.note = "ℹ️ Phiên này đã kết thúc trước đó."
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class StatusLayoutView(TxaView):
    admin_only = True

    def __init__(self, user_id: int, bot, bot_start_time: float, back=None):
        super().__init__(user_id, back)
        self.bot = bot
        self.bot_start_time = bot_start_time
        self.note = None
        self.build()

    def build(self):
        from limits_manager import load_limits, get_pending_transactions, get_price_per_quest
        from completer import get_progress_bar
        from system_info import get_process_memory
        self.clear_items()
        users = config.ACTIVE_USERS
        count = len(users)
        data = load_limits()
        done = data.get("total_completed", config.TOTAL_COMPLETED)
        failed = data.get("total_failed", config.TOTAL_FAILED)
        pending = len(get_pending_transactions())
        on = lambda v: "✅ Bật" if v else "❌ Tắt"

        items = [
            f"### 📡 Bảng Điều Khiển Admin {config.EMOJI_ROCKET}",
            self.note,
            f"{'🟢' if count else '⚪'} **{count:02d}** phiên đang chạy\n"
            f"✅ **{done:02d}** thành công  •  ❌ **{failed:02d}** thất bại  •  🧾 **{pending:02d}** đơn chờ thanh toán",
            "---",
        ]
        if not users:
            items.append("**🏃 Phiên đang chạy**\n💤 *Chưa có tài khoản nào hoạt động.*")
        else:
            for uid, info in list(users.items())[:5]:
                completer = info.get("completer")
                curr = getattr(completer, "current_quest_info", None) if completer else None
                if curr:
                    d, n = curr.get("done", 0), curr.get("needed", 1)
                    body = f"🎯 **{curr.get('name', 'Quest')}**\n`{get_progress_bar(d, n)}`  ⏳ `{int(d):02d}s/{int(n):02d}s`"
                else:
                    body = "🔍 *Đang quét / chờ nhiệm vụ...*"
                run_s = int(time.time() - info.get("start_timestamp", time.time()))
                g = self.bot.get_guild(info.get("guild_id")) if info.get("guild_id") else None
                where = f"🏠 {g.name}" if g else "🏠 (không rõ server)"
                items.append(
                    f"**🟢 {info.get('username')}** • <@{uid}> • {where}\n{body}\n"
                    f"🕒 Bắt đầu <t:{info.get('start_timestamp', 0)}:R> • đã chạy `{run_s // 3600:02d}h {run_s % 3600 // 60:02d}m`"
                )
            if count > 5:
                items.append(f"-# … và {count - 5:02d} phiên khác")
            items.append("---")
            row = discord.ui.ActionRow()
            row.add_item(StopSessionSelect(self, self.bot))
            items += ["**🛑 Dừng phiên**", row]

        uptime = int(time.time() - self.bot_start_time)
        timeout = get_transaction_timeout()
        items += [
            "---",
            f"**💳 Thanh toán** • `{get_price_per_quest():,}đ`/lượt • hạn QR `{timeout // 60:02d}p` • SePay {'✅' if config.SEPAY_API_KEY else '❌'}\n"
            f"**⚙️ Vận hành** • quét `{config.POLL_INTERVAL:02d}s` • heartbeat `{config.HEARTBEAT_INTERVAL:02d}s` • "
            f"tự nhận {on(config.AUTO_ACCEPT)} • Admin bypass {on(getattr(config, 'ADMIN_BYPASS_LIMIT', True))}\n"
            f"**🤖 Bot** • ping `{round(self.bot.latency * 1000)} ms` • uptime `{uptime // 3600:02d}h {uptime % 3600 // 60:02d}m` • RAM `{get_process_memory()} MB`",
        ]
        self.add_item(box(*items, accent=GREEN if count else ACCENT))

        async def refresh(interaction: discord.Interaction):
            self.note = None
            self.build()
            await interaction.response.edit_message(view=self)

        async def open_config(interaction: discord.Interaction):
            await self.go(interaction, ConfigLayoutView(self.user_id, back=lambda i: self))

        async def open_system(interaction: discord.Interaction):
            view = SystemLayoutView(self.user_id, self.bot, self.bot_start_time, back=lambda i: self)
            await self.go(interaction, view)
            view.start(interaction)

        self.add_item(row_of(
            btn("Làm mới", "🔄", refresh),
            btn("Cấu Hình", "⚙️", open_config),
            btn("Hệ Thống", "🖥️", open_system),
        ))


# ───────────────────────── Giám sát hệ thống (realtime) ─────────────────────────

class SystemLayoutView(TxaView):
    REFRESH_SECONDS = 3
    MAX_LIVE_SECONDS = 14 * 60  # token interaction sống 15 phút

    def __init__(self, user_id: int, bot, bot_start_time: float, back=None):
        super().__init__(user_id, back, timeout=None)
        self.bot = bot
        self.bot_start_time = bot_start_time
        self.live = True
        self._interaction = None
        self.build()

    def on_leave(self):
        self.live = False

    def build(self):
        from system_info import (
            get_cpu_info, get_ram_info, get_disk_info, get_process_memory, get_os_info,
            get_active_sessions_count, get_realtime_quest_stats, slider_bar, load_icon,
        )
        import platform
        self.clear_items()
        up = int(time.time() - self.bot_start_time)
        days, rem = divmod(up, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = (f"{days:02d}d " if days else "") + f"{hours:02d}:{mins:02d}:{secs:02d}"

        cpu, ram, disk = get_cpu_info(), get_ram_info(), get_disk_info()
        latency = round(self.bot.latency * 1000) if self.bot.latency is not None else 0
        ok, bad = get_realtime_quest_stats()
        worst = max(cpu["percent"], ram["percent"], disk["percent"])
        accent = GREEN if worst < 60 else YELLOW if worst < 85 else RED
        ping_icon = "🟢" if latency < 150 else "🟡" if latency < 400 else "🔴"

        status = "🔴 **LIVE** • tự cập nhật mỗi 03 giây" if self.live else "⏸️ Đã tạm dừng cập nhật"
        self.add_item(box(
            f"### ⚡ System Monitor {config.EMOJI_LOADING}",
            f"{status}\n🕒 Online từ <t:{int(self.bot_start_time)}:R> • Uptime **{uptime_str}**",
            f"```\nCPU   {cpu['percent']:>5.1f}%  {slider_bar(cpu['percent'])}\n"
            f"RAM   {ram['percent']:>5.1f}%  {slider_bar(ram['percent'])}\n"
            f"DISK  {disk['percent']:>5.1f}%  {slider_bar(disk['percent'])}\n```",
            "---",
            f"{load_icon(cpu['percent'])} **CPU** • {cpu['cores']:02d} nhân • `{str(cpu['model'])[:40]}`\n"
            f"{load_icon(ram['percent'])} **RAM** • {ram['used_gb']}/{ram['total_gb']} GB • Bot dùng `{get_process_memory()} MB`\n"
            f"{load_icon(disk['percent'])} **Ổ đĩa** • {disk['used_gb']}/{disk['total_gb']} GB",
            f"{ping_icon} **Ping** `{latency} ms` • 👥 **Phiên** `{get_active_sessions_count():02d}` • 🎯 **Quest** ✅ `{ok:02d}` ❌ `{bad:02d}`",
            f"-# {get_os_info()} • Python {platform.python_version()} • discord.py {discord.__version__} • cập nhật {time.strftime('%H:%M:%S')}",
            accent=accent,
        ))

        async def toggle(interaction: discord.Interaction):
            self.live = not self.live
            self.build()
            await interaction.response.edit_message(view=self)
            if self.live:
                self.start(interaction)

        async def refresh(interaction: discord.Interaction):
            self.build()
            await interaction.response.edit_message(view=self)

        self.add_item(self.nav_row(
            refresh=refresh,
            extra=[btn("Tạm Dừng" if self.live else "Tiếp Tục Live", "⏸️" if self.live else "▶️", toggle,
                       style=discord.ButtonStyle.secondary if self.live else discord.ButtonStyle.success)],
        ))

    def start(self, interaction: discord.Interaction):
        """Gọi sau khi gửi/sửa tin nhắn để bắt đầu vòng cập nhật realtime."""
        self._interaction = interaction
        asyncio.create_task(self._loop())

    async def _loop(self):
        began = time.time()
        try:
            while self.live and time.time() - began < self.MAX_LIVE_SECONDS:
                await asyncio.sleep(self.REFRESH_SECONDS)
                if not self.live:
                    return
                self.build()
                await self._interaction.edit_original_response(view=self)
        except Exception:
            pass
        if self.live:  # hết thời gian sống của token
            self.live = False
            try:
                self.build()
                await self._interaction.edit_original_response(view=self)
            except Exception:
                pass


# ───────────────────────── Thống kê nhanh (history / leaderboard / ping) ─────────────────────────

def _history_content(user_id: int, limit: int = 10):
    from limits_manager import get_payment_history
    rows = get_payment_history(user_id)
    if not rows:
        return "🧾 Lịch Sử Nạp Lượt", "Bạn chưa có giao dịch nào. Dùng `/buy` để mua lượt đầu tiên!"
    lines = [f"`{r['order_id']}` • **{r['amount']:,}đ** • +{r['quests']:02d} lượt • <t:{r['paid_at']}:R>" for r in reversed(rows[-limit:])]
    return "🧾 Lịch Sử Nạp Lượt", "\n".join(lines) + f"\n\n💰 Tổng đã nạp: **{sum(r['amount'] for r in rows):,} VNĐ**"


def _leaderboard_content():
    from limits_manager import get_leaderboard
    rows = get_leaderboard(10)
    medals = ["🥇", "🥈", "🥉"]
    if not rows:
        return "🏆 Bảng Xếp Hạng Quest", "Chưa có ai hoàn thành quest."
    body = "\n".join(f"{medals[i] if i < 3 else f'`#{i + 1:02d}`'} <@{u}> — **{t:02d}** quest" for i, (u, t) in enumerate(rows))
    return "🏆 Bảng Xếp Hạng Quest", body + f"\n\n🎯 Tổng toàn hệ thống: **{config.TOTAL_COMPLETED:02d}** quest"


def _quota_content(user_id: int):
    from limits_manager import get_user_quota_info
    info = get_user_quota_info(user_id)
    return "💰 Số Dư Của Bạn", f"💎 Lượt khả dụng: **{info['quota_str']}**\n🏆 Đã hoàn thành: `{info['total_completed']:02d} nhiệm vụ`"


def history_view(user_id, back=None, limit: int = 10):
    return SimpleLayoutView(user_id, *_history_content(user_id, limit), accent=YELLOW, back=back,
                            refresh=lambda: _history_content(user_id, limit))


def leaderboard_view(user_id, back=None):
    return SimpleLayoutView(user_id, *_leaderboard_content(), accent=YELLOW, back=back, refresh=_leaderboard_content)


def quota_view(user_id, back=None):
    return SimpleLayoutView(user_id, *_quota_content(user_id), accent=GREEN, back=back,
                            refresh=lambda: _quota_content(user_id))


def ping_view(user_id, bot, back=None):
    def content():
        ms = round(bot.latency * 1000)
        return "🏓 Pong!", f"Độ trễ gateway: **{ms} ms**\nPhiên đang chạy: **{len(config.ACTIVE_USERS):02d}**"
    ms = round(bot.latency * 1000)
    accent = GREEN if ms < 150 else ORANGE if ms < 400 else RED
    return SimpleLayoutView(user_id, *content(), accent=accent, back=back, refresh=content)


# ───────────────────────── Trợ giúp (Help) ─────────────────────────

HELP_GROUPS = {
    "quest": ("Làm Quest", "🚀", "Bắt đầu, theo dõi và dừng phiên làm quest tự động.",
              ["start", "details", "my_quests", "user_info", "stop_my_session"]),
    "pay": ("Lượt & Thanh Toán", "💎", "Mua lượt (VietQR tự động cộng), xem số dư và lịch sử nạp.",
            ["menu", "buy", "history"]),
    "info": ("Thông Tin", "📊", "Bảng xếp hạng, độ trễ, thông số máy chủ và link mời bot.",
             ["leaderboard", "ping", "system_stats", "invite", "help"]),
    "admin": ("Admin", "🛠️", "Quản trị: trạng thái, cấu hình, số dư người dùng, giá và kênh hoạt động.",
              ["status", "config", "set_limit", "add_balance", "set_price", "set_timeout",
               "stop_user", "channel", "setup_here", "clear_cache", "qr_test"]),
}


def _command_info(name: str):
    """Lấy mô tả + tham số của lệnh trực tiếp từ cây lệnh của bot (luôn đúng với code)."""
    try:
        import bot as botmod
        cmd = botmod.bot.tree.get_command(name)
    except Exception:
        cmd = None
    if not cmd:
        return "", []
    params = [(p.name, p.description or "", p.required) for p in getattr(cmd, "parameters", [])]
    return cmd.description or "", params


class HelpGroupSelect(discord.ui.Select):
    def __init__(self, layout, groups):
        options = [
            discord.SelectOption(label=g[0], value=k, emoji=g[1], description=g[2][:100])
            for k, g in groups.items()
        ]
        super().__init__(placeholder="📂 Nhấn vào đây để chọn nhóm lệnh", options=options)
        self.layout = layout

    async def callback(self, interaction: discord.Interaction):
        self.layout.group, self.layout.command = self.values[0], None
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class HelpCommandSelect(discord.ui.Select):
    def __init__(self, layout, names, selected):
        options = []
        for n in names:
            desc, _ = _command_info(n)
            options.append(discord.SelectOption(label=f"/{n}", value=n, description=(desc or "—")[:100], default=(n == selected)))
        super().__init__(placeholder="🔎 Chọn một lệnh để xem chi tiết", options=options[:25])
        self.layout = layout

    async def callback(self, interaction: discord.Interaction):
        self.layout.command = self.values[0]
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class HelpLayoutView(TxaView):
    def __init__(self, user_id: int, back=None):
        super().__init__(user_id, back)
        self.group = None
        self.command = None
        self.build()

    def _groups(self):
        is_admin = self.user_id in config.ADMIN_IDS
        return {k: g for k, g in HELP_GROUPS.items() if k != "admin" or is_admin}

    def build(self):
        self.clear_items()
        groups = self._groups()
        if self.group not in groups:
            self.group, self.command = None, None

        if self.group is None:
            total = sum(len(g[3]) for g in groups.values())
            row = discord.ui.ActionRow()
            row.add_item(HelpGroupSelect(self, groups))
            items = [
                f"### 📖 Trung Tâm Trợ Giúp • Quest Bot {config.EMOJI_ROCKET}",
                "Tự động quét • nhận • hoàn thành Discord Quest.",
                "\n".join(f"{g[1]} **{g[0]}** • {len(g[3]):02d} lệnh\n-# {g[2]}" for g in groups.values()),
                "---",
                "**Chọn nhóm lệnh**",
                row,
                f"-# Tổng cộng {total:02d} lệnh • Quest Bot v4.0 • Built by TXA",
            ]
        else:
            label, emoji, desc, names = groups[self.group]
            lines = []
            for n in names:
                d, _ = _command_info(n)
                lines.append(f"`/{n}` — {d or '—'}")
            row = discord.ui.ActionRow()
            row.add_item(HelpCommandSelect(self, names, self.command))
            items = [f"### {emoji} {label}", desc, "\n".join(lines), "---"]
            if self.command:
                d, params = _command_info(self.command)
                admin_cmd = self.command in HELP_GROUPS["admin"][3]
                detail = f"**/{self.command}**\n{d or '—'}\n👤 Ai dùng được: {'🛠️ Chỉ Admin' if admin_cmd else '🌐 Mọi người'}"
                if params:
                    detail += "\n**Tham số:**\n" + "\n".join(
                        f"• `{n}`{'' if req else ' (tuỳ chọn)'} — {pd or '—'}" for n, pd, req in params)
                else:
                    detail += "\n**Tham số:** không có"
                items += [detail, "---"]
            items += ["**Xem chi tiết**", row]
        self.add_item(box(*items))

        async def go_home(interaction: discord.Interaction):
            self.group, self.command = None, None
            self.build()
            await interaction.response.edit_message(view=self)

        async def open_ext(interaction: discord.Interaction):
            from bot import send_extension_guide
            await send_extension_guide(interaction)

        async def open_buy(interaction: discord.Interaction):
            from payment_views import get_limit_embed_and_view, register_invoice
            _, view = get_limit_embed_and_view(self.user_id, back=lambda i: self)
            await self.go(interaction, view)
            register_invoice(interaction, view)

        buttons = []
        if self.group is not None:
            buttons.append(btn("Quay lại", "◀️", go_home))
        elif self.back:
            buttons.append(btn("Quay lại", "◀️", self._on_back_click))
        buttons += [
            btn("Tải Tiện Ích", "📦", open_ext, style=discord.ButtonStyle.primary),
            btn("Mua Lượt", "💳", open_buy, style=discord.ButtonStyle.success),
        ]
        buttons += link_buttons("invite", "quest")
        self.add_item(row_of(*buttons))


# ───────────────────────── Hướng dẫn cài tiện ích lấy token ─────────────────────────

EXT_FILENAME = "TXA_Discord_Token_Retriever.crx"


class ExtensionGuideView(TxaView):
    """Hướng dẫn cài tiện ích bằng cách đổi đuôi .crx -> .zip, giải nén rồi nạp thủ công."""

    def __init__(self, user_id: int, has_file: bool = True):
        super().__init__(user_id, timeout=900)
        steps = (
            "**1️⃣ Tải file tiện ích**\n"
            "Tải file `TXA_Discord_Token_Retriever.crx` ở ngay bên dưới.\n\n"
            "**2️⃣ Đổi đuôi file từ `.crx` thành `.zip`**\n"
            "Đổi tên thành `TXA_Discord_Token_Retriever.zip`.\n"
            "-# Windows chưa thấy đuôi file? Mở File Explorer → tab **Xem** → tích **Phần mở rộng tên tệp**.\n\n"
            "**3️⃣ Giải nén ra một thư mục**\n"
            "Chuột phải file `.zip` → **Extract All / Giải nén tại đây**.\n"
            "-# ⚠️ Đừng xóa hay di chuyển thư mục này sau khi cài, nếu không tiện ích sẽ mất.\n\n"
            "**4️⃣ Mở trang quản lý tiện ích & bật Developer mode**\n"
            "Chrome: `chrome://extensions` • Edge: `edge://extensions`\n"
            "Bật công tắc **Chế độ nhà phát triển (Developer mode)** ở góc trên bên phải.\n\n"
            "**5️⃣ Nạp tiện ích**\n"
            "Bấm **Tải tiện ích đã giải nén (Load unpacked)** → chọn **thư mục vừa giải nén** ở bước 3.\n\n"
            "**6️⃣ Lấy token**\n"
            "Vào [discord.com](https://discord.com) và đăng nhập → bấm icon tiện ích để mở **Side Panel** → "
            "**Lấy & Sao Chép Token** → dán token vào nút 🚀 **Bắt Đầu** trong kênh của bot."
        )
        items = [
            f"### 📦 Cài Tiện Ích Lấy Token {config.EMOJI_LOADING}",
            "**TXA Discord Token Retriever** — lấy token nhanh chỉ với một cú bấm.\n"
            "Trình duyệt không cài trực tiếp được file `.crx`, nên hãy cài thủ công theo 06 bước dưới đây (khoảng 01 phút).",
            "---",
            steps,
        ]
        if has_file:
            items += ["---", "**📥 File tiện ích**", discord.ui.File(f"attachment://{EXT_FILENAME}")]
        items += [
            "---",
            "💡 **Mẹo:** nếu Windows báo lỗi khi giải nén, hãy dùng **7-Zip** hoặc **WinRAR**.\n"
            "🔒 Tiện ích chỉ đọc cookie tạm thời trên máy bạn và không gửi dữ liệu ra bên ngoài. **Không chia sẻ token cho ai.**",
        ]
        self.add_item(box(*items))

        async def show(interaction: discord.Interaction, text: str):
            await interaction.response.send_message(f"Sao chép dòng dưới rồi dán vào thanh địa chỉ:\n```\n{text}\n```", ephemeral=True)

        async def chrome(interaction: discord.Interaction):
            await show(interaction, "chrome://extensions")

        async def edge(interaction: discord.Interaction):
            await show(interaction, "edge://extensions")

        self.add_item(row_of(
            btn("chrome://extensions", "🌐", chrome),
            btn("edge://extensions", "🧭", edge),
            discord.ui.Button(label="Mở Discord Web", emoji="💬", style=discord.ButtonStyle.link, url="https://discord.com/app"),
        ))


# ───────────────────────── Kết quả nhận quà quest ─────────────────────────

def claim_result_view(user_id: int, kind: str, quest_name: str, code: str = None, detail: str = None) -> TxaView:
    """Màn hình kết quả khi bấm nhận quà: kind = 'success' | 'captcha' | 'error'."""
    view = TxaView(user_id, timeout=600)
    link_row = row_of(
        discord.ui.Button(label="Mở trang Quest", emoji="🎁", style=discord.ButtonStyle.link, url=QUEST_HOME_URL),
        discord.ui.Button(label="Mở Discord Web", emoji="💬", style=discord.ButtonStyle.link, url="https://discord.com/app"),
    )
    if kind == "success":
        body = (f"🔑 **Mã quà tặng (Promo Code):**\n```\n{code}\n```\n💡 Sao chép mã trên rồi kích hoạt trong **Gift Inventory** hoặc trong game."
                if code else "✨ Phần thưởng đã được kích hoạt trực tiếp vào tài khoản Discord của bạn!")
        view.add_item(box(f"### 🎉 Nhận Quà Thành Công! {config.EMOJI_SUCCESS}", f"🎯 **Nhiệm vụ:** {quest_name}", "---", body, accent=GREEN))
    elif kind == "captcha":
        view.add_item(box(
            f"### 🛡️ Cần Bạn Bấm Nhận Trên Discord {config.EMOJI_LOADING}",
            f"🎯 **Nhiệm vụ:** {quest_name}\n🎉 Tiến độ đã hoàn thành **100%** — quà vẫn nằm an toàn trên tài khoản của bạn.",
            "---",
            "**❓ Vì sao bot không tự nhận được?**\n"
            "Khi nhận thưởng, Discord bắt buộc **xác minh hCaptcha** (chống bot). Đây là bước chỉ người thật mới làm được — "
            "bot **không thể và không được phép** vượt qua nó, nên nút nhận quà phải bấm thủ công **một lần duy nhất** trên Discord.",
            "---",
            "**⚡ Nhận quà trong 10 giây**\n"
            "1️⃣ Bấm **Mở trang Quest** bên dưới (hoặc mở Discord → **Quests**).\n"
            "2️⃣ Tìm nhiệm vụ vừa xong → bấm **Nhận Thưởng (Claim Reward)**.\n"
            "3️⃣ Nếu hiện bảng chọn hình, chọn đúng 1 lần là nhận ngay mã hoặc vật phẩm.",
            "-# 💡 Nếu không thấy nút Nhận Thưởng: vào User Settings → Gift Inventory.",
            accent=YELLOW,
        ))
        view.add_item(link_row)
        return view
    else:
        view.add_item(box(
            f"### Không Thể Nhận Quà {config.EMOJI_FAIL}",
            f"🎯 **Nhiệm vụ:** {quest_name}",
            f"```\n{(detail or 'Lỗi không xác định')[:500]}\n```",
            "Hãy thử lại sau ít phút, hoặc nhận trực tiếp trên Discord.",
            accent=RED,
        ))
    view.add_item(link_row)
    return view


# ───────────────────────── Thẻ kết quả chung ─────────────────────────

def text_card(user_id: int, title: str, body: str, accent=GREEN, footer: str = None, links=()) -> TxaView:
    view = TxaView(user_id, timeout=300)
    view.add_item(box(f"### {title}", body, f"-# {footer}" if footer else None, accent=accent))
    btns = link_buttons(*links)
    if btns:
        view.add_item(row_of(*btns))
    return view


def embed_to_view(embed: discord.Embed, user_id: int, links=()) -> TxaView:
    """Chuyển một embed cũ thành thẻ Components V2 (giữ tiêu đề, mô tả, các trường, chân trang và màu)."""
    lines = []
    for f in embed.fields:
        lines.append(f"**{f.name}**\n{f.value}")
    accent = embed.color if embed.color else ACCENT
    footer = embed.footer.text if embed.footer and embed.footer.text else None
    view = TxaView(user_id, timeout=300)
    view.add_item(box(
        f"### {embed.title or 'Thông báo'}",
        embed.description or None,
        "---" if lines and embed.description else None,
        "\n\n".join(lines) if lines else None,
        f"-# {footer}" if footer else None,
        accent=accent,
    ))
    btns = link_buttons(*links)
    if btns:
        view.add_item(row_of(*btns))
    return view


def invite_view(user_id: int) -> TxaView:
    view = TxaView(user_id, timeout=300)
    view.add_item(box(
        f"### ➕ Thêm Quest Bot Vào Server Của Bạn {config.EMOJI_ROCKET}",
        "Nhấn nút **Mời Bot** bên dưới để đưa bot vào máy chủ Discord của bạn.",
        "---",
        "**✨ Bạn không cần cài đặt gì thêm**\n"
        "• Ngay khi vào server, bot **tự động tìm kênh phù hợp nhất** (ưu tiên kênh tên có `quest`, `bot`, `auto`…) và tạo **Bảng điều khiển** ở đó.\n"
        "• Mọi người chỉ cần bấm 🚀 **Bắt Đầu** trên bảng điều khiển, hoặc gõ `/help` để xem toàn bộ lệnh.\n"
        "• Nếu không thấy bảng điều khiển, hãy kiểm tra bot có quyền **Xem kênh, Gửi tin nhắn, Nhúng liên kết, Đọc lịch sử** — bot sẽ nhắn riêng cho chủ server nếu thiếu quyền.",
        "-# Bot chỉ xin các quyền cần thiết để gửi tin nhắn và tạo bảng điều khiển.",
        accent=ACCENT,
    ))
    btns = link_buttons("invite", "quest", "discord")
    if btns:
        view.add_item(row_of(*btns))
    return view


# ───────────────────────── Menu trung tâm (/menu) ─────────────────────────

def build_tree():
    from limits_manager import PACKAGES, get_package_info
    return {
        "buy": {
            "label": "Mua lượt Quest", "emoji": "💎", "desc": "Nạp lượt làm quest bằng VietQR",
            "children": {
                str(n): {
                    "label": f"{n:02d} lượt - {info['final']:,}đ",
                    "emoji": PACKAGE_EMOJI.get(n, "💎"),
                    "desc": (f"Giảm {info['pct']}% • tiết kiệm {info['saved']:,}đ" if info["pct"] else "Mua lẻ, không giảm giá"),
                    "children": {"qr": {"label": "Thanh toán VietQR", "emoji": "🏦", "desc": "Quét mã, tự động cộng lượt"}},
                }
                for n, info in ((n, get_package_info(n)) for n in PACKAGES)
            },
        },
        "stats": {
            "label": "Thống kê", "emoji": "📊", "desc": "Số dư, lịch sử, xếp hạng",
            "children": {
                "quota": {"label": "Số dư của tôi", "emoji": "💰", "desc": "Xem lượt khả dụng"},
                "history": {
                    "label": "Lịch sử nạp", "emoji": "🧾", "desc": "Các giao dịch gần đây",
                    "children": {
                        "5": {"label": "05 giao dịch gần nhất", "emoji": "5️⃣", "desc": "Gọn, xem nhanh"},
                        "10": {"label": "10 giao dịch gần nhất", "emoji": "🔟", "desc": "Đầy đủ hơn"},
                    },
                },
                "top": {"label": "Bảng xếp hạng", "emoji": "🏆", "desc": "Top người làm nhiều quest nhất"},
            },
        },
        "help": {
            "label": "Hỗ trợ", "emoji": "🛠️", "desc": "Hướng dẫn & liên hệ",
            "children": {
                "guide": {"label": "Cài tiện ích lấy token", "emoji": "📦", "desc": "Hướng dẫn + tải file .crx"},
                "how": {"label": "Cách hoạt động", "emoji": "❓", "desc": "Bot làm quest như thế nào"},
            },
        },
    }


class MenuLevelSelect(discord.ui.Select):
    def __init__(self, layout, level: int, placeholder: str, nodes: dict, selected):
        options = [
            discord.SelectOption(label=n["label"], value=k, emoji=n.get("emoji"),
                                 description=(n.get("desc") or None), default=(k == selected))
            for k, n in nodes.items()
        ]
        super().__init__(placeholder=placeholder, options=options)
        self.layout = layout
        self.level = level

    async def callback(self, interaction: discord.Interaction):
        self.layout.picks = self.layout.picks[: self.level] + [self.values[0]]
        self.layout.build()
        await interaction.response.edit_message(view=self.layout)


class MenuLayoutView(TxaView):
    HEADINGS = ["**① Danh mục**", "**② Mục con**", "**③ Tuỳ chọn**"]
    PLACEHOLDERS = ["Chọn danh mục...", "Chọn mục con...", "Chọn tuỳ chọn..."]

    def __init__(self, user_id: int, back=None):
        super().__init__(user_id, back)
        self.tree = build_tree()
        self.picks = []
        self.build()

    def _path(self):
        nodes, level = [], self.tree
        for key in self.picks:
            node = level.get(key)
            if not node:
                break
            nodes.append(node)
            level = node.get("children") or {}
        return nodes

    def _ready(self) -> bool:
        nodes = self._path()
        return bool(nodes) and (len(self.picks) == 3 or not nodes[-1].get("children"))

    def build(self):
        self.clear_items()
        nodes = self._path()
        crumbs = " ➜ ".join(f"{n.get('emoji', '')} {n['label']}" for n in nodes) or "*Chưa chọn gì*"
        items = [f"### 🧭 Trung Tâm Điều Khiển Quest Bot {config.EMOJI_ROCKET}",
                 "Chọn lần lượt từng mục. Menu sau sẽ đổi theo lựa chọn trước đó.",
                 f"**📍 Lựa chọn của bạn:** {crumbs}", "---"]
        level_nodes = self.tree
        for level in range(3):
            if not level_nodes:
                break
            selected = self.picks[level] if level < len(self.picks) else None
            row = discord.ui.ActionRow()
            row.add_item(MenuLevelSelect(self, level, self.PLACEHOLDERS[level], level_nodes, selected))
            items += [self.HEADINGS[level], row]
            if selected is None:
                break
            level_nodes = (level_nodes.get(selected) or {}).get("children")
        items.append("-# ✅ Đã sẵn sàng — bấm Xác Nhận" if self._ready() else "-# Hãy chọn tiếp ở menu mới vừa xuất hiện")
        self.add_item(box(*items))

        async def reset(interaction: discord.Interaction):
            self.picks = []
            self.build()
            await interaction.response.edit_message(view=self)

        self.add_item(self.nav_row(extra=[
            btn("Xác Nhận", config.EMOJI_SUCCESS, self._confirm, style=discord.ButtonStyle.success, disabled=not self._ready()),
            btn("Đặt Lại", "🔄", reset, disabled=not self.picks),
        ]))

    async def _confirm(self, interaction: discord.Interaction):
        from limits_manager import create_transaction
        uid, top = self.user_id, self.picks[0]
        me = lambda i: self
        if top == "buy":
            from payment_views import get_limit_embed_and_view, register_invoice
            if not create_transaction(uid, int(self.picks[1])):
                await interaction.response.send_message(
                    "❌ Bạn đang có một hóa đơn chưa hoàn tất. Dùng `/buy` để xem hoặc hủy nó.", ephemeral=True)
                return
            _, view = get_limit_embed_and_view(uid, back=me)
            await self.go(interaction, view)
            register_invoice(interaction, view)
            return
        if top == "stats":
            sub = self.picks[1]
            view = (quota_view(uid, back=me) if sub == "quota"
                    else history_view(uid, back=me, limit=int(self.picks[2])) if sub == "history"
                    else leaderboard_view(uid, back=me))
            await self.go(interaction, view)
            return
        if self.picks[1] == "guide":
            from bot import send_extension_guide
            await send_extension_guide(interaction)
            return
        view = SimpleLayoutView(
            uid, "❓ Cách Hoạt Động",
            "1️⃣ Lấy token bằng tiện ích của TXA.\n"
            "2️⃣ Bấm 🚀 **Bắt Đầu** ở kênh bot và dán token.\n"
            "3️⃣ Bot tự **quét • nhận • hoàn thành** quest và báo tiến độ qua DM.\n"
            "4️⃣ Mỗi ngày có 01 lượt free, mua thêm tại `/menu` hoặc `/buy`.",
            back=me,
        )
        await self.go(interaction, view)
