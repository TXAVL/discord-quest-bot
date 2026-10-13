import os
import json
import asyncio
import time
import platform
import sys
from datetime import datetime
import discord
from discord.ext import commands
from discord import app_commands
from logger import log, Colors
import config
from discord_client import DiscordAPI, fetch_latest_build_number
from completer import QuestAutocompleter, get_progress_bar
from quest_utils import (
    _get,
    get_user_status,
    get_quest_name,
    get_task_type,
    is_completed,
    is_expired,
    is_enrolled,
    get_seconds_needed,
    get_seconds_done
)

try:
    import psutil
except ImportError:
    psutil = None

bot_start_time = time.time()

# Persistence for channel configuration
CONFIG_FILE = "channel.json"

def load_channel_config():
    if not hasattr(config, 'SERVER_CHANNELS'):
        config.SERVER_CHANNELS = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                config.CHANNEL_ID = data.get("channel_id")
                config.SERVER_CHANNELS = data.get("server_channels", {})
                log(f"Đã tải cấu hình kênh hoạt động: default={config.CHANNEL_ID}, servers={len(config.SERVER_CHANNELS)}", "ok")
        except Exception as e:
            log(f"Không thể tải cấu hình kênh: {e}", "warn")

def save_channel_config(channel_id, guild_id=None):
    try:
        if not hasattr(config, 'SERVER_CHANNELS'):
            config.SERVER_CHANNELS = {}
        config.CHANNEL_ID = channel_id
        if guild_id:
            config.SERVER_CHANNELS[str(guild_id)] = channel_id
        with open(CONFIG_FILE, "w") as f:
            json.dump({
                "channel_id": channel_id,
                "server_channels": config.SERVER_CHANNELS
            }, f, indent=2)
        log(f"Đã lưu cấu hình kênh hoạt động mới: {channel_id} (Guild: {guild_id})", "ok")
    except Exception as e:
        log(f"Không thể lưu cấu hình kênh: {e}", "error")


