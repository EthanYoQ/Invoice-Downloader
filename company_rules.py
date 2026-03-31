"""
公司规则配置 — 用户自定义公司名称匹配发票购买方

用户在前端输入公司简称（如"诺华"），系统用该名称匹配发票上的购买方字段。
"""

DEFAULT_COMPANY = "generic"
UNKNOWN_PURCHASER_VALUES = {
    "",
    "未知",
    "未知抬头",
    "未知购买方",
    "暂无抬头",
    "暂无购买方",
    "unknown",
    "unknownbuyer",
    "unknownpurchaser",
}


def normalize_company_text(value: str) -> str:
    return str(value or "").strip().lower()


def classify_purchaser_relation(purchaser: str, company_name: str) -> str:
    """
    将购买方与目标公司关系分为:
    - target: 明确匹配目标公司，或未设置公司过滤
    - non_target: 购买方明确存在但与目标公司不匹配
    - unknown: 购买方缺失或仅为低价值占位词
    """
    normalized_company = normalize_company_text(company_name)
    if not normalized_company or normalized_company == DEFAULT_COMPANY:
        return "target"

    normalized_purchaser = normalize_company_text(purchaser)
    if normalized_purchaser in UNKNOWN_PURCHASER_VALUES:
        return "unknown"

    if normalized_company in normalized_purchaser:
        return "target"
    return "non_target"


def is_company_purchaser(purchaser: str, company_name: str) -> bool:
    """检查发票购买方是否包含用户指定的公司名称（不区分大小写）。"""
    return classify_purchaser_relation(purchaser, company_name) == "target"
