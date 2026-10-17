import os
import json
import time
import random
import string
import aiohttp
from datetime import date
from typing import Optional
from logger import log
import config

LIMITS_FILE = "user_limits.json"

def load_limits() -> dict:
    if os.path.exists(LIMITS_FILE):
        try:
            with open(LIMITS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"Lỗi đọc file limits: {e}", "error")
            return {}
    return {}

def save_limits(limits: dict):
    try:
        # Đồng bộ thống kê toàn cục trước khi lưu
        limits["total_completed"] = config.TOTAL_COMPLETED
        limits["total_failed"] = config.TOTAL_FAILED
        with open(LIMITS_FILE, "w", encoding="utf-8") as f:
            json.dump(limits, f, indent=4, ensure_ascii=False)
    except Exception as e:
        log(f"Lỗi ghi file limits: {e}", "error")

def is_user_limited(user_id: int) -> bool:
    """Kiểm tra xem người dùng có bị giới hạn quest hay không."""
    if getattr(config, "ADMIN_BYPASS_LIMIT", True) and user_id in config.ADMIN_IDS:
        return False  # Admins bypass limit when enabled

    uid = str(user_id)
    limits = load_limits()
    if uid not in limits:
        return False  # Chưa từng làm quest nào hôm nay, được làm free

    user_data = limits[uid]
    purchased_balance = user_data.get("purchased_balance", 0)
    if purchased_balance > 0:
        return False  # Vẫn còn lượt mua

    today_str = date.today().isoformat()
    free_date = user_data.get("free_completed_date")
    free_count = user_data.get("free_completed_count", 0)
    
    if free_date == today_str and free_count >= 1:
        return True  # Bị giới hạn (Đã làm xong 1 quest free hôm nay)

    return False

def consume_limit(user_id: int) -> bool:
    """Tiêu thụ 1 lượt làm quest của người dùng (lượt free hoặc lượt mua)."""
    if getattr(config, "ADMIN_BYPASS_LIMIT", True) and user_id in config.ADMIN_IDS:
        return True  # Admins don't consume limit when bypass enabled

    uid = str(user_id)
    limits = load_limits()
    today_str = date.today().isoformat()

    if uid not in limits:
        limits[uid] = {
            "free_completed_date": today_str,
            "free_completed_count": 1,
            "purchased_balance": 0
        }
        save_limits(limits)
        return True

    user_data = limits[uid]
    purchased_balance = user_data.get("purchased_balance", 0)

    if purchased_balance > 0:
        user_data["purchased_balance"] = purchased_balance - 1
        save_limits(limits)
        log(f"Người dùng {user_id} đã sử dụng 1 lượt quest mua. Lượt còn lại: {user_data['purchased_balance']}", "ok")
        return True

    free_date = user_data.get("free_completed_date")
    free_count = user_data.get("free_completed_count", 0)

    if free_date == today_str:
        if free_count >= 1:
            log(f"Người dùng {user_id} cố gắng chạy khi đã hết lượt miễn phí hôm nay", "warn")
            return False
        user_data["free_completed_count"] = free_count + 1
    else:
        user_data["free_completed_date"] = today_str
        user_data["free_completed_count"] = 1

    save_limits(limits)
    log(f"Người dùng {user_id} đã sử dụng lượt miễn phí hàng ngày", "ok")
    return True

def generate_order_id(limits: dict) -> str:
    """Tạo mã đơn hàng ngẫu nhiên duy nhất dạng QB[A-Z]{2}[0-9]{4}."""
    while True:
        letters = "".join(random.choices(string.ascii_uppercase, k=2))
        digits = "".join(random.choices(string.digits, k=4))
        order_id = f"QB{letters}{digits}"
        
        # Check if already exists in active transactions
        dup = False
        for user_data in limits.values():
            if isinstance(user_data, dict):
                active_tx = user_data.get("active_transaction")
                if active_tx and active_tx.get("order_id") == order_id:
                    dup = True
                    break
        if not dup:
            return order_id

