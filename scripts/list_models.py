"""列出当前 DashScope 账号可用的模型 ID（避免猜模型名）。

用法：
    .venv\\Scripts\\python.exe scripts\\list_models.py flash
    .venv\\Scripts\\python.exe scripts\\list_models.py qwen
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tripagent.config import get_settings  # noqa: E402


def main() -> int:
    from openai import OpenAI

    s = get_settings()
    if not s.dashscope_api_key:
        print("[X] 未配置 DASHSCOPE_API_KEY")
        return 1
    client = OpenAI(api_key=s.dashscope_api_key, base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", timeout=30)
    try:
        ids = sorted(m.id for m in client.models.list().data)
    except Exception as exc:
        print(f"[X] 拉取模型列表失败：{type(exc).__name__}: {str(exc)[:200]}")
        return 1
    needle = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    hits = [i for i in ids if not needle or needle in i.lower()]
    print(f"[i] 共 {len(ids)} 个模型，匹配 {len(hits)} 个（当前使用：{s.llm_model}）")
    for i in hits:
        print("   ", i)
    print("\n换模型：编辑 api_keys\\tripagent.env 的 TRIPAGENT_LLM_MODEL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
