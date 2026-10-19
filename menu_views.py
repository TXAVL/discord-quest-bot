import discord
import config
from limits_manager import (
    create_transaction,
    get_price_per_quest,
    get_user_quota_info,
    get_payment_history,
    get_leaderboard,
)

BLURPLE = discord.Color.from_rgb(88, 101, 242)

# Cây menu 3 cấp: danh mục -> mục con -> (tuỳ chọn, chỉ một số nhánh mới có)
# Nhánh nào không có cấp 3 thì menu thứ 3 tự ẩn đi.
def build_tree():
    price = get_price_per_quest()
    return {
        "buy": {
            "label": "Mua lượt Quest", "emoji": "💎", "desc": "Nạp lượt làm quest bằng VietQR",
            "children": {
                str(n): {
                    "label": f"{n} lượt - {price * n:,}đ", "emoji": e, "desc": d,
                    "children": {
                        "qr": {"label": "Thanh toán VietQR", "emoji": "🏦", "desc": "Quét mã, tự động cộng lượt"},
                    },
                }
                for n, e, d in ((1, "🎫", "Mua lẻ"), (2, "🎟️", "Gói nhỏ"), (5, "🔥", "Tiết kiệm"), (10, "👑", "Gói lớn"))
            },
        },
        "stats": {
            "label": "Thống kê", "emoji": "📊", "desc": "Số dư, lịch sử, xếp hạng",
            "children": {
                "quota": {"label": "Số dư của tôi", "emoji": "💰", "desc": "Xem lượt khả dụng"},
                "history": {
                    "label": "Lịch sử nạp", "emoji": "🧾", "desc": "Các giao dịch gần đây",
                    "children": {
                        "5": {"label": "5 giao dịch gần nhất", "emoji": "5️⃣", "desc": ""},
                        "10": {"label": "10 giao dịch gần nhất", "emoji": "🔟", "desc": ""},
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


class LevelSelect(discord.ui.Select):
    def __init__(self, level: int, placeholder: str, nodes: dict, selected: str | None):
        options = [
            discord.SelectOption(
                label=n["label"], value=k, emoji=n.get("emoji"),
                description=(n.get("desc") or None), default=(k == selected),
            )
            for k, n in nodes.items()
        ]
        super().__init__(placeholder=placeholder, options=options, min_values=1, max_values=1, row=level)
        self.level = level

    async def callback(self, interaction: discord.Interaction):
        view: "CascadeMenuView" = self.view
        if interaction.user.id != view.user_id:
            await interaction.response.send_message("❌ Menu này không dành cho bạn.", ephemeral=True)
            return
        view.picks = view.picks[: self.level] + [self.values[0]]
        view.rebuild()
        await interaction.response.edit_message(embed=view.make_embed(), view=view)


class CascadeMenuView(discord.ui.View):
    PLACEHOLDERS = ["① Chọn danh mục...", "② Chọn mục con...", "③ Chọn tuỳ chọn..."]

    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.tree = self.get_tree()
        self.picks: list[str] = []
        self.rebuild()

    def get_tree(self):
        return build_tree()

    def _path_nodes(self):
        nodes, level_nodes = [], self.tree
        for key in self.picks:
            node = level_nodes.get(key)
            if not node:
                break
            nodes.append(node)
            level_nodes = node.get("children") or {}
        return nodes

    def _leaf_ready(self) -> bool:
        nodes = self._path_nodes()
        if not nodes:
            return False
        last = nodes[-1]
        return len(self.picks) == 3 or not last.get("children")

    def rebuild(self):
        self.clear_items()
        level_nodes = self.tree
        for level in range(3):
            if not level_nodes:
                break  # Nhánh này không có cấp sâu hơn -> ẩn menu
            selected = self.picks[level] if level < len(self.picks) else None
            self.add_item(LevelSelect(level, self.PLACEHOLDERS[level], level_nodes, selected))
            if selected is None:
                break
            level_nodes = (level_nodes.get(selected) or {}).get("children")

        confirm = discord.ui.Button(label="Xác Nhận", style=discord.ButtonStyle.success, emoji="✅", row=3, disabled=not self._leaf_ready())
        confirm.callback = self.on_confirm
        reset = discord.ui.Button(label="Đặt Lại", style=discord.ButtonStyle.secondary, emoji="🔄", row=3, disabled=not self.picks)
        reset.callback = self.on_reset
        close = discord.ui.Button(label="Đóng", style=discord.ButtonStyle.danger, emoji="✖️", row=3)
        close.callback = self.on_close
        for b in (confirm, reset, close):
            self.add_item(b)

    def make_embed(self) -> discord.Embed:
        nodes = self._path_nodes()
        embed = discord.Embed(
            title="🧭 Trung Tâm Điều Khiển Quest Bot",
            color=BLURPLE,
            description="Chọn lần lượt từng mục bên dưới. Menu sau sẽ thay đổi theo lựa chọn trước đó.",
        )
        crumbs = " ➜ ".join(f"{n.get('emoji', '')} {n['label']}" for n in nodes) or "*Chưa chọn gì*"
        embed.add_field(name="📍 Lựa chọn của bạn", value=crumbs, inline=False)
        if nodes and not self._leaf_ready():
            embed.add_field(name="➡️ Bước tiếp theo", value="Hãy chọn tiếp ở menu mới vừa xuất hiện.", inline=False)
        elif nodes:
            embed.add_field(name="✅ Sẵn sàng", value="Bấm **Xác Nhận** để thực hiện.", inline=False)
        embed.set_footer(text="Menu tự đóng sau 5 phút không thao tác")
        return embed

    async def on_reset(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Không dành cho bạn.", ephemeral=True)
            return
        self.picks = []
        self.rebuild()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_close(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Không dành cho bạn.", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=discord.Embed(description="👋 Đã đóng menu.", color=discord.Color.greyple()), view=None
        )
        self.stop()

    async def on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Không dành cho bạn.", ephemeral=True)
            return
        if not self._leaf_ready():
            await interaction.response.send_message("⚠️ Bạn chưa chọn xong.", ephemeral=True)
            return

        uid = self.user_id
        top = self.picks[0]

        if top == "buy":
            from payment_views import get_limit_embed_and_view, register_invoice
            tx = create_transaction(uid, int(self.picks[1]))
            if not tx:
                await interaction.response.send_message(
                    "❌ Bạn đang có một hóa đơn chưa hoàn tất. Dùng `/buy` để xem hoặc hủy nó.", ephemeral=True
                )
                return
            embed, view = get_limit_embed_and_view(uid)
            await interaction.response.edit_message(embed=embed, view=view)
            register_invoice(interaction, view)
            self.stop()
            return

        if top == "stats":
            sub = self.picks[1]
            if sub == "quota":
                info = get_user_quota_info(uid)
                embed = discord.Embed(title="💰 Số Dư Của Bạn", color=discord.Color.green(), description=(
                    f"• 💎 Lượt khả dụng: **{info['quota_str']}**\n"
                    f"• 🏆 Đã hoàn thành: `{info['total_completed']} nhiệm vụ`"
                ))
            elif sub == "history":
                limit = int(self.picks[2])
                rows = get_payment_history(uid)[-limit:]
                embed = discord.Embed(title="🧾 Lịch Sử Nạp", color=discord.Color.gold())
                embed.description = "\n".join(
                    f"`{r['order_id']}` • **{r['amount']:,}đ** • +{r['quests']} lượt • <t:{r['paid_at']}:R>"
                    for r in reversed(rows)
                ) or "Bạn chưa có giao dịch nào."
            else:
                medals = ["🥇", "🥈", "🥉"]
                rows = get_leaderboard(10)
                embed = discord.Embed(title="🏆 Bảng Xếp Hạng", color=discord.Color.gold())
                embed.description = "\n".join(
                    f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{u}> — **{t}** quest" for i, (u, t) in enumerate(rows)
                ) or "Chưa có ai hoàn thành quest."
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Hỗ trợ
        if self.picks[1] == "guide":
            from bot import send_extension_guide
            await send_extension_guide(interaction)
        else:
            embed = discord.Embed(title="❓ Cách Hoạt Động", color=BLURPLE, description=(
                "1️⃣ Lấy token bằng tiện ích của TXA.\n"
                "2️⃣ Bấm 🚀 **Bắt Đầu** ở kênh bot và dán token.\n"
                "3️⃣ Bot tự **quét • nhận • hoàn thành** quest và báo tiến độ qua DM.\n"
                f"4️⃣ Mỗi ngày có 1 lượt free, mua thêm tại `/menu` hoặc `/buy`."
            ))
            await interaction.response.send_message(embed=embed, ephemeral=True)


# ───────────────────────── /config dạng menu 3 cấp ─────────────────────────

def _onoff(cur: bool, label: str, emoji: str, desc: str):
    return {
        "label": label, "emoji": emoji, "desc": f"Hiện tại: {'Bật' if cur else 'Tắt'} • {desc}",
        "children": {
            "True": {"label": "Bật", "emoji": "✅", "desc": ""},
            "False": {"label": "Tắt", "emoji": "❌", "desc": ""},
        },
    }


def _presets(cur_text: str, label: str, emoji: str, desc: str, values: list, fmt):
    return {
        "label": label, "emoji": emoji, "desc": f"Hiện tại: {cur_text} • {desc}",
        "children": {str(v): {"label": fmt(v), "emoji": "🔹", "desc": ""} for v in values},
    }


def build_config_embed() -> discord.Embed:
    from limits_manager import get_transaction_timeout
    price = get_price_per_quest()
    timeout = get_transaction_timeout()
    on = lambda v: "Bật ✅" if v else "Tắt ❌"
    embed = discord.Embed(title="⚙️ Bảng Cấu Hình Hệ Thống Quest Bot", color=discord.Color.blue(), timestamp=discord.utils.utcnow())
    embed.add_field(name="📺 Kênh hoạt động", value=f"<#{config.CHANNEL_ID}>" if config.CHANNEL_ID else "Chưa cấu hình", inline=True)
    embed.add_field(name="👑 Admin IDs", value=f"`{', '.join(map(str, config.ADMIN_IDS))}`", inline=True)
    embed.add_field(name="🤖 Trạng thái Bot", value="Đang hoạt động 🟢", inline=True)
    embed.add_field(name="💰 Đơn giá quest", value=f"`{price:,} VNĐ`", inline=True)
    embed.add_field(name="⏳ Hạn thanh toán QR", value=f"`{timeout // 60} phút`", inline=True)
    embed.add_field(name="👑 Admin Bypass", value=on(getattr(config, "ADMIN_BYPASS_LIMIT", True)), inline=True)
    embed.add_field(name="🔄 Quét quest", value=f"`{config.POLL_INTERVAL}s`", inline=True)
    embed.add_field(name="💓 Heartbeat", value=f"`{config.HEARTBEAT_INTERVAL}s`", inline=True)
    embed.add_field(name="📥 Tự nhận quest", value=on(config.AUTO_ACCEPT), inline=True)
    embed.add_field(name="📝 Log tiến độ", value=on(config.LOG_PROGRESS), inline=True)
    embed.add_field(name="🐞 Debug", value=on(config.DEBUG), inline=True)
    embed.add_field(name="🔑 SePay", value="Đã cấu hình ✅" if config.SEPAY_API_KEY else "Chưa cấu hình ❌", inline=True)
    embed.add_field(name="🏦 Tài khoản nhận", value=f"`{config.BANK_ID}` • `{config.ACCOUNT_NO}` • {config.ACCOUNT_NAME}", inline=False)
    return embed


class ConfigMenuView(CascadeMenuView):
    PLACEHOLDERS = ["① Nhóm cấu hình...", "② Chọn mục cần chỉnh...", "③ Chọn giá trị mới..."]

    def get_tree(self):
        from limits_manager import get_transaction_timeout
        return {
            "sys": {
                "label": "Hệ thống", "emoji": "⚙️", "desc": "Quét, heartbeat, tự nhận, log, debug",
                "children": {
                    "POLL_INTERVAL": _presets(f"{config.POLL_INTERVAL}s", "Tần suất quét quest", "🔄", "giây", [15, 30, 60, 120, 300], lambda v: f"{v} giây"),
                    "HEARTBEAT_INTERVAL": _presets(f"{config.HEARTBEAT_INTERVAL}s", "Chu kỳ heartbeat", "💓", "giây", [10, 15, 20, 30], lambda v: f"{v} giây"),
                    "AUTO_ACCEPT": _onoff(config.AUTO_ACCEPT, "Tự động nhận quest", "📥", "nhận quest mới"),
                    "LOG_PROGRESS": _onoff(config.LOG_PROGRESS, "Log từng nhịp tim", "📝", "in ra console"),
                    "DEBUG": _onoff(config.DEBUG, "Chế độ debug", "🐞", "log chi tiết"),
                },
            },
            "pay": {
                "label": "Quest & Thanh toán", "emoji": "💰", "desc": "Giá, hạn QR, quyền Admin",
                "children": {
                    "price": _presets(f"{get_price_per_quest():,}đ", "Đơn giá mỗi lượt", "💵", "VNĐ", [5000, 10000, 20000, 50000], lambda v: f"{v:,} VNĐ"),
                    "timeout": _presets(f"{get_transaction_timeout() // 60} phút", "Hạn hóa đơn QR", "⏳", "phút", [3, 5, 10, 15, 30], lambda v: f"{v} phút"),
                    "ADMIN_BYPASS_LIMIT": _onoff(getattr(config, "ADMIN_BYPASS_LIMIT", True), "Admin vô hạn lượt", "👑", "bypass giới hạn"),
                },
            },
        }

    def make_embed(self) -> discord.Embed:
        embed = build_config_embed()
        nodes = self._path_nodes()
        crumbs = " ➜ ".join(f"{n.get('emoji', '')} {n['label']}" for n in nodes) or "*Chọn nhóm cấu hình ở menu bên dưới để chỉnh sửa*"
        embed.add_field(name="🛠️ Đang chỉnh", value=crumbs, inline=False)
        embed.set_footer(text="Chỉ Admin • Thay đổi áp dụng ngay và lưu vào .env / user_limits.json")
        return embed

    async def on_confirm(self, interaction: discord.Interaction):
        if interaction.user.id not in config.ADMIN_IDS:
            await interaction.response.send_message("❌ Chỉ Admin được dùng menu này.", ephemeral=True)
            return
        if not self._leaf_ready():
            await interaction.response.send_message("⚠️ Bạn chưa chọn xong.", ephemeral=True)
            return
        from limits_manager import set_price_per_quest, set_transaction_timeout
        key, value = self.picks[1], self.picks[2]
        nodes = self._path_nodes()
        if key == "price":
            set_price_per_quest(int(value))
        elif key == "timeout":
            set_transaction_timeout(int(value) * 60)
        else:
            config.update_env_variable(key, value)
        note = f"✅ Đã đặt **{nodes[1]['label']}** = **{nodes[2]['label']}**"
        self.picks = []
        self.tree = self.get_tree()
        self.rebuild()
        embed = self.make_embed()
        embed.description = note
        await interaction.response.edit_message(embed=embed, view=self)
