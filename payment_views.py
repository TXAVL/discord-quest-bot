import asyncio

import aiohttp
import discord

import config
from txa_layout import (
    TxaView, box, row_of, btn, _resolve, MISSING, PACKAGE_EMOJI,
    GREEN, YELLOW, ORANGE,
)
from limits_manager import (
    get_active_transaction,
    create_transaction,
    cancel_transaction,
    fetch_sepay_transactions,
    match_payment,
    get_pending_transactions,
    complete_transaction,
    get_price_per_quest,
    get_package_info,
    PACKAGES,
    get_transaction_timeout,
    get_user_quota_info,
)

# order_id -> interaction đang hiển thị hóa đơn (để tự cập nhật tin nhắn khi thanh toán xong)
LIVE_INVOICES = {}
AUTO_CHECK_INTERVAL = 6


def qr_url(order_id: str, amount: int) -> str:
    return (
        f"https://img.vietqr.io/image/{config.BANK_ID}-{config.ACCOUNT_NO}-compact.png"
        f"?amount={amount}&addInfo={order_id}&accountName={config.ACCOUNT_NAME.replace(' ', '%20')}"
    )


def register_invoice(interaction: discord.Interaction, view):
    """Gắn interaction đang hiển thị hóa đơn để tự cập nhật khi tiền về."""
    if isinstance(view, InvoiceLayoutView):
        LIVE_INVOICES[view.order_id] = interaction


def build_success_view(tx: dict, user_id: int, auto: bool) -> TxaView:
    info = get_user_quota_info(user_id)
    view = TxaView(user_id, timeout=60)
    view.add_item(box(
        "### 🎉 THANH TOÁN THÀNH CÔNG!",
        f"{config.EMOJI_SUCCESS} **Hệ thống đã nhận được tiền!**\n"
        f"{'⚡ Hệ thống đã **tự động phát hiện** chuyển khoản của bạn.' if auto else 'Đã xác thực giao dịch chuyển khoản thành công từ ngân hàng.'}",
        "---",
        f"📝 **Mã đơn hàng:** `{tx['order_id']}`\n"
        f"💰 **Số tiền đã nhận:** `{tx['amount']:,} VNĐ`\n"
        f"📦 **Lượt quest được cộng:** `+{tx['quests_to_buy']} lượt`\n"
        f"💎 **Số dư khả dụng hiện tại:** **{info['quota_str']}**",
        "🚀 Dùng `/details` để theo dõi tiến độ hoặc `/history` để xem lịch sử nạp.",
        "-# ⏱️ Tin nhắn này sẽ tự đóng sau 10 giây • Cảm ơn quý khách",
        accent=GREEN,
    ))
    return view


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
                    success = build_success_view(done, user_id, auto=True)
                    inter = LIVE_INVOICES.pop(done["order_id"], None)
                    edited = False
                    if inter:
                        try:
                            await inter.edit_original_response(view=success)
                            _schedule_delete(inter)
                            edited = True
                        except Exception:
                            pass
                    if not edited:
                        try:
                            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
                            await user.send(view=success)
                        except Exception:
                            pass
        except Exception as e:
            from logger import log
            log(f"Lỗi payment_watcher: {e}", "warn")
        await asyncio.sleep(AUTO_CHECK_INTERVAL)


# ───────────────────────── Cửa hàng ─────────────────────────

