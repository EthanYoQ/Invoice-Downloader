import os
import re
import json
import base64
import copy
import logging
import shutil
import fitz  # PyMuPDF
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class InvoiceExtractor:
    def __init__(self, api_key=None, output_dir="extracted_invoices"):
        """
        初始化大模型提取器, 使用 GLM-4.5V 解决图文识别发票信息并结构化
        """
        self.api_key = api_key or ""
        self.model = "glm-4.5v"
        self.output_dir = os.path.abspath(output_dir)
        self.processed_records_file = os.path.join(self.output_dir, "processed_records.json")
        self.last_extraction_trace = {}
        self.last_route_trace = {}
        os.makedirs(self.output_dir, exist_ok=True)


    def pdf_to_base64_image(self, pdf_path):
        """3.1 Convert all pages of each standardized PDF into a list of Base64 image streams"""
        if not os.path.exists(pdf_path):
            logging.error(f"PDF file not found: {pdf_path}")
            return None
            
        try:
            with fitz.open(pdf_path) as doc:
                # 限制最多渲染 2 页，避免大体积长文档打爆后端和本地内存
                pages_to_render = min(2, len(doc))
                # 降低缩放比由 2.0 降到 1.5 降低 Payload 体积
                zoom = 1.5
                mat = fitz.Matrix(zoom, zoom)
                
                from PIL import Image
                import io
                
                base64_images = []
                for i in range(pages_to_render):
                    page = doc.load_page(i)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    
                    # 转成字节流再转Base64
                    buffered = io.BytesIO()
                    img.save(buffered, format="PNG")
                    img_bytes = buffered.getvalue()
                    
                    b64_str = base64.b64encode(img_bytes).decode('utf-8')
                    base64_images.append(b64_str)
            
            if not base64_images:
                return None
                
            return base64_images
        except Exception as e:
            logging.error(f"Failed to convert PDF to base64 images: {pdf_path}, Error: {e}")
            return None

    def _try_extract_didi_invoice_from_pdf_text(self, pdf_path):
        """Local fallback for the stable Didi ride invoice PDF layout."""
        if not pdf_path or not os.path.exists(pdf_path):
            return None
        if not str(pdf_path).lower().endswith(".pdf"):
            return None
        if "滴滴电子发票" not in os.path.basename(pdf_path):
            return None

        try:
            with fitz.open(pdf_path) as doc:
                if len(doc) < 1:
                    return None
                text = (doc.load_page(0).get_text("text") or "").replace("\xa0", " ")
        except Exception as e:
            logging.warning(f"Local Didi PDF fallback failed to read text: {e}")
            return None

        markers = [
            "电子发票（普通发票）",
            "旅客运输服务",
            "发票号码",
            "开票日期",
            "价税合计",
            "滴滴",
        ]
        if any(marker not in text for marker in markers):
            return None

        invoice_number_match = re.search(r"发票号码[:：]\s*([0-9]{8,})", text)
        date_match = re.search(r"开票日期[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日", text)
        amount_match = re.search(r"（小写）\s*¥?\s*([0-9]+\.[0-9]{2})", text)
        name_matches = re.findall(r"名称[:：]\s*([^\n]+)", text)

        if not invoice_number_match or not date_match or not amount_match or len(name_matches) < 2:
            return None

        purchaser = name_matches[0].strip()
        seller_raw = name_matches[1].strip()
        if not purchaser or not seller_raw or "滴滴" not in seller_raw:
            return None

        date_value = f"{date_match.group(1)}{int(date_match.group(2)):02d}{int(date_match.group(3)):02d}"

        return {
            "is_invoice": True,
            "Date": date_value,
            "Purchaser": purchaser,
            "Seller": "滴滴出行",
            "Amount": amount_match.group(1),
            "InvoiceCode": "",
            "InvoiceNumber": invoice_number_match.group(1),
            "Type": "打车",
            "category": "打车",
            "Departure_Date": "",
            "Departure_City": "",
            "Destination_City": "",
        }

    def extract_info_via_llm(self, base64_images, custom_rules="", pdf_path=None):
        """3.2 Construct the Vision/OCR API payload and extract structured JSON using dual engines"""
        from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_not_exception_type
        import time

        class LayoutParsingError(Exception): pass

        extraction_trace = {
            "engine": None,
            "reason_code": None,
            "track_a": {"status": "not_started", "reason_code": None, "message": None},
            "track_b": {"status": "not_started", "reason_code": None, "message": None},
            "result": None,
        }
        self.last_extraction_trace = copy.deepcopy(extraction_trace)

        # 兼容单张图片的传入情况
        if isinstance(base64_images, str):
            base64_images = [base64_images]
            
        prompt_text = """
        请从以下提取出的票据文本中提取关键信息，并严格且只能输出一个合法的 JSON 对象。绝对不要输出任何 markdown 标记（如 ```json），直接返回大括号 {} 包裹的内容。
        
        【补丁规则】：
        1. 商家名称净化：在提取 `Seller`（销售方/商家）时，必须去除冗余的后缀（如“xx分店”、“xx餐饮管理有限公司”等），保留核心品牌名（例如将“北京麦当劳食品有限公司南京路分店”简化为“北京麦当劳”）。这样生成的文件夹和文件名会更整洁。
        2. 金额强约束：在提取 `Amount` 时，必须寻找票据上标注为“价税合计”或“小写金额”的数值。如果发现有负数金额（红字对冲票），请在 Amount 前保留负号。
        3. 火车票字段兜底：若判定 Type 为火车票，必须提取 [出发城市] 和 [到达城市]。如果票据模糊无法确认城市，请在对应字段（Departure_City, Destination_City）填入 "未知"，严禁胡乱猜测。
        4. 日期识别补丁：注意：外资酒店账单(Folio)日期常采用 DD/MM/YY 格式（如 25/09/25 实际是 2025年9月25日）。请结合上下文推断年份，切勿将'日'当成'年'，统一提取为 YYYYMMDD 格式。

        必须提取并包含以下精确字段：
        {
            "is_invoice": true或false (布尔值，是否为有效发票、收据或行程单等凭证),
            "Date": "严格使用YYYYMMDD格式，例如20260215",
            "Purchaser": "购买方抬头全称。如果票面只有个人姓名或无公司名，请据实提取姓名或填'个人'。",
            "Seller": "开出该票据的商户或机构名称。遵循商家名称净化规则。如果是火车票/高铁票，固定填'中国铁路'；机票填对应航空公司；滴滴填'滴滴出行'。",
            "Amount": "遵循金额强约束规则提取的数字，保留两位小数。如145.00或-145.00，无金额则返回0.00",
            "InvoiceCode": "发票代码(如有则提供，否则返回空字符串\"\")",
            "InvoiceNumber": "发票号码(如有则提供，否则返回空字符串\"\")",
            "Type": "必须从 ['打车', '行程单', '火车票', '机票', '住宿发票', '住宿水单', '餐饮', '过路费', '其他'] 中选择。注意：带税务局监制章的选'住宿发票'；酒店打印的消费明细/宾客账单选'住宿水单'。",
            "category": "结合整体信息的归档分类短词，如'打车'、'火车票'、'餐饮'等",
            "Departure_Date": "仅当 Type 为火车票时提供，格式如同Date，其他为空",
            "Departure_City": "仅当 Type 为火车票时提供。遵循火车票字段兜底规则，模糊则填'未知'",
            "Destination_City": "仅当 Type 为火车票时提供。遵循火车票字段兜底规则，模糊则填'未知'"
        }
        """
        
        if custom_rules and len(custom_rules.strip()) > 0:
            prompt_text += f"\n此外要求：\n{custom_rules}\n"

        def _parse_json_result(content):
            # Clean markdown formatting explicitly before regex search
            content = content.replace("```json", "").replace("```", "").strip()
            
            # Use regex to forcibly extract dict block even if there is surrounding chatter
            # 支持一层嵌套的 JSON 对象（发票字段为 flat JSON 结构）
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
            
            try:
                result = json.loads(content)
            except json.JSONDecodeError as e:
                # 极端情况下若JSON严重破损，不直接崩溃，返回一个兜底包让 Manual_Check 接手
                logging.warning(f"Failed to decode LLM JSON: {e}. Raw content: {content}")
                return {"Type": "解析失败", "Date": "未知", "Seller": "无法读取商户", "Amount": "0.00"}
            
            # Type constraint validation heuristic fallback
            valid_types = ['打车', '行程单', '火车票', '机票', '住宿', '餐饮', '过路费', '定额发票', '其他']
            if result.get("Type") not in valid_types:
                t_str = str(result.get("Type", ""))
                if "火车" in t_str or "高铁" in t_str:
                    result["Type"] = "火车票"
                elif "打车" in t_str or "滴滴" in t_str or "出租" in t_str:
                    result["Type"] = "打车"
                elif "餐饮" in t_str or "餐" in t_str:
                    result["Type"] = "餐饮"
                elif "宿" in t_str or "酒店" in t_str:
                    result["Type"] = "住宿"
                else:
                    result["Type"] = "其他"
                    
            # 强化兜底：不再直接因为缺少 Date/Seller/Amount 就报错抛弃，而是填入未知并放行。
            # 分类白名单拦截网或者 Manual_Check 机制会自然处理这些"半残"发票。
            if "Date" not in result: result["Date"] = "未知日期"
            if "Seller" not in result: result["Seller"] = "未知开票方"
            if "Amount" not in result: result["Amount"] = "0.00"
            if "Type" not in result: result["Type"] = "未知分类"
            if "Purchaser" not in result: result["Purchaser"] = "暂无抬头"
            
            return result

        @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(2), reraise=True, retry=retry_if_not_exception_type(LayoutParsingError))
        def _call_track_a_ocr(file_path):
            logging.info("Track A - Calling OCR (glm-ocr layout_parsing)...")
            print(">>> [进度] 开始 OCR 提取...")
            try:
                # layout_parsing 的 base64 模式不支持 PDF 二进制，但支持 PNG 图片
                # 因此先将 PDF 第一页转为 PNG，再用 image/png base64 提交
                ext = str(file_path).lower().split('.')[-1]
                if ext == 'pdf':
                    import fitz
                    from PIL import Image
                    import io as _io
                    doc = fitz.open(file_path)
                    page = doc.load_page(0)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    doc.close()
                    buffered = _io.BytesIO()
                    img.save(buffered, format="PNG")
                    img_bytes = buffered.getvalue()
                    img_b64 = base64.b64encode(img_bytes).decode('utf-8')
                    file_data_uri = f"data:image/png;base64,{img_b64}"
                    print(f">>> [进度] PDF 已转为 PNG ({len(img_bytes):,} bytes)，准备提交 OCR")
                else:
                    # 非 PDF 文件直接读取图片 base64
                    with open(file_path, 'rb') as f:
                        file_bytes = f.read()
                    img_b64 = base64.b64encode(file_bytes).decode('utf-8')
                    mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png'}
                    mime_type = mime_map.get(ext, 'image/png')
                    file_data_uri = f"data:{mime_type};base64,{img_b64}"
                
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": "glm-ocr",
                    "file": file_data_uri
                }
                
                res = requests.post("https://open.bigmodel.cn/api/paas/v4/layout_parsing", headers=headers, json=payload, timeout=90)
                
                if res.status_code in [400, 404]:
                    print(f">>> [错误] layout_parsing 接口返回 {res.status_code}，详情: {res.text}。准备自动切换至 Track B (glm-4.5v)。")
                    raise LayoutParsingError(f"HTTP {res.status_code}")
                res.raise_for_status()
            except LayoutParsingError:
                raise
            except Exception as e:
                print(f">>> [错误] 模型调用失败，原因: {e}")
                raise
            
            text = res.json().get('md_results', '')
            if not text or len(text.strip()) < 5:
                print(">>> [错误] 模型调用失败，原因: OCR 文本过空")
                raise ValueError("OCR text missing or too short.")
            print(">>> [进度] OCR 提取完成")
            return text

        @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
        def _call_track_a_llm(ocr_text):
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            payload = {
                "model": "glm-4-flash",
                "messages": [
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": f"以下是提取出的票据文本，请提取信息并输出严格 JSON:\n\n{ocr_text}"}
                ],
                "temperature": 0.1
            }
            logging.info("Track A - Calling Text LLM (glm-4-flash)...")
            print(">>> [进度] 开始 LLM 分类及字段提取...")
            try:
                res = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions", headers=headers, json=payload, timeout=45)
                res.raise_for_status()
            except Exception as e:
                print(f">>> [错误] 模型调用失败，原因: {e}")
                raise
            content = res.json()["choices"][0]["message"]["content"]
            print(">>> [进度] LLM 分类完成")
            return _parse_json_result(content)

        @retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3), reraise=True)
        def _call_track_b_vision(b64_list):
            import threading
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            messages_content = [{"type": "text", "text": prompt_text}]
            for b64 in b64_list:
                messages_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
            payload = {
                "model": "glm-4.5v",
                "messages": [{"role": "user", "content": messages_content}],
                "temperature": 0.1
            }
            logging.info("Track B - Falling back to Vision LLM (glm-4.5v)...")
            print(">>> [进度] Track A 失败，开始调用 GLM-4.5V 进行视觉提取...")
            
            stop_event = threading.Event()
            def breathing_light():
                while not stop_event.is_set():
                    print(">>> [等待中] GLM-4.5V 正在解析票据视觉信息...")
                    stop_event.wait(5.0)
                    
            t = threading.Thread(target=breathing_light, daemon=True)
            t.start()
            
            try:
                res = requests.post("https://open.bigmodel.cn/api/paas/v4/chat/completions", headers=headers, json=payload, timeout=60)
                res.raise_for_status()
            except Exception as e:
                print(f">>> [错误] 模型调用失败，原因: {e}")
                raise
            finally:
                stop_event.set()
                t.join(timeout=1.0)
                
            content = res.json()["choices"][0]["message"]["content"]
            print(">>> [进度] 视觉提取完成")
            return _parse_json_result(content)

        def _print_success_summary(res_dict):
            if res_dict:
                seller = res_dict.get("Seller", "未知")
                amount = res_dict.get("Amount", "0.00")
                inv_type = res_dict.get("Type", "未知")
                print(f">>> [解析成功] 识别到: {seller} | 金额: {amount} | 类型: {inv_type}")

        # Execution Pipeline
        track_a_success = False
        track_a_result = None
        
        try:
            if not pdf_path or not os.path.exists(pdf_path):
                raise ValueError("未提供有效的 pdf_path，Track A 无法读取原始文件。")
                
            abs_pdf_path = os.path.abspath(pdf_path)
            actual_size = os.path.getsize(abs_pdf_path)
            if actual_size < 1000:
                raise ValueError(f"文件大小异常: 仅 {actual_size} bytes，拒绝处理。")
                
            # 强制日志输出物理校验信息
            print(f">>> [物理校验] 绝对路径: {abs_pdf_path} | 真实字节: {actual_size}")
            
            full_ocr_text = _call_track_a_ocr(abs_pdf_path)
            
            track_a_result = _call_track_a_llm(full_ocr_text)
            track_a_success = True
            extraction_trace["track_a"] = {"status": "success", "reason_code": None, "message": None}
        except Exception as e:
            print(f">>> [错误] Track A 处理异常，原因: {e}")
            logging.warning(f"Track A (OCR+LLM) failed: {e}")
            extraction_trace["track_a"] = {
                "status": "failed",
                "reason_code": "TRACK_A_FAILED",
                "message": str(e),
            }

        if track_a_success and track_a_result:
            extraction_trace["engine"] = "track_a"
            extraction_trace["result"] = copy.deepcopy(track_a_result)
            self.last_extraction_trace = copy.deepcopy(extraction_trace)
            _print_success_summary(track_a_result)
            return track_a_result

        # Track B Fallback
        try:
            track_b_result = _call_track_b_vision(base64_images)
            extraction_trace["track_b"] = {"status": "success", "reason_code": None, "message": None}
            extraction_trace["engine"] = "track_b"
            extraction_trace["reason_code"] = "TRACK_A_FAILED_TRACK_B_FALLBACK"
            extraction_trace["result"] = copy.deepcopy(track_b_result)
            self.last_extraction_trace = copy.deepcopy(extraction_trace)
            if track_b_result:
                _print_success_summary(track_b_result)
            return track_b_result
        except Exception as e:
            logging.error(f"Track B (Vision 4.6v fallback) permanently failed: {e}")
            extraction_trace["track_b"] = {
                "status": "failed",
                "reason_code": "TRACK_B_FAILED",
                "message": str(e),
            }
            local_fallback_result = self._try_extract_didi_invoice_from_pdf_text(pdf_path)
            if local_fallback_result:
                extraction_trace["engine"] = "local_didi_pdf_fallback"
                extraction_trace["reason_code"] = "TRACK_A_TRACK_B_FAILED_LOCAL_DIDI_PDF_FALLBACK"
                extraction_trace["result"] = copy.deepcopy(local_fallback_result)
                self.last_extraction_trace = copy.deepcopy(extraction_trace)
                _print_success_summary(local_fallback_result)
                return local_fallback_result

            extraction_trace["reason_code"] = "EXTRACTOR_ALL_ENGINES_FAILED"
            self.last_extraction_trace = copy.deepcopy(extraction_trace)
            return None

    def load_processed_records(self):
        """加载已处理的去重记录"""
        if os.path.exists(self.processed_records_file):
            try:
                with open(self.processed_records_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_processed_records(self, records):
        """保存去重记录"""
        with open(self.processed_records_file, 'w', encoding='utf-8') as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

    def is_duplicate(self, code, number, records):
        """3.3 Implement deduplication using 'Invoice Code + Invoice Number'"""
        # 有些行程单可能没有发票代码，只用发票号码来排重
        if not code and not number:
            return False
            
        key = f"{code}_{number}"
        return key in records

    def safe_filename(self, name):
        """清理文件名中的非法字符、多余空格和特殊标记"""
        if not name:
            return ""
        name = str(name).strip()
        # Remove common invalid path characters
        name = re.sub(r'[\\/:*?"<>|\r\n]', '_', name)
        # Squeeze spaces
        name = re.sub(r'\s+', ' ', name)
        return name

    def route_and_rename_file(self, pdf_path, invoice_info, custom_rules=None):
        """3.4 Implement the renaming and folder routing logic with Manual_Check rescuing"""
        import uuid

        route_trace = {
            "status": "not_started",
            "reason_code": None,
            "target_folder": None,
            "display_type": None,
            "candidate_filename": None,
            "final_filename": None,
            "final_path": None,
            "collision_resolved": False,
            "used_manual_check": False,
        }
        self.last_route_trace = copy.deepcopy(route_trace)
        
        def save_to_manual_check(reason_prefix):
            target_folder = "Manual_Check"
            new_filename = f"{reason_prefix}_{os.path.basename(pdf_path)}"
            final_dir = os.path.join(self.output_dir, target_folder)
            os.makedirs(final_dir, exist_ok=True)
            
            final_path = os.path.join(final_dir, new_filename)
            original_new_filename = new_filename
            while os.path.exists(final_path):
                name, ext = os.path.splitext(original_new_filename)
                short_uuid = str(uuid.uuid4())[:4]
                new_filename = f"{name}_{short_uuid}{ext}"
                final_path = os.path.join(final_dir, new_filename)
                route_trace["collision_resolved"] = True
                
            try:
                shutil.copy2(pdf_path, final_path)
                logging.warning(f"File rescued to Manual_Check: {final_path}")
                route_trace.update({
                    "status": "manual_check",
                    "reason_code": "ROUTE_TO_MANUAL_CHECK",
                    "target_folder": target_folder,
                    "display_type": reason_prefix,
                    "candidate_filename": original_new_filename,
                    "final_filename": new_filename,
                    "final_path": final_path,
                    "used_manual_check": True,
                })
                self.last_route_trace = copy.deepcopy(route_trace)
                return True, final_path
            except Exception as e:
                route_trace.update({
                    "status": "failed",
                    "reason_code": "MANUAL_CHECK_COPY_FAILED",
                    "target_folder": target_folder,
                    "display_type": reason_prefix,
                    "candidate_filename": original_new_filename,
                    "final_filename": new_filename,
                    "final_path": final_path,
                    "used_manual_check": True,
                    "error_message": str(e),
                })
                self.last_route_trace = copy.deepcopy(route_trace)
                return False, str(e)
                
        if not invoice_info or not isinstance(invoice_info, dict):
            logging.warning(f"No valid LLM info returned for {pdf_path}. Moving to Manual_Check.")
            return save_to_manual_check("Unrecognized")
            
        inv_type = self.safe_filename(invoice_info.get("Type", "未知分类"))
        _, ext = os.path.splitext(pdf_path)
        
        if inv_type == "火车票":
            date = self.safe_filename(invoice_info.get("Departure_Date", invoice_info.get("Date", "UnknownDate")))
            dep_city = self.safe_filename(invoice_info.get("Departure_City", "未知起始"))
            dest_city = self.safe_filename(invoice_info.get("Destination_City", "未知终点"))
            new_filename = f"{date}-{dep_city}-{dest_city}-火车票{ext}"
        else:
            date = self.safe_filename(invoice_info.get("Date", "UnknownDate"))
            seller = self.safe_filename(invoice_info.get("Seller", "UnknownSeller"))
            amount = self.safe_filename(str(invoice_info.get("Amount", "0.00")))
            
            # 行程单/水单在文件名中保留类型标识，供 Phase 2 撮合使用
            if invoice_info.get("_is_itinerary"):
                display_type = f"{inv_type}行程单"
            elif invoice_info.get("_is_folio"):
                display_type = "住宿水单"
            else:
                display_type = inv_type
            new_filename = f"{date}_{display_type}_{amount}_{seller}{ext}"
        route_trace["display_type"] = display_type if inv_type != "火车票" else inv_type
            
        # Routing Logics (Use Type as folder, fallback to '其他')
        target_folder = inv_type if inv_type else "其他"
        
        # Apply custom rules if provided
        if custom_rules:
            for keyword, folder_name in custom_rules.items():
                if keyword in target_folder:
                    target_folder = folder_name
                    break
                    
        final_dir = os.path.join(self.output_dir, target_folder)
        os.makedirs(final_dir, exist_ok=True)
        route_trace["target_folder"] = target_folder
        route_trace["candidate_filename"] = new_filename
        
        final_path = os.path.join(final_dir, new_filename)
        
        # Handle filename collisions with UUID
        original_new_filename = new_filename
        while os.path.exists(final_path):
            name, f_ext = os.path.splitext(original_new_filename)
            short_uuid = str(uuid.uuid4())[:4]
            new_filename = f"{name}_{short_uuid}{f_ext}"
            final_path = os.path.join(final_dir, new_filename)
            route_trace["collision_resolved"] = True
            
        try:
            # Move and rename
            shutil.copy2(pdf_path, final_path)
            logging.info(f"Archived: {final_path}")
            print(f">>> [进度] 文件重命名归档完成: {new_filename}")
            route_trace.update({
                "status": "archived",
                "reason_code": None,
                "final_filename": new_filename,
                "final_path": final_path,
                "used_manual_check": False,
            })
            self.last_route_trace = copy.deepcopy(route_trace)
            
            # Since we removed `key` generation from here earlier in favor of app_api.py persistent state tracking,
            # we just return successfully.
            return True, final_path
        except Exception as e:
            logging.error(f"Failed to move file {pdf_path}: {e}")
            print(f">>> [错误] 文件重命名或移动失败，原因: {e}")
            route_trace.update({
                "status": "failed",
                "reason_code": "ARCHIVE_COPY_FAILED",
                "final_filename": new_filename,
                "final_path": final_path,
                "used_manual_check": False,
                "error_message": str(e),
            })
            self.last_route_trace = copy.deepcopy(route_trace)
            return False, str(e)

