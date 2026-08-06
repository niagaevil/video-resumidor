import argparse
import gc
import glob
import os
import shutil
import site
import subprocess
import sys
import threading
import time

import model_config
import prompts
import requests

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def setup_cuda_dlls():
    """Windows: carrega cuBLAS/CUDNN dos pacotes pip nvidia-*-cu12."""
    if sys.platform != "win32":
        return
    search_roots = []
    for path in site.getsitepackages() + [site.getusersitepackages()]:
        if path and path not in search_roots:
            search_roots.append(path)
    for root in search_roots:
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            bin_path = os.path.join(root, "nvidia", sub, "bin")
            if not os.path.isdir(bin_path):
                continue
            os.environ["PATH"] = bin_path + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(bin_path)


setup_cuda_dlls()
from faster_whisper import WhisperModel

OLLAMA_URL = "http://localhost:11434/api"
OLLAMA_OPTIONS = {"temperature": 0.1, "top_p": 0.9, "num_ctx": 8192}
CHUNK_MAX_CHARS = int(os.environ.get("RESUMO_CHUNK_CHARS", "10000"))
# Quantos resumos parciais entram em cada rodada de consolidação.
# Mantém o prompt final pequeno mesmo quando o vídeo tem muitas horas.
SUMMARY_MERGE_BATCH_SIZE = int(os.environ.get("RESUMO_MERGE_LOTE", "4"))
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))
PROGRESS_EVERY_SEC = int(os.environ.get("PROGRESS_EVERY_SEC", "30"))


def get_model_chunk_chars(model):
    """Tamanho maximo de chunk otimizado para o modelo."""
    cfg = model_config.get_model_config(model)
    return cfg["chunk_chars"]


def get_model_merge_batch(model):
    """Tamanho do lote de merge otimizado para o modelo."""
    cfg = model_config.get_model_config(model)
    return cfg["merge_batch"]


def get_model_num_ctx(model):
    """Tamanho da janela de contexto otimizada para o modelo."""
    cfg = model_config.get_model_config(model)
    return cfg["num_ctx"]


def get_model_timeout(model):
    """Timeout otimizado para o modelo."""
    cfg = model_config.get_model_config(model)
    return cfg["timeout"]


def progress_log(msg):
    print(msg, flush=True)


class wait_indicator:
    """Imprime 'ainda trabalhando' a cada 15s (só texto, sem GPU extra)."""

    def __init__(self, label):
        self.label = label
        self._done = threading.Event()
        self._start = 0.0
        self._thread = None

    def __enter__(self):
        self._start = time.time()

        def loop():
            while not self._done.wait(15):
                elapsed = int(time.time() - self._start)
                mins, secs = divmod(elapsed, 60)
                progress_log(f"   ⏳ {self.label}... ({mins:02d}:{secs:02d})")

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._done.set()


def release_gpu_memory():
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def ollama_generate(prompt, model, timeout=None, label="Gerando com Ollama"):
    timeout = timeout or get_model_timeout(model)
    options = dict(OLLAMA_OPTIONS)
    options["num_ctx"] = get_model_num_ctx(model)
    try:
        with wait_indicator(label):
            r = requests.post(
                f"{OLLAMA_URL}/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": options,
                },
                timeout=timeout,
            )
        r.raise_for_status()
        return r.json().get("response", "")
    except requests.exceptions.Timeout:
        raise RuntimeError(
            f"Ollama demorou mais de {timeout}s. "
            "Tente um modelo mais leve ou reduza o tamanho do video."
        ) from None
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Erro ao chamar Ollama: {e}") from e


def default_ollama_mode():
    return "docker" if os.path.exists("/.dockerenv") else "local"


def is_wsl():
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def windows_host_ip():
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            for line in f:
                if line.startswith("nameserver "):
                    return line.split()[1]
    except OSError:
        pass
    return None


def ollama_api_url(base):
    base = base.rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"


def ollama_candidates_local():
    urls = []
    override = os.environ.get("OLLAMA_HOST")
    if override:
        urls.append(ollama_api_url(override))
    urls.append("http://localhost:11434/api")
    if is_wsl():
        host = windows_host_ip()
        if host:
            urls.append(f"http://{host}:11434/api")
    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def check_ollama_url(api_url, timeout=3):
    try:
        requests.get(f"{api_url}/tags", timeout=timeout).raise_for_status()
        return True
    except Exception:
        return False


