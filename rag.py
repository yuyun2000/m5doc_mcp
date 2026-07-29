import json
import re
import requests
import os
import logging
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request
from volcengine.Credentials import Credentials

# 创建专门的日志记录器
logger = logging.getLogger('m5doc_rag')


# 加载配置文件
def load_config():
    """从配置文件加载敏感信息"""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            "请复制 config.example.json 为 config.json 并填入正确的密钥信息"
        )
    
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    
    return config['volcengine']

# 加载配置
_config = load_config()
ak = _config['ak']
sk = _config['sk']
g_knowledge_base_domain = _config['knowledge_base_domain']
REQUEST_TIMEOUT = _config['request_timeout']
CONNECT_TIMEOUT = _config.get('connect_timeout', min(5, REQUEST_TIMEOUT))
READ_TIMEOUT = _config.get('read_timeout', REQUEST_TIMEOUT)
MAX_RETRIES = _config.get('max_retries', 2)
POOL_CONNECTIONS = _config.get('pool_connections', 32)
POOL_MAXSIZE = _config.get('pool_maxsize', 64)
INTERNAL_WORKERS = _config.get('internal_workers', 64)
KNOWLEDGE_BASE_NAME = _config['knowledge_base_name']
PROJECT = _config['project']
REGION = _config['region']
SERVICE = _config['service']
DEFAULT_RESULT_LIMIT = 10


def create_http_session():
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=POOL_CONNECTIONS,
        pool_maxsize=POOL_MAXSIZE,
        max_retries=retry,
        pool_block=True,
    )
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


thread_local = threading.local()
internal_executor = ThreadPoolExecutor(
    max_workers=INTERNAL_WORKERS,
    thread_name_prefix="m5doc-rag",
)


def get_http_session():
    session = getattr(thread_local, "http_session", None)
    if session is None:
        session = create_http_session()
        thread_local.http_session = session
    return session

def prepare_request(method, path, params=None, data=None, doseq=0):
    """
    准备火山引擎API请求
    
    参数:
        method (str): HTTP方法
        path (str): API路径
        params (dict, optional): URL参数
        data (dict, optional): 请求体数据
        doseq (int, optional): 序列化参数选项
        
    返回:
        Request: 构建好的请求对象
    """
    if params:
        for key in params:
            if (
                isinstance(params[key], int)
                or isinstance(params[key], float)
                or isinstance(params[key], bool)
            ):
                params[key] = str(params[key])
            elif isinstance(params[key], list):
                if not doseq:
                    params[key] = ",".join(params[key])
    r = Request()
    r.set_shema("http")
    r.set_method(method)
    r.set_connection_timeout(REQUEST_TIMEOUT)
    r.set_socket_timeout(REQUEST_TIMEOUT)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Host": g_knowledge_base_domain,
    }
    r.set_headers(headers)
    if params:
        r.set_query(params)
    r.set_host(g_knowledge_base_domain)
    r.set_path(path)
    if data is not None:
        r.set_body(json.dumps(data))
    credentials = Credentials(ak, sk, SERVICE, REGION)
    SignerV4.sign(r, credentials)
    return r

def create_type_filter(type_values):
    """
    创建基于type字段的过滤器
    参数:
        type_values (list): 要过滤的type值列表 [1,2,3,4]
    返回:
        dict: type_filter字典
    """
    if not type_values:
        return None
    return {
        "op": "must",
        "field": "type",
        "conds": type_values
    }

