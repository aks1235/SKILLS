#!/usr/bin/env python3
"""AI Search - 通用 AI 搜索工具，支持多维度查询拆分和并发搜索。

配置文件: ~/.ai-search-mcp/config.json
或通过环境变量: AI_API_URL, AI_API_KEY, AI_SEARCH_MODEL_ID 等
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

CONFIG_PATH = os.path.expanduser("~/.ai-search-mcp/config.json")

DEFAULT_SYSTEM_PROMPT = """你是一个专业的搜索助手,擅长联网搜索并提供准确、详细的答案。

当前时间: {current_time}

搜索策略:
1. **优先使用最新、权威的信息源**
2. 对于时间敏感的查询,明确标注信息的时间
3. 提供多个来源的信息进行交叉验证
4. 对于技术问题,优先参考官方文档和最新版本
5. **时效性优先原则**: 当搜索结果包含不同时间的信息时:
   - 优先采信最新日期的信息
   - 自动过滤明显过时的内容(如去年的促销活动、旧版本的政策)
   - 如果旧信息与新信息冲突,以新信息为准并说明
   - 对于政策、价格、促销类查询,只返回当前有效的信息

输出要求:
- 尽可能全面详细没有遗漏的回答用户问题
- 时间相关信息必须基于上述当前时间判断
- **明确过滤过时信息**: 不要返回已失效的促销、过期的政策、旧版本的价格"""

DEFAULT_SPLIT_PROMPT = "你是查询拆分助手。只返回 JSON 数组，不要任何解释、标记或其他文本。直接输出 JSON 数组。"

DEFAULT_SYNTHESIZE_PROMPT = """你是一个搜索结果整合助手。基于下方搜索结果回答用户问题。

当前时间: {current_time}

要求:
1. 综合所有相关来源，给出全面、准确、详细的回答
2. 引用关键信息时用 [来源 N] 标注 (N 对应下面来源列表的编号)
3. 如果不同来源信息冲突，明确指出
4. 不确定的内容不要编造
5. 时间相关信息必须基于上述当前时间判断
6. **时效性优先原则**:
   - 优先采信最新日期的信息源
   - 自动过滤明显过时的内容(如2024年的促销活动、已失效的政策)
   - 如果搜索结果包含不同时期的信息,只返回当前有效的最新信息
   - 对于政策、价格、促销类查询,明确标注信息的时效性
