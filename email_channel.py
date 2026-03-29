"""
邮箱通道注册表 — 统一 IMAP 路由配置

替代 app_api.py 中散布的 if/else IMAP 服务器选择逻辑。
新增邮箱只需在 EMAIL_CHANNELS 字典中加一行。
"""

EMAIL_CHANNELS = {
    "qq.com": {
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "requires_id_cmd": False,
        "display": "QQ 邮箱",
    },
    "163.com": {
        "imap_host": "imap.163.com",
        "imap_port": 993,
        "requires_id_cmd": True,
        "display": "163 邮箱",
    },
}

# 默认通道（向后兼容：未知域名回落到 QQ）
_DEFAULT_DOMAIN = "qq.com"


def resolve_channel(email_address: str) -> dict:
    """根据邮箱地址返回对应的通道配置。"""
    domain = email_address.rsplit("@", 1)[-1].lower() if "@" in email_address else ""
    return EMAIL_CHANNELS.get(domain, EMAIL_CHANNELS[_DEFAULT_DOMAIN])


def supported_domains() -> list[str]:
    """返回所有支持的邮箱域名列表。"""
    return list(EMAIL_CHANNELS.keys())


def is_supported_email(email_address: str) -> bool:
    """检查邮箱是否属于已支持的通道。"""
    domain = email_address.rsplit("@", 1)[-1].lower() if "@" in email_address else ""
    return domain in EMAIL_CHANNELS
