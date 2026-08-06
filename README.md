# 🎬 Video Resumidor — Reuniões (Whisper + Ollama)

Transcreve e resume **gravações de reuniões** localmente usando **Whisper** + **Ollama (LLM)**.

Gera automaticamente:
- Transcrição com timestamps (`*_transcricao.txt`)
- Resumo executivo com decisões, ações, responsáveis e pendências (`*_resumo.txt`)

Você pode rodar de quatro formas:

| Modo | Onde roda | Ollama | Ideal para |
|---|---|---|---|
| **Local Windows** | Python no Windows | Ollama do Windows | Uso simples, sem Docker |
| **Local WSL** | Python no WSL | Ollama do Windows | WSL + Ollama já instalado no Windows |
| **Local Linux** | Python nativo | Ollama nativo | Linux desktop/server |
| **Docker** | Containers com GPU | Container Ollama | GPU garantida, ambiente isolado |

---

## 🪟 Windows — modo local (sem Docker)

### Instalação automática

1. Clique com botão direito em `install_windows.bat` → **Executar como administrador**
2. Escolha **1) Local** — Python no Windows + Ollama do Windows
3. Escolha o modelo LLM (ou **4) Pular** se já tiver)

O instalador configura Python, FFmpeg, venv, bibliotecas CUDA (cuBLAS) para GPU e o comando global `resumir`.

### Uso

Abra um **novo** Prompt de Comando:

```cmd
resumir "D:\caminho\do\video.mp4"
```

Ou direto na pasta do projeto:

```cmd
python video_resumidor.py --ollama local "D:\caminho\do\video.mp4"
```

### Interface web (arrastar e soltar)

Execute `abrir_interface.bat` ou:

```cmd
python interface_web.py
```

Abre o navegador em `http://127.0.0.1:8765`. Fluxo:

1. Escolha o modelo LLM no menu.
2. Selecione o vídeo (arraste ou clique) — **nada roda ainda**.
3. Clique em **Iniciar processamento**.
4. Quando a transcrição terminar, o download do `.txt` e o chat ficam disponíveis; o resumo continua em segundo plano.
5. Use **Prompt + transcrição (LLM online)** para baixar o texto pronto para colar no ChatGPT, Claude, etc.
6. Para testar outro modelo sem re-transcrever, use **Gerar resumo com este modelo**.
7. O **Histórico** guarda sessões anteriores em `%APPDATA%\video-resumidor\history.json`.

### Só transcrever ou só resumir (CLI)

```cmd
python video_resumidor.py --ollama local --transcribe-only "D:\video.mp4"
python video_resumidor.py --ollama local --summarize-only "D:\video_transcricao.txt" --model qwen2.5:7b-instruct
```

### Pré-requisitos (modo local Windows)

| Requisito | Observação |
|---|---|
| Python 3.11+ | Instalado pelo `.bat` se não tiver |
| FFmpeg | Instalado pelo `.bat` se não tiver |
| Ollama | Instalado pelo `.bat` ou [ollama.com](https://ollama.com) |
| GPU NVIDIA (opcional) | Driver atualizado; o instalador baixa `nvidia-cublas-cu12` etc. |

Baixar modelo no Ollama (se ainda não tiver):

```cmd
ollama pull qwen2.5:7b-instruct
```

### Instalação manual (sem o `.bat`)

```cmd
cd %USERPROFILE%\video-resumidor
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
python video_resumidor.py --ollama local "D:\caminho\do\video.mp4"
```

> Os pacotes `nvidia-*-cu12` são necessários para Whisper usar a GPU no Windows. Sem eles, o script cai para CPU automaticamente.

---

## 🐧 WSL — modo local (Whisper no WSL + Ollama no Windows)

No WSL, `localhost` é o Linux — **não** o Ollama do Windows. O script detecta o IP do Windows automaticamente.

### Instalação

```bash
bash install_wsl.sh
# escolha 2) Local
```

### Uso

```bash
resumir /mnt/d/caminho/do/video.mp4
```

**No Windows**, antes de rodar:
- Ollama aberto
- Modelo baixado: `ollama pull qwen2.5:7b-instruct`

Se o WSL não alcançar o Ollama, no Windows (cmd):

```cmd
setx OLLAMA_HOST "0.0.0.0"
```

Reinicie o Ollama e tente de novo.

---

## 🐧 Linux — modo local (Python + Ollama nativos)

### Instalação

```bash
chmod +x install_linux.sh
bash install_linux.sh
# escolha 2) Local
```

O instalador detecta seu gerenciador de pacotes (apt/dnf/pacman) automaticamente.

### Uso

```bash
resumir ~/Videos/reuniao.mp4
resumir --interface              # interface web no navegador
resumir --transcribe-only video.mp4
resumir --summarize-only transcricao.txt --model qwen2.5:7b-instruct
```

### Pré-requisitos manuais

```bash
# Ubuntu/Debian
sudo apt install python3 python3-pip python3-venv ffmpeg curl

# Fedora
sudo dnf install python3 python3-pip python3-virtualenv ffmpeg curl

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b-instruct
```

---

## 🐳 Docker (Windows, WSL ou Linux)

### Pré-requisitos

| Requisito | Versão mínima |
|---|---|
| Docker | 24+ |
| Docker Compose | 2.20+ |
| NVIDIA Driver | 525+ |
| nvidia-container-toolkit | WSL/Linux |

#### Instalar nvidia-container-toolkit (WSL/Linux)

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Instalação automática

**Windows:** `install_windows.bat` → opção **2) Docker**