def get_price_per_quest() -> int:
    """Lấy đơn giá cho mỗi lượt quest (mặc định 10,000đ)."""
    limits = load_limits()
    return limits.get("price_per_quest", 10000)

def set_price_per_quest(price: int):
    """Đặt đơn giá mới cho mỗi lượt quest."""
    limits = load_limits()
    limits["price_per_quest"] = price
    save_limits(limits)
    log(f"Đã cập nhật giá mới: {price} VNĐ/lượt", "ok")

def create_transaction(user_id: int, quests: int) -> Optional[dict]:
    """Tạo giao dịch mua lượt cho người dùng. Trả về thông tin giao dịch hoặc None nếu đã có giao dịch chờ."""
    uid = str(user_id)
    limits = load_limits()
    
    if uid not in limits:
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0
        }
        
    user_data = limits[uid]
    
    # Check if there is an active transaction already
    if "active_transaction" in user_data and user_data["active_transaction"]:
        return None  # Block duplicate requests
        
    order_id = generate_order_id(limits)
    price = get_price_per_quest()
    amount = quests * price
    
    tx = {
        "order_id": order_id,
        "amount": amount,
        "quests_to_buy": quests,
        "created_at": int(time.time())
    }
    
    user_data["active_transaction"] = tx
    save_limits(limits)
    log(f"Tạo giao dịch mới cho {user_id}: {order_id} - {amount} VNĐ cho {quests} quest (giá {price} VNĐ/lượt)", "info")
    return tx

def cancel_transaction(user_id: int):
    """Hủy giao dịch hiện tại của người dùng."""
    uid = str(user_id)
    limits = load_limits()
    if uid in limits and "active_transaction" in limits[uid]:
        limits[uid]["active_transaction"] = None
        save_limits(limits)
        log(f"Đã hủy giao dịch của {user_id}", "info")

def get_transaction_timeout() -> int:
    """Lấy thời gian hết hạn giao dịch (mặc định 300 giây = 5 phút)."""
    limits = load_limits()
    return limits.get("transaction_timeout", 300)

def set_transaction_timeout(seconds: int):
    """Đặt thời gian hết hạn giao dịch mới."""
    limits = load_limits()
    limits["transaction_timeout"] = seconds
    save_limits(limits)
    log(f"Đã cập nhật thời hạn giao dịch mới: {seconds} giây", "ok")

def get_active_transaction(user_id: int) -> Optional[dict]:
    uid = str(user_id)
    limits = load_limits()
    if uid in limits:
        user_data = limits[uid]
        tx = user_data.get("active_transaction")
        if tx:
            created_at = tx.get("created_at", 0)
            timeout = get_transaction_timeout()
            if int(time.time()) - created_at > timeout:
                # Giao dịch hết hạn -> Tự động hủy
                user_data["active_transaction"] = None
                save_limits(limits)
                log(f"Giao dịch {tx['order_id']} của {user_id} đã hết hạn (quá {timeout} giây)", "info")
                return None
            return tx
    return None

def add_purchased_balance(user_id: int, quantity: int):
    uid = str(user_id)
    limits = load_limits()
    if uid not in limits:
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0
        }
    user_data = limits[uid]
    user_data["purchased_balance"] = user_data.get("purchased_balance", 0) + quantity
    user_data["active_transaction"] = None  # Clear transaction
    save_limits(limits)
    log(f"Cộng thêm {quantity} lượt cho {user_id}. Số dư hiện tại: {user_data['purchased_balance']}", "ok")

