FROM python:3.11-slim

ARG APP_GIT_SHA=unknown
ARG APP_BUILD_TIME=unknown
ENV APP_GIT_SHA=${APP_GIT_SHA}
ENV APP_BUILD_TIME=${APP_BUILD_TIME}

# 让 Python 的输出直接进 docker logs，不要在缓冲区里等。
# 容器被 OOM Kill 时缓冲区内容会一起消失，而那几行恰恰是排障的全部线索
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