def print_ollama_help():
    print("\n❌ Ollama não está acessível.")
    if is_wsl():
        print("   No WSL, o Ollama do Windows não fica em localhost do Linux.")
        print("   Certifique-se de que o Ollama está aberto no Windows.")
        print("   Se ainda falhar, no Windows defina e reinicie o Ollama:")
        print("     setx OLLAMA_HOST \"0.0.0.0\"")
        print("   Ou force o host manualmente:")
        host = windows_host_ip()
        if host:
            print(f"     export OLLAMA_HOST=http://{host}:11434")
    else:
        print("   Inicie o Ollama (Menu Iniciar no Windows ou: ollama serve)")
    print("   Baixe um modelo: ollama pull qwen2.5:7b-instruct\n")


def resolve_ollama_url(mode):
    if mode == "local":
        for url in ollama_candidates_local():
            if check_ollama_url(url):
                return url
        print_ollama_help()
        sys.exit(1)
    base = os.environ.get("OLLAMA_HOST", "http://ollama:11434").rstrip("/")
    return base if base.endswith("/api") else f"{base}/api"

# =========================
# MODELOS OLLAMA
# =========================
def get_models():
    r = requests.get(f"{OLLAMA_URL}/tags")
    data = r.json()
    return [m["name"] for m in data["models"]]

def filter_llms(models):
    bad = ["embed", "vision", "clip"]
    return [m for m in models if not any(b in m for b in bad)]

def choose_model(preset=None):
    models = filter_llms(get_models())
    if not models:
        print("\n❌ Nenhum modelo LLM encontrado no Ollama.")
        print("   Baixe um: ollama pull qwen2.5:7b-instruct\n")
        sys.exit(1)

    if preset:
        if preset not in models:
            print(f"\n❌ Modelo não encontrado: {preset}")
            print(f"   Disponíveis: {', '.join(models)}\n")
            sys.exit(1)
        return preset

    print("\n🧠 Modelos disponíveis:\n")
    for i, m in enumerate(models):
        print(f"{i+1}. {m}")

    idx = int(input("\nEscolha o modelo: ")) - 1
    return models[idx]

# =========================
# UTIL
# =========================
def format_timestamp(seconds):
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"

# =========================
# ÁUDIO
# =========================
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe

    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "video-resumidor", "ffmpeg.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    pattern = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg_*", "*", "bin", "ffmpeg.exe",
    )
    matches = sorted(glob.glob(pattern), reverse=True)
    if matches:
        return matches[0]

    print("\n❌ FFmpeg não encontrado!")
    print("   Instale com:  winget install Gyan.FFmpeg")
    print("   Ou rode o install_windows.bat como administrador.")
    print("   Depois abra um NOVO terminal e tente novamente.\n")
    sys.exit(1)


def find_ffprobe():
    ffmpeg = find_ffmpeg()
    if ffmpeg.lower().endswith("ffmpeg.exe"):
        candidate = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe")
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ffprobe")


