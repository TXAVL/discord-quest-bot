import asyncio
import discord
import time
import config
from limits_manager import (
    get_active_transaction,
    create_transaction,
    cancel_transaction,
    add_purchased_balance,
    verify_payment_on_sepay,
    get_price_per_quest,
    get_transaction_timeout,
    get_user_quota_info
)

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

    @discord.ui.button(label="Xác Nhận Chuyển Khoản", style=discord.ButtonStyle.success, emoji="✅")
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

        # Defer tương tác nút để tránh timeout Discord trong lúc truy vấn SePay
        await interaction.response.defer()
        
        # Gọi SePay kiểm tra chuyển khoản
        is_paid = await verify_payment_on_sepay(self.order_id, self.amount)
        if is_paid:
            # Cộng lượt vào tài khoản
            add_purchased_balance(self.user_id, self.quantity)
            quota_info = get_user_quota_info(self.user_id)
            
            # Embed thông báo thành công xịn sò
            embed = discord.Embed(
                title="🎉 THANH TOÁN THÀNH CÔNG!",
                color=discord.Color.from_rgb(87, 242, 135),
                description=(
                    f"### {config.EMOJI_SUCCESS} Hệ Thống Đã Nhận Được Tiền!\n"
                    f"Đã xác thực giao dịch chuyển khoản thành công từ ngân hàng.\n\n"
                    f"• 📝 **Mã đơn hàng:** `{self.order_id}`\n"
                    f"• 💰 **Số tiền đã nhận:** `{self.amount:,} VNĐ`\n"
                    f"• 📦 **Lượt quest được cộng:** `+{self.quantity} lượt`\n"
                    f"• 💎 **Số dư khả dụng hiện tại:** **{quota_info['quota_str']}**\n\n"
                    f"⚡ **Tiến trình làm quest của bạn đã được kích hoạt chạy tiếp tự động!**"
                )
            )
            embed.set_footer(text="⏱️ Tin nhắn này sẽ tự động đóng sau 10 giây • Cảm ơn quý khách")
            embed.timestamp = discord.utils.utcnow()

            # Chỉnh sửa tin nhắn hiện tại sang embed thành công và gỡ toàn bộ nút
            await interaction.edit_original_response(embed=embed, view=None)

            # Tự động xóa tin nhắn sau 10 giây theo yêu cầu
            async def auto_delete_task():
                await asyncio.sleep(10)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    try:
                        if interaction.message:
                            await interaction.message.delete()
                    except Exception:
                        pass

            asyncio.create_task(auto_delete_task())

        else:
            await interaction.followup.send(
                f"❌ **Chưa nhận được thanh toán!**\n"
                f"Hệ thống SePay chưa quét thấy giao dịch chuyển khoản có nội dung: `{self.order_id}` (Số tiền: **{self.amount:,}đ**).\n\n"
                f"💡 **Mẹo:** Nếu bạn vừa chuyển khoản thành công qua App ngân hàng, hệ thống có thể cần từ 30s đến 1 phút để đồng bộ. Vui lòng đợi một lát rồi bấm lại nút **Xác Nhận Chuyển Khoản**.",
                ephemeral=True
            )

    @discord.ui.button(label="Hủy Giao Dịch", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Nút bấm này không dành cho bạn.", ephemeral=True)
            return

        cancel_transaction(self.user_id)
        
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
                f"Hệ thống kiểm tra tự động 24/7 qua SePay. Bạn bắt buộc phải ghi **chính xác nội dung** `{order_id}` để được cộng lượt tự động ngay tức thì."
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
        embed.set_footer(text=f"Sau khi chuyển tiền xong, vui lòng nhấn nút 'Xác Nhận Chuyển Khoản' bên dưới.")
        
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
