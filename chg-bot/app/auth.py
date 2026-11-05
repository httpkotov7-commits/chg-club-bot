"""Проверка подписи Telegram Mini App (initData).

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Без валидной подписи сервер не принимает ни одного запроса — подделать пользователя нельзя.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def check_init_data(init_data: str, bot_token: str, max_age: int):
    if not init_data or not bot_token:
        return None
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = vals.pop("hash", None)
    if not received:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        if time.time() - int(vals.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(vals.get("user", "{}"))
    except (ValueError, TypeError):
        return None
    return user if user.get("id") else None


def sign_init_data(user: dict, bot_token: str, auth_date: int | None = None) -> str:
    """Только для тестов: собирает валидную initData."""
    from urllib.parse import urlencode
    vals = {"auth_date": str(auth_date or int(time.time())), "query_id": "TEST",
            "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False)}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    vals["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(vals)