def get_user_quota_info(user_id: int) -> dict:
    """Lấy thông tin chi tiết về số dư lượt làm quest của người dùng."""
    is_admin = user_id in config.ADMIN_IDS
    uid = str(user_id)
    limits = load_limits()
    user_data = limits.get(uid, {}) if isinstance(limits.get(uid), dict) else {}
    
    purchased = user_data.get("purchased_balance", 0)
    today_str = date.today().isoformat()
    free_date = user_data.get("free_completed_date")
    free_count = user_data.get("free_completed_count", 0)
    
    # 1 lượt miễn phí hàng ngày (reset qua ngày mới)
    free_available = 1 if (free_date != today_str or free_count < 1) else 0
    total_completed = user_data.get("total_completed", 0)
    
    if is_admin and getattr(config, 'ADMIN_BYPASS_LIMIT', True):
        quota_str = "👑 Vô hạn (Admin)"
        if purchased > 0:
            quota_str += f" • {purchased} lượt mua dự trữ"
    else:
        if purchased > 0:
            quota_str = f"{purchased} lượt mua"
            if free_available > 0:
                quota_str += " + 1 lượt free hôm nay"
        elif free_available > 0:
            quota_str = "1 lượt free hôm nay"
        else:
            quota_str = "0 lượt (Hết lượt free hôm nay)"

    return {
        "is_admin": is_admin,
        "purchased_balance": purchased,
        "free_available": free_available,
        "free_completed_count": free_count if free_date == today_str else 0,
        "total_completed": total_completed,
        "quota_str": quota_str
    }

async def verify_payment_on_sepay(order_id: str, expected_amount: float) -> bool:
    """Gọi SePay API để xác nhận xem người dùng đã thanh toán đơn hàng này chưa."""
    api_key = config.SEPAY_API_KEY
    if not api_key:
        log("SEPAY_API_KEY chưa được cấu hình, không thể xác thực thanh toán", "error")
        return False

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    endpoints = [
        "https://userapi.sepay.vn/v2/transactions?limit=50",
        "https://my.sepay.vn/userapi/transactions/list?limit=50"
    ]

    log(f"Đang kết nối SePay API để đối soát đơn hàng: {order_id} (Số tiền mong đợi: {expected_amount:,} VNĐ)...", "info")
    
    for url in endpoints:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=10) as r:
                    log(f"Kết quả gọi SePay ({url.split('?')[0]}) - Status: {r.status}", "info")
                    if r.status == 200:
                        data = await r.json()
                        tx_list = data.get("data") if isinstance(data.get("data"), list) else data.get("transactions", [])
                        for tx in tx_list:
                            content = tx.get("transaction_content", "") or ""
                            amount_val = tx.get("amount_in", 0)
                            try:
                                amount = float(amount_val)
                            except (ValueError, TypeError):
                                amount = 0.0
                            
                            # Match order_id (case-insensitive) and verify expected amount
                            if order_id.lower() in content.lower() and amount >= expected_amount:
                                log(f"Xác thực thanh toán thành công cho đơn hàng: {order_id} - Số tiền nhận: {amount:,.0f} VNĐ", "ok")
                                return True
        except Exception as e:
            log(f"Lỗi khi kiểm tra SePay endpoint {url}: {e}", "warn")
            continue

    log(f"Đối soát giao dịch {order_id} thất bại (chưa tìm thấy chuyển khoản khớp)", "warn")
    return False

def save_global_stats():
    """Lưu thống kê toàn cục hiện tại vào file limits."""
    limits = load_limits()
    save_limits(limits)

def increment_user_total_completed(user_id: int):
    """Tăng số lượng quest hoàn thành trọn đời của người dùng cụ thể."""
    uid = str(user_id)
    limits = load_limits()
    if uid not in limits:
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0,
            "total_completed": 0,
            "active_transaction": None
        }
    elif not isinstance(limits[uid], dict):
        limits[uid] = {
            "free_completed_date": "",
            "free_completed_count": 0,
            "purchased_balance": 0,
            "total_completed": 0,
            "active_transaction": None
        }
    
    user_data = limits[uid]
    user_data["total_completed"] = user_data.get("total_completed", 0) + 1
    save_limits(limits)
    log(f"Đã tăng tổng quest hoàn thành của {user_id} lên {user_data['total_completed']}", "ok")

def get_user_total_completed(user_id: int) -> int:
    """Lấy tổng số quest hoàn thành trọn đời của người dùng cụ thể."""
    uid = str(user_id)
    limits = load_limits()
    if uid in limits and isinstance(limits[uid], dict):
        return limits[uid].get("total_completed", 0)
    return 0
