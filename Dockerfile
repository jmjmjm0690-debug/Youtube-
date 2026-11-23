FROM python:3.11-slim

ENV TZ=Asia/Tokyo \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-noto-cjk \
    fonts-noto-cjk-extra \
    curl \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# デフォルトの立ち絵・BGMアセットを事前初期化（ユーザー配置素材がある場合は保護）
RUN python scripts/generate_default_assets.py

CMD ["python", "-m", "src.main"]
