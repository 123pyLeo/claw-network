"""SMS verification abstraction for Claw Network.

Current implementation: prints verification codes to console (dev/testing).
To switch to a real SMS provider, replace send_sms() with an API call.

Supported future providers:
  - Alibaba Cloud SMS (aliyun dysms)
  - Tencent Cloud SMS
"""

from __future__ import annotations

import logging
import random
import re
import string

logger = logging.getLogger(__name__)

# Phone number validation: Chinese mainland mobile numbers
_CHINA_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

# Verification code length
CODE_LENGTH = 6

# Code expiry in seconds
CODE_EXPIRY_SECONDS = 300  # 5 minutes

# Rate limit: minimum seconds between sending codes to the same phone
SEND_COOLDOWN_SECONDS = 60


def validate_phone(phone: str) -> str:
    """Validate and normalize a Chinese mobile phone number.

    Returns the cleaned phone number. Raises ValueError if invalid.
    """
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    # Strip +86 or 86 prefix
    if cleaned.startswith("+86"):
        cleaned = cleaned[3:]
    elif cleaned.startswith("86") and len(cleaned) == 13:
        cleaned = cleaned[2:]
    if not _CHINA_MOBILE_RE.match(cleaned):
        raise ValueError("请输入有效的中国大陆手机号（11位，1开头）。")
    return cleaned


def generate_code() -> str:
    """Generate a random numeric verification code."""
    return "".join(random.choices(string.digits, k=CODE_LENGTH))


def send_sms(phone: str, code: str) -> bool:
    """Send a verification code via SMS.

    Current implementation: console output (dev mode).
    Replace this function body for production SMS delivery.

    Returns True if sent successfully, False otherwise.
    """
    # ============================================================
    # DEV MODE: Print to console. Replace with real SMS API call.
    # ============================================================
    # Example for Alibaba Cloud SMS:
    #   from alibabacloud_dysmsapi20170525.client import Client
    #   client.send_sms(SendSmsRequest(
    #       phone_numbers=phone,
    #       sign_name="沙堆网络",
    #       template_code="SMS_XXXXXX",
    #       template_param=json.dumps({"code": code}),
    #   ))
    #
    # Example for Tencent Cloud SMS:
    #   from tencentcloud.sms.v20210111 import sms_client, models
    #   req = models.SendSmsRequest()
    #   req.PhoneNumberSet = [f"+86{phone}"]
    #   req.TemplateId = "XXXXXX"
    #   req.TemplateParamSet = [code]
    #   client.SendSms(req)
    # ============================================================

    logger.info("[DEV SMS] phone=%s code=%s", phone, code)
    print(f"\n{'='*50}")
    print(f"  验证码（开发模式）: {code}")
    print(f"  发送至手机号: {phone}")
    print(f"{'='*50}\n", flush=True)
    return True
