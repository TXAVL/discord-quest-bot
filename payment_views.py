import asyncio
import discord
import time
import config
from limits_manager import (
    get_active_transaction,
    create_transaction,
    cancel_transaction,
    fetch_sepay_transactions,
    match_payment,
    get_pending_transactions,
    complete_transaction,
    get_price_per_quest,
    get_transaction_timeout,
    get_user_quota_info
)


# order_id -> interaction đang hiển thị hóa đơn (để tự cập nhật tin nhắn khi thanh toán xong)
LIVE_INVOICES = {}
AUTO_CHECK_INTERVAL = 6


def register_invoice(interaction: discord.Interaction, view):
    """Gắn interaction đang hiển thị hóa đơn để tự cập nhật khi tiền về."""
    if isinstance(view, QuestPaymentConfirmView):
        LIVE_INVOICES[view.order_id] = interaction


def build_success_embed(tx: dict, user_id: int, auto: bool) -> discord.Embed:
    quota_info = get_user_quota_info(user_id)
    embed = discord.Embed(
        title="🎉 THANH TOÁN THÀNH CÔNG!",
        color=discord.Color.from_rgb(87, 242, 135),
        description=(
            f"### {config.EMOJI_SUCCESS} Hệ Thống Đã Nhận Được Tiền!\n"
            f"{'⚡ Hệ thống đã **tự động phát hiện** chuyển khoản của bạn.' if auto else 'Đã xác thực giao dịch chuyển khoản thành công từ ngân hàng.'}\n\n"
            f"• 📝 **Mã đơn hàng:** `{tx['order_id']}`\n"
            f"• 💰 **Số tiền đã nhận:** `{tx['amount']:,} VNĐ`\n"
            f"• 📦 **Lượt quest được cộng:** `+{tx['quests_to_buy']} lượt`\n"
            f"• 💎 **Số dư khả dụng hiện tại:** **{quota_info['quota_str']}**\n\n"
            f"🚀 Dùng `/details` để theo dõi tiến độ hoặc `/history` để xem lịch sử nạp."
        )
    )
    embed.set_footer(text="⏱️ Tin nhắn này sẽ tự động đóng sau 10 giây • Cảm ơn quý khách")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _schedule_delete(interaction: discord.Interaction, delay: int = 10):
    async def task():
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
    asyncio.create_task(task())


