import json
import requests
import os
from pathlib import Path
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request
from volcengine.Credentials import Credentials


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
KNOWLEDGE_BASE_NAME = _config['knowledge_base_name']
PROJECT = _config['project']
REGION = _config['region']
SERVICE = _config['service']

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
            "retrieve_count": limit_num * 2,
            "chunk_diffusion_count": 3
        }
    }
    
    # 如果有type过滤条件，则添加到query_param中
    if type_filter:
        request_params["query_param"] = {
            "doc_filter": type_filter
        }
    
    info_req = prepare_request(method=method, path=path, data=request_params)
    rsp = requests.request(
        method=info_req.method,
        url=f"http://{g_knowledge_base_domain}{info_req.path}",
        headers=info_req.headers,
        data=info_req.body
    )
    return rsp.text

def retrieve_knowledge_text(query_text, num=1, is_chip=True, filter_type=None):
    """
    将输入文本与知识库进行匹配，返回匹配到的纯文本内容
    参数:
        query_text (str): 输入知识库查询的文本
        num (int): 用户问题中设计的产品数量或者操作数量
        is_chip(bool): 用户问题中是否涉及芯片（现在用于决定是否查询PDF文档）
        filter_type (str): 过滤类型
            - "product": 查询产品文档（type=1:在售产品, type=2:EOL产品）
            - "product_no_eol": 只查询在售产品文档（type=1）
            - "program": 查询编程相关文档（type=3）
    返回:
        dict: 包含匹配到的知识库内容的字典
    """
    # ----------------- 过滤条件映射 ---------------------
    type_filter = None
    if filter_type == "product":
        # 查询在售产品和EOL产品文档 (type=1,2)
        type_filter = create_type_filter([0,1, 2])
    elif filter_type == "product_no_eol":
        # 只查询在售产品文档 (type=1)
        type_filter = create_type_filter([0,1])
    elif filter_type == "program":
        # 查询编程相关文档 (type=3)
        type_filter = create_type_filter([0,3])
    elif filter_type == "esphome":
        # 查询esphome官方文档 (type=11)
        type_filter = create_type_filter([11])
    # ----------------- 其余逻辑保持不变 ---------------------
    limit_num = num * 20  # 多问一个产品或者操作就多返回10个切片
    if limit_num == 0:
        limit_num = 10
    elif limit_num > 50:
        limit_num = 50
    
    print(f"\n----- 知识库查询 -----")
    print(f"查询文本: {query_text}")
    print(f"限制数量: {limit_num}")
    print(f"过滤类型: {filter_type}")
    print(f"是否查询芯片文档: {is_chip}")
    
    # 调用知识库检索（查询type=1,2,3的文档）
    rsp_txt_doc = search_knowledge_documents(query_text, limit_num, type_filter)
    print(f"知识库原始响应: {rsp_txt_doc[:200]}...")
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
            except Exception as e:
                print(f"Error parsing product doc JSON: {e}")
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
        # 查询PDF文档 (type=4)
        pdf_filter = create_type_filter([4])
        rsp_txt_pdf = search_knowledge_documents(query_text, 10, pdf_filter)
        rsp_pdf = json.loads(rsp_txt_pdf)
        if rsp_pdf["code"] == 0:
            rsp_data_pdf = rsp_pdf["data"]
            # 处理可能的字符串情况
            if isinstance(rsp_data_pdf, str):
                try:
                    rsp_data_pdf = json.loads(rsp_data_pdf)
                except Exception as e:
                    print(f"Error parsing PDF doc JSON: {e}")
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
    result_product = retrieve_knowledge_text(query, 1, is_chip=True, filter_type="product")
    print("查询结果:")
    print(result_product['info'])
    
    # 查询芯片文档
    # print("\n=== 查询芯片文档 ===")
    # result_chip = retrieve_knowledge_text("芯片手册", 10, is_chip=True, filter_type="product")
    # print("查询结果:")
    # print(result_chip['info'][:500] + "..." if len(result_chip['info']) > 500 else result_chip['info'])