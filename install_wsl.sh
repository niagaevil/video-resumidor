#!/bin/bash
# ============================================================
#  Video Resumidor — Instalador WSL
#  Modos: Docker (tudo em containers) | Local (Whisper WSL + Ollama Windows)
#  Testado em: Ubuntu 22.04 / WSL2
# ============================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
erro() { echo -e "${RED}❌ $1${NC}"; exit 1; }
titulo() {
    echo -e "\n${BOLD}${BLUE}══════════════════════════════════════${NC}"
    echo -e "${BOLD}${BLUE}  $1${NC}"
    echo -e "${BOLD}${BLUE}══════════════════════════════════════${NC}\n"
}

windows_host_ip() {
    awk '/^nameserver / {print $2; exit}' /etc/resolv.conf 2>/dev/null
}

check_windows_ollama() {
    local host ip
    for host in "localhost" "$(windows_host_ip)"; do
        [ -z "$host" ] && continue
        if curl -sf "http://${host}:11434/api/tags" &>/dev/null; then
            ok "Ollama no Windows acessível em http://${host}:11434"
            return 0
        fi
    done
    warn "Ollama não respondeu no Windows"
    warn "Abra o Ollama no Windows antes de usar o modo Local"
    warn "Se falhar, no Windows (cmd): setx OLLAMA_HOST \"0.0.0.0\" e reinicie o Ollama"
    return 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/video-resumidor"
VENV_DIR="$INSTALL_DIR/venv"

titulo "🎬 Video Resumidor — Instalador WSL"
echo "Pasta de instalação: $INSTALL_DIR"
echo "Origem do projeto:   $SCRIPT_DIR"
echo ""

echo -e "${BOLD}Como deseja rodar?${NC}"
echo ""
echo "  1) Docker — Whisper + Ollama em containers (GPU via Docker)"
echo "  2) Local  — Whisper no WSL + Ollama do Windows (fora do Docker)"
echo ""
read -p "Escolha [1-2]: " MODE_CHOICE

case $MODE_CHOICE in
    2) INSTALL_MODE="local" ;;
    *) INSTALL_MODE="docker" ;;
esac

echo ""
info "Modo selecionado: $INSTALL_MODE"
echo ""

mkdir -p "$INSTALL_DIR"

# ─────────────────────────────────────────
# ARQUIVOS COMUNS
# ─────────────────────────────────────────
for f in video_resumidor.py interface_web.py prompts.py prompts.json model_config.py requirements.txt VERSION; do
    [ -f "$SCRIPT_DIR/$f" ] || erro "Arquivo não encontrado: $SCRIPT_DIR/$f"
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done

echo "$INSTALL_MODE" > "$INSTALL_DIR/mode"

if [ "$INSTALL_MODE" = "local" ]; then
    # ═══════════════════════════════════════
    # MODO LOCAL — Python no WSL + Ollama Windows
    # ═══════════════════════════════════════
    titulo "1/3 — Dependências do sistema"

    info "Atualizando apt..."
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3 python3-pip python3-venv ffmpeg curl

    ok "Dependências instaladas"

    titulo "2/3 — Ambiente Python"

    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$INSTALL_DIR/requirements.txt"
    ok "Pacotes Python instalados"

    titulo "3/3 — Ollama no Windows"
    check_windows_ollama || true

    LAUNCHER="$HOME/.local/bin/resumir"
    mkdir -p "$HOME/.local/bin"

    cat > "$LAUNCHER" << LAUNCHEREOF
#!/bin/bash
set -e

INSTALL_DIR="$INSTALL_DIR"
VENV_DIR="$VENV_DIR"

if [ -z "\$1" ]; then
    echo "Uso: resumir /caminho/do/video.mp4"
    echo "      resumir --interface          (abrir interface web)"
    exit 1
fi

if [ "\$1" = "--interface" ]; then
    source "\$VENV_DIR/bin/activate"
    python "\$INSTALL_DIR/interface_web.py"
    exit 0
fi

if [ ! -f "\$1" ]; then
    echo "❌ Arquivo não encontrado: \$1"
    exit 1
fi

source "\$VENV_DIR/bin/activate"
python "\$INSTALL_DIR/video_resumidor.py" --ollama local "\$1"
LAUNCHEREOF

    chmod +x "$LAUNCHER"
    MODE_LABEL="Local (Whisper WSL + Ollama Windows)"

