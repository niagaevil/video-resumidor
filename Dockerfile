# Base com CUDA 12.1 + Python 3.11 — compatível com GTX 1650 (Turing / sm_75)
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Dependências do sistema
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3-pip \
    python3.11-dev \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Garante que python3 aponta para 3.11
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

WORKDIR /app

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Scripts e configs
COPY video_resumidor.py interface_web.py prompts.py prompts.json model_config.py VERSION ./

# Pasta de vídeos (montada pelo usuário no run)
VOLUME ["/videos"]

ENTRYPOINT ["python", "video_resumidor.py", "--ollama", "docker"]
