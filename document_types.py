"""
文档类型注册表 — 统一票据/订单分类配置

定义所有已支持的文档类型及其归档和校验规则。
新增类型只需在 DOCUMENT_TYPES 字典中加一个条目。
"""

MANUAL_REVIEW_FOLDER = "待人工复核"
NON_TARGET_COMPANY_FOLDER = "非目标公司发票"
PERSONAL_NON_REIMBURSEMENT_FOLDER = "个人非报销发票"

DOCUMENT_TYPES = {
    # 现有发票类型
    "打车":     {"exempt_from_purchaser_check": True,  "archive_folder": "打车"},
    "行程单":   {"exempt_from_purchaser_check": True,  "archive_folder": "打车"},
    "火车票":   {"exempt_from_purchaser_check": True,  "archive_folder": "火车票"},
    "机票":     {"exempt_from_purchaser_check": True,  "archive_folder": "机票"},
    "住宿发票": {"exempt_from_purchaser_check": False, "archive_folder": "住宿发票"},
    "住宿水单": {"exempt_from_purchaser_check": True,  "archive_folder": "住宿发票"},
    "餐饮":     {"exempt_from_purchaser_check": False, "archive_folder": "餐饮"},
    "过路费":   {"exempt_from_purchaser_check": True,  "archive_folder": "过路费"},
    "定额发票": {"exempt_from_purchaser_check": True,  "archive_folder": "定额发票"},
    "其他":     {"exempt_from_purchaser_check": False, "archive_folder": "其他"},
    # 国旅运通新增
    "航班行程单": {"exempt_from_purchaser_check": True, "archive_folder": "机票"},
    "住宿确认单": {"exempt_from_purchaser_check": True, "archive_folder": "住宿发票"},
    # 差旅服务费 (GBT Travel Services SCCT 发票)
    "差旅服务费": {"exempt_from_purchaser_check": True, "archive_folder": "差旅服务费"},
    # 隔离类型
    "非目标公司发票": {"exempt_from_purchaser_check": True, "archive_folder": NON_TARGET_COMPANY_FOLDER},
    "个人非报销发票": {"exempt_from_purchaser_check": True, "archive_folder": PERSONAL_NON_REIMBURSEMENT_FOLDER},
}


def is_exempt_type(doc_type: str) -> bool:
    """检查文档类型是否豁免购买方校验。"""
    entry = DOCUMENT_TYPES.get(doc_type)
    if entry:
        return entry["exempt_from_purchaser_check"]
    return False


def get_archive_folder(doc_type: str) -> str:
    """获取文档类型对应的归档目录。"""
    entry = DOCUMENT_TYPES.get(doc_type)
    if entry:
        return entry["archive_folder"]
    return doc_type or "其他"
