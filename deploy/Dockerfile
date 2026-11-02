# 云端控制平面镜像：只跑 Python，没有 Stata（执行在用户本机代理上）。
# 构建上下文是仓库根目录：
#   docker build -f deploy/Dockerfile -t text2stata-cloud .

FROM python:3.12-slim

# tini：让 gunicorn 正确转发 SIGTERM，docker stop 时不等 10 秒强杀。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY deploy/requirements-cloud.txt /app/deploy/requirements-cloud.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements-cloud.txt

# backend/core 被 cloud/plans.py 通过 sys.path 引用（模板装配与结果体检共用本地版实现），
# 拷整个 backend/ 但 uploads/workspace 在镜像里不会有人写。
COPY backend /app/backend
COPY cloud /app/cloud

# 数据（SQLite、产物、secret）全部落 volume，容器随时可重建。
ENV T2S_CLOUD_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 为什么 workers=1：突发限流是进程内存态（cloud/quota.py 的 _BURST_LOCK），
# 多 worker 各算各的，限流形同虚设；SQLite 也偏好单写入者。
# 吞吐靠 threads 撑，这个小站点绰绰有余。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--graceful-timeout", "20", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "cloud.server:create_app()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