async def payment_watcher(bot):
    """Vòng lặp nền: tự đối soát SePay cho mọi đơn đang chờ và cộng lượt ngay khi tiền về."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            pending = get_pending_transactions()
            if pending and config.SEPAY_API_KEY:
                tx_list = await fetch_sepay_transactions()
                for user_id, tx in pending:
                    if not match_payment(tx_list, tx["order_id"], tx["amount"]):
                        continue
                    done = complete_transaction(user_id, tx["order_id"])
                    if not done:
                        continue
                    embed = build_success_embed(done, user_id, auto=True)
                    inter = LIVE_INVOICES.pop(done["order_id"], None)
                    edited = False
                    if inter:
                        try:
                            await inter.edit_original_response(embed=embed, view=None)
                            _schedule_delete(inter)
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        try:
                            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                            await user.send(embed=embed)
                        except Exception:
                            pass
        except Exception as e:
            from logger import log
            log(f"Lỗi payment_watcher: {e}", "warn")
        await asyncio.sleep(AUTO_CHECK_INTERVAL)


class QuestPurchaseDropdown(discord.ui.Select):
    def __init__(self, user_id):
        price = get_price_per_quest()
        options = [
            discord.SelectOption(label=f"1 Lượt Quest - {price:,}đ", value="1", emoji="💎", description="Mua lẻ 1 lượt làm quest tự động"),
            discord.SelectOption(label=f"2 Lượt Quest - {price*2:,}đ", value="2", emoji="🎫", description="Mua 2 lượt làm quest tự động"),
            discord.SelectOption(label=f"5 Lượt Quest - {price*5:,}đ", value="5", emoji="🔥", description="Tiết kiệm - Mua 5 lượt làm quest"),
            discord.SelectOption(label=f"10 Lượt Quest - {price*10:,}đ", value="10", emoji="👑", description="Gói lớn - Mua 10 lượt làm quest")
        ]
        super().__init__(placeholder="Chọn số lượng lượt quest muốn mua...", min_values=1, max_values=1, options=options)
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Menu này không dành cho bạn.", ephemeral=True)
            return

        # Hiển thị trạng thái khởi tạo giao dịch
        loading_embed = discord.Embed(
            title="⚙️ Đang Khởi Tạo Giao Dịch",
            color=discord.Color.from_rgb(88, 101, 242),
            description=f"{config.EMOJI_LOADING} Hệ thống đang tạo mã đơn hàng và mã VietQR thanh toán, vui lòng đợi..."
        )
        self.disabled = True
        await interaction.response.edit_message(embed=loading_embed, view=None)

        quantity = int(self.values[0])
        
        # Tạo giao dịch mới
        tx = create_transaction(self.user_id, quantity)
        if not tx:
            await interaction.followup.send(
                "❌ **Bạn đang có một giao dịch chưa hoàn tất!**\n"
                "Vui lòng thanh toán giao dịch trước đó hoặc bấm nút **Hủy Giao Dịch** để tạo đơn mới.",
                ephemeral=True
            )
            embed, view = get_limit_embed_and_view(self.user_id)
            await interaction.edit_original_response(embed=embed, view=view)
            return

        # Tải trước ảnh VietQR để tối ưu tốc độ hiển thị
        import aiohttp
        order_id = tx["order_id"]
        amount = tx["amount"]
        qr_url = (
            f"https://img.vietqr.io/image/{config.BANK_ID}-{config.ACCOUNT_NO}-compact.png"
            f"?amount={amount}&addInfo={order_id}&accountName={config.ACCOUNT_NAME.replace(' ', '%20')}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(qr_url, timeout=5) as r:
                    if r.status == 200:
                        await r.read()
        except Exception:
            pass

        # Cập nhật giao diện hóa đơn kèm VietQR
        embed, view = get_limit_embed_and_view(self.user_id)
        LIVE_INVOICES[order_id] = interaction
        await interaction.edit_original_response(embed=embed, view=view)


class QuestLimitPurchaseView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        self.add_item(QuestPurchaseDropdown(user_id))


class QuestPaymentConfirmView(discord.ui.View):
    def __init__(self, user_id, order_id, amount, quantity):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.order_id = order_id
        self.amount = amount
        self.quantity = quantity

    @discord.ui.button(label="Kiểm Tra Ngay", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nút bấm này không dành cho bạn.", ephemeral=True)
            return

        # Kiểm tra xem đơn hàng còn hiệu lực hay đã hết hạn
        tx = get_active_transaction(self.user_id)
        if not tx or tx.get("order_id") != self.order_id:
            embed, view = get_limit_embed_and_view(self.user_id)
            if interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(
                "❌ **Giao dịch đã hết hạn!**\n"
                "Mã QR này không còn hiệu lực. Vui lòng chọn số lượng để tạo đơn mới.",
                ephemeral=True
            )
            return

        await interaction.response.defer()
        LIVE_INVOICES[self.order_id] = interaction

        tx_list = await fetch_sepay_transactions()
        if match_payment(tx_list, self.order_id, self.amount):
            done = complete_transaction(self.user_id, self.order_id)
            if done:
                LIVE_INVOICES.pop(self.order_id, None)
                await interaction.edit_original_response(embed=build_success_embed(done, self.user_id, auto=False), view=None)
                _schedule_delete(interaction)
                return
            # Đơn đã được vòng quét tự động xử lý trước đó
            return

        await interaction.followup.send(
            f"⏳ **Chưa thấy tiền về.**\n"
            f"Chưa quét thấy giao dịch nội dung `{self.order_id}` (**{self.amount:,}đ**).\n"
            f"💡 Không cần bấm lại — hệ thống **tự động kiểm tra mỗi {AUTO_CHECK_INTERVAL}s** và sẽ cộng lượt ngay khi tiền về.",
            ephemeral=True
        )

    @discord.ui.button(label="Hủy Giao Dịch", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nút bấm này không dành cho bạn.", ephemeral=True)
            return

        cancel_transaction(self.user_id)
        LIVE_INVOICES.pop(self.order_id, None)
        
        # Quay về màn hình chọn số lượng mua
        embed, view = get_limit_embed_and_view(self.user_id)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send("🛑 Đã hủy giao dịch hiện tại.", ephemeral=True)


def get_limit_embed_and_view(user_id: int):
    """Lấy Embed và View giao diện thanh toán hoặc giới hạn/số dư phù hợp cho user."""
    tx = get_active_transaction(user_id)
    quota_info = get_user_quota_info(user_id)
    
    if tx:
        # User đang có đơn hàng chờ thanh toán: hiển thị hóa đơn VietQR
        order_id = tx["order_id"]
        amount = tx["amount"]
        quantity = tx["quests_to_buy"]
        created_at = tx["created_at"]
        timeout = get_transaction_timeout()
        expire_ts = created_at + timeout
        
        if timeout < 3600:
            time_limit_str = f"<t:{expire_ts}:R> (vào lúc <t:{expire_ts}:T>)"
        else:
            time_limit_str = f"vào lúc <t:{expire_ts}:F>"
        
        qr_url = (
            f"https://img.vietqr.io/image/{config.BANK_ID}-{config.ACCOUNT_NO}-compact.png"
            f"?amount={amount}&addInfo={order_id}&accountName={config.ACCOUNT_NAME.replace(' ', '%20')}"
        )
        
        embed = discord.Embed(
            title="💳 HÓA ĐƠN CHUYỂN KHOẢN MUA LƯỢT QUEST",
            color=discord.Color.from_rgb(254, 231, 92), # Gold
            description=(
                f"### Quét mã VietQR bên dưới để thanh toán nhanh\n"
                f"App ngân hàng sẽ tự động điền đúng số tài khoản, số tiền và nội dung chuyển khoản.\n\n"
                f"⏳ **Thời hạn thanh toán:** {time_limit_str}\n\n"
                f"⚠️ **LƯU Ý QUAN TRỌNG:**\n"
                f"Hệ thống **tự động phát hiện** chuyển khoản qua SePay 24/7 và cộng lượt ngay, không cần bấm gì. Hãy ghi **chính xác nội dung** `{order_id}`."
            )
        )
        embed.add_field(name="🏦 Ngân Hàng", value=f"**{config.BANK_ID}** (Quân Đội)", inline=True)
        embed.add_field(name="🔢 Số Tài Khoản", value=f"`{config.ACCOUNT_NO}`", inline=True)
        embed.add_field(name="👤 Chủ Tài Khoản", value=f"`{config.ACCOUNT_NAME}`", inline=True)
        embed.add_field(name="💰 Số Tiền", value=f"`{amount:,} VNĐ`", inline=True)
        embed.add_field(name="📝 Nội Dung CK", value=f"`{order_id}`", inline=True)
        embed.add_field(name="📦 Gói Mua", value=f"**+{quantity} lượt làm quest**", inline=True)
        embed.add_field(
            name="💎 Số Dư Hiện Có",
            value=f"• Lượt khả dụng: **{quota_info['quota_str']}**\n• Đã hoàn thành: `{quota_info['total_completed']} nhiệm vụ`",
            inline=False
        )
        
        embed.set_image(url=qr_url)
        embed.set_footer(text="🔄 Tự động kiểm tra mỗi 6 giây — chuyển khoản xong chỉ cần đợi, hoặc bấm 'Kiểm Tra Ngay'.")
        
        view = QuestPaymentConfirmView(user_id, order_id, amount, quantity)
        return embed, view
    else:
        # Không có giao dịch chờ: hiển thị menu mua lượt và số dư hiện có
        price = get_price_per_quest()
        embed = discord.Embed(
            title="💎 CỬA HÀNG LƯỢT QUEST & SỐ DƯ",
            color=discord.Color.from_rgb(88, 101, 242), # Blurple
            description=(
                f"### {config.EMOJI_ROCKET} Mua Thêm Lượt Làm Quest Tự Động\n"
                f"Hệ thống cho phép bạn làm nhiệm vụ Discord Quest nhanh chóng không cần thao tác tay.\n\n"
                f"📊 **Thông Tin Số Dư Của Bạn:**\n"
                f"• 👤 Người dùng: <@{user_id}>\n"
                f"• 💎 Lượt quest hiện có: **{quota_info['quota_str']}**\n"
                f"• 🏆 Đã hoàn thành tích lũy: `{quota_info['total_completed']} nhiệm vụ`\n\n"
                f"💵 **Bảng Giá:** `{price:,} VNĐ` / 1 lượt quest\n"
                f"✨ *Lượt mua không giới hạn thời gian sử dụng và có thể tích lũy.*"
            )
        )
        embed.set_footer(text="Chọn gói lượt bạn muốn mua ở menu bên dưới để tạo mã VietQR.")
        
        view = QuestLimitPurchaseView(user_id)
        return embed, view
