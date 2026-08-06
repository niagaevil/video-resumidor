#!/bin/bash
# ============================================================
#  Video Resumidor — Instalador Linux Nativo
#  Modos: Docker (containers) | Local (Python + Ollama)
#  Testado em: Ubuntu 22.04/24.04, Debian 12, Fedora 40
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

detect_pkg_manager() {
    if command -v apt-get &>/dev/null; then
        echo "apt"
    elif command -v dnf &>/dev/null; then
        echo "dnf"
    elif command -v yum &>/dev/null; then
        echo "yum"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    else
        echo "unknown"
    fi
}

install_system_deps() {
    local pkg="$1"
    case "$pkg" in
        apt)
            sudo apt-get update -qq
            sudo apt-get install -y -qq python3 python3-pip python3-venv ffmpeg curl
            ;;
        dnf|yum)
            sudo "$pkg" install -y python3 python3-pip python3-virtualenv ffmpeg curl
            ;;
        pacman)
            sudo pacman -S --noconfirm python python-pip python-virtualenv ffmpeg curl
            ;;
        *)
            warn "Gerenciador de pacotes desconhecido. Instale manualmente:"
            warn "  python3, python3-pip, python3-venv, ffmpeg, curl"
            ;;
    esac
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/video-resumidor"
VENV_DIR="$INSTALL_DIR/venv"
VERSION_FILE="$SCRIPT_DIR/VERSION"
VERSION="2.0.0"
if [ -f "$VERSION_FILE" ]; then
    VERSION=$(head -1 "$VERSION_FILE")
fi

titulo "🎬 Video Resumidor v${VERSION} — Instalador Linux"

# Detecta se é WSL
if [ -n "$WSL_DISTRO_NAME" ] || grep -qi microsoft /proc/version 2>/dev/null; then
    warn "WSL detectado. Use install_wsl.sh para melhor integração com o Windows."
    echo "   Continuando mesmo assim..."
fi

echo "Pasta de instalação: $INSTALL_DIR"
echo "Origem do projeto:   $SCRIPT_DIR"
echo ""

echo -e "${BOLD}Como deseja rodar?${NC}"
echo ""
echo "  1) Docker — Whisper + Ollama em containers (GPU via nvidia-container-toolkit)"
echo "  2) Local  — Python + Ollama na máquina"
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
ok "Arquivos copiados para $INSTALL_DIR"

if [ "$INSTALL_MODE" = "local" ]; then
    # ═══════════════════════════════════════
    # MODO LOCAL — Python + Ollama
    # ═══════════════════════════════════════
    titulo "1/3 — Dependências do sistema"
    PKG=$(detect_pkg_manager)
    info "Gerenciador: $PKG"
    install_system_deps "$PKG"
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

    titulo "3/3 — Ollama"
    if command -v ollama &>/dev/null; then
        ok "Ollama já instalado: $(ollama --version 2>/dev/null || echo 'OK')"
    else
        info "Instalando Ollama..."
        curl -fsSL https://ollama.com/install.sh | sh
        ok "Ollama instalado"
    fi

    # Verifica se Ollama está rodando
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        ok "Ollama respondendo em localhost:11434"
    else
        warn "Ollama não está rodando. Inicie com: ollama serve"
    fi

    LAUNCHER="$HOME/.local/bin/resumir"
    mkdir -p "$HOME/.local/bin"

    cat > "$LAUNCHER" << LAUNCHEREOF
#!/bin/bash
set -e

INSTALL_DIR="$INSTALL_DIR"
VENV_DIR="$VENV_DIR"

if [ -z "\$1" ]; then
    echo "Uso: resumir /caminho/do/video.mp4"
    echo ""
    echo "Comandos:"
    echo "  resumir video.mp4           Processar vídeo"
    echo "  resumir --interface          Abrir interface web"
    echo "  resumir --transcribe-only    Só transcrever"
    echo "  resumir --summarize-only txt Só resumir de transcrição"
    exit 1
fi

if [ "\$1" = "--interface" ]; then
    source "\$VENV_DIR/bin/activate"
    python "\$INSTALL_DIR/interface_web.py"
    exit 0
fi

if [ "\$1" = "--transcribe-only" ]; then
    shift
    source "\$VENV_DIR/bin/activate"
    python "\$INSTALL_DIR/video_resumidor.py" --ollama local --transcribe-only "\$@"
    exit 0
