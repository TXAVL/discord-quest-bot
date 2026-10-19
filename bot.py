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
import session_msgs
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
    """Gửi hướng dẫn cài tiện ích lấy token kèm file .crx (người dùng tự đổi đuôi .zip, giải nén rồi nạp thủ công)."""
    from txa_layout import ExtensionGuideView, EXT_FILENAME
    import os

    # Tự động đóng gói bản build mới nhất của CRX trước khi gửi
    make_crx_script = os.path.join(os.path.dirname(__file__), "make_crx.py")
    if os.path.exists(make_crx_script):
        try:
            import make_crx
            make_crx.build_crx()
        except Exception:
            pass

    crx_path = os.path.join(os.path.dirname(__file__), "extension.crx")
    zip_path = os.path.join(os.path.dirname(__file__), "extension.zip")
    target_path = crx_path if os.path.exists(crx_path) else zip_path
    has_file = os.path.exists(target_path)

    view = ExtensionGuideView(interaction.user.id, has_file=has_file)
    if has_file:
        file = discord.File(target_path, filename=EXT_FILENAME)
        await interaction.response.send_message(view=view, file=file, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def handle_claim_reward_response(
    res: dict,
    quest_name: str,
    interaction: discord.Interaction,
    btn: discord.ui.Button = None,
    view: discord.ui.View = None
):
    """Xử lý phản hồi từ API nhận quà Discord. Dính hCaptcha thì hướng dẫn nhận trực tiếp trên Discord (bot không vượt captcha)."""
    from txa_layout import claim_result_view, QUEST_HOME_URL
    uid = interaction.user.id
    is_captcha = (
        res.get("error") == "captcha_required"
        or res.get("captcha") is True
        or "captcha" in str(res.get("details", "")).lower()
        or "captcha-required" in str(res.get("details", ""))
        or "captcha_key" in str(res.get("details", ""))
    )

    async def refresh_message():
        if view and interaction.message:
            try:
                await interaction.message.edit(view=view)
            except Exception:
                pass

    if is_captcha:
        # Thay nút nhận quà bằng nút dẫn thẳng tới trang Quest của Discord
        if btn and view:
            try:
                row = btn.row
                view.remove_item(btn)
                view.add_item(discord.ui.Button(
                    label="Nhận trên Discord", emoji="🎁", style=discord.ButtonStyle.link,
                    url=QUEST_HOME_URL, row=row
                ))
            except Exception:
                pass
        await interaction.followup.send(view=claim_result_view(uid, "captcha", quest_name), ephemeral=True)
        await refresh_message()
        return

    if "error" in res:
        detail = str(res.get("details", res["error"]))
        await interaction.followup.send(view=claim_result_view(uid, "error", quest_name, detail=detail), ephemeral=True)
        return

    code = res.get("code")
    if btn:
        btn.disabled = True
        btn.label = f"Đã nhận: {quest_name[:14]}"
        btn.style = discord.ButtonStyle.secondary
    await interaction.followup.send(view=claim_result_view(uid, "success", quest_name, code=code), ephemeral=True)
    await refresh_message()


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

            target_btn = None
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == f"dm_claim_{quest_id}":
                    target_btn = child
                    break

            res = await self.completer.claim_reward(quest_id)
            await handle_claim_reward_response(res, quest_name, interaction, target_btn, self)
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
            f"• **Phiên đồng thời:** `{len(config.ACTIVE_USERS):02d} phiên đang chạy`"
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
                msg = session_msgs.track(await admin_user.send(embed=embed, view=view))
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

        # Kiểm tra giới hạn lượt quest trước khi tiếp tục
        from limits_manager import is_user_limited
        if is_user_limited(user_id):
            from payment_views import get_limit_embed_and_view, register_invoice
            embed, view = get_limit_embed_and_view(user_id)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            register_invoice(interaction, view)
            return

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
            dm_msg = session_msgs.track(await dm_channel.send(embed=embed, view=view))
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
                    _, view_edit = get_limit_embed_and_view(user_id)
                    await dm_msg.edit(embed=discord.Embed(
                        title="⚠️ Đã Đạt Giới Hạn Quest Hôm Nay",
                        color=discord.Color.orange(),
                        description="Phiên đang tạm dừng. Hãy mua thêm lượt ở tin nhắn bên dưới để tiếp tục."
                    ), view=None)
                    session_msgs.track(await dm_channel.send(view=view_edit))

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
                        dm_msg = session_msgs.track(await dm_channel.send(embed=embed_edit, view=view))

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
                            f"• **Số quest hoàn tất:** `✅ {completed_count:02d}/{total_count:02d} nhiệm vụ`\n"
                            f"• **Tổng quest tích lũy:** `🏆 {user_total:02d} nhiệm vụ`"
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
                        f"• **Tiến độ phiên:** `{overall_bar}` **{overall_ratio:.0f}%** ({completed_count:02d}/{total_count:02d} xong)\n"
                        f"• **Lượt quest khả dụng:** `💎 {quota_info['quota_str']}`\n"
                        f"• **Tổng quest tích lũy:** `🏆 {user_total:02d} nhiệm vụ`"
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
                    eta_text = f"{remaining // 60:02d}p {remaining % 60:02d}s" if remaining >= 60 else f"{remaining:02d}s"

                    embed_edit.add_field(
                        name="⚡ Nhiệm Vụ Đang Xử Lý",
                        value=(
                            f"• **Nhiệm vụ:** **{active_name}**\n"
                            f"• **Loại hình:** `{task_label}`\n"
                            f"• **Tiến độ:** `{quest_bar}` **{pct:.0f}%** (`{int(done):02d}s / {int(needed):02d}s`)\n"
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
                        name=f"📋 Danh Sách Nhiệm Vụ ({completed_count:02d}/{total_count:02d})",
                        value="\n".join(quest_lines[:12]),
                        inline=False
                    )
                else:
                    embed_edit.add_field(name="📋 Danh Sách Nhiệm Vụ", value=status_text, inline=False)

                embed_edit.set_footer(text=f"🔄 Tự động cập nhật • {datetime.now().strftime('%H:%M:%S')} • TXA Quest Bot v4.0")
                try:
                    await dm_msg.edit(embed=embed_edit, view=view)
                except discord.NotFound:
                    dm_msg = session_msgs.track(await dm_channel.send(embed=embed_edit, view=view))

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
                        f"• **Tiến độ tổng:** `{overall_bar}` **{overall_ratio:.0f}%** ({completed_count:02d}/{total_count:02d} xong)\n"
                        f"• **Phiên đồng thời:** `{len(config.ACTIVE_USERS):02d} phiên đang chạy`"
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
                        name=f"📋 Danh Sách Nhiệm Vụ ({completed_count:02d}/{total_count:02d})",
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


TASK_LABELS = {
    "WATCH_VIDEO": "🎬 Xem Video",
    "WATCH_VIDEO_ON_MOBILE": "📱 Xem Video trên Điện Thoại",
    "PLAY_ON_DESKTOP": "🖥️ Chơi Game trên PC/Desktop",
    "STREAM_ON_DESKTOP": "📡 Phát Trực Tuyến (Stream)",
    "PLAY_ACTIVITY": "🕹️ Hoạt Động (Discord Activity)"
}


class SessionDetailsView(discord.ui.View):
    def __init__(self, bot_instance, user_id, completer, unclaimed_quests=None):
        super().__init__(timeout=180)
        self.bot_instance = bot_instance
        self.user_id = user_id
        self.completer = completer
        self.unclaimed_quests = unclaimed_quests or {}

        # Row 0: Các nút nhận quà nếu có
        for qid, qname in list(self.unclaimed_quests.items())[:4]:
            btn = discord.ui.Button(
                label=f"Nhận Quà: {qname[:16]}",
                style=discord.ButtonStyle.success,
                emoji="🎁",
                custom_id=f"claim_det_{qid}_{int(time.time())}",
                row=0
            )
            btn.callback = self.make_claim_callback(qid, qname, btn)
            self.add_item(btn)

        # Row 1: Nút Làm Mới & Nút Dừng Phiên
        btn_refresh = discord.ui.Button(
            label="Làm Mới",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            row=1
        )
        btn_refresh.callback = self.refresh_callback
        self.add_item(btn_refresh)

        btn_stop = discord.ui.Button(
            label="Dừng Phiên",
            style=discord.ButtonStyle.danger,
            emoji="🛑",
            row=1
        )
        btn_stop.callback = self.stop_callback
        self.add_item(btn_stop)

    def make_claim_callback(self, qid, qname, btn):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("❌ Nút bấm này không dành cho bạn.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            if not self.completer:
                await interaction.followup.send("❌ Phiên đã kết thúc, không thể gửi yêu cầu nhận quà.", ephemeral=True)
                return
            res = await self.completer.claim_reward(qid)
            await handle_claim_reward_response(res, qname, interaction, btn, self)
        return callback

    async def refresh_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên bảng này.", ephemeral=True)
            return
        await interaction.response.defer()
        embed, view = await build_session_details_response(self.user_id, interaction.user, self.bot_instance)
        await interaction.edit_original_response(embed=embed, view=view)

    async def stop_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bạn không có quyền dừng phiên của người khác.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        info = config.ACTIVE_USERS.pop(self.user_id, None)
        if info:
            try:
                await info['completer'].stop()
                if info.get('task'):
                    info['task'].cancel()
            except Exception as e:
                log(f"Lỗi khi dừng phiên {self.user_id}: {e}", "warn")
            await self.bot_instance.update_control_panel()
            await interaction.followup.send("🛑 **Đã dừng thành công phiên làm quest của bạn!**", ephemeral=True)
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
        else:
            await interaction.followup.send("ℹ️ Phiên này đã kết thúc trước đó.", ephemeral=True)


class IdleDetailsView(discord.ui.View):
    def __init__(self, bot_instance, user_id):
        super().__init__(timeout=180)
        self.bot_instance = bot_instance
        self.user_id = user_id

    @discord.ui.button(label="Làm Mới", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên bảng này.", ephemeral=True)
            return
        await interaction.response.defer()
        embed, view = await build_session_details_response(self.user_id, interaction.user, self.bot_instance)
        await interaction.edit_original_response(embed=embed, view=view)

    @discord.ui.button(label="Mua Lượt (VietQR)", style=discord.ButtonStyle.success, emoji="💳", row=0)
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nút này không dành cho bạn.", ephemeral=True)
            return
        from payment_views import get_limit_embed_and_view, register_invoice
        _, view = get_limit_embed_and_view(self.user_id)
        await interaction.response.send_message(view=view, ephemeral=True)
        register_invoice(interaction, view)


async def build_session_details_response(user_id: int, discord_user: discord.User, bot_instance):
    from limits_manager import get_user_quota_info, get_user_total_completed
    from system_info import make_bar

    quota = get_user_quota_info(user_id)
    user_total = get_user_total_completed(user_id)

    # 1. TRƯỜNG HỢP: KHÔNG CÓ PHIÊN HOẠT ĐỘNG
    if user_id not in config.ACTIVE_USERS:
        channel_mention = f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "Bảng điều khiển chính"
        embed = discord.Embed(
            title="📋 BÁO CÁO CHI TIẾT TÀI KHOẢN & SỐ DƯ QUEST",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=f"Người dùng: {discord_user.name}", icon_url=discord_user.display_avatar.url)
        embed.set_thumbnail(url=discord_user.display_avatar.url)

        embed.add_field(
            name="📊 Tình Trạng Tài Khoản",
            value=(
                f"• **Trạng thái:** `💤 Hiện không có phiên chạy nào`\n"
                f"• **Tài khoản Discord:** <@{user_id}> (ID: `{user_id}`)\n"
                f"• **Kênh hoạt động:** {channel_mention}"
            ),
            inline=False
        )

        if quota['is_admin'] and getattr(config, "ADMIN_BYPASS_LIMIT", True):
            free_status = "♾️ Vô hạn (Admin không bị trừ lượt)"
        else:
            free_status = "✅ Còn 01 lượt (Làm miễn phí ngay hôm nay)" if quota['free_available'] else "❌ Đã dùng hết hôm nay (Hệ thống reset lúc 00:00)"
        embed.add_field(
            name="💎 Hạn Mức & Số Dư Làm Quest",
            value=(
                f"• **Lượt khả dụng:** `💎 {quota['quota_str']}`\n"
                f"• **Lượt miễn phí hôm nay:** `{free_status}`\n"
                f"• **Lượt mua trả phí dự trữ:** `{quota['purchased_balance']:02d} lượt`\n"
                f"• **Tổng quest đã hoàn thành:** `🏆 {user_total:02d} nhiệm vụ`"
            ),
            inline=False
        )

        embed.add_field(
            name="🚀 Hướng Dẫn Bắt Đầu & Nhận Thưởng",
            value=(
                f"1️⃣ Nhấp nút **🚀 Bắt Đầu** tại {channel_mention} và nhập token Discord.\n"
                f"2️⃣ Bot sẽ tự động quét, nhận và hoàn thành 100% nhiệm vụ trong nền.\n"
                f"3️⃣ Nhận Gift Code hoặc trang trí tài khoản ngay sau khi hoàn thành.\n"
                f"💡 *Bấm **💳 Mua Lượt (VietQR)** bên dưới nếu bạn muốn tích lũy thêm lượt chạy.*"
            ),
            inline=False
        )
        embed.set_footer(text="TXA Discord Quest Bot • Cập nhật tự động")
        view = IdleDetailsView(bot_instance, user_id)
        return embed, view

    # 2. TRƯỜNG HỢP: ĐANG CÓ PHIÊN HOẠT ĐỘNG
    info = config.ACTIVE_USERS[user_id]
    completer = info.get('completer')
    start_ts = info.get('start_timestamp', int(time.time()))
    acc_username = info.get('username', discord_user.name)

    raw_quests = getattr(completer, "last_quests", None) or []
    if not raw_quests and completer:
        try:
            raw_quests = await completer.fetch_quests()
        except Exception:
            raw_quests = []

    active_quests = [
        q for q in raw_quests
        if (is_completed(q) or not is_expired(q)) and q.get("id") not in getattr(completer, "failed_404_ids", set())
    ] if raw_quests else []

    completed_quests = [q for q in active_quests if is_completed(q)]
    completed_count = len(completed_quests)
    total_count = len(active_quests)
    overall_ratio = (completed_count / total_count * 100) if total_count > 0 else 0
    overall_bar = make_bar(overall_ratio, length=12)

    is_all_finished = (completed_count == total_count and total_count > 0)
    color = discord.Color.green() if is_all_finished else discord.Color.from_rgb(88, 101, 242)
    status_badge = "🟢 Đã Hoàn Thành 100%" if is_all_finished else "🚀 Đang Tự Động Làm Quest"

    embed = discord.Embed(
        title="⚡ CHI TIẾT TIẾN ĐỘ HOẠT ĐỘNG THỜI GIAN THỰC",
        color=color,
        timestamp=discord.utils.utcnow()
    )
    embed.set_author(name=f"Tài khoản Discord: {acc_username}", icon_url=discord_user.display_avatar.url)
    embed.set_thumbnail(url=discord_user.display_avatar.url)

    # Khối 1: Tổng quan phiên
    embed.add_field(
        name="📊 Tổng Quan Phiên Chạy",
        value=(
            f"• **Trạng thái:** `{status_badge}`\n"
            f"• **Bắt đầu lúc:** <t:{start_ts}:R> *(lúc <t:{start_ts}:T>)*\n"
            f"• **Tiến độ hoàn tất:** `{overall_bar}` **{overall_ratio:.1f}%** (`{completed_count:02d}/{total_count:02d}` nhiệm vụ)\n"
            f"• **Lượt khả dụng:** `💎 {quota['quota_str']}`\n"
            f"• **Tổng quest tích lũy:** `🏆 {user_total:02d} nhiệm vụ`"
        ),
        inline=False
    )

    # Khối 2: Nhiệm vụ đang chạy (Highlight)
    active_q = None
    if completer and completer.current_quest_id and active_quests:
        for q in active_quests:
            if q.get("id") == completer.current_quest_id:
                active_q = q
                break

    if active_q and not is_all_finished:
        q_name = get_quest_name(active_q)
        q_task = get_task_type(active_q) or "?"
        task_label = TASK_LABELS.get(q_task, f"🎮 {q_task}")
        needed = get_seconds_needed(active_q)
        done = get_seconds_done(active_q)
        if hasattr(completer, "current_quest_info") and completer.current_quest_info:
            done = max(done, completer.current_quest_info.get("done", done))
            needed = max(needed, completer.current_quest_info.get("needed", needed))
        pct = (done / needed * 100) if needed > 0 else 0
        qbar = make_bar(pct, length=10)
        rem = max(0, int(needed - done))
        eta_str = f"{rem // 60:02d} phút {rem % 60:02d} giây" if rem >= 60 else f"{rem:02d} giây"

        embed.add_field(
            name="⚡ Nhiệm Vụ Đang Xử Lý",
            value=(
                f"• **Nhiệm vụ:** **{q_name}**\n"
                f"• **Hình thức:** `{task_label}`\n"
                f"• **Tiến độ:** `{qbar}` **{pct:.1f}%** (`{int(done):02d}s / {int(needed):02d}s`)\n"
                f"• **Thời gian còn lại:** `⏳ ~{eta_str}`"
            ),
            inline=False
        )

    # Khối 3: Phân loại chi tiết danh sách nhiệm vụ
    unclaimed_quests = {}
    completed_lines = []
    pending_lines = []

    for q in active_quests:
        name = get_quest_name(q)
        task = get_task_type(q) or "?"
        task_str = TASK_LABELS.get(task, task)
        qid = q.get("id")
        us = get_user_status(q)
        claimed = bool(_get(us, "claimedAt", "claimed_at"))

        if is_completed(q):
            if claimed:
                completed_lines.append(f"✅ ~~{name}~~ `(Đã nhận quà)`")
            else:
                completed_lines.append(f"🎁 **{name}** `[Chờ nhận quà - Nhấn nút dưới]`")
                unclaimed_quests[qid] = name
        elif active_q and qid == active_q.get("id"):
            pass  # Đã hiện ở mục nổi bật bên trên
        else:
            status_text = "Đang chờ đến lượt" if is_enrolled(q) else "Chưa nhận"
            pending_lines.append(f"⚪ **{name}** • `{task_str}` *({status_text})*")

    if completed_lines:
        embed.add_field(
            name=f"🟢 Đã Hoàn Thành ({len(completed_lines):02d}/{total_count:02d} nhiệm vụ)",
            value="\n".join(completed_lines[:10]),
            inline=False
        )

    if pending_lines:
        embed.add_field(
            name=f"⏳ Danh Sách Chờ Xử Lý ({len(pending_lines):02d} nhiệm vụ)",
            value="\n".join(pending_lines[:10]),
            inline=False
        )

    if not active_quests:
        embed.add_field(
            name="📭 Danh Sách Nhiệm Vụ",
            value="*Đang tải danh sách nhiệm vụ từ Discord... Vui lòng bấm 🔄 Làm Mới sau vài giây.*",
            inline=False
        )

    embed.set_footer(text="🔄 Bấm Làm Mới để xem số giây live · 🛑 Dừng Phiên · Built by TXA")
    view = SessionDetailsView(bot_instance, user_id, completer, unclaimed_quests)
    return embed, view


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

        from limits_manager import is_user_limited
        if is_user_limited(interaction.user.id):
            from payment_views import get_limit_embed_and_view, register_invoice
            embed, view = get_limit_embed_and_view(interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            register_invoice(interaction, view)
            return
        
        modal = TokenModal(self.bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Chi Tiết", style=discord.ButtonStyle.secondary, emoji="📋", custom_id="btn_details")
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_channels = set()
        if config.CHANNEL_ID:
            allowed_channels.add(config.CHANNEL_ID)
        if hasattr(config, 'SERVER_CHANNELS') and config.SERVER_CHANNELS:
            allowed_channels.update(config.SERVER_CHANNELS.values())

        if allowed_channels and interaction.channel_id not in allowed_channels:
            await interaction.response.send_message("❌ Nút bấm này không thể sử dụng ở kênh này.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        embed, view = await build_session_details_response(interaction.user.id, interaction.user, self.bot)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

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

        from payment_views import get_limit_embed_and_view, register_invoice
        embed, view = get_limit_embed_and_view(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        register_invoice(interaction, view)

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
    await asyncio.sleep(0.5)
    try:
        await session_msgs.purge_tracked(bot)
    except Exception as e:
        log(f"Lỗi dọn tin nhắn phiên: {e}", "warn")

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

        from limits_manager import roll_discounts
        roll_discounts()
        from payment_views import payment_watcher
        self.loop.create_task(payment_watcher(self))
        log("Đã bật tự động đối soát thanh toán SePay", "ok")

        # Fetch build number
        config.BUILD_NUMBER = await fetch_latest_build_number()

    async def on_ready(self):
        log(f"Bot đăng nhập thành công với tên: {Colors.BOLD}{self.user}{Colors.RESET}", "ok")
        
        try:
            import txa_layout
            txa_layout.configure(avatar=str(self.user.display_avatar.url), invite=get_bot_invite_url(self))
        except Exception as e:
            log(f"Không cấu hình được ảnh đại diện/link mời cho giao diện: {e}", "warn")

        # Dọn tin nhắn phiên của lần chạy trước (nút cũ không còn hoạt động)
        await session_msgs.purge_tracked(self)

        # Load saved channel config
        load_channel_config()

        # Update/Create Control Panel in channel
        await self.update_control_panel()

        # Chỉ đồng bộ GLOBAL. Xóa bản sao lệnh cấp guild (nguyên nhân khiến mỗi lệnh hiện 2 lần)
        for guild in self.guilds:
            try:
                self.tree.clear_commands(guild=guild)
                await self.tree.sync(guild=guild)
                log(f"Đã dọn lệnh trùng ở máy chủ: {guild.name}", "ok")
            except Exception as ge:
                log(f"Lỗi dọn lệnh guild {guild.id}: {ge}", "warn")
        try:
            synced = await self.tree.sync()
            log(f"Đã sync toàn cục {len(synced)} slash commands.", "ok")
        except Exception as e:
            log(f"Lỗi sync slash commands: {e}", "error")

    async def on_interaction(self, interaction: discord.Interaction):
        """Bắt các nút/menu của tin nhắn cũ (trước khi bot khởi động lại) để báo rõ thay vì 'không phản hồi kịp thời'."""
        if interaction.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return
        await asyncio.sleep(2.5)
        if interaction.response.is_done():
            return
        try:
            await interaction.response.send_message(
                "⌛ **Bảng này đã hết hạn** (bot vừa khởi động lại hoặc để quá lâu).\n"
                "Vui lòng gõ lại lệnh để mở bảng mới.",
                ephemeral=True
            )
        except Exception:
            pass

    async def on_guild_join(self, guild: discord.Guild):
        """Tự động xử lý khi Bot được mời vào một Server mới."""
        log(f"🎉 Bot vừa được thêm vào máy chủ mới: {guild.name} (ID: {guild.id})", "ok")
        
        # 1. Tự động sync slash commands sang Server mới ngay lập tức
        try:
            log(f"Slash commands toàn cục sẽ tự áp dụng cho guild mới: {guild.name}", "ok")
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
                embed.add_field(name="👤 Phiên hiện tại", value=f"```\n{active_count:02d}\n```", inline=True)
                embed.add_field(name="⏳ Hàng chờ", value="```\n00\n```", inline=True)
                embed.add_field(name="▶️ Đang chạy", value=f"```\n{active_count:02d}\n```", inline=True)
                embed.add_field(name="✅ Tổng quest đã hoàn thành", value=f"```\n{config.TOTAL_COMPLETED:02d}\n```", inline=False)
                
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
@app_commands.default_permissions(administrator=True)
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
@app_commands.default_permissions(administrator=True)
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
    from txa_layout import invite_view
    await interaction.response.send_message(view=invite_view(interaction.user.id), ephemeral=True)


def is_admin_or_owner(interaction: discord.Interaction) -> bool:
    if interaction.user.id in config.ADMIN_IDS:
        return True
    if interaction.guild and getattr(interaction.user, 'guild_permissions', None) and interaction.user.guild_permissions.administrator:
        return True
    return False


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
        from limits_manager import is_user_limited
        if is_user_limited(interaction.user.id):
            from payment_views import get_limit_embed_and_view, register_invoice
            embed, view = get_limit_embed_and_view(interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            register_invoice(interaction, view)
            return

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
    if not is_admin_or_owner(interaction):
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    from txa_layout import StatusLayoutView
    await interaction.response.send_message(
        view=StatusLayoutView(interaction.user.id, bot, bot_start_time), ephemeral=True
    )


# Slash Command: View detailed bot configuration (slash command for Admin)
@bot.tree.command(name="config", description="[Admin] Xem cấu hình chi tiết của hệ thống Bot")
@app_commands.default_permissions(administrator=True)
async def view_config(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    from txa_layout import ConfigLayoutView
    await interaction.response.send_message(view=ConfigLayoutView(interaction.user.id), ephemeral=True)


# Prefix Command (Hoàn toàn ẩn khỏi menu Slash Command): !admin_bypass [true/false]
@bot.command(name="admin_bypass")
async def cmd_admin_bypass(ctx: commands.Context, enabled: str = None):
    if ctx.author.id not in config.ADMIN_IDS:
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if enabled is None:
        curr = getattr(config, "ADMIN_BYPASS_LIMIT", True)
        status_txt = "👑 **BẬT (Admin vô hạn lượt làm quest)**" if curr else "🛡️ **TẮT (Admin bị giới hạn lượt như user thường)**"
        await ctx.send(f"⚙️ Trạng thái `ADMIN_BYPASS_LIMIT` hiện tại: {status_txt}", delete_after=15)
        return

    is_on = enabled.strip().lower() in ("true", "1", "yes", "on", "bat", "bật")
    config.update_env_variable("ADMIN_BYPASS_LIMIT", str(is_on))
    status_txt = "👑 **BẬT (Admin vô hạn lượt làm quest)**" if is_on else "🛡️ **TẮT (Admin bị tính và giới hạn lượt như user thường)**"
    embed = discord.Embed(
        title="⚙️ Cập Nhật Cấu Hình Admin Bypass",
        color=discord.Color.green() if is_on else discord.Color.orange(),
        description=(
            f"✅ Đã cập nhật `ADMIN_BYPASS_LIMIT` thành: {status_txt}\n\n"
            f"• **RAM:** Áp dụng ngay lập tức\n"
            f"• **File:** Đã lưu vào file `.env`"
        ),
        timestamp=discord.utils.utcnow()
    )
    await ctx.send(embed=embed, delete_after=15)


# Prefix Command (Hoàn toàn ẩn khỏi menu Slash Command): !set_env <key> <val>
@bot.command(name="set_env")
async def cmd_set_env(ctx: commands.Context, key: str = None, value: str = None):
    if ctx.author.id not in config.ADMIN_IDS:
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if not key or not value:
        await ctx.send("❌ Cú pháp: `!set_env <KEY> <VALUE>` (Ví dụ: `!set_env ADMIN_BYPASS_LIMIT False`)", delete_after=10)
        return

    clean_key = key.strip().upper()
    clean_val = value.strip()

    if clean_key in ("ADMIN_BYPASS_LIMIT", "AUTO_ACCEPT", "LOG_PROGRESS", "DEBUG"):
        if clean_val.lower() not in ("true", "false", "1", "0", "yes", "no"):
            await ctx.send(f"❌ Biến `{clean_key}` chỉ chấp nhận giá trị `True` hoặc `False`.", delete_after=10)
            return
    elif clean_key in ("POLL_INTERVAL", "HEARTBEAT_INTERVAL"):
        if not clean_val.isdigit() or int(clean_val) < 1:
            await ctx.send(f"❌ Biến `{clean_key}` phải là số nguyên dương (giây).", delete_after=10)
            return

    config.update_env_variable(clean_key, clean_val)
    embed = discord.Embed(
        title="⚙️ Đã Cập Nhật Biến Môi Trường",
        color=discord.Color.green(),
        description=(
            f"✅ Biến **`{clean_key}`** đã được đặt thành: **`{clean_val}`**\n\n"
            f"• **RAM:** Áp dụng ngay lập tức\n"
            f"• **File:** Đã ghi vào `.env` vĩnh viễn"
        ),
        timestamp=discord.utils.utcnow()
    )
    await ctx.send(embed=embed, delete_after=15)


# Command: Admin set quickly quest limits/balance (slash command for Admin)
@bot.tree.command(name="set_limit", description="[Admin] Đặt số lượt làm quest (số dư) cho người dùng")
@app_commands.default_permissions(administrator=True)
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
    
    old_balance = limits[uid].get("purchased_balance", 0)
    limits[uid]["purchased_balance"] = balance
    save_limits(limits)

    embed = discord.Embed(
        title="🎯 Đặt Lại Số Dư Thành Công",
        color=discord.Color.from_rgb(88, 101, 242),
        description=f"Đã **đặt** số dư lượt cho <@{uid}>."
    )
    embed.add_field(name="👤 Người dùng", value=f"<@{uid}> (ID: `{uid}`)", inline=False)
    embed.add_field(name="📉 Trước", value=f"`{old_balance:02d} lượt`", inline=True)
    embed.add_field(name="📈 Sau", value=f"`{balance:02d} lượt`", inline=True)
    embed.add_field(name="🔁 Thay đổi", value=f"`{balance - old_balance:+d}`", inline=True)
    embed.set_footer(text="/set_limit đặt số tuyệt đối • /add_balance để cộng thêm")

    from txa_layout import embed_to_view
    await interaction.response.send_message(view=embed_to_view(embed, interaction.user.id), ephemeral=True)


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
@app_commands.default_permissions(administrator=True)
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


async def get_stop_user_choices(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    if not is_admin_or_owner(interaction):
        return []

    if not config.ACTIVE_USERS:
        return [app_commands.Choice(name="⚠️ Hiện không có phiên nào đang chạy trong hệ thống", value="none")]

    choices = []
    current_lower = current.strip().lower()
    for uid, info in list(config.ACTIVE_USERS.items()):
        uname = info.get("username", "Unknown")
        d_user = info.get("discord_user")
        d_name = d_user.name if d_user else ""
        completer = info.get("completer")

        curr_info = getattr(completer, "current_quest_info", None) if completer else None
        if curr_info:
            q_name = curr_info.get("name", "Quest")[:14]
            done = curr_info.get("done", 0)
            needed = curr_info.get("needed", 1)
            pct = int((done / needed * 100)) if needed else 0
            detail = f"[{q_name} {pct}%]"
        else:
            detail = "[Đang quét]"

        display_label = f"🛑 {uname} {detail} • ID: {uid}"
        if d_name and d_name != uname:
            display_label = f"🛑 {uname} ({d_name}) {detail} • ID: {uid}"

        if not current_lower or current_lower in display_label.lower() or current_lower in str(uid):
            choices.append(app_commands.Choice(name=display_label[:100], value=str(uid)))
            if len(choices) >= 25:
                break
    return choices


# Command: Force stop a user (slash command)
@bot.tree.command(name="stop_user", description="[Admin] Dừng tiến trình tự động làm quest của một người dùng")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_discord_id="Chọn người dùng đang có phiên chạy cần dừng")
async def stop_user(interaction: discord.Interaction, user_discord_id: str):
    if not is_admin_or_owner(interaction):
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    if user_discord_id == "none" or not user_discord_id.isdigit():
        await interaction.response.send_message("❌ Vui lòng chọn một phiên hoạt động hợp lệ từ danh sách gợi ý.", ephemeral=True)
        return

    uid = int(user_discord_id)
    if uid not in config.ACTIVE_USERS:
        await interaction.response.send_message("❌ Người dùng này hiện không hoạt động hoặc phiên đã kết thúc trước đó.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    info = config.ACTIVE_USERS.pop(uid, None)
    if info:
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
    else:
        await interaction.followup.send("❌ Phiên chạy đã kết thúc.", ephemeral=True)


@stop_user.autocomplete("user_discord_id")
async def stop_user_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await get_stop_user_choices(interaction, current)


# Command: Short /stop alias (handles both Admin stopping users & User stopping self)
@bot.tree.command(name="stop", description="Dừng phiên làm quest (Admin dừng người khác, User dừng chính mình)")
@app_commands.describe(user_discord_id="[Admin] Chọn người dùng đang chạy cần dừng (để trống nếu muốn dừng chính bạn)")
async def stop_command(interaction: discord.Interaction, user_discord_id: str = None):
    if user_discord_id and user_discord_id != "none":
        await stop_user(interaction, user_discord_id)
    else:
        await stop_my_session(interaction)


@stop_command.autocomplete("user_discord_id")
async def stop_command_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return await get_stop_user_choices(interaction, current)


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
            target_btn = None
            for child in self.children:
                if isinstance(child, discord.ui.Button) and child.custom_id == f"claim_{quest_id}":
                    target_btn = child
                    break

            res = await self.completer.claim_reward(quest_id)
            await handle_claim_reward_response(res, quest_name, interaction, target_btn, self)
        return callback


# Command: Help command (slash command)
@bot.tree.command(name="help", description="Xem hướng dẫn sử dụng và tải tiện ích TXA Discord Token Retriever (.crx)")
async def help_command(interaction: discord.Interaction):
    from txa_layout import HelpLayoutView
    await interaction.response.send_message(view=HelpLayoutView(interaction.user.id), ephemeral=True)


# Command: View active session details (slash command)
@bot.tree.command(name="details", description="Xem chi tiết tiến độ nhiệm vụ thời gian thực hoặc số dư của bạn")
async def details_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed, view = await build_session_details_response(interaction.user.id, interaction.user, bot)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# Command: View active user quests (slash command alias for details)
@bot.tree.command(name="my_quests", description="Xem trạng thái các quest và nhận phần thưởng Gift Code")
async def my_quests(interaction: discord.Interaction):
    await details_command(interaction)


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

    from txa_layout import SimpleLayoutView
    display = global_name if global_name != "Không có" else username
    body = (
        f"```yaml\nUsername : {username}\nUser ID  : {uid}\nLocale   : {locale}\n```\n"
        f"💎 **Nitro:** {nitro_str}  •  🔒 **MFA:** {mfa}\n"
        f"📧 **Email:** ||`{email}`||\n"
        f"📞 **SĐT:** ||`{phone}`||\n"
        f"📅 **Tạo tài khoản:** {creation_str}"
    )
    view = SimpleLayoutView(
        interaction.user.id, f"👤 {display}", body, thumb=avatar_url,
        footer="Email & SĐT được ẩn (spoiler) • Chỉ bạn thấy tin này",
    )
    await interaction.followup.send(view=view, ephemeral=True)


# Command: View system stats (slash command)
@bot.tree.command(name="system_stats", description="Xem thông số kỹ thuật máy chủ và trạng thái bot thời gian thực")
async def system_stats(interaction: discord.Interaction):
    from txa_layout import SystemLayoutView
    view = SystemLayoutView(interaction.user.id, bot, bot_start_time)
    await interaction.response.send_message(view=view, ephemeral=True)
    view.start(interaction)


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
@app_commands.default_permissions(administrator=True)
async def qr_test(interaction: discord.Interaction):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin được cấu hình mới có quyền.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    # Clean previous transaction to allow repeated testing
    from limits_manager import cancel_transaction
    cancel_transaction(interaction.user.id)
    
    from payment_views import get_limit_embed_and_view, register_invoice
    _, view = get_limit_embed_and_view(interaction.user.id)
    await interaction.followup.send(view=view, ephemeral=True)
    register_invoice(interaction, view)


# Command: Admin set price per quest (slash command)
@bot.tree.command(name="set_price", description="[Admin] Đặt đơn giá cho mỗi lượt làm quest")
@app_commands.default_permissions(administrator=True)
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
    
    from txa_layout import text_card
    from limits_manager import PACKAGES, get_package_info
    table = "\n".join(
        f"• **{q} lượt** — {get_package_info(q)['final']:,}đ" + (f" `-{get_package_info(q)['pct']}%`" if get_package_info(q)['pct'] else "")
        for q in PACKAGES
    )
    await interaction.response.send_message(view=text_card(
        interaction.user.id, "✅ Đã Cập Nhật Đơn Giá",
        f"Đơn giá mới: **{price:,} VNĐ** / lượt.\n\n**🎁 Ưu đãi mới (vừa quay ngẫu nhiên):**\n{table}",
        footer="Đổi giá sẽ tự quay lại mức giảm của các gói"
    ), ephemeral=True)


# Command: Admin set transaction timeout (slash command)
@bot.tree.command(name="set_timeout", description="[Admin] Đặt thời gian hết hạn hóa đơn QR")
@app_commands.default_permissions(administrator=True)
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
    
    from txa_layout import text_card
    await interaction.response.send_message(view=text_card(
        interaction.user.id, "✅ Đã Cập Nhật Thời Hạn Hóa Đơn",
        f"Mã QR hóa đơn sẽ hết hạn sau **{minutes} phút** ({seconds} giây).",
        footer="Áp dụng cho các hóa đơn tạo từ bây giờ"
    ), ephemeral=True)


# Command: Buy quest turns (slash command)
@bot.tree.command(name="buy", description="Mua thêm lượt làm quest tự động (Tạo mã VietQR)")
async def buy_quests(interaction: discord.Interaction):
    from payment_views import get_limit_embed_and_view, register_invoice
    _, view = get_limit_embed_and_view(interaction.user.id)
    await interaction.response.send_message(view=view, ephemeral=True)
    register_invoice(interaction, view)


# Command: Payment history
@bot.tree.command(name="history", description="Xem lịch sử nạp lượt của bạn")
async def history_command(interaction: discord.Interaction):
    from txa_layout import history_view
    await interaction.response.send_message(view=history_view(interaction.user.id), ephemeral=True)


# Command: Leaderboard
@bot.tree.command(name="leaderboard", description="Bảng xếp hạng người hoàn thành nhiều quest nhất")
async def leaderboard_command(interaction: discord.Interaction):
    from txa_layout import leaderboard_view
    await interaction.response.send_message(view=leaderboard_view(interaction.user.id), ephemeral=True)


# Command: Ping
@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_command(interaction: discord.Interaction):
    from txa_layout import ping_view
    await interaction.response.send_message(view=ping_view(interaction.user.id, bot), ephemeral=True)


# Command: Admin add balance (cộng thêm lượt cho 1 người, kể cả chính admin)
@bot.tree.command(name="add_balance", description="[Admin] Cộng thêm lượt làm quest cho một người dùng (kể cả chính bạn)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_discord_id="Chọn hoặc nhập ID Discord của người nhận", amount="Số lượt cộng thêm (số âm để trừ bớt)")
async def add_balance_command(interaction: discord.Interaction, user_discord_id: str, amount: int):
    if interaction.user.id not in config.ADMIN_IDS:
        await interaction.response.send_message("❌ **Bạn không có quyền thực hiện lệnh này!** Chỉ Admin mới có quyền.", ephemeral=True)
        return
    if not user_discord_id.isdigit():
        await interaction.response.send_message("❌ ID người dùng không hợp lệ.", ephemeral=True)
        return
    if amount == 0:
        await interaction.response.send_message("❌ Số lượt cộng phải khác 0.", ephemeral=True)
        return

    from limits_manager import load_limits, save_limits
    uid = user_discord_id
    limits = load_limits()
    if not isinstance(limits.get(uid), dict):
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0,
            "total_completed": 0,
            "active_transaction": None
        }
    old_balance = limits[uid].get("purchased_balance", 0)
    new_balance = max(0, old_balance + amount)
    limits[uid]["purchased_balance"] = new_balance
    save_limits(limits)

    is_self = str(interaction.user.id) == uid
    embed = discord.Embed(
        title="➕ Cộng Lượt Thành Công" if amount > 0 else "➖ Trừ Lượt Thành Công",
        color=discord.Color.green() if amount > 0 else discord.Color.orange(),
        description=f"Đã {'cộng' if amount > 0 else 'trừ'} **{abs(amount)} lượt** cho {'**chính bạn**' if is_self else f'<@{uid}>'}."
    )
    embed.add_field(name="👤 Người dùng", value=f"<@{uid}> (ID: `{uid}`)", inline=False)
    embed.add_field(name="📉 Trước", value=f"`{old_balance:02d} lượt`", inline=True)
    embed.add_field(name="📈 Sau", value=f"`{new_balance:02d} lượt`", inline=True)
    embed.add_field(name="🔁 Thay đổi", value=f"`{new_balance - old_balance:+d}`", inline=True)
    embed.set_footer(text="/add_balance cộng dồn • /set_limit để đặt số tuyệt đối")
    from txa_layout import embed_to_view
    await interaction.response.send_message(view=embed_to_view(embed, interaction.user.id), ephemeral=True)


@add_balance_command.autocomplete("user_discord_id")
async def add_balance_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    return await set_limit_autocomplete(interaction, current)


# Command: Cascading menu hub
@bot.tree.command(name="menu", description="Trung tâm điều khiển: menu 3 cấp (mua lượt, thống kê, hỗ trợ)")
async def menu_command(interaction: discord.Interaction):
    from txa_layout import MenuLayoutView
    await interaction.response.send_message(view=MenuLayoutView(interaction.user.id), ephemeral=True)


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
