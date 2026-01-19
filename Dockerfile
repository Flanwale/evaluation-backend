FROM python:3.11-slim

WORKDIR /opt
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssl ca-certificates \
    libatomic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /opt/requirements.txt
RUN pip install -U pip \
 && pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=100 -r /opt/requirements.txt

RUN pip install -U prisma -i https://pypi.tuna.tsinghua.edu.cn/simple --default-timeout=100

COPY . /opt

RUN set -eux; \
    echo "Searching schema.prisma under /opt ..."; \
    find /opt -maxdepth 5 -name schema.prisma -print; \
    SCHEMA_PATH="$(find /opt -maxdepth 5 -name schema.prisma | head -n 1)"; \
    if [ -z "$SCHEMA_PATH" ]; then \
      echo "ERROR: schema.prisma not found under /opt"; \
      ls -lah /opt; \
      find /opt -maxdepth 3 -type d -print; \
      exit 1; \
    fi; \
    echo "Using schema: $SCHEMA_PATH"; \
    prisma generate --schema="$SCHEMA_PATH"
ENV DATABASE_URL=""
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
