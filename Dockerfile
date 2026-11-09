FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 TRIPAGENT_LLM_TIMEOUT=150

# 先装依赖再拷代码：改代码重新构建时这一层命中缓存，创空间构建快很多
COPY requirements-web.txt /app/requirements-web.txt
RUN pip install --no-cache-dir -r /app/requirements-web.txt

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY app.py ui_render.py ui_theme.py /app/

# 本地知识库与全国区划表：断网/无 key 也能出完整方案（现场演示的保险丝）
COPY data/fixtures /app/data/fixtures

# --no-deps：依赖已在上层装好，避免 setuptools 再去解析一遍网络
RUN pip install --no-cache-dir --no-deps -e /app

# 创空间只放通一个端口，且必须与 README 里的 app_port 一致
EXPOSE 7860

# enableCORS/enableXsrfProtection 关掉是刻意的：创空间把应用嵌在 iframe 里，
# Streamlit 默认的跨源检查会把 modelscope.cn 的 WebSocket 连接拒掉，表现为"页面空白/一直转圈"。
CMD ["python", "-m", "streamlit", "run", "app.py", \
     "--server.port", "7860", \
     "--server.address", "0.0.0.0", \
     "--server.headless", "true", \
     "--server.enableCORS", "false", \
     "--server.enableXsrfProtection", "false", \
     "--browser.gatherUsageStats", "false"]