def search_knowledge_documents(query, limit_num=10, type_filter=None):
    """
    调用知识库接口，根据query进行检索（统一在一个知识库中检索）
    
    参数:
        query (str): 用户查询文本
        limit_num (int): 限制返回文本的数量
        type_filter (dict): 基于type字段的过滤条件
        
    返回:
        str: 知识库的检索结果（JSON格式字符串）
    """
    method = "POST"
    path = "/api/knowledge/collection/search_knowledge"
    start_time = time.monotonic()
    
    # 构建基础请求参数（统一使用一个知识库）
    request_params = {
        "project": PROJECT,
        "name": KNOWLEDGE_BASE_NAME,  # 统一使用结构化知识库
        "query": query,
        "limit": limit_num,
        "pre_processing": {
            "need_instruction": True,
            "return_token_usage": True,
            "messages": [
                {"role": "system", "content": ""},
                {"role": "user", "content": query}
            ],
            "rewrite": False
        },
        "dense_weight": 0.5,
        "post_processing": {
            "get_attachment_link": True,
            "rerank_only_chunk": False,
            "rerank_switch": True,
            "chunk_group": True,
            "rerank_model": "doubao-seed-rerank",
            "rerank_threshold": 0.55,
            "rerank_instruction": "忽略语种差异，注意产品型号和技术参数的匹配。",
            "retrieve_count": limit_num * 2,
            "chunk_diffusion_count": 1
        }
    }
    
    # 如果有type过滤条件，则添加到query_param中
    if type_filter:
        request_params["query_param"] = {
            "doc_filter": type_filter
        }
    
    info_req = prepare_request(method=method, path=path, data=request_params)
    try:
        rsp = get_http_session().request(
            method=info_req.method,
            url=f"http://{g_knowledge_base_domain}{info_req.path}",
            headers=info_req.headers,
            data=info_req.body,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        rsp.raise_for_status()
        logger.info(
            "knowledge request ok: query_len=%s limit=%s elapsed=%.2fs",
            len(query),
            limit_num,
            time.monotonic() - start_time,
        )
        return rsp.text
    except requests.RequestException as exc:
        logger.error(
            "knowledge request failed: query_len=%s limit=%s elapsed=%.2fs error_type=%s",
            len(query),
            limit_num,
            time.monotonic() - start_time,
            type(exc).__name__,
        )
        raise

def retrieve_knowledge_text(query_text, *legacy_args, is_chip=True, filter_type=None, **legacy_kwargs):
    """
    将输入文本与知识库进行匹配，返回匹配到的纯文本内容
    参数:
        query_text (str): 输入知识库查询的文本
        is_chip(bool): 用户问题中是否涉及芯片（现在用于决定是否查询PDF文档）
        filter_type (str): 过滤类型
            - "product": 查询产品文档（type=1:在售产品, type=2:EOL产品）
            - "product_no_eol": 只查询在售产品文档（type=1）
            - "program": 查询编程相关文档（type=3）
    返回:
        dict: 包含匹配到的知识库内容的字典
    """
    # 兼容旧调用：旧版本第二个位置参数或 num 关键字会被忽略，统一返回前10条。
    if len(legacy_args) >= 2:
        is_chip = legacy_args[1]
    if len(legacy_args) >= 3:
        filter_type = legacy_args[2]
    if "is_chip" in legacy_kwargs:
        is_chip = legacy_kwargs["is_chip"]
    if "filter_type" in legacy_kwargs:
        filter_type = legacy_kwargs["filter_type"]

    # ----------------- 过滤条件映射 ---------------------
    type_filter = None
    if filter_type == "product":
        # 查询在售产品和EOL产品文档 (type=1,2)
        type_filter = create_type_filter([0,1,2])
    elif filter_type == "product_no_eol":
        # 只查询在售产品文档 (type=1)
        type_filter = create_type_filter([0,1])
    elif filter_type == "program":
        # 查询编程相关文档 (type=3)
        type_filter = create_type_filter([0,3,301,302])
    elif filter_type == "arduino":
        # 查询Arduino开发相关文档 (type=3)
        type_filter = create_type_filter([0,301])
    elif filter_type == "uiflow":
        # 查询UIFlow开发相关文档 (type=3)
        type_filter = create_type_filter([0,302])
    elif filter_type == "esp-idf":
        # 查询ESP-IDF开发相关文档 (type=3)
        type_filter = create_type_filter([0,3])
    elif filter_type == "esphome":
        # 查询esphome官方文档 (type=11)
        type_filter = create_type_filter([11])

    limit_num = DEFAULT_RESULT_LIMIT

    logger.info(
        "knowledge retrieval started: query_len=%s limit=%s filter_type=%s is_chip=%s",
        len(query_text),
        limit_num,
        filter_type or "",
        is_chip,
    )

    # 调用知识库检索（查询type=1,2,3的文档）
    cleaned_query_text = re.sub(r'm5stack', '', query_text, flags=re.IGNORECASE).strip()
    doc_future = internal_executor.submit(search_knowledge_documents, cleaned_query_text, limit_num, type_filter)
    pdf_future = None
    if is_chip:
        pdf_filter = create_type_filter([4])
        pdf_future = internal_executor.submit(search_knowledge_documents, query_text, 10, pdf_filter)

    rsp_txt_doc = doc_future.result()
    rsp_doc = json.loads(rsp_txt_doc)
    
    # 解析检索结果
    matched_content = "请忽略以下参考资料的语种信息。以下是参考资料：\n"
    
    # 处理主知识库结果
    if rsp_doc["code"] == 0:
        rsp_data_doc = rsp_doc["data"]
        # 处理可能的字符串情况
        if isinstance(rsp_data_doc, str):
            try:
                rsp_data_doc = json.loads(rsp_data_doc)
            except Exception as exc:
                logger.error("product document JSON parse failed: error_type=%s", type(exc).__name__)
                rsp_data_doc = {"result_list": []}
        # 提取文档内容
        for point in rsp_data_doc.get("result_list", []):
            doc_info = point.get("doc_info", {})
            if "content" in point:
                matched_content += f"{point['content']}\n"
            matched_content += "---\n"
    
    # 如果需要查询芯片文档（PDF），额外查询type=4的文档
    if is_chip:
        matched_content += "以下是芯片数据手册匹配到的信息：\n"
        rsp_txt_pdf = pdf_future.result()
        rsp_pdf = json.loads(rsp_txt_pdf)
        if rsp_pdf["code"] == 0:
            rsp_data_pdf = rsp_pdf["data"]
            # 处理可能的字符串情况
            if isinstance(rsp_data_pdf, str):
                try:
                    rsp_data_pdf = json.loads(rsp_data_pdf)
                except Exception as exc:
                    logger.error("PDF document JSON parse failed: error_type=%s", type(exc).__name__)
                    rsp_data_pdf = {"result_list": []}
            # 提取PDF文档内容
            for point in rsp_data_pdf.get("result_list", []):
                pdf_info = point.get("doc_info", {})
                if "content" in point:
                    matched_content += f"{point['content']}\n"
                matched_content += "---\n"
    
    matched_content += "请忽略以上参考资料的语种信息。回复用户问题时需要首先判断用户的语种，以相同语种进行回复。"
    return {"info": matched_content.strip()}

# 使用示例
if __name__ == "__main__":
    query = "Module13.2 QRCode 序号10 条码"
    
    # 查询产品文档
    print("=== 查询产品文档 ===")
    result_product = retrieve_knowledge_text(query, is_chip=True, filter_type="product")
    print("查询结果:")
    print(result_product['info'])
    
    # 查询芯片文档
    # print("\n=== 查询芯片文档 ===")
    # result_chip = retrieve_knowledge_text("芯片手册", is_chip=True, filter_type="product")
    # print("查询结果:")
    # print(result_chip['info'][:500] + "..." if len(result_chip['info']) > 500 else result_chip['info'])