class PackageSelect(discord.ui.Select):
    def __init__(self, layout):
        options = []
        for qty in PACKAGES:
            info = get_package_info(qty)
            desc = f"Giảm {info['pct']}% • tiết kiệm {info['saved']:,}đ" if info["pct"] else "Mua lẻ, không giảm giá"
            options.append(discord.SelectOption(
                label=f"{qty} Lượt Quest - {info['final']:,}đ",
                value=str(qty), emoji=PACKAGE_EMOJI.get(qty, "💎"), description=desc
            ))
        super().__init__(placeholder="🛒 Nhấn vào đây để chọn gói muốn mua", options=options)
        self.layout = layout

    async def callback(self, interaction: discord.Interaction):
        user_id = self.layout.user_id
        quantity = int(self.values[0])

        loading = TxaView(user_id, timeout=60)
        loading.add_item(box(
            "### ⚙️ Đang Khởi Tạo Giao Dịch",
            f"{config.EMOJI_LOADING} Hệ thống đang tạo mã đơn hàng và mã VietQR thanh toán, vui lòng đợi...",
        ))
        await interaction.response.edit_message(view=loading)

        tx = create_transaction(user_id, quantity)
        if not tx:
            await interaction.followup.send(
                "❌ **Bạn đang có một giao dịch chưa hoàn tất!** Hãy thanh toán hoặc bấm **Hủy Giao Dịch** để tạo đơn mới.",
                ephemeral=True
            )
            _, view = get_limit_embed_and_view(user_id, back=self.layout.back)
            await interaction.edit_original_response(view=view)
            register_invoice(interaction, view)
            return

        # Tải trước ảnh VietQR để hiển thị nhanh
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(qr_url(tx["order_id"], tx["amount"]), timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        await r.read()
        except Exception:
            pass

        _, view = get_limit_embed_and_view(user_id, back=self.layout.back)
        LIVE_INVOICES[tx["order_id"]] = interaction
        await interaction.edit_original_response(view=view)


class ShopLayoutView(TxaView):
    def __init__(self, user_id: int, back=None):
        super().__init__(user_id, back, timeout=300)
        self.build()

    def build(self):
        self.clear_items()
        info = get_user_quota_info(self.user_id)
        lines = []
        for qty in PACKAGES:
            p = get_package_info(qty)
            emoji = PACKAGE_EMOJI.get(qty, "💎")
            if p["pct"]:
                lines.append(f"{emoji} **{qty} lượt** • ~~{p['original']:,}đ~~ ➜ **{p['final']:,}đ** `-{p['pct']}%`")
            else:
                lines.append(f"{emoji} **{qty} lượt** • **{p['final']:,}đ**")
        row = discord.ui.ActionRow()
        row.add_item(PackageSelect(self))
        self.add_item(box(
            "### 💎 Cửa Hàng Lượt Quest & Số Dư",
            f"{config.EMOJI_ROCKET} Mua thêm lượt làm Quest tự động, không cần thao tác tay.",
            f"👤 <@{self.user_id}>\n💎 Lượt hiện có: **{info['quota_str']}**\n🏆 Đã hoàn thành: `{info['total_completed']} nhiệm vụ`",
            "---",
            f"**🏷️ Bảng Giá • {get_price_per_quest():,}đ / lượt**\n" + "\n".join(lines),
            "🎁 Mua càng nhiều, giảm càng sâu! Mức giảm được **quay ngẫu nhiên** mỗi lần bot khởi động hoặc khi Admin đổi giá.",
            "---",
            "**🛒 Chọn gói**",
            row,
            "-# ✨ Lượt mua không giới hạn thời gian sử dụng và có thể tích lũy.",
        ))

        async def refresh(interaction: discord.Interaction):
            self.build()
            await interaction.response.edit_message(view=self)

        self.add_item(self.nav_row(
            refresh=refresh,
            help_text="ℹ️ Chọn một gói ở menu để tạo mã VietQR. Chuyển khoản đúng số tiền và nội dung, hệ thống **tự động cộng lượt** khi tiền về.",
        ))


# ───────────────────────── Hóa đơn ─────────────────────────

class ConfirmLeaveView(TxaView):
    """Hỏi xác nhận khi rời khỏi hóa đơn chưa thanh toán."""

    def __init__(self, user_id: int, tx: dict, back):
        super().__init__(user_id, back, timeout=120)
        expire_ts = tx["created_at"] + get_transaction_timeout()
        self.add_item(box(
            "### ⚠️ Hóa Đơn Chưa Thanh Toán",
            f"Bạn đang có hóa đơn `{tx['order_id']}` (**{tx['amount']:,} VNĐ**) chưa được thanh toán.",
            f"• Nếu quay lại, hóa đơn **vẫn còn hiệu lực** đến <t:{expire_ts}:T> (<t:{expire_ts}:R>).\n"
            f"• Nếu bạn chuyển khoản trong thời gian này, hệ thống **vẫn tự cộng lượt**.\n"
            f"• Muốn xóa hẳn hóa đơn, hãy chọn **Hủy Giao Dịch** ở màn hình hóa đơn.",
            "**Bạn có chắc muốn quay lại không?**",
            accent=ORANGE,
        ))

        async def leave(interaction: discord.Interaction):
            view = await _resolve(self.back, interaction)
            await interaction.response.edit_message(view=view)

        async def stay(interaction: discord.Interaction):
            _, view = get_limit_embed_and_view(self.user_id, back=self.back)
            await interaction.response.edit_message(view=view)
            register_invoice(interaction, view)

        self.add_item(row_of(
            btn("Vẫn Quay Lại", "◀️", leave, style=discord.ButtonStyle.primary),
            btn("Ở Lại Hóa Đơn", "💳", stay, style=discord.ButtonStyle.success),
        ))


class InvoiceLayoutView(TxaView):
    def __init__(self, user_id: int, tx: dict, back=None):
        super().__init__(user_id, back, timeout=300)
        self.tx = tx
        self.order_id = tx["order_id"]
        self.amount = tx["amount"]
        self.quantity = tx["quests_to_buy"]
        self.build()

    def build(self):
        self.clear_items()
        info = get_user_quota_info(self.user_id)
        pkg = get_package_info(self.quantity)
        expire_ts = self.tx["created_at"] + get_transaction_timeout()
        deadline = f"<t:{expire_ts}:R> (lúc <t:{expire_ts}:T>)"
        discount = f" • giảm {pkg['pct']}%" if pkg["pct"] else ""
        self.add_item(box(
            "### 💳 Hóa Đơn Chuyển Khoản Mua Lượt Quest",
            "Quét mã VietQR bên dưới — app ngân hàng sẽ tự điền đúng số tài khoản, số tiền và nội dung.",
            f"⏳ **Thời hạn thanh toán:** {deadline}",
            "---",
            f"🏦 **Ngân hàng:** {config.BANK_ID} (Quân Đội)\n"
            f"🔢 **Số tài khoản:** `{config.ACCOUNT_NO}`\n"
            f"👤 **Chủ tài khoản:** `{config.ACCOUNT_NAME}`\n"
            f"💰 **Số tiền:** `{self.amount:,} VNĐ`\n"
            f"📝 **Nội dung CK:** `{self.order_id}`\n"
            f"📦 **Gói mua:** +{self.quantity} lượt làm quest{discount}",
            discord.ui.MediaGallery(discord.MediaGalleryItem(qr_url(self.order_id, self.amount))),
            "---",
            f"💎 Số dư hiện có: **{info['quota_str']}** • Đã hoàn thành: `{info['total_completed']}`",
            f"-# 🔄 Hệ thống tự động kiểm tra mỗi {AUTO_CHECK_INTERVAL} giây — chuyển khoản xong chỉ cần đợi, "
            f"hoặc bấm 'Kiểm Tra Ngay'. Hãy ghi chính xác nội dung `{self.order_id}`.",
            accent=YELLOW,
        ))
        items = [
            btn("Kiểm Tra Ngay", "✅", self._check, style=discord.ButtonStyle.success),
            btn("Hủy Giao Dịch", "❌", self._cancel, style=discord.ButtonStyle.danger),
        ]
        if self.back:
            items.append(btn("Quay lại", "◀️", self._leave))
        self.add_item(row_of(*items))

    async def _check(self, interaction: discord.Interaction):
        tx = get_active_transaction(self.user_id)
        if not tx or tx.get("order_id") != self.order_id:
            _, view = get_limit_embed_and_view(self.user_id, back=self.back)
            await interaction.response.edit_message(view=view)
            await interaction.followup.send(
                "❌ **Giao dịch đã hết hạn hoặc đã xử lý!** Vui lòng chọn gói để tạo đơn mới.", ephemeral=True)
            return

        await interaction.response.defer()
        LIVE_INVOICES[self.order_id] = interaction
        tx_list = await fetch_sepay_transactions()
        if match_payment(tx_list, self.order_id, self.amount):
            done = complete_transaction(self.user_id, self.order_id)
            if done:
                LIVE_INVOICES.pop(self.order_id, None)
                await interaction.edit_original_response(view=build_success_view(done, self.user_id, auto=False))
                _schedule_delete(interaction)
            return  # nếu không: đã được vòng quét tự động xử lý trước đó

        await interaction.followup.send(
            f"⏳ **Chưa thấy tiền về.**\n"
            f"Chưa quét thấy giao dịch nội dung `{self.order_id}` (**{self.amount:,}đ**).\n"
            f"💡 Không cần bấm lại — hệ thống **tự động kiểm tra mỗi {AUTO_CHECK_INTERVAL}s** và sẽ cộng lượt ngay khi tiền về.",
            ephemeral=True
        )

    async def _cancel(self, interaction: discord.Interaction):
        cancel_transaction(self.user_id)
        LIVE_INVOICES.pop(self.order_id, None)
        _, view = get_limit_embed_and_view(self.user_id, back=self.back)
        await interaction.response.edit_message(view=view)
        await interaction.followup.send("🛑 Đã hủy giao dịch hiện tại.", ephemeral=True)

    async def _leave(self, interaction: discord.Interaction):
        tx = get_active_transaction(self.user_id)
        if not tx or tx.get("order_id") != self.order_id:
            view = await _resolve(self.back, interaction)  # đã thanh toán / hết hạn -> quay lại luôn
        else:
            view = ConfirmLeaveView(self.user_id, tx, self.back)
        await interaction.response.edit_message(view=view)


def get_limit_embed_and_view(user_id: int, back=None):
    """Trả về (MISSING, view) — giữ chữ ký cũ (embed, view); embed luôn bỏ trống vì giao diện dùng Components V2."""
    tx = get_active_transaction(user_id)
    if tx:
        return MISSING, InvoiceLayoutView(user_id, tx, back)
    return MISSING, ShopLayoutView(user_id, back)
