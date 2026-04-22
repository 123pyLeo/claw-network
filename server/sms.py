"""SMS verification via Alibaba Cloud SMS (dysms).

Configuration (all env vars):
  ALIYUN_SMS_AK_ID       — Alibaba Cloud AccessKey ID (required for real sending)
  ALIYUN_SMS_AK_SECRET   — Alibaba Cloud AccessKey Secret (required)
  ALIYUN_SMS_SIGN_NAME   — approved sign name (e.g. your company name)
  ALIYUN_SMS_TEMPLATE_CODE — approved template code (e.g. SMS_XXXXXX)

If AK_ID or AK_SECRET are missing, send_sms() falls back to a dev mode that
prints the code to stdout — useful for local development without a real
SMS provider. Operators deploying this for real users MUST configure all
four env vars (the signature + template must be pre-approved by Aliyun).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import string

logger = logging.getLogger(__name__)

_CHINA_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

CODE_LENGTH = 6
CODE_EXPIRY_SECONDS = 300  # 5 minutes
SEND_COOLDOWN_SECONDS = 60


def _sign_name() -> str:
    return os.environ.get("ALIYUN_SMS_SIGN_NAME", "").strip()


def _template_code() -> str:
    return os.environ.get("ALIYUN_SMS_TEMPLATE_CODE", "").strip()


def validate_phone(phone: str) -> str:
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if cleaned.startswith("+86"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("86") and len(cleaned) == 13:
        cleaned = cleaned[2:]
    if not _CHINA_MOBILE_RE.match(cleaned):
        raise ValueError("请输入有效的中国大陆手机号（11位，1开头）。")
    return cleaned


def generate_code() -> str:
    return "".join(random.choices(string.digits, k=CODE_LENGTH))


def _build_client():
    from alibabacloud_dysmsapi20170525.client import Client
    from alibabacloud_tea_openapi.models import Config

    ak_id = os.environ.get("ALIYUN_SMS_AK_ID", "")
    ak_secret = os.environ.get("ALIYUN_SMS_AK_SECRET", "")
    if not ak_id or not ak_secret:
        raise RuntimeError("ALIYUN_SMS_AK_ID / ALIYUN_SMS_AK_SECRET not set")
    config = Config(
        access_key_id=ak_id,
        access_key_secret=ak_secret,
        endpoint="dysmsapi.aliyuncs.com",
    )
    return Client(config)


def send_sms(phone: str, code: str) -> bool:
    """Send a verification code via SMS.

    Dev mode (default): if AK credentials are missing, prints the code to
    stdout and returns True. This keeps local development trivial.

    Production: configure ALIYUN_SMS_AK_ID / AK_SECRET / SIGN_NAME /
    TEMPLATE_CODE env vars. The signature + template must be pre-approved
    by Aliyun.
    """
    ak_id = os.environ.get("ALIYUN_SMS_AK_ID", "").strip()
    ak_secret = os.environ.get("ALIYUN_SMS_AK_SECRET", "").strip()
    sign = _sign_name()
    tpl = _template_code()

    # --- Dev mode: no credentials configured, just print ---
    if not ak_id or not ak_secret or not sign or not tpl:
        logger.info("[DEV SMS] phone=%s code=%s (missing Aliyun env vars)", phone, code)
        print(f"\n{'=' * 50}")
        print(f"  验证码（开发模式,未配置真实短信服务商）: {code}")
        print(f"  发送至手机号: {phone}")
        print(f"{'=' * 50}\n", flush=True)
        return True

    # --- Production: real Aliyun send ---
    from alibabacloud_dysmsapi20170525.models import SendSmsRequest
    try:
        client = _build_client()
        req = SendSmsRequest(
            phone_numbers=phone,
            sign_name=sign,
            template_code=tpl,
            template_param=json.dumps({"code": code}),
        )
        resp = client.send_sms(req)
        body = resp.body
        if body.code == "OK":
            logger.info("SMS sent phone=%s biz_id=%s", phone, body.biz_id)
            return True
        logger.error("SMS failed phone=%s code=%s message=%s", phone, body.code, body.message)
        return False
    except Exception:
        logger.exception("SMS send error phone=%s", phone)
        return False
