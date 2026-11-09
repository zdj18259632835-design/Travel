"""LLM provider：通义千问（阿里百炼 DashScope，OpenAI 兼容通道）。

设计原则：LLM 是「可选增强」而不是「必需依赖」。
所有调用方都必须在拿到 None 时退回规则引擎/模板文案，
这样没 key、断网、超配额三种情况下产品依然可用（比赛现场演示的保险丝）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import Provider, ProviderError, provider
from .usage import UsageLedger, usage_of

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@provider("llm", "dashscope")
class DashScope(Provider):
    def available(self) -> bool:
        return bool(self.settings.dashscope_api_key) and self.settings.online

    def describe(self) -> tuple[bool, str]:
        if not self.settings.dashscope_api_key:
            return False, "未配置 DASHSCOPE_API_KEY（意图理解/口碑归纳/文案润色将走规则引擎）"
        return True, f"通义千问 {self.settings.llm_model} @ DashScope 兼容模式"

    def _client(self):
        from openai import OpenAI

        return OpenAI(
            api_key=self.settings.dashscope_api_key,
            base_url=DASHSCOPE_BASE_URL,
            timeout=self.settings.llm_timeout,
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.4,
        json_mode: bool = False,
        web_search: bool = False,
        max_tokens: int = 1800,
        search_strategy: str = "max",
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # 本项目最划算的一次改动：qwen3 系列默认开思考模式。
        # 实测同一条推荐语请求，思考开=36.8s（生成 5040 tokens，其中 11260 字是 reasoning_content），
        # 思考关=4.0s，正文质量没有可感差别——因为我们做的全是"抽取与改写"，不是多步推理。
        # 联网检索同理：开思考 51.2s / 关思考 7.9s。
        # 需要恢复推理深度时把 TRIPAGENT_LLM_THINKING=1 设上即可，不用改代码。
        extra: dict[str, Any] = {} if self.settings.llm_thinking else {"enable_thinking": False}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if web_search:
            # 兼容通道下 enable_search 属非标准参数，必须走 extra_body
            extra["enable_search"] = True
            extra["search_options"] = {"search_strategy": search_strategy}
        if extra:
            kwargs["extra_body"] = extra

        ledger = UsageLedger(self.settings.usage_path)
        limit = self.settings.llm_daily_token_budget
        if limit > 0 and ledger.over_budget(limit):
            # 明确报错而不是静默降级：调用方会走本地知识库/模板文案，
            # 界面能告诉用户"今天模型额度用完了"，而不是让他等 40 秒拿到一份变笨的方案
            raise ProviderError(
                f"今日模型 token 额度已用完（{ledger.tokens_today()}/{limit}），已改用本地知识库与模板文案"
            )

        try:
            resp = self._client().chat.completions.create(**kwargs)
        except Exception as exc:
            raise ProviderError(f"DashScope 调用失败：{type(exc).__name__}: {str(exc)[:180]}") from exc
        prompt, completion, reasoning = usage_of(resp)
        ledger.record(prompt=prompt, completion=completion, reasoning=reasoning)
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        if not text:
            raise ProviderError("DashScope 返回空内容")
        return text

    def json_call(self, messages: list[dict[str, str]], **kw: Any) -> Any:
        """要求模型输出 JSON，并容忍 ```json 包裹/前后解释文字。"""
        raw = self.chat(messages, json_mode=True, **kw)
        return extract_json(raw)

    def search_json(self, messages: list[dict[str, str]], **kw: Any) -> Any:
        """联网检索 + JSON 抽取。

        刻意不开 response_format=json_object：DashScope 开启 enable_search 后会把
        引用来源混进正文，强制 JSON 模式反而产出无法解析的文本（实测报
        "模型输出中找不到合法 JSON"）。这里改用普通模式 + 容错抽取。
        """
        raw = self.chat(messages, json_mode=False, web_search=True, temperature=kw.pop("temperature", 0.3), **kw)
        return extract_json(raw)


def _strip_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _close_truncated(text: str) -> str:
    """模型被 max_tokens 截断时，退回到最后一个完整元素再补闭合括号。"""
    body = text.lstrip()
    for opener, closer in (("[", "]"), ("{", "}")):
        if not body.startswith(opener):
            continue
        if body.count(opener) == body.count(closer):
            return body
        cut = body.rfind(closer)
        if cut == -1:
            return opener + closer  # 一个完整元素都没有，给空容器而不是硬拼坏 JSON
        tail = closer if opener == "[" else ""
        return body[: cut + 1] + tail
    return body


def extract_json(text: str) -> Any:
    """从模型输出里抠出第一个合法 JSON 结构。

    联网检索模式下 qwen 的输出是间歇性不合法的（有时裸数组、有时包一层对象、
    有时带 ```json 围栏、有时尾逗号、有时被截断）。每一种都单独兜，
    否则上游只会看到一句没用的"找不到合法 JSON"。
    """
    if not text:
        raise ProviderError("空响应，无法解析 JSON")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    candidates = [cleaned]
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            candidates.append(cleaned[start : end + 1])
    for cand in candidates:
        for variant in (cand, _strip_trailing_commas(cand), _close_truncated(cand), _strip_trailing_commas(_close_truncated(cand))):
            try:
                return json.loads(variant)
            except Exception:
                continue
    raise ProviderError(f"模型输出中找不到合法 JSON（前 80 字：{text[:80]!r}）")
