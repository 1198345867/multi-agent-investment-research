"""LLM 客户端封装，统一走 OpenAI 兼容协议。

默认接入 AGICTO 聚合平台，可通过环境变量切换到任意兼容厂商：
    LLM_API_KEY    默认回退到 AGICTO_API_KEY
    LLM_BASE_URL   默认 https://api.agicto.cn/v1
    LLM_MODEL      默认 qwen-plus

本模块不做重试与降级：靠 response_format 约束输出格式，
再由 pydantic 严格校验。校验不通过即抛错，让问题暴露而不是被掩盖。
"""

from __future__ import annotations

import json
import os
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

DEFAULT_BASE_URL = "https://api.agicto.cn/v1"
DEFAULT_MODEL = "qwen-plus"

T = TypeVar("T", bound=BaseModel)

_client: OpenAI | None = None

# 累计用量：引擎在环节前后各取一次快照做差，得到该环节的调用次数与 token 消耗
_USAGE = {"calls": 0, "total_tokens": 0}


def _client_singleton() -> OpenAI:
    """复用同一个客户端实例，避免每次调用重建连接池。"""
    global _client
    if _client is None:
        api_key = os.environ.get("LLM_API_KEY") or os.environ["AGICTO_API_KEY"]
        _client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
        )
    return _client


def usage_snapshot() -> dict:
    """返回累计用量快照，供引擎按环节做差统计。"""
    return dict(_USAGE)


def _record_usage(resp) -> None:
    """累加一次调用的用量，供可观测统计使用。"""
    _USAGE["calls"] += 1
    _USAGE["total_tokens"] += resp.usage.total_tokens


def chat(system: str, user: str, temperature: float = 0.3) -> str:
    """普通文本对话，返回模型输出正文。"""
    resp = _client_singleton().chat.completions.create(
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
    )
    _record_usage(resp)
    return resp.choices[0].message.content


def chat_json(system: str, user: str, schema: type[T], temperature: float = 0.3) -> T:
    """结构化对话，返回经过 pydantic 校验的模型实例。

    schema 的 JSON Schema 会拼进系统提示词，让模型明确该产出哪些字段；
    配合 response_format=json_object 约束输出形态。
    """
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    resp = _client_singleton().chat.completions.create(
        model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        messages=[
            {
                "role": "system",
                "content": (
                    f"{system}\n\n"
                    f"只输出 JSON 对象，且必须严格符合以下 JSON Schema：\n{schema_text}"
                ),
            },
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=temperature,
    )
    _record_usage(resp)
    return schema.model_validate_json(resp.choices[0].message.content)