fi

if [ "\$1" = "--summarize-only" ]; then
    shift
    source "\$VENV_DIR/bin/activate"
    python "\$INSTALL_DIR/video_resumidor.py" --ollama local --summarize-only "\$@"
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
    MODE_LABEL="Local (Python + Ollama)"

else
    # ═══════════════════════════════════════
    # MODO DOCKER
    # ═══════════════════════════════════════
    titulo "1/4 — Docker"
    command -v docker &>/dev/null || erro "Docker não encontrado. Instale: https://docs.docker.com/engine/install/"
    docker compose version &>/dev/null || erro "Docker Compose não encontrado."
    docker info &>/dev/null || erro "Docker não está rodando. Inicie o serviço."
    ok "Docker: $(docker --version | head -1)"
    ok "Compose: $(docker compose version | head -1)"

    titulo "2/4 — GPU (NVIDIA)"
    if command -v nvidia-smi &>/dev/null; then
        ok "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
        if docker run --rm --gpus all nvidia/cuda:12.1.1-base-ubuntu22.04 nvidia-smi &>/dev/null; then
            ok "GPU acessível no Docker"
        else
            warn "GPU não acessível no Docker — instale nvidia-container-toolkit:"
            warn "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
            warn "Continuando com CPU..."
        fi
    else
        warn "nvidia-smi não encontrado — usando CPU"
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
    echo -e "${BOLD}Qual modelo LLM deseja baixar?${NC}"
    echo ""
    echo "  1) qwen2.5:7b-instruct  — ~4.7 GB (recomendado)"
    echo "  2) llama3.1:8b          — ~4.7 GB"
    echo "  3) qwen2.5:14b          — ~9.0 GB (precisa GPU 12GB+)"
    echo "  4) llama3.2:3b          — ~2.0 GB (leve)"
    echo "  5) Pular (já tenho modelo)"
    echo ""
    read -p "Escolha [1-5]: " MODEL_CHOICE

    case $MODEL_CHOICE in
        1) MODEL="qwen2.5:7b-instruct" ;;
        2) MODEL="llama3.1:8b" ;;
        3) MODEL="qwen2.5:14b" ;;
        4) MODEL="llama3.2:3b" ;;
        5) MODEL="" ;;
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
    echo ""
    echo "Comandos:"
    echo "  resumir video.mp4    Processar vídeo"
    echo "  resumir --interface   Abrir interface web"
    exit 1
fi

if [ "\$1" = "--interface" ]; then
    echo "No modo Docker, use a interface web no Windows/WSL ou rode:"
    echo "  cd \$INSTALL_DIR && docker compose --profile run run --rm app --interface"
    exit 0
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

# ── PATH ──
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    # Detecta shell
    SHELL_RC=""
    if [ -f "$HOME/.bashrc" ]; then SHELL_RC="$HOME/.bashrc"; fi
    if [ -f "$HOME/.zshrc" ]; then SHELL_RC="$HOME/.zshrc"; fi
    if [ -n "$SHELL_RC" ]; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        info "PATH adicionado ao $SHELL_RC"
    fi
    export PATH="$HOME/.local/bin:$PATH"
fi

titulo "🎉 Instalação concluída! (v${VERSION}, $MODE_LABEL)"

echo -e "  ${BOLD}Como usar:${NC}"
echo ""
echo -e "  ${GREEN}resumir /caminho/do/video.mp4${NC}"
echo -e "  ${GREEN}resumir --interface${NC}              (abrir interface web)"
echo ""
echo -e "  Exemplo:"
echo -e "  ${GREEN}resumir ~/Videos/reuniao.mp4${NC}"
echo ""

if [ "$INSTALL_MODE" = "local" ]; then
    echo -e "  ${YELLOW}Certifique-se de que o Ollama está rodando:${NC}"
    echo -e "  ${YELLOW}  ollama serve${NC}"
    echo -e "  ${YELLOW}Baixe um modelo: ollama pull qwen2.5:7b-instruct${NC}"
else
    echo -e "  ${YELLOW}Mantenha o Docker rodando.${NC}"
fi

echo -e "  ${YELLOW}Se 'resumir' não funcionar: source ~/.bashrc (ou ~/.zshrc)${NC}"
echo ""