7. 末尾保留"## 参考来源"段落，按 [N] 标题 — URL 的格式列出所有引用过的来源"""

TAVILY_ENDPOINT = "https://api.tavily.com/search"
EXA_ENDPOINT = "https://api.exa.ai/search"

THINKING_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL)
WHITESPACE_RE = re.compile(r"\n\s*\n")

# ---- config ---------------------------------------------------------


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}

    def env(key: str, default):
        val = os.environ.get(key)
        return type(default)(val) if val is not None else default

    return {
        "api_url": cfg.get("api_url") or os.environ.get("AI_API_URL", ""),
        "api_key": cfg.get("api_key") or os.environ.get("AI_API_KEY", ""),
        # analysis (拆分查询 + Tavily/Exa 结果整合) 用的 endpoint,缺省回退到主 api_url/api_key
        "analysis_api_url": (
            cfg.get("analysis_api_url")
            or os.environ.get("AI_ANALYSIS_API_URL")
            or cfg.get("api_url")
            or os.environ.get("AI_API_URL", "")
        ),
        "analysis_api_key": (
            cfg.get("analysis_api_key")
            or os.environ.get("AI_ANALYSIS_API_KEY")
            or cfg.get("api_key")
            or os.environ.get("AI_API_KEY", "")
        ),
        "search_model_id": cfg.get("search_model_id")
        or os.environ.get("AI_SEARCH_MODEL_ID")
        or os.environ.get("AI_MODEL_ID", ""),
        "analysis_model_id": cfg.get("analysis_model_id")
        or os.environ.get("AI_ANALYSIS_MODEL_ID"),
        "system_prompt": cfg.get("system_prompt")
        or os.environ.get("AI_SYSTEM_PROMPT")
        or DEFAULT_SYSTEM_PROMPT,
        "split_prompt": cfg.get("split_prompt")
        or os.environ.get("AI_SPLIT_PROMPT")
        or DEFAULT_SPLIT_PROMPT,
        "synthesize_prompt": cfg.get("synthesize_prompt")
        or os.environ.get("AI_SYNTHESIZE_PROMPT")
        or DEFAULT_SYNTHESIZE_PROMPT,
        "timeout": int(cfg.get("timeout", env("AI_TIMEOUT", "60"))),
        # analysis(拆分查询 + tavily/exa 结果整合) 独立超时: 整合失败会降级为原始结果,
        # 故用较短窗口快速失败降级, 不傻等(对齐 smart-search 的 90s 量级)
        "analysis_timeout": int(cfg.get("analysis_timeout", env("AI_ANALYSIS_TIMEOUT", "90"))),
        "stream": str(cfg.get("stream", env("AI_STREAM", "true"))).lower() == "true",
        "filter_thinking": str(cfg.get("filter_thinking", env("AI_FILTER_THINKING", "true"))).lower() == "true",
        "search_retry_count": int(cfg.get("search_retry_count", env("AI_SEARCH_RETRY_COUNT", "1"))),
        "analysis_retry_count": int(cfg.get("analysis_retry_count", env("AI_ANALYSIS_RETRY_COUNT", "1"))),
        "max_query_length": int(cfg.get("max_query_length", env("AI_MAX_QUERY_LENGTH", "10000"))),
        # Tavily 配置
        "tavily_api_url": cfg.get("tavily_api_url")
        or os.environ.get("TAVILY_API_URL", TAVILY_ENDPOINT),
        "tavily_api_key": cfg.get("tavily_api_key") or os.environ.get("TAVILY_API_KEY", ""),
        "tavily_search_depth": cfg.get("tavily_search_depth")
        or os.environ.get("TAVILY_SEARCH_DEPTH", "advanced"),
        "tavily_max_results": int(cfg.get("tavily_max_results", env("TAVILY_MAX_RESULTS", "8"))),
        "tavily_include_answer": str(
            cfg.get("tavily_include_answer", env("TAVILY_INCLUDE_ANSWER", "true"))
        ).lower() == "true",
        # Tavily 独立超时与并发: 第三方中转站在 split 并发下易 554/超时,
        # 限制并发避免雪崩; 超时对齐 smart-search service.py 的 call_tavily_search (90s), 不复用主 timeout
        "tavily_timeout": int(cfg.get("tavily_timeout", env("TAVILY_TIMEOUT_SECONDS", "90"))),
        "tavily_concurrency": int(cfg.get("tavily_concurrency", env("TAVILY_CONCURRENCY", "2"))),
        # Exa 配置
        "exa_api_url": cfg.get("exa_api_url")
        or os.environ.get("EXA_API_URL", EXA_ENDPOINT),
        "exa_api_key": cfg.get("exa_api_key") or os.environ.get("EXA_API_KEY", ""),
        "exa_search_type": cfg.get("exa_search_type") or os.environ.get("EXA_SEARCH_TYPE", "auto"),
        "exa_num_results": int(cfg.get("exa_num_results", env("EXA_NUM_RESULTS", "8"))),
        "exa_text_max_chars": int(cfg.get("exa_text_max_chars", env("EXA_TEXT_MAX_CHARS", "1500"))),
        # 默认 provider
        "default_provider": cfg.get("default_provider") or os.environ.get("AI_PROVIDER", "ai"),
    }


# ---- API helpers ----------------------------------------------------


def build_endpoint(api_url: str) -> str:
    if not api_url.endswith("/v1/chat/completions"):
        api_url = api_url.rstrip("/") + "/v1/chat/completions"
    return api_url


def get_error_message(code: int) -> str:
    messages = {
        400: "请求参数错误，请检查查询内容",
        401: "认证失败，请检查 api_key 是否正确",
        403: "访问被拒绝，请检查 api_key 权限",
        404: "API 端点不存在，请检查 api_url 配置",
        408: "请求超时，请稍后重试",
        413: "请求体过大，请减少查询内容",
        429: "请求过于频繁，建议稍后重试或切换 API 渠道",
    }
    if 500 <= code < 600:
        return "服务暂时不可用，请稍后重试"
    return messages.get(code, f"请求失败 (HTTP {code})")


def chat_request(endpoint: str, api_key: str, model: str, system_prompt: str,
                 user_query: str, stream: bool, timeout: int) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        "stream": stream,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return raw


def parse_stream(data: str) -> str:
    chunks: list[str] = []
    for line in data.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
            delta = obj["choices"][0].get("delta", {})
            content = delta.get("content", "")
            if content:
                chunks.append(content)
        except (json.JSONDecodeError, KeyError, IndexError):
            pass
    return "".join(chunks)


def parse_json_response(data: str) -> str:
    obj = json.loads(data)
    return obj["choices"][0]["message"]["content"]


def filter_thinking(text: str) -> str:
    text = THINKING_RE.sub("", text)
    text = WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


def call_api(cfg: dict, query: str, *, custom_prompt: str | None = None,
             model_id: str | None = None, retry_count: int = 0,
             api_url: str | None = None, api_key: str | None = None,
             timeout: int | None = None) -> str:
    endpoint = build_endpoint(api_url or cfg["api_url"])
    key = api_key or cfg["api_key"]
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S %A")
    system_prompt = custom_prompt or cfg["system_prompt"].replace(
        "{current_time}", now_str,
    )
    # 把当前时间注入用户消息，模型更关注用户消息
    query = f"[当前时间: {now_str}]\n\n{query}"
    model = model_id or cfg["search_model_id"]
    retryable = {401, 402, 403, 408, 429, 500, 501, 502, 503, 504}

    for attempt in range(retry_count + 1):
        try:
            raw = chat_request(
                endpoint, key, model, system_prompt, query,
                cfg["stream"], timeout or cfg["timeout"],
            )
            text = parse_stream(raw) if cfg["stream"] else parse_json_response(raw)
            if cfg["filter_thinking"]:
                text = filter_thinking(text)
            return text
        except urllib.error.HTTPError as e:
            code = e.code
            if code in retryable and attempt < retry_count:
                print(f"  请求失败 (HTTP {code}), 重试 {attempt+1}/{retry_count}", file=sys.stderr)
                time.sleep(1)
            else:
                return f"[错误] {get_error_message(code)}"
        except Exception as e:
            if attempt < retry_count:
                print(f"  请求失败, 重试 {attempt+1}/{retry_count}: {e}", file=sys.stderr)
                time.sleep(1)
            else:
                return f"[错误] {e}"
    return "[错误] 未知错误"


# ---- web search providers (tavily / exa) ----------------------------


class SearchProviderError(Exception):
    """搜索引擎调用失败，区别于 HTTP 错误"""


def http_post_json(url: str, headers: dict, body: dict, timeout: int) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            **headers,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def tavily_search(cfg: dict, query: str) -> list[dict]:
    if not cfg["tavily_api_key"]:
        raise SearchProviderError("未配置 tavily_api_key，请在 ~/.ai-search-mcp/config.json 或 TAVILY_API_KEY 环境变量中设置")

    body = {
        "query": query,
        "topic": "general",
        "search_depth": cfg["tavily_search_depth"],
        "max_results": cfg["tavily_max_results"],
        "include_answer": cfg["tavily_include_answer"],
        "include_raw_content": False,
        "include_images": False,
    }

    # 如果是时效性查询，添加时间过滤（只搜索最近6个月）
    if is_time_sensitive_query(query):
        now = datetime.now(timezone(timedelta(hours=8)))
        six_months_ago = now - timedelta(days=180)
        body["days"] = 180  # Tavily 支持 days 参数限制搜索时间范围
        print(f"  [Tavily] 时效性查询，限制搜索范围: 最近180天", file=sys.stderr)

    headers = {"Authorization": f"Bearer {cfg['tavily_api_key']}"}

    # 随机抖动，避免 --split 并发请求同时到达触发限流
    import random
    time.sleep(random.uniform(0, 1.5))

    tavily_timeout = cfg.get("tavily_timeout") or cfg["timeout"]
    max_retries = 3
    # 429 限流 + 5xx 中转站错误(含 554) 都重试, 中转站在并发压力下常返 5xx
    retryable_codes = {429, 500, 502, 503, 504, 554}
    for attempt in range(max_retries + 1):
        try:
            resp = http_post_json(cfg["tavily_api_url"], headers, body, tavily_timeout)
            break
        except urllib.error.HTTPError as e:
            if e.code in retryable_codes and attempt < max_retries:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"  Tavily {e.code}, {wait:.1f}s 后重试 {attempt+1}/{max_retries}", file=sys.stderr)
                time.sleep(wait)
            else:
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    detail = err_body.get("detail") or err_body.get("error") or err_body.get("message") or str(err_body)
                except Exception:
                    detail = get_error_message(e.code)
                raise SearchProviderError(f"Tavily {e.code}: {detail}") from e
        except Exception as e:
            if attempt < max_retries:
                wait = (2 ** attempt) + random.uniform(0, 1)
                print(f"  Tavily 调用失败, {wait:.1f}s 后重试 {attempt+1}/{max_retries}: {e}", file=sys.stderr)
                time.sleep(wait)
            else:
                raise SearchProviderError(f"Tavily 调用失败: {e}") from e

    # 异步 deep research endpoint 会返回 request_id + status,而非 results
    if "results" not in resp:
        if "request_id" in resp and "status" in resp:
            raise SearchProviderError(
                f"Tavily 返回异步任务 (status={resp.get('status')}),当前 skill 仅支持同步 search 端点。"
                f"请把 tavily_api_url 改为 search 端点(如 https://api.tavily.com/search),不要用 /research"
            )
        raise SearchProviderError(f"Tavily 响应格式异常,缺少 results 字段: {str(resp)[:200]}")

    results = []
    # Tavily 自带的简短 answer 作为额外条目放在最前
    if resp.get("answer"):
        results.append({
            "title": "Tavily 摘要",
            "url": "",
            "content": resp["answer"],
            "score": 1.0,
            "source": "tavily-answer",
        })
    for item in resp.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": item.get("content", ""),
            "score": item.get("score", 0.0),
            "published_date": item.get("published_date", ""),
            "source": "tavily",
        })
    return results


def exa_search(cfg: dict, query: str) -> list[dict]:
    if not cfg["exa_api_key"]:
        raise SearchProviderError("未配置 exa_api_key，请在 ~/.ai-search-mcp/config.json 或 EXA_API_KEY 环境变量中设置")

    body = {
        "query": query,
        "numResults": cfg["exa_num_results"],
        "type": cfg["exa_search_type"],
        "contents": {
            "text": {"maxCharacters": cfg["exa_text_max_chars"]},
        },
    }

    # 如果是时效性查询，添加时间过滤
    if is_time_sensitive_query(query):
        now = datetime.now(timezone(timedelta(hours=8)))
        six_months_ago = now - timedelta(days=180)
        # Exa 使用 ISO 8601 格式的日期过滤
        body["startPublishedDate"] = six_months_ago.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        print(f"  [Exa] 时效性查询，限制搜索范围: {six_months_ago.strftime('%Y-%m-%d')} 之后", file=sys.stderr)

    headers = {"x-api-key": cfg["exa_api_key"]}
    try:
        resp = http_post_json(cfg["exa_api_url"], headers, body, cfg["timeout"])
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            detail = err_body.get("message") or err_body.get("error") or str(err_body)
        except Exception:
            detail = get_error_message(e.code)
        raise SearchProviderError(f"Exa {e.code}: {detail}") from e
    except Exception as e:
        raise SearchProviderError(f"Exa 调用失败: {e}") from e

    results = []
    for item in resp.get("results", []):
        text = item.get("text") or item.get("summary") or ""
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "content": text,
            "score": item.get("score", 0.0),
            "published_date": item.get("publishedDate", ""),
            "author": item.get("author", ""),
            "source": "exa",
        })
    return results


def format_search_results(results: list[dict]) -> str:
    if not results:
        return "(无搜索结果)"
    lines = []
    for i, r in enumerate(results, start=1):
        header = f"[来源 {i}] {r.get('title') or '(无标题)'}"
        url = r.get("url") or ""
        if url:
            header += f"\nURL: {url}"
        date = r.get("published_date") or ""
        if date:
            header += f"\n发布时间: {date}"
        content = (r.get("content") or "").strip()
        lines.append(f"{header}\n内容: {content}")
    return "\n\n".join(lines)


def synthesize_with_ai(cfg: dict, query: str, results: list[dict],
                       provider_label: str) -> str:
    if not cfg["analysis_api_url"] or not cfg["analysis_api_key"]:
        # 没配 AI,直接退回原始格式化结果
        print(
            f"  [warn] 未配置 analysis_api_url/api_key (主 api_url/api_key 也未配),跳过 AI 整合，返回 {provider_label} 原始结果",
            file=sys.stderr,
        )
        return format_search_results(results)

    context = format_search_results(results)
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S %A")
    user_prompt = (
        f"[当前时间: {now_str}]\n\n"
        f"用户问题: {query}\n\n"
        f"以下是来自 {provider_label} 的搜索结果，请基于这些结果回答用户问题:\n\n"
        f"{context}"
    )
    system_prompt = cfg["synthesize_prompt"].replace(
        "{current_time}", now_str,
    )
    model = cfg.get("analysis_model_id") or cfg["search_model_id"]
    text = call_api(
        cfg, user_prompt,
        custom_prompt=system_prompt,
        model_id=model,
        retry_count=cfg.get("analysis_retry_count", 1),
        api_url=cfg["analysis_api_url"],
        api_key=cfg["analysis_api_key"],
        timeout=cfg.get("analysis_timeout") or cfg["timeout"],
    )
    # 整合失败(超时/网络错误) → 降级为原始格式化结果, 不外露 [错误], 至少保住来源的 URL+内容
    # 对齐 smart-search service.py 的做法: 整合层失败静默降级, 不让用户看到 timed out
    if isinstance(text, str) and text.startswith("[错误]"):
        print(f"  [warn] {provider_label} 结果整合失败 ({text[:60]}), 降级返回原始结果", file=sys.stderr)
        return format_search_results(results)
    return text


def search(cfg: dict, query: str, provider: str) -> str:
    """统一入口: provider in {ai, tavily, exa}"""
    if provider == "ai":
        return call_api(cfg, query, retry_count=cfg["search_retry_count"])

    if provider == "tavily":
        try:
            results = tavily_search(cfg, query)
        except SearchProviderError as e:
            return f"[错误] {e}"
        return synthesize_with_ai(cfg, query, results, "Tavily")

    if provider == "exa":
        try:
            results = exa_search(cfg, query)
        except SearchProviderError as e:
            return f"[错误] {e}"
        return synthesize_with_ai(cfg, query, results, "Exa")

    return f"[错误] 未知 provider: {provider} (可选: ai / tavily / exa)"


VALID_PROVIDERS = {"ai", "tavily", "exa"}


def parse_providers(raw: str) -> list[str]:
    """解析 provider 字符串。支持单个 (ai) 或组合 (ai+tavily / ai,tavily / ai tavily)。
    返回去重后的有序 list,保留首次出现顺序。"""
    if not raw:
        return ["ai"]
    parts = re.split(r"[+,\s]+", raw.strip())
    out: list[str] = []
    for p in parts:
        p = p.strip().lower()
        if not p:
            continue
        if p not in VALID_PROVIDERS:
            raise ValueError(f"未知 provider: {p} (可选: {'/'.join(sorted(VALID_PROVIDERS))})")
        if p not in out:
            out.append(p)
    return out or ["ai"]


# ---- time-sensitive query detection ---------------------------------


def is_time_sensitive_query(query: str) -> bool:
    """检测查询是否具有时效性（政策、价格、促销、新闻等）"""
    time_sensitive_keywords = [
        # 政策类
        "政策", "补贴", "规定", "标准", "细则", "通知", "公告",
        # 价格/促销类
        "价格", "多少钱", "优惠", "折扣", "促销", "活动", "补贴", "额度",
        "厂补", "置换", "以旧换新",
        # 时间相关
        "最新", "现在", "当前", "今年", "本月", "最近",
        # 新闻/动态类
        "发布", "推出", "上市", "更新", "调整",
        # 技术/产品版本
        "版本", "新功能", "更新日志", "changelog",
    ]
    query_lower = query.lower()
    return any(keyword in query_lower for keyword in time_sensitive_keywords)


def enhance_query_with_time(query: str) -> str:
    """为时效性查询自动添加时间限定"""
    now = datetime.now(timezone(timedelta(hours=8)))
    current_year = now.year
    current_month = now.month

    # 如果查询已经包含明确的时间，不重复添加
    if str(current_year) in query or f"{current_year}年" in query:
        return query

    if is_time_sensitive_query(query):
        # 添加年份和"最新"关键词
        return f"{query} {current_year}年{current_month}月最新"

    return query


# ---- query splitting ------------------------------------------------


def split_query(cfg: dict, query: str, count: int) -> list[str]:
    system_prompt = cfg["split_prompt"]
    model = cfg.get("analysis_model_id") or cfg["search_model_id"]

    user_prompt = (
        f'将查询拆分成 {count} 个子问题，返回 JSON 数组。\n\n'
        f'查询: {query}\n\n'
        f'只返回 JSON 数组，格式: ["子问题1", "子问题2", "子问题3"]'
    )

    result = call_api(
        cfg, user_prompt,
        custom_prompt=system_prompt,
        model_id=model,
        retry_count=cfg.get("analysis_retry_count", 1),
        api_url=cfg["analysis_api_url"],
        api_key=cfg["analysis_api_key"],
        timeout=cfg.get("analysis_timeout") or cfg["timeout"],
    )

    cleaned = result.strip()
    for prefix in ("```json", "```"):
        cleaned = cleaned.removeprefix(prefix)
    cleaned = cleaned.removesuffix("```").strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return parsed.get("subquestions", [])
    except json.JSONDecodeError:
        pass

    # 兜底: 按句号/问号简单切分
    print(f"  AI 拆分失败，使用简单切分", file=sys.stderr)
    parts = [p.strip() for p in re.split(r"[。？?！!\n]", query) if p.strip()]
    return parts[:count] if parts else [query]


# ---- main -----------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI 搜索工具 (provider: ai / tavily / exa,支持组合如 ai+tavily 并发)",
    )
    parser.add_argument("query", nargs="?", help="搜索查询")
    parser.add_argument("--split", type=int, default=1, metavar="N", help="拆分查询数量（默认 1 即不拆分）")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--read-stdin", action="store_true", help="从 stdin 读取查询")
    parser.add_argument(
        "--provider",
        default=None,
        help="搜索 provider: ai / tavily / exa,或组合如 'ai+tavily' / 'ai,tavily' (并发执行后拼接结果)",
    )
    args = parser.parse_args()

    query = args.query
    if args.read_stdin or not query:
        query = sys.stdin.read().strip()

    if not query:
        print("错误: 请提供搜索查询", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    try:
        providers = parse_providers(args.provider or cfg["default_provider"])
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    # 按每个 provider 做配置校验
    for p in providers:
        if p == "ai":
            if not cfg["api_url"] or not cfg["api_key"]:
                print(
                    "错误: provider=ai 需要配置 api_url 和 api_key。请在 ~/.ai-search-mcp/config.json 中设置",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif p == "tavily":
            if not cfg["tavily_api_key"]:
                print(
                    "错误: provider=tavily 需要配置 tavily_api_key (或 TAVILY_API_KEY 环境变量)",
                    file=sys.stderr,
                )
                sys.exit(1)
        elif p == "exa":
            if not cfg["exa_api_key"]:
                print(
                    "错误: provider=exa 需要配置 exa_api_key (或 EXA_API_KEY 环境变量)",
                    file=sys.stderr,
                )
                sys.exit(1)

    # 拆分查询和 AI 整合都依赖 OpenAI 兼容 API (analysis endpoint,缺省回退到主 endpoint)
    needs_llm = args.split > 1 or any(p in {"tavily", "exa"} for p in providers)
    if needs_llm and (not cfg["analysis_api_url"] or not cfg["analysis_api_key"]):
        print(
            "  [warn] 未配置 analysis_api_url/api_key 也未配置主 api_url/api_key: "
            "--split 拆分及 tavily/exa 结果 AI 整合都会降级",
            file=sys.stderr,
        )

    max_len = cfg["max_query_length"]
    if len(query) > max_len:
        query = query[:max_len]

    # 自动增强时效性查询
    original_query = query
    query = enhance_query_with_time(query)
    if query != original_query:
        print(f"检测到时效性查询，自动增强为: {query}", file=sys.stderr)

    def search_with_split(p: str, sub_queries: list[str]) -> str:
        """用给定的子查询列表搜索，结果按 ### 子查询 N 分段组装"""
        total = len(sub_queries)
        # tavily 第三方中转站扛不住高并发, 限并发避免 554/超时雪崩; ai 路打本地代理可高并发
        max_workers = min(total, cfg.get("tavily_concurrency", 2)) if p == "tavily" else min(total, 10)
        print(f"[{p}] 并发搜索 {total} 个子查询 (max_workers={max_workers})...", file=sys.stderr)
        sub_results: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for i, sq in enumerate(sub_queries):
                sq_clean = str(sq).removeprefix("[SUB_QUERY] ").removeprefix("[SUB_QUERY]")
                sub_q = f"[SUB_QUERY] {sq_clean}" if p == "ai" else sq_clean
                fut = pool.submit(search, cfg, sub_q, p)
                futures[fut] = (i, sq_clean)
            for fut in as_completed(futures):
                i, sq_clean = futures[fut]
                try:
                    sub_results[i] = fut.result()
                    print(f"  [{p}] 子查询 {i+1}/{total} 完成", file=sys.stderr)
                except Exception as e:
                    sub_results[i] = f"[错误] {e}"
                    print(f"  [{p}] 子查询 {i+1}/{total} 失败: {e}", file=sys.stderr)
        # 全部子查询失败 → 降级为一行提示, 不输出满屏错误拖垮整体结果
        if sub_results and all(str(v).startswith("[错误]") for v in sub_results.values()):
            first_err = next(iter(sub_results.values()))
            return f"⚠️ {p} 源暂不可用 ({first_err[:80]}), 已跳过该来源, 请参考其他来源结果。"
        parts = []
        for i, sq in enumerate(sub_queries):
            sq_clean = str(sq).removeprefix("[SUB_QUERY] ").removeprefix("[SUB_QUERY]")
            parts.append(
                f"### 子查询 {i+1}\n\n"
                f"**子问题**: {sq_clean}\n\n"
                f"{sub_results.get(i, '[错误] 未返回结果')}"
            )
        return "\n\n".join(parts)

    def run_one_provider(p: str, pre_split: list[str] | None = None) -> str:
        """对单个 provider 跑搜索。pre_split 非空时复用已拆好的子查询，避免重复拆分。"""
        if args.split > 1:
            if pre_split is not None:
                return search_with_split(p, pre_split)
            else:
                print(f"[{p}] 拆分查询为 {args.split} 个子问题...", file=sys.stderr)
                sub_queries = split_query(cfg, query, args.split)
                if not isinstance(sub_queries, list) or len(sub_queries) == 0:
                    print(f"[{p}] 拆分失败，使用原始查询", file=sys.stderr)
                    return search(cfg, query, p)
                return search_with_split(p, sub_queries)
        else:
            print(f"[{p}] 搜索中...", file=sys.stderr)
            return search(cfg, query, p)

    start = time.time()

    # 多 provider 时拆分只做一次，结果复用，避免并发冲到分析 API
    shared_sub_queries: list[str] | None = None
    if args.split > 1 and len(providers) > 1:
        print(f"拆分查询为 {args.split} 个子问题 (多 provider 共享)...", file=sys.stderr)
        shared = split_query(cfg, query, args.split)
        if isinstance(shared, list) and len(shared) > 0:
            shared_sub_queries = shared
        else:
            print("拆分失败，各 provider 使用原始查询", file=sys.stderr)

    if len(providers) == 1:
        result = run_one_provider(providers[0])
    else:
        print(f"并发执行 {len(providers)} 个 provider: {' + '.join(providers)}", file=sys.stderr)
        provider_results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(providers)) as pool:
            futures = {pool.submit(run_one_provider, p, shared_sub_queries): p for p in providers}
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    provider_results[p] = fut.result()
                    print(f"[{p}] 完成", file=sys.stderr)
                except Exception as e:
                    provider_results[p] = f"[错误] {e}"
                    print(f"[{p}] 失败: {e}", file=sys.stderr)
        sections = []
        for p in providers:
            sections.append(
                f"# 来源: {p}\n\n{provider_results.get(p, '[错误] 未返回结果')}"
            )
        result = "\n\n---\n\n".join(sections)

    elapsed = time.time() - start
    print(f"搜索完成，耗时 {elapsed:.1f}s", file=sys.stderr)

    if args.json:
        print(json.dumps(
            {
                "query": query,
                "providers": providers,
                "result": result,
                "elapsed_s": round(elapsed, 1),
            },
            ensure_ascii=False,
        ))
    else:
        print(result)


if __name__ == "__main__":
    main()