async def send_extension_guide(interaction: discord.Interaction):
    """Gửi hướng dẫn cài tiện ích và file crx tải về."""
    embed = discord.Embed(
        title="📦 Hướng Dẫn Cài Đặt Tiện Ích Lấy Token",
        color=discord.Color.from_rgb(88, 101, 242),
        description="""
**Bước 1 — Tải file tiện ích xuống**
📥 Nhấn nút **Tải Tiện Ích** ở bên trên để tải file `TXA_Discord_Token_Retriever.crx`.

**Bước 2 — Mở trình quản lý Extension**
Trên trình duyệt Chrome/Edge, mở tab mới và truy cập:
`chrome://extensions`

**Bước 3 — Bật Chế độ Developer**
Góc trên bên phải, bật công tắc **Chế độ Nhà phát triển** (Developer mode) ✅

**Bước 4 — Cài đặt tiện ích**
Kéo thả trực tiếp file `TXA_Discord_Token_Retriever.crx` vào trang `chrome://extensions` để cài đặt.

**Bước 5 — Dùng tiện ích**
1️⃣ Vào trang [discord.com](https://discord.com) và đăng nhập.
2️⃣ Nhấp vào icon tiện ích trên thanh công cụ trình duyệt → **Side Panel** sẽ hiện ra bên phải màn hình.
3️⃣ Bấm **Lấy & Sao Chép Token** — Token tự động được sao chép + hiện thông báo trực tiếp trên Discord!
4️⃣ Dán token vào nút **Bắt Đầu** 🚀 trong kênh này.
        """
    )
    embed.set_footer(text="Tiện ích chỉ đọc cookie tạm thời và không gửi dữ liệu ra bên ngoài. An toàn 100%.")
    
    # Tự động đóng gói bản build mới nhất của CRX trước khi gửi
    import os
    crx_path = os.path.join(os.path.dirname(__file__), "extension.crx")
    make_crx_script = os.path.join(os.path.dirname(__file__), "make_crx.py")
    if os.path.exists(make_crx_script):
        try:
            import make_crx
            make_crx.build_crx()
        except Exception:
            pass

    zip_path = os.path.join(os.path.dirname(__file__), "extension.zip")
    target_path = crx_path if os.path.exists(crx_path) else zip_path
    if os.path.exists(target_path):
        file = discord.File(target_path, filename="TXA_Discord_Token_Retriever.crx")
        await interaction.response.send_message(embed=embed, file=file, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DMProgressView(discord.ui.View):
    def __init__(self, completer=None, user_id=None, status_callback=None):
        super().__init__(timeout=None)
        self.completer = completer
        self.user_id = user_id
        self.status_callback = status_callback

    @discord.ui.button(label="Dừng Lại", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="btn_stop_quest", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if self.completer:
            await self.completer.stop()
            config.ACTIVE_USERS.pop(self.user_id, None)
            if self.completer.status_callback:
                await self.completer.status_callback("🛑 Đã dừng tiến trình.", finished=False)
            await interaction.followup.send("🛑 Đã dừng phiên làm quest của bạn.", ephemeral=True)

    @discord.ui.button(label="Làm Mới", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="btn_refresh_quest", row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self.status_callback:
            await self.status_callback("Đang làm mới...")

    def update_claim_buttons(self, unclaimed_quests: dict):
        """Tự động thêm nút nhận quà trực tiếp ngay trong tin nhắn DM."""
        current_claim_ids = {str(c.custom_id) for c in self.children if c.custom_id and c.custom_id.startswith("dm_claim_")}
        
        for qid, qname in unclaimed_quests.items():
            btn_id = f"dm_claim_{qid}"
            if btn_id not in current_claim_ids and len(self.children) < 25:
                row_idx = 1 if len([c for c in self.children if getattr(c, 'row', 0) == 1]) < 5 else 2
                btn = discord.ui.Button(
                    label=f"Nhận: {qname[:14]}",
                    style=discord.ButtonStyle.success,
                    emoji="🎁",
                    custom_id=btn_id,
                    row=row_idx
                )
                btn.callback = self.make_claim_callback(qid, qname)
                self.add_item(btn)

    def make_claim_callback(self, quest_id, quest_name):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not self.completer:
                await interaction.followup.send("❌ Không tìm thấy thông tin phiên làm quest.", ephemeral=True)
                return

            res = await self.completer.claim_reward(quest_id)
            if "error" in res:
                err_msg = res.get("details", res["error"])
                await interaction.followup.send(
                    f"❌ **Lỗi khi nhận thưởng từ Discord:**\n"
                    f"Nhiệm vụ: **{quest_name}**\n"
                    f"Chi tiết: `{err_msg}`",
                    ephemeral=True
                )
            else:
                code = res.get("code")
                if code:
                    embed = discord.Embed(
                        title="🎉 Nhận Quà Thành Công!",
                        color=discord.Color.green(),
                        description=(
                            f"Nhiệm vụ: **{quest_name}**\n\n"
                            f"🔑 **Mã quà tặng (Promo Code):**\n"
                            f"```\n{code}\n```\n"
                            f"💡 *Sao chép mã trên và kích hoạt trong Gift Inventory hoặc trong game.*"
                        )
                    )
                else:
                    embed = discord.Embed(
                        title="🎉 Nhận Quà Thành Công!",
                        color=discord.Color.green(),
                        description=(
                            f"Nhiệm vụ: **{quest_name}**\n\n"
                            f"✨ *Phần thưởng đã được kích hoạt trực tiếp vào tài khoản Discord của bạn!*"
                        )
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)

                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child.custom_id == f"dm_claim_{quest_id}":
                        child.disabled = True
                        child.label = f"Đã nhận: {quest_name[:10]}"
                        child.style = discord.ButtonStyle.secondary
                        break
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass

        return callback


class AdminSessionControlView(discord.ui.View):
    def __init__(self, target_user_id: int, completer=None, bot=None):
        super().__init__(timeout=None)
        self.target_user_id = target_user_id
        self.completer = completer
        self.bot = bot

    @discord.ui.button(label="Dừng Phiên Này", style=discord.ButtonStyle.danger, emoji="🛑", custom_id=None)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in config.ADMIN_IDS:
            await interaction.response.send_message("❌ Bạn không phải Admin.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        info = config.ACTIVE_USERS.pop(self.target_user_id, None)
        if self.completer:
            await self.completer.stop()
        if info and info.get('task'):
            info['task'].cancel()

        button.disabled = True
        button.label = "Đã Dừng Phiên"
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        try:
            user = self.bot.get_user(self.target_user_id) or await self.bot.fetch_user(self.target_user_id)
            if user:
                await user.send("🛑 **Tiến trình làm quest của bạn đã bị dừng bởi quản trị viên.**")
        except Exception:
            pass

        if self.bot:
            await self.bot.update_control_panel()

        await interaction.followup.send(f"🛑 **Đã dừng phiên của người dùng <@{self.target_user_id}>!**", ephemeral=True)


async def notify_admins_session_started(bot, user_id, discord_user, quest_username, quest_user_id, start_ts, completer=None):
    """Gửi tin nhắn riêng (DM) tới các Admin khi có người dùng bắt đầu chạy phiên làm quest và trả về danh sách message để update realtime."""
    if not config.ADMIN_IDS:
        return []

    from limits_manager import get_user_quota_info
    quota = get_user_quota_info(user_id)
    
    embed = discord.Embed(
        title=f"⚡ [LIVE MONITOR] Phiên Auto-Quest: {quest_username}",
        color=discord.Color.from_rgb(88, 101, 242),
        timestamp=discord.utils.utcnow()
    )
    if hasattr(discord_user, "display_avatar") and discord_user.display_avatar:
        embed.set_author(name=f"Tài khoản: {quest_username} (Tag: {getattr(discord_user, 'name', 'Unknown')})", icon_url=discord_user.display_avatar.url)
    else:
        embed.set_author(name=f"Tài khoản: {quest_username}")

    embed.add_field(
        name="👤 Thông Tin Người Dùng",
        value=(
            f"• **Discord:** <@{user_id}> (`{getattr(discord_user, 'name', 'Unknown')}`, ID: `{user_id}`)\n"
            f"• **Quest ID:** `{quest_user_id}`\n"
            f"• **Hạn mức/Số dư:** `💎 {quota.get('quota_str', 'N/A')}`"
        ),
        inline=False
    )
    embed.add_field(
        name="📊 Trạng Thái Phiên Chạy",
        value=(
            f"• **Trạng thái:** `🚀 Đang khởi động kết nối...`\n"
            f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
            f"• **Tiến độ tổng:** `▱▱▱▱▱▱▱▱▱▱` **0%**\n"
            f"• **Phiên đồng thời:** `{len(config.ACTIVE_USERS)} phiên đang chạy`"
        ),
        inline=False
    )
    embed.set_footer(text="🔄 Live Admin Monitor • Tự động cập nhật real-time • TXA Quest Bot")

    records = []
    for admin_id in config.ADMIN_IDS:
        try:
            admin_user = bot.get_user(admin_id)
            if not admin_user:
                admin_user = await bot.fetch_user(admin_id)
            if admin_user:
                view = AdminSessionControlView(target_user_id=user_id, completer=completer, bot=bot)
                msg = await admin_user.send(embed=embed, view=view)
                records.append((msg, view))
        except Exception as e:
            log(f"Không thể gửi thông báo DM tới admin {admin_id}: {e}", "warn")

    return records


class TokenModal(discord.ui.Modal, title="🚀 Bắt đầu Quest Auto-Completer"):
    token_input = discord.ui.TextInput(
        label="Nhập Discord User Token của bạn:",
        placeholder="mfa.xxxx... hoặc Token chuẩn 3 phần (Tuyệt đối không chia sẻ cho người khác)",
        style=discord.TextStyle.long,
        min_length=50,
        required=True
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        # Immediate defer to avoid timeout
        await interaction.response.defer(ephemeral=True)

        completer = None
        import re
        user_token = self.token_input.value.strip().strip('"').strip("'").strip()
        if not user_token:
            await interaction.followup.send("❌ Token không được để trống!", ephemeral=True)
            return

        # Validate token format: MFA or Standard 3-part base64 token
        token_pattern = r"^(mfa\.[A-Za-z0-9_\-\=]+|[A-Za-z0-9_\-\=]+\.[A-Za-z0-9_\-\=]+\.[A-Za-z0-9_\-\=]+)$"
        if not re.match(token_pattern, user_token):
            await interaction.followup.send(
                "❌ **Định dạng Token không đúng quy chuẩn!**\n"
                "Discord User Token hợp lệ phải thuộc một trong hai dạng:\n"
                "1. Token chuẩn 3 phần cách nhau bằng dấu chấm (ví dụ: `MTI... .xxx... .yyy...`)\n"
                "2. Token MFA bắt đầu bằng `mfa.` (ví dụ: `mfa.xxx...`)\n"
                "Vui lòng copy chính xác token và thử lại!",
                ephemeral=True
            )
            return

        user_id = interaction.user.id
        username = interaction.user.name

        # Validate token
        api = DiscordAPI(user_token, getattr(config, 'BUILD_NUMBER', 504649))
        user_info = await api.validate_token()
        if not user_info:
            await interaction.followup.send(
                "❌ **Token không hợp lệ hoặc không thể kết nối tới Discord!**\n"
                "Vui lòng kiểm tra lại token của bạn hoặc lấy token mới theo hướng dẫn.",
                ephemeral=True
            )
            return

        # Check if already running for this user
        if user_id in config.ACTIVE_USERS:
            await interaction.followup.send("🔄 **Tài khoản của bạn đang chạy rồi!** Đang khởi động lại tiến trình mới...", ephemeral=True)
            try:
                # Stop existing completer
                old_info = config.ACTIVE_USERS[user_id]
                await old_info['completer'].stop()
                if old_info['task']:
                    old_info['task'].cancel()
            except Exception as e:
                log(f"Lỗi khi dừng luồng cũ: {e}", "warn")
            await asyncio.sleep(1)

        # Open DM Channel with the user
        try:
            dm_channel = await interaction.user.create_dm()
        except Exception as e:
            await interaction.followup.send(
                "❌ **Không thể gửi tin nhắn riêng cho bạn!**\n"
                "Vui lòng bật quyền nhận tin nhắn riêng (DM) từ thành viên máy chủ để nhận tiến độ.",
                ephemeral=True
            )
            return

        # Initialize view
        view = DMProgressView(user_id=user_id)

        # Send initial progress embed in DM
        start_ts = int(time.time())
        embed = discord.Embed(
            title="⚡ TIẾN TRÌNH AUTO-QUEST DISCORD",
            color=discord.Color.from_rgb(88, 101, 242),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"Tài khoản: {user_info.get('username', username)}", icon_url=interaction.user.display_avatar.url)
        embed.add_field(
            name="📊 Tổng Quan & Trạng Thái",
            value=(
                f"• **Trạng thái:** `🚀 Đang khởi động kết nối...`\n"
                f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
                f"• **Tiến độ phiên:** `▱▱▱▱▱▱▱▱▱▱` **0%**\n"
                f"• **Tiến trình:** Đang quét danh sách nhiệm vụ từ tài khoản của bạn..."
            ),
            inline=False
        )
        embed.set_footer(text="🔄 Tự động cập nhật • TXA Quest Bot v4.0")
        try:
            dm_msg = await dm_channel.send(embed=embed, view=view)
        except Exception as e:
            await interaction.followup.send(
                "❌ **Không thể gửi DM cho bạn!** Vui lòng mở khóa DM từ người lạ trong cài đặt bảo mật.",
                ephemeral=True
            )
            return

        # Admin DM messages for real-time monitoring
        admin_records = []

        async def update_admin_monitors(embed_to_send, is_ended=False, ended_label="Phiên Đã Kết Thúc"):
            if not admin_records:
                return
            for a_msg, a_view in list(admin_records):
                try:
                    if is_ended and a_view:
                        for btn in a_view.children:
                            if isinstance(btn, discord.ui.Button):
                                btn.disabled = True
                                btn.label = ended_label
                    await a_msg.edit(embed=embed_to_send, view=a_view)
                except Exception:
                    pass

        # Define status callback to edit the DM message
        async def status_callback(status_text: str, finished: bool = False, update_panel: bool = False):
            try:
                nonlocal dm_msg, view, admin_records
                
                if update_panel:
                    await self.bot.update_control_panel()
                
                if status_text == "LIMIT_REACHED":
                    from payment_views import get_limit_embed_and_view
                    embed_edit, view_edit = get_limit_embed_and_view(user_id)
                    await dm_msg.edit(embed=embed_edit, view=view_edit)

                    # Cập nhật DM Admin thời gian thực khi đạt giới hạn
                    limit_admin_embed = discord.Embed(
                        title=f"⚠️ [ĐẠT GIỚI HẠN] Phiên của {user_info.get('username', username)}",
                        color=discord.Color.orange(),
                        description=(
                            f"Người dùng <@{user_id}> đã đạt giới hạn quest hôm nay.\n"
                            f"Tiến trình đang tạm dừng chờ người dùng thanh toán mua thêm lượt làm quest."
                        ),
                        timestamp=discord.utils.utcnow()
                    )
                    limit_admin_embed.set_footer(text="🔄 Live Admin Monitor • TXA Quest Bot")
                    await update_admin_monitors(limit_admin_embed)
                    return
                
                quests = []
                try:
                    if completer:
                        raw_quests = getattr(completer, "last_quests", None) or await completer.fetch_quests()
                        quests = [
                            q for q in raw_quests 
                            if (is_completed(q) or not is_expired(q)) 
                            and q.get("id") not in completer.failed_404_ids
                        ]
                except Exception:
                    pass
                
                # Auto-detect finished if all quests are completed
                if quests and all(is_completed(q) for q in quests):
                    finished = True

                from limits_manager import get_user_total_completed, get_user_quota_info
                from system_info import make_bar
                user_total = get_user_total_completed(user_id)
                quota_info = get_user_quota_info(user_id)
                user_avatar_url = interaction.user.display_avatar.url
                user_display_name = user_info.get('username', username)
                
                completed_count = sum(1 for q in quests if is_completed(q)) if quests else 0
                total_count = len(quests) if quests else 0

                # Lấy danh sách quest chưa claim
                unclaimed_quests = {}
                for q in quests:
                    qid = q.get("id")
                    us = get_user_status(q)
                    claimed = bool(_get(us, "claimedAt", "claimed_at"))
                    if is_completed(q) and not claimed:
                        unclaimed_quests[qid] = get_quest_name(q)

                # Tự động cập nhật các nút nhận quà vào View
                view.update_claim_buttons(unclaimed_quests)

                if finished:
                    color = discord.Color.green()
                    for item in view.children:
                        if isinstance(item, discord.ui.Button) and item.custom_id == "btn_stop_quest":
                            item.disabled = True

                    embed_edit = discord.Embed(
                        title="🎉 TẤT CẢ NHIỆM VỤ ĐÃ HOÀN THÀNH!",
                        color=color,
                        timestamp=discord.utils.utcnow()
                    )
                    embed_edit.set_author(name=f"Tài khoản: {user_display_name}", icon_url=user_avatar_url)

                    embed_edit.add_field(
                        name="📊 Thống Kê Phiên Này",
                        value=(
                            f"• **Trạng thái:** `🟢 Hoàn thành 100%`\n"
                            f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
                            f"• **Số quest hoàn tất:** `✅ {completed_count}/{total_count} nhiệm vụ`\n"
                            f"• **Lượt quest khả dụng:** `💎 {quota_info['quota_str']}`\n"
                            f"• **Tổng quest tích lũy:** `🏆 {user_total} nhiệm vụ`"
                        ),
                        inline=False
                    )

                    if unclaimed_quests:
                        embed_edit.add_field(
                            name="🎁 Quà Tặng Chưa Nhận",
                            value="Nhấn các nút **Nhận Quà** bên dưới để lấy ngay mã quà tặng (Promo Code) hoặc kích hoạt phần thưởng!",
                            inline=False
                        )
                    else:
                        embed_edit.add_field(
                            name="✨ Phần Thưởng",
                            value="Tất cả phần thưởng đã sẵn sàng trong mục **Quest / Gift Inventory** của Discord.",
                            inline=False
                        )

                    embed_edit.set_footer(text=f"Hoàn tất lúc {datetime.now().strftime('%H:%M:%S')} • TXA Quest Bot v4.0")
                    try:
                        await dm_msg.edit(embed=embed_edit, view=view)
                    except discord.NotFound:
                        dm_msg = await dm_channel.send(embed=embed_edit, view=view)

                    # Cập nhật DM Admin thời gian thực khi hoàn tất 100%
                    admin_finish_embed = discord.Embed(
                        title=f"🎉 [HOÀN TẤT 100%] Phiên của {user_display_name}",
                        color=discord.Color.green(),
                        timestamp=discord.utils.utcnow()
                    )
                    admin_finish_embed.set_author(name=f"Tài khoản: {user_display_name} (Tag: {getattr(interaction.user, 'name', 'Unknown')})", icon_url=user_avatar_url)
                    admin_finish_embed.add_field(
                        name="👤 Thông Tin Người Dùng",
                        value=f"• Tag: <@{user_id}>\n• Username: **{getattr(interaction.user, 'name', 'Unknown')}**\n• ID: `{user_id}`",
                        inline=False
                    )
                    admin_finish_embed.add_field(
                        name="📊 Kết Quả Phiên Chạy",
                        value=(
                            f"• **Trạng thái:** `🟢 Hoàn thành 100%`\n"
                            f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
                            f"• **Số quest hoàn tất:** `✅ {completed_count}/{total_count} nhiệm vụ`\n"
                            f"• **Tổng quest tích lũy:** `🏆 {user_total} nhiệm vụ`"
                        ),
                        inline=False
                    )
                    admin_finish_embed.set_footer(text=f"🔄 Live Admin Monitor • Hoàn tất lúc {datetime.now().strftime('%H:%M:%S')} • TXA Quest Bot")
                    await update_admin_monitors(admin_finish_embed, is_ended=True, ended_label="Đã Hoàn Thành 100%")

                    # Gửi tin nhắn thông báo riêng báo đã xong và tự xóa sau 1 phút
                    if completer and not getattr(completer, '_completion_notice_sent', False):
                        completer._completion_notice_sent = True
                        async def send_and_delete_notice():
                            try:
                                notice_msg = await dm_channel.send(
                                    f"🔔 <@{user_id}> **Tất cả các nhiệm vụ Discord Quest của bạn đã hoàn thành xong rồi nhé!** 🎉\n"
                                    f"*(Tin nhắn thông báo này sẽ tự động xóa sau 1 phút)*"
                                )
                                await asyncio.sleep(60)
                                await notice_msg.delete()
                            except Exception as notice_err:
                                log(f"Lỗi khi gửi/xóa thông báo hoàn thành: {notice_err}", "warn")
                        asyncio.create_task(send_and_delete_notice())

                    return

                elif completer and completer.stop_event.is_set():
                    status_badge = "🛑 Đã dừng tiến trình"
                    color = discord.Color.red()
                    for item in view.children:
                        if isinstance(item, discord.ui.Button) and item.custom_id == "btn_stop_quest":
                            item.disabled = True
                else:
                    status_badge = "🚀 Đang thực hiện"
                    color = discord.Color.from_rgb(88, 101, 242)

                overall_ratio = (completed_count / total_count * 100) if total_count > 0 else 0
                overall_bar = make_bar(overall_ratio, length=10)

                embed_edit = discord.Embed(
                    title="⚡ TIẾN ĐỘ AUTO-QUEST DISCORD",
                    color=color,
                    timestamp=discord.utils.utcnow()
                )
                embed_edit.set_author(name=f"Tài khoản: {user_display_name}", icon_url=user_avatar_url)

                # Khối 1: Tổng quan
                embed_edit.add_field(
                    name="📊 Tổng Quan & Trạng Thái",
                    value=(
                        f"• **Trạng thái:** `{status_badge}`\n"
                        f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
                        f"• **Tiến độ phiên:** `{overall_bar}` **{overall_ratio:.0f}%** ({completed_count}/{total_count} xong)\n"
                        f"• **Lượt quest khả dụng:** `💎 {quota_info['quota_str']}`\n"
                        f"• **Tổng quest tích lũy:** `🏆 {user_total} nhiệm vụ`"
                    ),
                    inline=False
                )

                # Khối 2: Nhiệm vụ đang chạy (nếu có)
                active_q = None
                if completer and completer.current_quest_id and quests:
                    for q in quests:
                        if q.get("id") == completer.current_quest_id:
                            active_q = q
                            break

                if active_q:
                    active_name = get_quest_name(active_q)
                    active_task = get_task_type(active_q) or "?"
                    task_label = {
                        "WATCH_VIDEO": "🎬 Xem Video",
                        "PLAY_ON_DESKTOP": "🖥️ Chơi Game Desktop",
                        "STREAM_ON_DESKTOP": "📡 Stream Game Desktop",
                        "PLAY_ACTIVITY": "🕹️ Chơi Discord Activity"
                    }.get(active_task, f"🎮 {active_task}")

                    needed = get_seconds_needed(active_q)
                    done = get_seconds_done(active_q)
                    if hasattr(completer, "current_quest_info") and completer.current_quest_info:
                        done = max(done, completer.current_quest_info.get("done", done))
                        needed = max(needed, completer.current_quest_info.get("needed", needed))

                    pct = (done / needed * 100) if needed > 0 else 0
                    quest_bar = make_bar(pct, length=10)
                    remaining = max(0, int(needed - done))
                    eta_text = f"{remaining // 60}p {remaining % 60}s" if remaining >= 60 else f"{remaining}s"

                    embed_edit.add_field(
                        name="⚡ Nhiệm Vụ Đang Xử Lý",
                        value=(
                            f"• **Nhiệm vụ:** **{active_name}**\n"
                            f"• **Loại hình:** `{task_label}`\n"
                            f"• **Tiến độ:** `{quest_bar}` **{pct:.0f}%** (`{done:.0f}s / {needed}s`)\n"
                            f"• **Thời gian còn lại:** `⏳ ~{eta_text}`"
                        ),
                        inline=False
                    )

                # Khối 3: Danh sách nhiệm vụ
                if quests:
                    quest_lines = []
                    for q in quests:
                        q_name = get_quest_name(q)
                        q_task = get_task_type(q) or "?"
                        q_id = q.get("id")
                        us = get_user_status(q)
                        claimed = bool(_get(us, "claimedAt", "claimed_at"))

                        if is_completed(q):
                            if claimed:
                                quest_lines.append(f"✅ ~~{q_name}~~ `(Đã nhận)`")
                            else:
                                quest_lines.append(f"🎁 **{q_name}** `[Chờ nhận quà]`")
                        elif completer and completer.current_quest_id == q_id:
                            quest_lines.append(f"🔄 **{q_name}** `[Đang chạy...]`")
                        elif is_enrolled(q):
                            quest_lines.append(f"⏳ {q_name} `[{q_task}]` *(Đang chờ)*")
                        else:
                            quest_lines.append(f"⚪ {q_name} `[{q_task}]` *(Chưa nhận)*")

                    embed_edit.add_field(
                        name=f"📋 Danh Sách Nhiệm Vụ ({completed_count}/{total_count})",
                        value="\n".join(quest_lines[:12]),
                        inline=False
                    )
                else:
                    embed_edit.add_field(name="📋 Danh Sách Nhiệm Vụ", value=status_text, inline=False)

                embed_edit.set_footer(text=f"🔄 Tự động cập nhật • {datetime.now().strftime('%H:%M:%S')} • TXA Quest Bot v4.0")
                try:
                    await dm_msg.edit(embed=embed_edit, view=view)
                except discord.NotFound:
                    dm_msg = await dm_channel.send(embed=embed_edit, view=view)

                # Cập nhật Real-time cho tin nhắn riêng (DM) của Admin
                is_stopped = bool(completer and completer.stop_event.is_set())
                admin_status_title = f"🛑 [ĐÃ DỪNG] Phiên của {user_display_name}" if is_stopped else f"⚡ [LIVE MONITOR] Phiên Auto-Quest: {user_display_name}"
                admin_status_color = discord.Color.red() if is_stopped else color

                admin_embed = discord.Embed(
                    title=admin_status_title,
                    color=admin_status_color,
                    timestamp=discord.utils.utcnow()
                )
                admin_embed.set_author(name=f"Tài khoản: {user_display_name} (Tag: {getattr(interaction.user, 'name', 'Unknown')})", icon_url=user_avatar_url)
                admin_embed.add_field(
                    name="👤 Thông Tin Người Dùng",
                    value=(
                        f"• **Discord:** <@{user_id}> (`{getattr(interaction.user, 'name', 'Unknown')}`, ID: `{user_id}`)\n"
                        f"• **Quest ID:** `{user_info.get('id', 'N/A')}`\n"
                        f"• **Hạn mức/Số dư:** `💎 {quota_info['quota_str']}`"
                    ),
                    inline=False
                )
                admin_embed.add_field(
                    name="📊 Tiến Độ Thời Gian Thực",
                    value=(
                        f"• **Trạng thái:** `{status_badge}`\n"
                        f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
                        f"• **Tiến độ tổng:** `{overall_bar}` **{overall_ratio:.0f}%** ({completed_count}/{total_count} xong)\n"
                        f"• **Phiên đồng thời:** `{len(config.ACTIVE_USERS)} phiên đang chạy`"
                    ),
                    inline=False
                )

                if active_q:
                    admin_embed.add_field(
                        name="⚡ Nhiệm Vụ Đang Xử Lý",
                        value=(
                            f"• **Nhiệm vụ:** **{active_name}**\n"
                            f"• **Loại:** `{task_label}`\n"
                            f"• **Tiến độ:** `{quest_bar}` **{pct:.0f}%** (`{done:.0f}s / {needed}s`)\n"
                            f"• **Thời gian còn lại:** `⏳ ~{eta_text}`"
                        ),
                        inline=False
                    )

                if quests:
                    admin_embed.add_field(
                        name=f"📋 Danh Sách Nhiệm Vụ ({completed_count}/{total_count})",
                        value="\n".join(quest_lines[:8]),
                        inline=False
                    )

                admin_embed.set_footer(text=f"🔄 Live Admin Monitor • {datetime.now().strftime('%H:%M:%S')} • TXA Quest Bot v4.0")
                await update_admin_monitors(admin_embed, is_ended=is_stopped, ended_label="Đã Dừng Phiên")
            except Exception as e:
                log(f"Không thể cập nhật DM cho user {user_id}: {e}", "warn")

        # Initialize completer
        completer = QuestAutocompleter(api, user_id, status_callback=status_callback)
        view.completer = completer
        view.status_callback = status_callback

        # Gửi DM thông báo khởi chạy tới Admin và lưu danh sách message để update realtime
        admin_records = await notify_admins_session_started(
            bot=self.bot,
            user_id=user_id,
            discord_user=interaction.user,
            quest_username=user_info.get('username', username),
            quest_user_id=user_info.get('id', 'N/A'),
            start_ts=start_ts,
            completer=completer
        )

        # Define worker that guarantees ACTIVE_USERS cleanup and panel update
        async def session_worker():
            try:
                await completer.run()
            finally:
                config.ACTIVE_USERS.pop(user_id, None)
                try:
                    await self.bot.update_control_panel()
                except Exception:
                    pass

        # Start in background task
        task = asyncio.create_task(session_worker())
        
        # Store metadata
        config.ACTIVE_USERS[user_id] = {
            'completer': completer,
            'task': task,
            'username': user_info.get('username', username),
            'start_timestamp': start_ts,
            'discord_user': interaction.user,
            'admin_records': admin_records
        }

        # Ephemeral notification
        await interaction.followup.send(
            f"{config.EMOJI_SUCCESS} **Đăng nhập thành công tài khoản `{user_info.get('username')}`!**\n"
            f"📬 Tiến độ chi tiết đang được gửi vào tin nhắn riêng (DM) của bạn.",
            ephemeral=True
        )

        # Update main control panel stats
        await self.bot.update_control_panel()


class ControlView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None) # Persistent view
        self.bot = bot
        if config.EMOJI_ROCKET:
            self.start_button.emoji = config.EMOJI_ROCKET

    @discord.ui.button(label="Bắt Đầu", style=discord.ButtonStyle.success, emoji="🚀", custom_id="btn_start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_channels = set()
        if config.CHANNEL_ID:
            allowed_channels.add(config.CHANNEL_ID)
        if hasattr(config, 'SERVER_CHANNELS') and config.SERVER_CHANNELS:
            allowed_channels.update(config.SERVER_CHANNELS.values())
            
        if allowed_channels and interaction.channel_id not in allowed_channels:
            await interaction.response.send_message("❌ Nút bấm này không thể sử dụng ở kênh này.", ephemeral=True)
            return
        
        modal = TokenModal(self.bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Số Dư Quest", style=discord.ButtonStyle.primary, emoji="💎", custom_id="btn_quota")
    async def quota_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_channels = set()
        if config.CHANNEL_ID:
            allowed_channels.add(config.CHANNEL_ID)
        if hasattr(config, 'SERVER_CHANNELS') and config.SERVER_CHANNELS:
            allowed_channels.update(config.SERVER_CHANNELS.values())

        if allowed_channels and interaction.channel_id not in allowed_channels:
            await interaction.response.send_message("❌ Nút bấm này không thể sử dụng ở kênh này.", ephemeral=True)
            return

        from payment_views import get_limit_embed_and_view
        embed, view = get_limit_embed_and_view(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Hướng Dẫn", style=discord.ButtonStyle.secondary, emoji="❓", custom_id="btn_guide")
    async def guide_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_channels = set()
        if config.CHANNEL_ID:
            allowed_channels.add(config.CHANNEL_ID)
        if hasattr(config, 'SERVER_CHANNELS') and config.SERVER_CHANNELS:
            allowed_channels.update(config.SERVER_CHANNELS.values())

        if allowed_channels and interaction.channel_id not in allowed_channels:
            await interaction.response.send_message("❌ Nút bấm này không thể sử dụng ở kênh này.", ephemeral=True)
            return
        
        await send_extension_guide(interaction)


async def cleanup_all_users():
    log(f"Đang dừng {len(config.ACTIVE_USERS)} tài khoản đang chạy ngầm...", "info")
    for uid, info in list(config.ACTIVE_USERS.items()):
        try:
            await info['completer'].stop()
            if info['task']:
                info['task'].cancel()
            log(f"  -> Đã dừng thành công luồng của {Colors.BOLD}{info['username']}{Colors.RESET}", "ok")
        except Exception as e:
            log(f"Lỗi khi dừng luồng của {uid}: {e}", "error")

class QuestBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.control_msg_id = None
        self.persistent_views_added = False

    async def close(self):
        log("Đang đóng các phiên hoạt động và dọn dẹp...", "info")
        await cleanup_all_users()
        await super().close()
        log("Hệ thống đã dừng hoàn toàn. Tạm biệt!", "ok")

    async def setup_hook(self):
        # Register persistent view
        self.add_view(ControlView(self))
        log("Đã tải view điều khiển persistent", "ok")

        # Fetch build number
        config.BUILD_NUMBER = await fetch_latest_build_number()

    async def on_ready(self):
        log(f"Bot đăng nhập thành công với tên: {Colors.BOLD}{self.user}{Colors.RESET}", "ok")
        
        # Load saved channel config
        load_channel_config()

        # Update/Create Control Panel in channel
        await self.update_control_panel()

        # Sync slash commands with Discord
        try:
            synced = await self.tree.sync()
            log(f"Đã sync {len(synced)} slash commands.", "ok")
        except Exception as e:
            log(f"Lỗi sync slash commands: {e}", "error")

    async def on_guild_join(self, guild: discord.Guild):
        """Tự động xử lý khi Bot được mời vào một Server mới."""
        log(f"🎉 Bot vừa được thêm vào máy chủ mới: {guild.name} (ID: {guild.id})", "ok")
        
        # 1. Tự động sync slash commands sang Server mới ngay lập tức
        try:
            await self.tree.sync(guild=guild)
            log(f"Đã sync slash commands tới guild mới: {guild.name}", "ok")
        except Exception as e:
            log(f"Lỗi sync slash commands tới guild mới: {e}", "warn")

        # 2. Tìm kênh văn bản thích hợp nhất để tạo Bảng điều khiển
        target_channel = guild.system_channel
        if not target_channel or not hasattr(target_channel, "permissions_for") or not target_channel.permissions_for(guild.me).send_messages:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages and ch.permissions_for(guild.me).view_channel:
                    target_channel = ch
                    break

        if target_channel:
            # Lưu kênh làm kênh hoạt động chính của Server
            save_channel_config(target_channel.id, guild_id=guild.id)
            
            # Gửi tin nhắn chào mừng và Bảng điều khiển
            try:
                welcome_embed = discord.Embed(
                    title="🎉 Cảm ơn bạn đã thêm Quest Bot!",
                    description=(
                        f"Bot đã sẵn sàng hoạt động tại máy chủ **{guild.name}**!\n\n"
                        f"📍 **Kênh hoạt động tự động:** {target_channel.mention}\n"
                        f"⚙️ **Admin có thể đổi kênh hoạt động bất cứ lúc nào bằng lệnh:** `/setup_here` hoặc `/channel`"
                    ),
                    color=discord.Color.green()
                )
                await target_channel.send(embed=welcome_embed)
                await self.update_control_panel(target_channel=target_channel)
            except Exception as e:
                log(f"Không thể gửi tin nhắn chào mừng tại guild {guild.id}: {e}", "warn")

    async def update_control_panel(self, target_channel=None):
        """Creates or updates the main statistics control panel embed across configured server channels."""
        channels_to_update = []
        seen_ids = set()

        if target_channel:
            if isinstance(target_channel, (discord.TextChannel, discord.Thread, discord.abc.GuildChannel)) or hasattr(target_channel, "send"):
                channels_to_update.append(target_channel)
                seen_ids.add(target_channel.id)
            elif isinstance(target_channel, (int, str)) and str(target_channel).isdigit():
                seen_ids.add(int(target_channel))

        if config.CHANNEL_ID:
            seen_ids.add(config.CHANNEL_ID)
        if hasattr(config, 'SERVER_CHANNELS') and config.SERVER_CHANNELS:
            for cid in config.SERVER_CHANNELS.values():
                seen_ids.add(cid)

        # Resolve channel objects for any channel IDs not resolved yet
        for cid in seen_ids:
            if any(getattr(ch, 'id', None) == cid for ch in channels_to_update):
                continue

            ch = self.get_channel(cid)
            if not ch:
                for guild in self.guilds:
                    ch = guild.get_channel(cid)
                    if ch:
                        break
            if not ch:
                try:
                    ch = await self.fetch_channel(cid)
                except Exception as fetch_err:
                    log(f"Chưa thể kết nối tới kênh ID {cid}: {fetch_err}", "warn")
                    continue
            if ch:
                channels_to_update.append(ch)

        if not channels_to_update:
            log("Chưa cấu hình kênh hoạt động nào. Chờ admin sử dụng lệnh /channel hoặc /setup_here.", "warn")
            return

        for channel in channels_to_update:
            try:
                embed = discord.Embed(
                    title="🎯 Tự Động Hoàn Thành Quest Discord",
                    description=(
                        "Nhận **Nitro, avatar, profile decoration...** mà không cần thao tác thủ công.\n"
                        "Nhấn 🚀 **Bắt Đầu** để nhập token — hệ thống sẽ tự **quét • nhận • hoàn thành** quest cho bạn."
                    ),
                    color=discord.Color.blue()
                )
                embed.set_author(name="TXA")
                
                active_count = len(config.ACTIVE_USERS)
                embed.add_field(name="👤 Phiên hiện tại", value=f"```\n{active_count}\n```", inline=True)
                embed.add_field(name="⏳ Hàng chờ", value="```\n0\n```", inline=True)
                embed.add_field(name="▶️ Đang chạy", value=f"```\n{active_count}\n```", inline=True)
                embed.add_field(name="✅ Tổng quest đã hoàn thành", value=f"```\n{config.TOTAL_COMPLETED}\n```", inline=False)
                
                current_time = time.strftime("%H:%M %d/%m/%y")
                embed.set_footer(text=f"Auto Scan • Auto Accept • Auto Complete • {current_time} | Quest Bot v4.0 - Built by TXA")

                # Clean old bot messages and send a clean panel
                try:
                    async for msg in channel.history(limit=15):
                        if msg.author == self.user:
                            await msg.delete()
                except Exception as e:
                    log(f"Lỗi dọn dẹp tin nhắn cũ ở kênh {getattr(channel, 'id', 'unknown')}: {e}", "warn")

                try:
                    await channel.send(embed=embed, view=ControlView(self))
                except Exception as e:
                    log(f"Không thể gửi tin nhắn bảng điều khiển tới kênh {getattr(channel, 'id', 'unknown')}: {e}", "error")
            except Exception as e:
                log(f"Lỗi khi xử lý kênh {getattr(channel, 'id', 'unknown')}: {e}", "error")


def check_bot_channel_permissions(channel, me):
    """Kiểm tra xem Bot có đủ quyền hạn tại kênh chỉ định hay không."""
    if not me or not hasattr(channel, "permissions_for"):
        return True, ""
    perms = channel.permissions_for(me)
    missing = []
    if not perms.view_channel:
        missing.append("👁️ **Xem Kênh** (View Channel / Read Messages)")
    if not perms.send_messages:
        missing.append("💬 **Gửi Tin Nhắn** (Send Messages)")
    if not perms.embed_links:
        missing.append("🔗 **Chèn Liên Kết** (Embed Links)")
    if not perms.read_message_history:
        missing.append("📜 **Đọc Lịch Sử Tin Nhắn** (Read Message History)")
    
    if missing:
        return False, "\n".join([f"• {m}" for m in missing])
    return True, ""


bot = QuestBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log(f"Lỗi lệnh slash [{interaction.command.name if interaction.command else 'Unknown'}]: {error}", "error")
    
    if isinstance(error, app_commands.TransformerError):
        msg = f"❌ **Lỗi dữ liệu kênh/tham số:** `{error.value}` không phải là kênh hợp lệ hoặc không thể sử dụng cho lệnh này."
    else:
        msg = f"❌ **Có lỗi xảy ra khi thực hiện lệnh:** `{error}`"

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception as e:
        log(f"Không thể gửi thông báo lỗi tới user: {e}", "warn")


import typing


# Slash Command: Set up channel by selecting channel or current channel
@bot.tree.command(name="channel", description="[Admin] Cài đặt kênh hoạt động cho Bot (để trống sẽ lấy kênh hiện tại)")
@app_commands.describe(target_channel="(Tuỳ chọn) Chọn kênh văn bản từ danh sách. Để trống sẽ lấy kênh bạn đang đứng.")
async def set_channel(
    interaction: discord.Interaction,
    target_channel: typing.Optional[discord.TextChannel] = None
):
    # Verify Admin permission
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    channel = target_channel or interaction.channel

    if not channel or not hasattr(channel, "send"):
        await interaction.followup.send("❌ **Không thể xác định kênh văn bản hợp lệ!** Vui lòng thực hiện lệnh trong một kênh chat chữ.", ephemeral=True)
        return

    # Check bot permissions in target channel
    me = interaction.guild.me if interaction.guild else None
    has_perms, missing_str = check_bot_channel_permissions(channel, me)
    if not has_perms:
        await interaction.followup.send(
            f"❌ **Bot không có đủ quyền hạn tại kênh {getattr(channel, 'mention', channel.name)}!**\n\n"
            f"Vui lòng vào **Cài đặt kênh ➔ Quyền (Permissions)** và cấp cho Bot/Vai trò của Bot các quyền sau:\n"
            f"{missing_str}",
            ephemeral=True
        )
        return

    # Save to dynamic config for this specific server guild
    guild_id = interaction.guild_id if interaction.guild else None
    save_channel_config(channel.id, guild_id=guild_id)

    # Recreate the control panel for this channel
    await bot.update_control_panel(target_channel=channel)

    channel_mention = getattr(channel, "mention", f"<#{channel.id}>")
    await interaction.followup.send(f"✅ **Đã thiết lập kênh hoạt động thành công tại:** {channel_mention}", ephemeral=True)


# Slash Command: Quick Setup in Current Channel
@bot.tree.command(name="setup_here", description="[Admin] Cài đặt NGAY tại kênh hiện tại bạn đang gõ lệnh và gửi Bảng điều khiển")
async def setup_here(interaction: discord.Interaction):
    # Verify Admin permission
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = interaction.channel
    if not target_channel or not hasattr(target_channel, "send"):
        await interaction.followup.send("❌ **Kênh hiện tại không hỗ trợ gửi tin nhắn!**", ephemeral=True)
        return

    # Check bot permissions in target channel
    me = interaction.guild.me if interaction.guild else None
    has_perms, missing_str = check_bot_channel_permissions(target_channel, me)
    if not has_perms:
        await interaction.followup.send(
            f"❌ **Bot không có đủ quyền hạn tại kênh {getattr(target_channel, 'mention', target_channel.name)}!**\n\n"
            f"Vui lòng vào **Cài đặt kênh ➔ Quyền (Permissions)** và cấp cho Bot các quyền sau:\n"
            f"{missing_str}",
            ephemeral=True
        )
        return

    guild_id = interaction.guild_id if interaction.guild else None
    save_channel_config(target_channel.id, guild_id=guild_id)

    await bot.update_control_panel(target_channel=target_channel)

    channel_mention = getattr(target_channel, "mention", f"<#{target_channel.id}>")
    await interaction.followup.send(f"✅ **Đã thiết lập Bảng điều khiển thành công NGAY tại kênh:** {channel_mention}", ephemeral=True)


def get_bot_invite_url(bot):
    permissions = discord.Permissions(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        use_external_emojis=True,
        manage_messages=True,
        manage_channels=True
    )
    return discord.utils.oauth_url(
        bot.user.id,
        permissions=permissions,
        scopes=("bot", "applications.commands")
    )


# Slash Command: Get Bot Invite Link
@bot.tree.command(name="invite", description="Lấy link mời Quest Bot vào máy chủ (Server) Discord của bạn")
async def invite_bot(interaction: discord.Interaction):
    invite_url = get_bot_invite_url(bot)
    
    embed = discord.Embed(
        title="➕ Thêm Quest Bot Vào Server Của Bạn",
        description=(
            "Nhấn nút bên dưới để mời Bot tham gia máy chủ Discord của bạn.\n\n"
            "✨ **Khi Bot tham gia Server mới:**\n"
            "• Bot sẽ tự động tìm kênh và tạo **Bảng điều khiển** tự động.\n"
            "• Admin có thể dùng lệnh `/setup_here` tại bất kỳ kênh nào để đổi kênh hoạt động."
        ),
        color=discord.Color.blue()
    )
    
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Mời Bot Vào Server",
        style=discord.ButtonStyle.link,
        url=invite_url,
        emoji="➕"
    ))
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Slash Command: Start/Join guide (slash command)
@bot.tree.command(name="start", description="Bắt đầu tự động làm quest hoặc hướng dẫn kênh hoạt động")
async def start_command(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id) if interaction.guild else None
    server_channel_id = config.SERVER_CHANNELS.get(guild_id) if (guild_id and hasattr(config, 'SERVER_CHANNELS')) else config.CHANNEL_ID

    if not server_channel_id and not config.CHANNEL_ID:
        await interaction.response.send_message(
            "❌ **Cấu hình kênh hoạt động chưa được thiết lập!** Vui lòng liên hệ Admin sử dụng lệnh `/setup_here` hoặc `/channel`.",
            ephemeral=True
        )
        return

    allowed = (interaction.channel_id == server_channel_id) or (interaction.channel_id == config.CHANNEL_ID)
    if allowed:
        modal = TokenModal(bot)
        await interaction.response.send_modal(modal)
    else:
        active_mention = f"<#{server_channel_id}>" if server_channel_id else f"<#{config.CHANNEL_ID}>"
        await interaction.response.send_message(
            f"📢 **Để bắt đầu làm quest, vui lòng truy cập kênh hoạt động:** {active_mention}\n"
            f"Tại đó, bạn có thể nhấn nút **Bắt Đầu** 🚀 hoặc sử dụng lệnh `/start` để nhập token.",
            ephemeral=True
        )


# Command: Global status checker (slash command)
@bot.tree.command(name="status", description="[Admin] Xem danh sách người dùng đang chạy và trạng thái hệ thống")
async def show_status(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚙️ Trạng thái hệ thống & Người dùng hoạt động",
        color=discord.Color.orange()
    )
    
    active_users = config.ACTIVE_USERS
    if not active_users:
        embed.description = "Không có tài khoản nào đang hoạt động."
    else:
        user_list = []
        for uid, info in active_users.items():
            run_time = int(time.time() - info['start_timestamp'])
            user_list.append(f"• <@{uid}> (`{info['username']}`, ID: `{uid}`) - Chạy từ <t:{info['start_timestamp']}:R> ({run_time}s trước)")
        embed.description = "\n".join(user_list)

    from limits_manager import load_limits, get_price_per_quest, get_transaction_timeout
    limits_data = load_limits()
    total_completed = limits_data.get("total_completed", config.TOTAL_COMPLETED)
    total_failed = limits_data.get("total_failed", config.TOTAL_FAILED)

    embed.add_field(name="✅ Thành công", value=f"`{total_completed}`", inline=True)
    embed.add_field(name="❌ Thất bại", value=f"`{total_failed}`", inline=True)
    embed.add_field(name="📦 Phiên bản", value="v4.0 (Async/Bot Mode)", inline=True)

    price = get_price_per_quest()
    timeout = get_transaction_timeout()
    channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "Chưa cấu hình"
    
    embed.add_field(name="📺 Kênh hoạt động", value=channel_mention, inline=True)
    embed.add_field(name="💰 Đơn giá quest", value=f"`{price:,} VNĐ`", inline=True)
    embed.add_field(name="⏳ Hạn thanh toán QR", value=f"`{timeout // 60} phút ({timeout}s)`", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# Slash Command: View detailed bot configuration (slash command for Admin)
@bot.tree.command(name="config", description="[Admin] Xem cấu hình chi tiết của hệ thống Bot")
async def view_config(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    from limits_manager import get_price_per_quest, get_transaction_timeout
    price = get_price_per_quest()
    timeout = get_transaction_timeout()
    
    sepay_status = "Đã cấu hình ✅" if config.SEPAY_API_KEY else "Chưa cấu hình ❌"
    channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "Chưa cấu hình"
    
    embed = discord.Embed(
        title="⚙️ Bảng Cấu Hình Hệ Thống Quest Bot",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    
    # Discord & Admin settings
    embed.add_field(name="📺 Kênh hoạt động", value=channel_mention, inline=True)
    embed.add_field(name="👑 Danh sách Admin IDs", value=f"`{', '.join(map(str, config.ADMIN_IDS))}`", inline=True)
    embed.add_field(name="🤖 Trạng thái Bot", value="Đang hoạt động 🟢", inline=True)

    # Quest settings
    embed.add_field(name="💰 Đơn giá quest", value=f"`{price:,} VNĐ`", inline=True)
    embed.add_field(name="⏳ Hạn thanh toán QR", value=f"`{timeout // 60} phút ({timeout}s)`", inline=True)
    embed.add_field(name="🔄 Tần suất quét quest", value=f"`{config.POLL_INTERVAL} giây`", inline=True)
    embed.add_field(name="💓 Tần suất Heartbeat", value=f"`{config.HEARTBEAT_INTERVAL} giây`", inline=True)
    embed.add_field(name="📥 Tự động nhận quest", value="Bật ✅" if config.AUTO_ACCEPT else "Tắt ❌", inline=True)
    
    # Payment settings
    embed.add_field(name="🏦 Ngân hàng", value=f"`{config.BANK_ID}`", inline=True)
    embed.add_field(name="🔢 Số tài khoản", value=f"`{config.ACCOUNT_NO}`", inline=True)
    embed.add_field(name="👤 Tên chủ tài khoản", value=f"`{config.ACCOUNT_NAME}`", inline=True)
    embed.add_field(name="🔑 Cổng thanh toán SePay", value=sepay_status, inline=False)
    
    embed.set_footer(text="Cấu hình chỉ hiển thị dạng Ephemeral dành riêng cho Admin.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Command: Admin set quickly quest limits/balance (slash command for Admin)
@bot.tree.command(name="set_limit", description="[Admin] Đặt số lượt làm quest (số dư) cho người dùng")
@app_commands.describe(user_discord_id="Chọn hoặc nhập ID Discord của người dùng", balance="Số lượt quest muốn đặt (ví dụ: 5)")
async def set_limit_command(interaction: discord.Interaction, user_discord_id: str, balance: int):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin mới có quyền.", ephemeral=True)
        return

    if not user_discord_id.isdigit():
        await interaction.response.send_message("❌ ID người dùng không hợp lệ.", ephemeral=True)
        return

    if balance < 0:
        await interaction.response.send_message("❌ Số lượt phải lớn hơn hoặc bằng 0.", ephemeral=True)
        return

    from limits_manager import load_limits, save_limits
    uid = user_discord_id
    limits = load_limits()
    if uid not in limits:
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0,
            "total_completed": 0,
            "active_transaction": None
        }
    
    limits[uid]["purchased_balance"] = balance
    save_limits(limits)

    embed = discord.Embed(
        title="💳 Cập Nhật Giới Hạn Thành Công",
        color=discord.Color.green(),
        description=f"Đã cập nhật số dư lượt dùng cho người dùng <@{uid}> thành công."
    )
    embed.add_field(name="👤 Người dùng", value=f"<@{uid}> (ID: `{uid}`)", inline=True)
    embed.add_field(name="💎 Số dư mới", value=f"`{balance} lượt làm quest`", inline=True)
    embed.set_footer(text="Hệ thống quản lý giới hạn Quest Bot")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@set_limit_command.autocomplete("user_discord_id")
async def set_limit_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if interaction.user.id not in config.ADMIN_IDS:
        return []

    from limits_manager import load_limits
    limits = load_limits()
    choices = []
    current_lower = current.strip().lower()
    
    seen = set()
    for uid, info in config.ACTIVE_USERS.items():
        seen.add(str(uid))
        uname = info.get("username", "Đang chạy")
        label = f"[Đang chạy] {uname} ({uid})"
        if not current_lower or current_lower in label.lower() or current_lower in str(uid):
            choices.append(app_commands.Choice(name=label[:100], value=str(uid)))

    for uid, udata in limits.items():
        if uid in seen or not str(uid).isdigit():
            continue
        balance = udata.get("purchased_balance", 0) if isinstance(udata, dict) else 0
        label = f"ID: {uid} (Số dư: {balance} lượt)"
        if not current_lower or current_lower in label.lower() or current_lower in str(uid):
            choices.append(app_commands.Choice(name=label[:100], value=str(uid)))
            if len(choices) >= 25:
                break

    return choices[:25]


# Command: Admin clear pycache and compile files (slash command for Admin)
@bot.tree.command(name="clear_cache", description="[Admin] Xóa sạch các thư mục __pycache__ và giải phóng dung lượng")
async def clear_cache_command(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin mới có quyền.", ephemeral=True)
        return

    import shutil
    deleted_files = 0
    deleted_dirs = 0
    total_size = 0
    root_dir = os.path.dirname(os.path.abspath(__file__))

    try:
        for root, dirs, files in os.walk(root_dir, topdown=False):
            for file in files:
                if file.endswith('.pyc') or file.endswith('.pyo'):
                    fp = os.path.join(root, file)
                    try:
                        total_size += os.path.getsize(fp)
                        os.remove(fp)
                        deleted_files += 1
                    except Exception:
                        pass
            for d in dirs:
                if d == '__pycache__':
                    dp = os.path.join(root, d)
                    try:
                        for r, ds, fs in os.walk(dp):
                            for f in fs:
                                total_size += os.path.getsize(os.path.join(r, f))
                        shutil.rmtree(dp)
                        deleted_dirs += 1
                    except Exception:
                        pass

        size_kb = total_size / 1024
        size_str = f"{size_kb:.2f} KB" if size_kb < 1024 else f"{size_kb / 1024:.2f} MB"

        embed = discord.Embed(
            title="🧹 Dọn Dẹp Bộ Nhớ Đệm Thành Công",
            color=discord.Color.green(),
            description="Đã xóa sạch bộ nhớ cache và các file biên dịch python tạm thời."
        )
        embed.add_field(name="📁 Số thư mục dọn dẹp", value=f"`{deleted_dirs} thư mục __pycache__`", inline=True)
        embed.add_field(name="📄 Số tệp tin đã xóa", value=f"`{deleted_files} tệp .pyc/.pyo`", inline=True)
        embed.add_field(name="💾 Dung lượng giải phóng", value=f"`{size_str}`", inline=False)
        embed.set_footer(text="Hệ thống dọn dẹp Quest Bot")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Có lỗi xảy ra khi xóa cache: `{e}`", ephemeral=True)


# Command: Force stop a user (slash command)
@bot.tree.command(name="stop_user", description="[Admin] Dừng tiến trình tự động làm quest của một người dùng")
@app_commands.describe(user_discord_id="Chọn người dùng đang có phiên chạy cần dừng")
async def stop_user(interaction: discord.Interaction, user_discord_id: str):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!**", ephemeral=True)
        return

    if not user_discord_id.isdigit():
        await interaction.response.send_message("❌ ID người dùng không hợp lệ.", ephemeral=True)
        return

    uid = int(user_discord_id)
    if uid not in config.ACTIVE_USERS:
        await interaction.response.send_message("❌ Người dùng này hiện không hoạt động hoặc phiên đã kết thúc trước đó.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    info = config.ACTIVE_USERS.pop(uid)
    try:
        await info['completer'].stop()
        if info['task']:
            info['task'].cancel()
        
        # Send a DM to that user informing them they were stopped
        user = bot.get_user(uid)
        if not user:
            user = await bot.fetch_user(uid)
        if user:
            await user.send("🛑 **Tiến trình làm quest của bạn đã bị dừng bởi quản trị viên.**")
    except Exception as e:
        log(f"Lỗi dừng user: {e}", "warn")

    await bot.update_control_panel()
    await interaction.followup.send(f"✅ **Đã dừng tiến trình hoạt động của:** **{info['username']}** (ID: `{uid}`)", ephemeral=True)


@stop_user.autocomplete("user_discord_id")
async def stop_user_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if interaction.user.id not in config.ADMIN_IDS:
        return []

    choices = []
    current_lower = current.strip().lower()
    for uid, info in config.ACTIVE_USERS.items():
        uname = info.get("username", "Unknown")
        d_user = info.get("discord_user")
        d_name = d_user.name if d_user else ""
        
        # Format label showing clearly the username and Discord ID
        display_label = f"{uname}"
        if d_name and d_name != uname:
            display_label += f" ({d_name})"
        display_label += f" - ID: {uid}"
        
        if not current_lower or current_lower in display_label.lower() or current_lower in str(uid):
            choices.append(app_commands.Choice(name=display_label[:100], value=str(uid)))
            if len(choices) >= 25:
                break
    return choices


class QuestClaimView(discord.ui.View):
    def __init__(self, completer, quest_choices, user_id):
        super().__init__(timeout=180)  # Timeout after 3 minutes
        self.completer = completer
        self.user_id = user_id

        # Dynamically add buttons for each unclaimed quest
        for quest_id, quest_name in quest_choices.items():
            button = discord.ui.Button(
                label=f"Nhận: {quest_name[:20]}",
                style=discord.ButtonStyle.primary,
                emoji="🎁",
                custom_id=f"claim_{quest_id}"
            )
            button.callback = self.make_callback(quest_id, quest_name)
            self.add_item(button)

    def make_callback(self, quest_id, quest_name):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Nút bấm này không dành cho bạn.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            res = await self.completer.claim_reward(quest_id)
            if "error" in res:
                err_msg = res.get("details", res["error"])
                await interaction.followup.send(
                    f"❌ **Lỗi khi nhận thưởng từ Discord:**\n"
                    f"Nhiệm vụ: **{quest_name}**\n"
                    f"Chi tiết: `{err_msg}`\n"
                    f"Vui lòng thử lại sau.",
                    ephemeral=True
                )
            else:
                code = res.get("code")
                if code:
                    embed = discord.Embed(
                        title="🎉 Nhận Quà Thành Công!",
                        color=discord.Color.green(),
                        description=(
                            f"Nhiệm vụ: **{quest_name}**\n\n"
                            f"🔑 **Mã quà tặng (Promo Code):**\n"
                            f"```\n{code}\n```\n"
                            f"💡 *Sao chép mã trên và kích hoạt trong Gift Inventory hoặc trò chơi tương ứng.*"
                        )
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    embed = discord.Embed(
                        title="🎉 Nhận Quà Thành Công!",
                        color=discord.Color.green(),
                        description=(
                            f"Nhiệm vụ: **{quest_name}**\n\n"
                            f"✨ *Phần thưởng đã được kích hoạt trực tiếp vào tài khoản của bạn!* (Trang trí hồ sơ/Khung đại diện)"
                        )
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)

                # Disable this button so it can't be clicked again
                for child in self.children:
                    if isinstance(child, discord.ui.Button) and child.custom_id == f"claim_{quest_id}":
                        child.disabled = True
                        child.label = f"Đã nhận: {quest_name[:15]}"
                        child.style = discord.ButtonStyle.secondary
                        break
                
                # Edit original message to show updated view
                try:
                    await interaction.message.edit(view=self)
                except Exception:
                    pass

        return callback


# Command: Help command (slash command)
@bot.tree.command(name="help", description="Xem hướng dẫn sử dụng và tải tiện ích TXA Discord Token Retriever (.crx)")
async def help_command(interaction: discord.Interaction):
    await send_extension_guide(interaction)


# Command: View active user quests (slash command)
@bot.tree.command(name="my_quests", description="Xem trạng thái các quest và nhận phần thưởng Gift Code")
async def my_quests(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in config.ACTIVE_USERS:
        channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "bảng điều khiển chính"
        await interaction.response.send_message(
            f"❌ **Bạn hiện không có phiên hoạt động nào!**\n"
            f"Vui lòng nhấn nút **Bắt Đầu** ở {channel_mention} để nhập token.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    
    from limits_manager import is_user_limited
    if is_user_limited(user_id):
        from payment_views import get_limit_embed_and_view
        embed, view = get_limit_embed_and_view(user_id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        return

    info = config.ACTIVE_USERS[user_id]
    completer = info['completer']
    
    quests = await completer.fetch_quests()
    if not quests:
        await interaction.followup.send(
            "📭 **Không tìm thấy nhiệm vụ nào trong tài khoản của bạn.**",
            ephemeral=True
        )
        return

    # Filter quests to show active/completed
    active_quests = []
    for q in quests:
        qid = q.get("id")
        if qid not in completer.failed_404_ids and (is_completed(q) or not is_expired(q)):
            active_quests.append(q)

    if not active_quests:
        await interaction.followup.send(
            "💤 **Không có nhiệm vụ nào khả dụng hoặc chưa hết hạn trong tài khoản của bạn.**",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📋 Danh Sách Nhiệm Vụ Của Bạn",
        color=discord.Color.blue(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name=info['username'], icon_url=interaction.user.display_avatar.url)

    from limits_manager import get_user_total_completed
    user_total = get_user_total_completed(user_id)
    embed.add_field(name="🏆 Tổng quest đã hoàn thành của bạn", value=f"`{user_total}` nhiệm vụ", inline=False)

    desc_lines = []
    unclaimed_quests = {}  # maps qid -> name

    for q in active_quests:
        name = get_quest_name(q)
        task = get_task_type(q) or "?"
        qid = q.get("id")
        us = get_user_status(q)
        
        # Check claim status
        claimed = bool(_get(us, "claimedAt", "claimed_at"))
        
        if is_completed(q):
            if claimed:
                status_str = f"{config.EMOJI_SUCCESS} ~~{name}~~ `(Đã nhận quà)`"
            else:
                status_str = f"🎁 **{name}** `[Chờ nhận quà]`"
                unclaimed_quests[qid] = name
        elif completer.current_quest_id == qid:
            seconds_needed = get_seconds_needed(q)
            seconds_done = get_seconds_done(q)
            remaining = max(0, int(seconds_needed - seconds_done))
            eta_str = f"{remaining // 60}p {remaining % 60}s" if remaining >= 60 else f"{remaining}s"
            bar = get_progress_bar(seconds_done, seconds_needed)
            status_str = (
                f"{config.EMOJI_LOADING} **{name}** ({seconds_done:.0f}/{seconds_needed}s) - ⏳ Còn lại: {eta_str}\n"
                f"└─ `{bar}`"
            )
        elif is_enrolled(q):
            status_str = f"⚪ {name} `[{task}]` (Đang chờ)"
        else:
            status_str = f"⚪ {name} `[{task}]` (Chưa nhận)"
            
        desc_lines.append(status_str)

    embed.description = "\n".join(desc_lines)
    embed.set_footer(text="Giao diện cập nhật tự động · Built by TXA")

    if unclaimed_quests:
        view = QuestClaimView(completer, unclaimed_quests, user_id)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, ephemeral=True)


# Command: View detailed active user Discord info (slash command)
@bot.tree.command(name="user_info", description="Xem chi tiết thông tin tài khoản Discord đang chạy")
async def user_info_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in config.ACTIVE_USERS:
        channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "bảng điều khiển chính"
        await interaction.response.send_message(
            f"❌ **Bạn hiện không có phiên hoạt động nào!**\n"
            f"Vui lòng truy cập {channel_mention} để bắt đầu.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    info = config.ACTIVE_USERS[user_id]
    api = info['completer'].api

    r = await api.get("/users/@me")
    if r.status != 200:
        await interaction.followup.send(
            "❌ Không thể truy xuất thông tin tài khoản từ Discord API.",
            ephemeral=True
        )
        return

    user_data = await r.json()
    
    # Extract details
    username = user_data.get("username", "?")
    global_name = user_data.get("global_name") or "Không có"
    uid = user_data.get("id", "?")
    email = user_data.get("email") or "Không có"
    phone = user_data.get("phone") or "Không có"
    mfa = "Bật 🔐" if user_data.get("mfa_enabled") else "Tắt 🔓"
    locale = user_data.get("locale", "en-US")
    
    premium_type = user_data.get("premium_type", 0)
    nitro_map = {
        0: "Không ❌",
        1: "Nitro Classic 🎫",
        2: "Nitro Boost 🚀",
        3: "Nitro Basic 💎"
    }
    nitro_str = nitro_map.get(premium_type, "Không xác định")

    # Creation date from snowflake ID
    try:
        snowflake_time = int(uid) >> 22
        creation_timestamp = int((snowflake_time + 1420070400000) / 1000)
        creation_str = f"<t:{creation_timestamp}:R> (<t:{creation_timestamp}:F>)"
    except Exception:
        creation_str = "Không rõ"

    # Profile Avatar URL
    avatar_hash = user_data.get("avatar")
    if avatar_hash:
        avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=256"
    else:
        try:
            disc = int(user_data.get("discriminator", "0"))
            if disc == 0:
                avatar_index = (int(uid) >> 22) % 6
            else:
                avatar_index = disc % 5
        except Exception:
            avatar_index = 0
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{avatar_index}.png"

    embed = discord.Embed(
        title="👤 Thông Tin Tài Khoản Discord",
        color=discord.Color.from_rgb(32, 102, 168),
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name=f"{username} (@{global_name})", icon_url=avatar_url)
    embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="🆔 User ID", value=f"`{uid}`", inline=True)
    embed.add_field(name="🌐 Quốc gia/Ngôn ngữ", value=f"`{locale}`", inline=True)
    embed.add_field(name="🔒 Bảo mật 2 lớp (MFA)", value=mfa, inline=True)
    
    embed.add_field(name="📧 Email", value=f"`{email}`", inline=True)
    embed.add_field(name="📞 Số điện thoại", value=f"`{phone}`", inline=True)
    embed.add_field(name="💎 Gói Nitro", value=nitro_str, inline=True)
    
    embed.add_field(name="📅 Ngày tạo tài khoản", value=creation_str, inline=False)
    
    embed.set_footer(text="Dữ liệu nhạy cảm chỉ hiển thị dạng Ephemeral cho bạn.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# Command: View system stats (slash command)
@bot.tree.command(name="system_stats", description="Xem thông số kỹ thuật máy chủ và trạng thái bot thời gian thực")
async def system_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    from system_info import build_system_embed, SystemStatsView
    embed = build_system_embed(bot, bot_start_time)
    view = SystemStatsView(bot, bot_start_time)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# Command: Stop active session for calling user (slash command)
@bot.tree.command(name="stop_my_session", description="Dừng phiên làm quest tự động của bạn")
async def stop_my_session(interaction: discord.Interaction):
    user_id = interaction.user.id
    if user_id not in config.ACTIVE_USERS:
        channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "bảng điều khiển chính"
        await interaction.response.send_message(
            f"❌ **Tài khoản của bạn hiện không có phiên hoạt động nào đang chạy!**\n"
            f"Vui lòng truy cập {channel_mention} để bắt đầu.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    
    info = config.ACTIVE_USERS.pop(user_id)
    try:
        await info['completer'].stop()
        if info['task']:
            info['task'].cancel()
    except Exception as e:
        log(f"Lỗi khi người dùng tự dừng phiên: {e}", "warn")

    await bot.update_control_panel()
    await interaction.followup.send(
        "🛑 **Đã dừng thành công tiến trình làm quest tự động của bạn.**\n"
        "Tất cả tài nguyên đã được giải phóng.",
        ephemeral=True
    )


# Command: Admin test QR payment interface (slash command)
@bot.tree.command(name="qr_test", description="[Admin] Gửi giao diện thanh toán thử nghiệm (chọn dropdown để xem QR)")
async def qr_test(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    # Clean previous transaction to allow repeated testing
    from limits_manager import cancel_transaction
    cancel_transaction(interaction.user.id)
    
    from payment_views import get_limit_embed_and_view
    embed, view = get_limit_embed_and_view(interaction.user.id)
    
    # Customize the embed description slightly for admin testing info
    embed.title = "🧪 [Thử nghiệm Admin] Giao Diện Mua Lượt Quest"
    embed.description = (
        "**Đây là giao diện mua lượt thử nghiệm dành riêng cho Admin.**\n"
        "Vui lòng chọn số lượng lượt muốn mua từ menu thả xuống bên dưới để tạo mã VietQR tương ứng."
    )
    
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# Command: Admin set price per quest (slash command)
@bot.tree.command(name="set_price", description="[Admin] Đặt đơn giá cho mỗi lượt làm quest")
@app_commands.describe(price="Đơn giá mới cho mỗi lượt (ví dụ: 10000)")
async def set_price(interaction: discord.Interaction, price: int):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    if price <= 0:
        await interaction.response.send_message("❌ Đơn giá phải lớn hơn 0.", ephemeral=True)
        return

    from limits_manager import set_price_per_quest
    set_price_per_quest(price)
    
    await interaction.response.send_message(
        f"✅ **Đã cập nhật đơn giá thành công!**\n"
        f"Đơn giá mới: **{price:,} VNĐ** cho mỗi lượt làm quest.",
        ephemeral=True
    )


# Command: Admin set transaction timeout (slash command)
@bot.tree.command(name="set_timeout", description="[Admin] Đặt thời gian hết hạn hóa đơn QR")
@app_commands.describe(minutes="Thời gian hết hạn tính bằng phút (ví dụ: 5)")
async def set_timeout(interaction: discord.Interaction, minutes: int):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    if minutes <= 0:
        await interaction.response.send_message("❌ Thời gian hết hạn phải lớn hơn 0 phút.", ephemeral=True)
        return

    from limits_manager import set_transaction_timeout
    seconds = minutes * 60
    set_transaction_timeout(seconds)
    
    await interaction.response.send_message(
        f"✅ **Đã cập nhật thời hạn giao dịch thành công!**\n"
        f"Mã QR hóa đơn sẽ hết hạn sau: **{minutes} phút** ({seconds} giây).",
        ephemeral=True
    )


# Command: Check quest quota balance (slash command)
@bot.tree.command(name="quota", description="Kiểm tra số dư lượt làm quest và hạn mức tài khoản của bạn")
async def check_quota(interaction: discord.Interaction):
    from payment_views import get_limit_embed_and_view
    embed, view = get_limit_embed_and_view(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# Command: Buy quest turns (slash command)
@bot.tree.command(name="buy", description="Mua thêm lượt làm quest tự động (Tạo mã VietQR)")
async def buy_quests(interaction: discord.Interaction):
    from payment_views import get_limit_embed_and_view
    embed, view = get_limit_embed_and_view(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


def run_bot():
    if os.environ.get("TXA_LAUNCHED") != "1":
        log("=" * 65, "error")
        log("❌ BẠN KHÔNG THỂ CHẠY TRỰC TIẾP TỆP BOT.PY!", "error")
        log("👉 Vui lòng khởi động Bot bằng tệp chính quy: python txa.py", "warn")
        log("=" * 65, "error")
        sys.exit(1)

    missing = []
    
    token = config.BOT_TOKEN.strip()
    if not token or "YOUR_DISCORD_BOT_TOKEN" in token or token == "":
        missing.append("BOT_TOKEN (Token của Discord Bot từ Discord Developer Portal)")
        
    if not config.ADMIN_IDS:
        missing.append("ADMIN_IDS (ID Discord tài khoản của bạn để phân quyền Admin)")

    if missing:
        log("=" * 65, "error")
        log("❌ CẤU HÌNH THIẾU HOẶC KHÔNG HỢP LỆ TRONG TỆP .env", "error")
        log("Vui lòng bổ sung đầy đủ các thông tin sau trước khi chạy:", "error")
        for item in missing:
            log(f"   👉 Thiếu: {item}", "error")
        log("💡 Hướng dẫn: Mở tệp `.env`, thay thế các giá trị mặc định bằng thông tin của bạn và lưu lại.", "info")
        log("=" * 65, "error")
        return
    
    banner = f"""{Colors.CYAN}{Colors.BOLD}
============================================================
       🤖 DISCORD QUEST AUTO-COMPLETER BOT v4.0 🤖
                🚀 Built by TXA 🚀
       Mã nguồn đã được tối ưu hóa bất đồng bộ
============================================================{Colors.RESET}"""
    print(banner)
    log("Đang khởi động Discord Bot...", "info")
    
    try:
        bot.run(config.BOT_TOKEN)
    except KeyboardInterrupt:
        log("Đã nhận tín hiệu Ctrl+C. Hệ thống đang tắt...", "warn")

if __name__ == "__main__":
    log("=" * 65, "error")
    log("❌ BẠN KHÔNG THỂ CHẠY TRỰC TIẾP TỆP BOT.PY!", "error")
    log("👉 Vui lòng khởi động Bot bằng tệp chính quy: python txa.py", "warn")
    log("=" * 65, "error")
    sys.exit(1)