**WSL:** `bash install_wsl.sh` → opção **1) Docker**

### Uso manual (sem instalador)

```bash
# 1. Subir Ollama e baixar modelo (primeira vez)
docker compose up -d ollama ollama-pull
# Acompanhe: docker compose logs -f ollama-pull

# 2. Processar vídeo
cp /caminho/do/video.mp4 ./videos/
docker compose --profile run run --rm app /videos/video.mp4
```

Com pasta de vídeos customizada:

```bash
VIDEO_DIR=/mnt/d/Users/Fulano/Videos docker compose --profile run run --rm app /videos/meu_video.mp4
```

Com o instalador, use o comando global:

```bash
resumir /mnt/d/caminho/do/video.mp4
```

---

## ⚙️ Parâmetro `--ollama`

```bash
python video_resumidor.py --ollama local  video.mp4   # Ollama na máquina / Windows host (WSL)
python video_resumidor.py --ollama docker video.mp4   # Rede Docker (padrão dentro do container)
```

Dentro do container Docker, o modo `docker` é usado automaticamente.

---

## 🧠 Modelos recomendados

### Por hardware

| Hardware | Modelos recomendados | Tamanho |
|---|---|---|
| 🖥️ **Básico** (8GB RAM, sem GPU) | `llama3.2:3b`, `qwen2.5:3b`, `phi3:mini` | ~2 GB |
| 💻 **Médio** (16GB RAM, 6-8GB VRAM) | `qwen2.5:7b`, `llama3.1:8b`, `mistral:7b` | ~4.7 GB |
| 🚀 **Avançado** (32GB RAM, 12GB+ VRAM) | `qwen2.5:14b`, `phi4:14b` | ~9 GB |

> O app ajusta automaticamente o tamanho dos chunks e o batch de merge conforme o modelo escolhido. Modelos menores recebem chunks menores para evitar que "se percam" no contexto.

### Para 6 GB VRAM (GTX 1650)

| Modelo | VRAM | Uso |
|---|---|---|
| `qwen2.5:7b-instruct` | ~4.7 GB | ⭐⭐⭐⭐ Padrão — melhor equilíbrio |
| `llama3.1:8b` | ~4.7 GB | ⭐⭐⭐⭐ Sólido, multilíngue |
| `mistral:7b` | ~4.1 GB | ⭐⭐⭐⭐ Bom com português |
| `llama3.2:3b` | ~2.0 GB | ⭐⭐⭐ Leve e rápido |

### Local (Ollama na máquina)

```cmd
ollama pull qwen2.5:7b-instruct
```

### Docker

```bash
docker exec -it ollama ollama pull llama3.2:3b
```

Trocar modelo padrão no pull automático: edite `OLLAMA_MODEL` em `docker-compose.yml` e rode `docker compose up ollama-pull` novamente.

---

## 📁 Arquivos gerados

Salvos ao lado do vídeo:

```
reuniao_gravada.mp4
reuniao_gravada_transcricao.txt   ← fala com [MM:SS]
reuniao_gravada_resumo.txt        ← resumo mais recente
reuniao_gravada_resumo_MODELO.txt ← um arquivo por modelo (re-resumos)
```

---

## 🔧 Comandos úteis (Docker)

```bash
docker compose logs -f ollama          # logs do Ollama
docker exec -it ollama ollama list     # modelos instalados
docker exec -it ollama ollama rm nome  # remover modelo
docker compose down                    # parar tudo
docker compose down -v                 # parar e apagar modelos
```
