FROM python:3.11-slim

WORKDIR /opt
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ===== system deps =====
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl ca-certificates \
    libatomic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# ===== python deps =====
COPY requirements.txt /opt/requirements.txt
RUN pip install -U pip \
 && pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=100 -r /opt/requirements.txt

# prisma python client/cli
RUN pip install -U prisma -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=100

# ===== app source =====
COPY . /opt

# ===== build-time env =====
# prisma generate 有时要求 DATABASE_URL 存在（不需要真实可连）
ARG DATABASE_URL="mysql://user:pass@127.0.0.1:3306/dummy"
ENV DATABASE_URL=${DATABASE_URL}

# ===== ✅ generate python prisma client only =====
# 固定 schema 路径：/opt/prisma/schema.prisma
# 下面的 --generator 名字要和 schema.prisma 里的 python generator 名一致：
#   generator client_py { provider = "prisma-client-py" ... }
RUN prisma generate --schema=/opt/prisma/schema.prisma --generator client_py

# ===== runtime =====
# 运行时由 k8s env 注入真实 DATABASE_URL，这里清空避免误用 build 值
ENV DATABASE_URL=""

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