def get_media_duration(path):
    ffprobe = find_ffprobe()
    if not ffprobe:
        return None
    result = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def extract_audio(video, out=None):
    if out is None:
        out = os.path.join(os.environ.get("TEMP", "/tmp"), "audio_resumidor.wav")
    ffmpeg = find_ffmpeg()
    duration = get_media_duration(video)
    if duration:
        progress_log(f"   📹 Duração: {format_timestamp(duration)}")
    with wait_indicator("Extraindo áudio"):
        result = subprocess.run([
            ffmpeg, "-y",
            "-i", video,
            "-ar", "16000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            out
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        print(f"\n❌ FFmpeg falhou ao extrair áudio (código {result.returncode})")
        sys.exit(1)
    size_mb = os.path.getsize(out) / (1024 * 1024)
    progress_log(f"   ✅ Áudio extraído ({size_mb:.1f} MB)")
    return out, duration

# =========================
# TRANSCRIÇÃO
# =========================
def transcribe(audio, duration_hint=None):
    def collect_segments(segments, total_duration):
        seg_list = []
        last_report = -PROGRESS_EVERY_SEC
        for seg in segments:
            seg_list.append(seg)
            if total_duration and total_duration > 0:
                if seg.end - last_report >= PROGRESS_EVERY_SEC or len(seg_list) == 1:
                    pct = min(100, int(seg.end / total_duration * 100))
                    progress_log(
                        f"   🎤 {format_timestamp(seg.end)} / {format_timestamp(total_duration)}"
                        f" ({pct}%) — {len(seg_list)} trechos"
                    )
                    last_report = seg.end
            elif len(seg_list) % 25 == 0:
                progress_log(
                    f"   🎤 {format_timestamp(seg.end)} — {len(seg_list)} trechos"
                )
        return seg_list

    def run(device, compute_type):
        model = WhisperModel("base", device=device, compute_type=compute_type)
        try:
            segments, info = model.transcribe(audio)
            total_duration = duration_hint or getattr(info, "duration", None) or 0
            seg_list = collect_segments(segments, total_duration)
            plain = " ".join(s.text.strip() for s in seg_list if s.text.strip())
            timestamped = "\n".join(
                f"[{format_timestamp(s.start)}] {s.text.strip()}"
                for s in seg_list
                if s.text.strip()
            )
            progress_log(f"   ✅ {len(seg_list)} trechos transcritos")
            return plain, timestamped
        finally:
            del model
            release_gpu_memory()

    try:
        progress_log("   🔄 Tentando GPU (CUDA)...")
        plain, timestamped = run("cuda", "float16")
        progress_log("   ✅ Transcrição na GPU")
        return plain, timestamped
    except Exception as e:
        err = str(e).lower()
        if "cublas" in err or "cuda" in err or "cudnn" in err:
            progress_log("   ⚠️  CUDA incompleto no Windows (falta cuBLAS/CUDA 12)")
            progress_log("   ⚠️  Caindo para CPU — mais lento, mas funciona")
        else:
            progress_log(f"   ⚠️  GPU falhou ({e}) — usando CPU")

    progress_log("   🔄 Transcrevendo na CPU...")
    plain, timestamped = run("cpu", "int8")
    progress_log("   ✅ Transcrição na CPU")
    return plain, timestamped

# =========================
# RESUMO
# =========================
def split_transcription_chunks(text, max_chars=CHUNK_MAX_CHARS):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [text] if text.strip() else []

    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def summarize_chunk(chunk, model, chunk_num, total):
    prompt = prompts.format_chunk_prompt(chunk_text=chunk, chunk_num=chunk_num, total=total)
    return ollama_generate(prompt, model, label=f"Analisando trecho {chunk_num}/{total}")


def summarize_merge(partials, model):
    combined = "\n\n".join(
        f"### Trecho {i + 1}\n{p.strip()}" for i, p in enumerate(partials) if p.strip()
    )
    prompt = prompts.format_merge_prompt(combined_notes=combined)
    return ollama_generate(prompt, model, label="Montando resumo final")


def summarize_single(text, model):
    prompt = build_summary_prompt(text)
    return ollama_generate(prompt, model, label="Gerando resumo")


def build_summary_prompt(text):
    """Monta o prompt completo (instruções + transcrição) para resumo em LLM externa."""
    return prompts.format_single_prompt(transcription_text=text)


def summarize(text, model):
    chunk_chars = get_model_chunk_chars(model)
    merge_batch = get_model_merge_batch(model)

    chunks = split_transcription_chunks(text, max_chars=chunk_chars)
    if len(chunks) <= 1:
        progress_log("   📝 Resumo em lote único")
        return summarize_single(text, model)

    progress_log(f"   📦 Transcrição longa ({len(text):,} caracteres) — {len(chunks)} lotes (chunk={chunk_chars}, merge_batch={merge_batch})")
    partials = []
    for i, chunk in enumerate(chunks, 1):
        progress_log(f"   🔄 Lote {i}/{len(chunks)}...")
        partials.append(summarize_chunk(chunk, model, i, len(chunks)))
        progress_log(f"   ✅ Lote {i}/{len(chunks)} concluído")

    partials = [partial.strip() for partial in partials if partial and partial.strip()]
    if not partials:
        return ""

    batch_size = min(8, max(2, merge_batch))
    merge_round = 1
    while len(partials) > 1:
        progress_log(
            f"   🔗 Consolidando rodada {merge_round} "
            f"({len(partials)} resumos, até {batch_size} por vez)..."
        )
        merged = []
        for start in range(0, len(partials), batch_size):
            group = partials[start:start + batch_size]
            if len(group) == 1:
                merged.append(group[0])
                continue
            merged.append(summarize_merge(group, model))
        partials = [item.strip() for item in merged if item and item.strip()]
        if not partials:
            return ""
        merge_round += 1

    return partials[0]


def sanitize_model_for_filename(model):
    return model.replace(":", "_").replace("/", "_").replace("\\", "_")


def resumo_path_for_model(base_path, model):
    base = os.path.splitext(base_path)[0]
    safe = sanitize_model_for_filename(model)
    return f"{base}_resumo_{safe}.txt"


def transcribe_video(video):
    """Extrai áudio, transcreve e salva *_transcricao.txt. Retorna (txt_path, texto)."""
    progress_log("\n🎬 Extraindo áudio...")
    audio, duration = extract_audio(video)

    progress_log("🎤 Transcrevendo...")
    plain, timestamped = transcribe(audio, duration)

    if audio != video and os.path.isfile(audio):
        try:
            os.remove(audio)
        except OSError:
            pass

    txt_path = os.path.splitext(video)[0] + "_transcricao.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(timestamped)
    print(f"📄 Transcrição salva em: {txt_path}")

    progress_log("🧹 Liberando memória da transcrição (GPU/RAM) para o resumo...")
    del plain
    release_gpu_memory()
    return txt_path, timestamped


def summarize_transcription(timestamped, model, base_path):
    """Gera resumo e salva *_resumo.txt e *_resumo_{modelo}.txt."""
    print("\n🧠 Gerando resumo...\n")
    summary = summarize(timestamped, model)

    print("\n===== RESUMO =====\n")
    print(summary)

    base = os.path.splitext(base_path)[0]
    resumo_path = base + "_resumo.txt"
    model_path = resumo_path_for_model(base_path, model)

    with open(resumo_path, "w", encoding="utf-8") as f:
        f.write(summary)
    with open(model_path, "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"\n💾 Resumo salvo em: {resumo_path}")
    print(f"💾 Resumo ({model}) salvo em: {model_path}")
    return summary, resumo_path, model_path


def load_transcription_file(txt_path):
    with open(txt_path, encoding="utf-8") as f:
        return f.read()

# =========================
# CHAT COM VÍDEO
# =========================
def answer_question(text, question, model):
    prompt = prompts.format_chat_prompt(transcription_text=text, question=question)
    return ollama_generate(prompt, model)


def chat_loop(text, model):
    print("\n💬 Modo pergunta ativado (digite 'sair' para encerrar)\n")

    while True:
        q = input("Você: ")
        if q.lower() == "sair":
            break

        print("\n🤖:", answer_question(text, q, model), "\n")

# =========================
# MAIN
# =========================
def main():
    global OLLAMA_URL

    parser = argparse.ArgumentParser(
        description="Transcreve e resume reuniões em vídeo com Whisper + Ollama"
    )
    parser.add_argument(
        "input",
        help="Caminho do vídeo (ou .txt de transcrição com --summarize-only)",
    )
    parser.add_argument(
        "--ollama",
        choices=["local", "docker"],
        default=default_ollama_mode(),
        help="local = Ollama na máquina (no WSL tenta Windows host); docker = rede Docker",
    )
    parser.add_argument(
        "--model",
        help="Modelo Ollama (pula seleção interativa)",
    )
    parser.add_argument(
        "--no-chat",
        action="store_true",
        help="Encerra após gerar o resumo (sem modo pergunta)",
    )
    parser.add_argument(
        "--transcribe-only",
        action="store_true",
        help="Só transcreve o vídeo e salva *_transcricao.txt",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Só resume a partir de um *_transcricao.txt (input deve ser o .txt)",
    )
    args = parser.parse_args()

    if args.transcribe_only and args.summarize_only:
        print("❌ Use apenas um de --transcribe-only ou --summarize-only")
        sys.exit(1)

    OLLAMA_URL = resolve_ollama_url(args.ollama)
    print(f"🔗 Ollama: {OLLAMA_URL} (modo {args.ollama})")

    if args.summarize_only:
        txt_path = args.input
        if not os.path.isfile(txt_path):
            print(f"❌ Arquivo não encontrado: {txt_path}")
            sys.exit(1)
        timestamped = load_transcription_file(txt_path)
        base_path = txt_path.replace("_transcricao.txt", "") if txt_path.endswith("_transcricao.txt") else os.path.splitext(txt_path)[0]
        model = choose_model(args.model)
        summarize_transcription(timestamped, model, base_path)
        if not args.no_chat:
            chat_loop(timestamped, model)
        return

    video = args.input
    txt_path, timestamped = transcribe_video(video)

    if args.transcribe_only:
        return

    model = choose_model(args.model)
    summarize_transcription(timestamped, model, video)

    if not args.no_chat:
        chat_loop(timestamped, model)


if __name__ == "__main__":
    main()
