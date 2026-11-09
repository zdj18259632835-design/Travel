"""上传 GitHub 前的密钥扫描。

用法（项目根目录）：
    .venv\\Scripts\\python.exe scripts\\check_secrets.py

在 git 仓库里：只扫「真的会被提交」的文件（尊重 .gitignore，含未跟踪文件）。
还不在 git 仓库里：退化成按目录黑名单扫全树。
命中即以退出码 1 结束，可以直接接进 pre-commit 或推送前的固定动作。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".venv", ".git", "__pycache__", ".cache", "output", "node_modules", ".pytest_cache", "api_keys"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".db", ".pyc", ".webp", ".woff", ".woff2"}

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("DashScope/OpenAI 风格 Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("32 位十六进制令牌（高德 Key 形态）", re.compile(r"\b[0-9a-f]{32}\b")),
    ("阿里云 AccessKey ID", re.compile(r"\bLTAI[A-Za-z0-9]{8,}\b")),
    ("私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("赋值形态的密钥（引号字面量）", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\b\s*[=:]\s*[\"']([^\"'\s]{16,})[\"']")),
    ("环境变量形态 KEY=值", re.compile(r"^[A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)\s*=\s*([^#\s'\"=]{12,})", re.M)),
]

# 明显是占位符的值不算泄露
PLACEHOLDER_HINTS = ("xxx", "your", "example", "placeholder", "changeme", "replaceme", "<", "你的", "此处")


def _looks_placeholder(value: str) -> bool:
    low = value.lower()
    if any(h in low for h in PLACEHOLDER_HINTS):
        return True
    # 同一个字符重复占多数（sk-xxxxxxx / aaaaaaaa / 00000000）
    if len(set(low.strip("-_")) or "x") <= 2:
        return True
    return False


def _files_to_scan() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        if out.returncode == 0 and out.stdout.strip():
            files = [ROOT / line.strip() for line in out.stdout.splitlines() if line.strip()]
            return [f for f in files if f.exists()]
    except Exception:
        pass
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        files.append(p)
    return files


def _scan(path: Path) -> list[str]:
    hits: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return hits
    rel = path.relative_to(ROOT).as_posix()
    for i, line in enumerate(text.splitlines(), start=1):
        if "# nosec" in line or "check_secrets: ignore" in line:
            continue
        for label, pat in PATTERNS:
            for m in pat.finditer(line):
                value = m.group(1) if m.groups() and m.group(1) else m.group(0)
                if _looks_placeholder(value):
                    continue
                # 十六进制形态额外排除：出现在 hash/digest/md5 语境里的是代码，不是密钥
                if pat.pattern == r"\b[0-9a-f]{32}\b" and re.search(r"(hexdigest|sha1|sha256|md5|hash)", line, re.I):
                    continue
                hits.append(f"{rel}:{i}  [{label}]  {value[:8]}…{value[-4:]}")
    return hits


def main() -> int:
    files = _files_to_scan()
    hits: list[str] = []
    for f in files:
        hits.extend(_scan(f))
    print(f"[i] 扫描 {len(files)} 个文件（api_keys/ 与 .venv/.cache/output 已排除）")
    if not hits:
        print("[OK] 未发现密钥形态的内容，可以推送。")
        return 0
    print(f"[X] 发现 {len(hits)} 处疑似密钥，禁止推送：\n")
    for h in hits[:40]:
        print("   " + h)
    if len(hits) > 40:
        print(f"   … 另有 {len(hits) - 40} 处")
    print(
        "\n处理方式：\n"
        "  1) 把真实值移入 api_keys/tripagent.env（该目录已被两层 .gitignore 排除）\n"
        "  2) 文档/示例里改用占位符，如 DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx\n"
        "  3) 确属误报，在该行末尾加  # check_secrets: ignore\n"
        "  4) 若已提交过 git：历史里仍然存在，需吊销重建 Key 并用 git filter-repo 清理"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
