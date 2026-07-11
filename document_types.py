"""
文档类型注册表 — 统一票据/订单分类配置

定义所有已支持的文档类型及其归档和校验规则。
新增类型只需在 DOCUMENT_TYPES 字典中加一个条目。
"""

from typing import NewType, cast

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

DocumentType = NewType("DocumentType", str)


def get_document_type_names() -> list[str]:
    """Return the canonical document type vocabulary in declaration order."""
    return list(DOCUMENT_TYPES)


def normalize_document_type(doc_type: object) -> DocumentType:
    """Map free-form type text to the closest registered document type."""
    text = str(doc_type or "").strip()
    if text in DOCUMENT_TYPES:
        return cast(DocumentType, text)

    if "火车" in text or "高铁" in text:
        return "火车票"
    if "航班行程" in text:
        return "航班行程单"
    if "机票" in text or "航空" in text or "航班" in text:
        return "机票"
    if "水单" in text or "folio" in text.lower() or "账单" in text or "明细" in text:
        return "住宿水单"
    if "确认单" in text:
        return "住宿确认单"
    if "住宿" in text or "酒店" in text:
        return "住宿发票"
    if "行程单" in text or "报销单" in text:
        return "行程单"
    if "打车" in text or "滴滴" in text or "出租" in text or "高德" in text:
        return "打车"
    if "餐饮" in text or "餐" in text:
        return "餐饮"
    if "过路" in text or "高速" in text:
        return "过路费"
    if "定额" in text:
        return "定额发票"
    if "非目标" in text:
        return "非目标公司发票"
    if "个人非报销" in text:
        return "个人非报销发票"
    if "差旅服务" in text:
        return "差旅服务费"
    return "其他"


def classify_cwt_document_type(
    info_json: dict,
    info: dict,
    file_name: str,
    local_cits_fast_path: bool = False,
) -> tuple[DocumentType, list[str]]:
    """Apply the existing CWT-specific type precedence and side effects."""
    doc_type = str((info_json or {}).get("Type", ""))
    seller = str((info_json or {}).get("Seller", ""))
    reason_codes = []
    file_text = str(file_name or "")
    file_text_lower = file_text.lower()
    subject_lower = str((info or {}).get("subject", "")).lower()

    if local_cits_fast_path and doc_type in {"机票", "住宿水单", "非目标公司发票"}:
        if doc_type == "住宿水单":
            info_json["_is_folio"] = True
        reason_codes.append("PRESERVED_LOCAL_CITS_GBT_TYPE")
        return cast(DocumentType, doc_type), reason_codes

    if "取消" in file_text:
        info_json["_cwt_cancellation"] = True
        reason_codes.append("CWT_HOTEL_CANCELLATION")
        return "住宿确认单", reason_codes
    if "GBT Travel" in seller or "scct" in file_text_lower or "scct" in subject_lower:
        reason_codes.append("CLASSIFIED_AS_CWT_SERVICE_FEE")
        return "差旅服务费", reason_codes
    if any(keyword in file_text_lower for keyword in ("flight", "air", "机票", "航班", "行程单 - 机票")):
        reason_codes.append("CLASSIFIED_AS_CWT_FLIGHT_BY_FILENAME")
        return "航班行程单", reason_codes
    if any(keyword in doc_type.lower() for keyword in ("机票", "航班", "flight", "air")):
        reason_codes.append("CLASSIFIED_AS_CWT_FLIGHT")
        return "航班行程单", reason_codes
    if any(keyword in file_text_lower for keyword in ("酒店", "行程单 - 酒店")):
        reason_codes.append("CLASSIFIED_AS_CWT_HOTEL_BY_FILENAME")
        return "住宿确认单", reason_codes
    reason_codes.append("CLASSIFIED_AS_CWT_HOTEL")
    return "住宿确认单", reason_codes


def normalize_document_type_for_archive(
    info_json: dict,
    file_name: str,
    cwt_classified: bool = False,
) -> tuple[str, list[str]]:
    """Apply the current archive-specific type precedence and compatibility flags."""
    doc_type = str((info_json or {}).get("Type", ""))
    seller = str((info_json or {}).get("Seller", ""))
    reason_codes = []
    if cwt_classified:
        return doc_type, reason_codes
    if doc_type == "非目标公司发票":
        reason_codes.append("PRESERVED_NON_TARGET_COMPANY")
        return doc_type, reason_codes

    file_text = str(file_name or "")
    file_text_lower = file_text.lower()
    doc_type_lower = doc_type.lower()
    folio_signal = (
        any(keyword in doc_type for keyword in ("水单", "账单", "结账单", "住宿明细"))
        or "folio" in doc_type_lower
        or any(keyword in file_text for keyword in ("水单", "结账单", "账单", "住宿明细"))
        or "folio" in file_text_lower
    )

    if folio_signal:
        doc_type = "住宿水单"
        info_json["_is_folio"] = True
        reason_codes.append("CLASSIFIED_AS_HOTEL_FOLIO")
    elif any(keyword in doc_type for keyword in ("行程单", "报销单")) or any(
        keyword in file_text for keyword in ("行程单", "行程报销单", "报销单")
    ):
        is_flight = (
            "机票" in file_text_lower
            or any(keyword in doc_type_lower for keyword in ("机票", "航班", "flight", "air"))
            or any(keyword in seller for keyword in ("航空", "Airlines", "Air China", "东航", "南航", "国航"))
        )
        if is_flight:
            doc_type = "航班行程单"
            reason_codes.append("CLASSIFIED_AS_FLIGHT_ITINERARY")
        else:
            doc_type = "打车"
            info_json["_is_itinerary"] = True
            reason_codes.append("CLASSIFIED_AS_RIDE_ITINERARY")
    elif any(keyword in doc_type for keyword in ("打车", "出租", "滴滴", "高德", "约车")):
        doc_type = "打车"
        reason_codes.append("CLASSIFIED_AS_RIDE_BY_TYPE")
    elif any(keyword in seller for keyword in ("滴滴", "高德", "约车", "盛智", "畅行")):
        doc_type = "打车"
        reason_codes.append("CLASSIFIED_AS_RIDE_BY_SELLER")
    elif any(keyword in doc_type for keyword in ("火车", "高铁", "铁路")):
        doc_type = "火车票"
        reason_codes.append("CLASSIFIED_AS_TRAIN_BY_TYPE")
    elif "住宿" in doc_type:
        doc_type = "住宿发票"
        reason_codes.append("CLASSIFIED_AS_HOTEL_INVOICE")
    else:
        reason_codes.append("CLASSIFICATION_FROM_MODEL_TYPE")

    return doc_type, reason_codes


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