else
    # ═══════════════════════════════════════
    # MODO DOCKER — tudo em containers
    # ═══════════════════════════════════════
    titulo "1/4 — Docker"

    command -v docker &>/dev/null || erro "Docker não encontrado. Instale o Docker Desktop com WSL2."
    docker compose version &>/dev/null || erro "Docker Compose não encontrado."
    docker info &>/dev/null || erro "Docker não está rodando. Abra o Docker Desktop."

    ok "Docker: $(docker --version | head -1)"
    ok "Compose: $(docker compose version | head -1)"

    titulo "2/4 — GPU (NVIDIA)"

    if command -v nvidia-smi &>/dev/null; then
        ok "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
        if docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi &>/dev/null; then
            ok "GPU acessível no Docker"
        else
            warn "GPU não acessível no Docker — usando CPU"
        fi
    else
        warn "nvidia-smi não encontrado"
    fi

    titulo "3/4 — Imagens Docker"

    for f in docker-compose.yml Dockerfile; do
        [ -f "$SCRIPT_DIR/$f" ] || erro "Arquivo não encontrado: $SCRIPT_DIR/$f"
        cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
    done

    mkdir -p "$INSTALL_DIR/videos"
    cd "$INSTALL_DIR"

    info "Construindo imagem do app..."
    docker compose build app
    docker compose up -d ollama
    sleep 3
    ok "Containers prontos"

    echo ""
    echo -e "${BOLD}Qual modelo LLM deseja baixar no container?${NC}"
    echo ""
    echo "  1) qwen2.5:7b-instruct       — ~4.7 GB  (recomendado GTX 1650)"
    echo "  2) qwen2.5-coder:7b-instruct — ~4.7 GB  (código e técnico)"
    echo "  3) qwen3:8b                  — ~5.5 GB  (8B, apertado na 1650)"
    echo "  4) deepseek-r1:8b            — ~5.0 GB  (raciocínio)"
    echo "  5) llama3.2:3b               — ~2.0 GB  (leve e rápido)"
    echo "  6) Pular (já tenho modelo)"
    echo ""
    read -p "Escolha [1-6]: " MODEL_CHOICE

    case $MODEL_CHOICE in
        1) MODEL="qwen2.5:7b-instruct" ;;
        2) MODEL="qwen2.5-coder:7b-instruct" ;;
        3) MODEL="qwen3:8b" ;;
        4) MODEL="deepseek-r1:8b" ;;
        5) MODEL="llama3.2:3b" ;;
        6) MODEL="" ;;
        *) MODEL="qwen2.5:7b-instruct" ;;
    esac

    if [ -n "$MODEL" ]; then
        info "Baixando $MODEL..."
        docker exec ollama ollama pull "$MODEL"
        ok "Modelo $MODEL pronto"
    fi

    titulo "4/4 — Comando resumir"

    LAUNCHER="$HOME/.local/bin/resumir"
    mkdir -p "$HOME/.local/bin"

    cat > "$LAUNCHER" << LAUNCHEREOF
#!/bin/bash
set -e

INSTALL_DIR="$INSTALL_DIR"

if [ -z "\$1" ]; then
    echo "Uso: resumir /caminho/do/video.mp4"
    exit 1
fi

if [ ! -f "\$1" ]; then
    echo "❌ Arquivo não encontrado: \$1"
    exit 1
fi

VIDEO="\$(realpath "\$1")"
VIDEO_DIR="\$(dirname "\$VIDEO")"
VIDEO_NAME="\$(basename "\$VIDEO")"

cd "\$INSTALL_DIR"
docker compose up -d ollama >/dev/null 2>&1 || true
export VIDEO_DIR="\$VIDEO_DIR"
docker compose --profile run run --rm app "/videos/\$VIDEO_NAME"
LAUNCHEREOF

    chmod +x "$LAUNCHER"
    MODE_LABEL="Docker"
fi

if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    export PATH="$HOME/.local/bin:$PATH"
    info "PATH atualizado no .bashrc"
fi

titulo "🎉 Instalação concluída! ($MODE_LABEL)"

echo -e "  ${BOLD}Como usar:${NC}"
echo ""
echo -e "  ${GREEN}resumir /caminho/do/video.mp4${NC}"
echo ""
echo -e "  Exemplo (pasta de instalação):"
echo -e "  ${GREEN}resumir \"$INSTALL_DIR/videos/seu_video.mp4\"${NC}"
echo ""
echo -e "  Exemplo (unidade D: no Windows):"
echo -e "  ${GREEN}resumir /mnt/d/caminho/do/video.mp4${NC}"
echo ""

if [ "$INSTALL_MODE" = "local" ]; then
    echo -e "  ${YELLOW}Modo Local: o Ollama deve estar rodando no Windows.${NC}"
    echo -e "  ${YELLOW}Modelos: rode no Windows → ollama pull qwen2.5:7b-instruct${NC}"
else
    echo -e "  ${YELLOW}Modo Docker: mantenha o Docker Desktop aberto.${NC}"
fi

echo -e "  ${YELLOW}Se 'resumir' não funcionar: source ~/.bashrc${NC}"
echo ""
