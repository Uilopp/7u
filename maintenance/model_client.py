"""OpenAI-compatible transport with bounded transient retries and sanitized errors."""

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


class ProviderError(RuntimeError):
    pass


class ProviderConfigurationError(ProviderError):
    """Requires account/configuration action; repeating the request will not help."""


class ProviderLengthError(ProviderError):
    """The model reached its output limit; callers may request one concise rewrite."""


class _NoModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A configured endpoint does not authorize forwarding its bearer key.
        return None


def open_model_request(request, *, timeout):
    return urllib.request.build_opener(_NoModelRedirect()).open(request, timeout=timeout)


def complete(
    messages,
    *,
    key=None,
    base_url="https://api.deepseek.com",
    model="deepseek-flash",
    tools=None,
    max_tokens=8000,
    thinking=None,
):
    key = key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise ProviderConfigurationError("未配置 API Key")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ProviderError("模型地址须为有效 HTTPS 地址")
    if type(max_tokens) is not int or not 256 <= max_tokens <= 32768:
        raise ValueError("max_tokens must be 256..32768")
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    if thinking is not None:
        if thinking not in ("enabled", "disabled"):
            raise ValueError("thinking must be enabled or disabled")
        body["thinking"] = {"type": thinking}
    if tools:
        body.update(tools=tools, tool_choice="auto")
    for attempt in range(3):
        request = urllib.request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        try:
            with open_model_request(request, timeout=90) as response:
                result = json.load(response)
            choices = result.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ProviderError("模型响应缺少候选答案")
            if choices[0].get("finish_reason") == "length":
                raise ProviderLengthError("模型输出被长度上限截断，请缩小任务范围")
            msg = choices[0].get("message")
            if not isinstance(msg, dict):
                raise ProviderError("模型响应格式无效")
            return msg
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if 300 <= status < 400:
                raise ProviderConfigurationError(
                    "模型地址返回重定向，已停止以保护密钥；请检查配置的直接接口地址。"
                ) from None
            if status in (401, 403):
                raise ProviderConfigurationError("模型服务拒绝授权，请检查 API Key") from None
            if status == 402:
                raise ProviderConfigurationError(
                    "模型服务要求处理账户计费（HTTP 402），请检查余额或账户状态。不会自动充值。"
                ) from None
            if status not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise ProviderError(f"模型服务请求失败（HTTP {status}）") from None
        except urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException:
            if attempt == 2:
                raise ProviderError("模型连接中断，已重试，请稍后再试") from None
        except json.JSONDecodeError, UnicodeError:
            raise ProviderError("模型服务返回了无效 JSON") from None
        time.sleep(2**attempt)
    raise ProviderError("模型请求未完成")
