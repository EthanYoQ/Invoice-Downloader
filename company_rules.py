"""
公司规则配置 — 用户自定义公司名称匹配发票购买方

用户在前端输入公司简称（如"诺华"），系统用该名称匹配发票上的购买方字段。
"""


def is_company_purchaser(purchaser: str, company_name: str) -> bool:
    """检查发票购买方是否包含用户指定的公司名称（不区分大小写）。"""
    if not company_name or not company_name.strip():
        return True  # 未设置公司则不拦截
    return company_name.strip().lower() in purchaser.lower()
