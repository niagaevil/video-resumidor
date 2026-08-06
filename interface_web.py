"""Interface web local com arrastar-e-soltar para o Video Resumidor."""

import cgi
import json
import os
import threading
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

import model_config
import prompts

PORT = int(os.environ.get("VIDEO_RESUMIDOR_PORT", "8765"))
UI_VERSION = 5
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(os.environ.get("TEMP", "/tmp"), "video-resumidor-uploads")
HISTORY_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "video-resumidor")
HISTORY_FILE = os.path.join(HISTORY_DIR, "history.json")
MAX_HISTORY = 20
BUSY_STATUSES = frozenset({"transcribing", "summarizing"})
TRANSCRIPTION_READY_STATUSES = frozenset({"transcribed", "summarizing", "done"})

job = {
    "status": "idle",
    "log": [],
    "summary": "",
    "transcricao_path": "",
    "resumo_path": "",
    "video_path": "",
    "transcription": "",
    "download_base": "",
    "video_name": "",
    "model": "",
    "summaries": [],
    "history_id": "",
    "chat": [],
    "chat_busy": False,
    "error": "",
}
job_lock = threading.Lock()
history_lock = threading.Lock()
_ollama_ready = False


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ollama_models():
    urls = ["http://localhost:11434/api/tags"]
    for url in urls:
        try:
            r = requests.get(url, timeout=3)
            r.raise_for_status()
            models = [m["name"] for m in r.json().get("models", [])]
            bad = ("embed", "vision", "clip")
            return [m for m in models if not any(b in m for b in bad)]
        except Exception:
            continue
    return []


def append_log(line):
    with job_lock:
        job["log"].append(line)
        if len(job["log"]) > 200:
            job["log"] = job["log"][-200:]


def ensure_ollama():
    global _ollama_ready
    if _ollama_ready:
        return
    import video_resumidor as vr
    vr.OLLAMA_URL = vr.resolve_ollama_url("local")
    _ollama_ready = True


@contextmanager
def capture_progress_log():
    import video_resumidor as vr

    original = vr.progress_log

    def hooked(msg):
        append_log(msg)

    vr.progress_log = hooked
    try:
        yield vr
    finally:
        vr.progress_log = original


def ask_transcription(transcription, question, model):
    ensure_ollama()
    import video_resumidor as vr
    return vr.answer_question(transcription, question, model)


def build_prompt_llm_content(transcription):
    import video_resumidor as vr
    return vr.build_summary_prompt(transcription)


def safe_upload_path(path):
    if not path:
        return False
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(UPLOAD_DIR)
        return real == base or real.startswith(base + os.sep)
    except OSError:
        return False


def load_history():
    with history_lock:
        if not os.path.isfile(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []


def save_history(entries):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    with history_lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(entries[:MAX_HISTORY], f, ensure_ascii=False, indent=2)


def find_history_entry(entries, history_id):
    for entry in entries:
        if entry.get("id") == history_id:
            return entry
    return None


def history_entry_for_api(entry):
    return {
        "id": entry.get("id", ""),
        "video_name": entry.get("video_name", ""),
        "created_at": entry.get("created_at", ""),
        "transcricao_path": entry.get("transcricao_path", ""),
        "summaries": [
            {
                "model": s.get("model", ""),
                "path": s.get("path", ""),
                "created_at": s.get("created_at", ""),
            }
            for s in entry.get("summaries", [])
        ],
    }


def upsert_history_transcription(history_id, video_name, video_path, transcricao_path):
    entries = load_history()
    entry = find_history_entry(entries, history_id)
    if not entry:
        entry = {
            "id": history_id,
            "video_name": video_name,
            "video_path": video_path,
            "created_at": now_iso(),
            "transcricao_path": transcricao_path,
            "summaries": [],
        }
        entries.insert(0, entry)
    else:
        entry["video_name"] = video_name
        entry["video_path"] = video_path
        entry["transcricao_path"] = transcricao_path
        entries.remove(entry)
        entries.insert(0, entry)
    save_history(entries)


def add_history_summary(history_id, model, path, created_at=None):
    entries = load_history()
    entry = find_history_entry(entries, history_id)
    if not entry:
        return
    summary_entry = {
        "model": model,
        "path": path,
        "created_at": created_at or now_iso(),
    }
    summaries = [s for s in entry.get("summaries", []) if s.get("model") != model]
    summaries.append(summary_entry)
    entry["summaries"] = summaries
    entries.remove(entry)
    entries.insert(0, entry)
    save_history(entries)


def remove_history_entry(history_id):
    entries = [e for e in load_history() if e.get("id") != history_id]
    save_history(entries)


def load_summaries_from_disk(summary_meta):
    summaries = []
    for item in summary_meta:
        path = item.get("path", "")
        text = ""
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                text = f.read().strip()
        summaries.append({**item, "text": text})
    return summaries


def init_job(video_path, model, download_base, video_name, history_id=None):
    with job_lock:
        job.clear()
        job.update({
            "status": "transcribing",
            "log": [],
            "summary": "",
            "transcricao_path": "",
            "resumo_path": "",
            "video_path": video_path,
            "transcription": "",
            "download_base": download_base,
            "video_name": video_name,
            "model": model,
            "summaries": [],
            "history_id": history_id or str(uuid.uuid4()),
            "chat": [],
            "chat_busy": False,
            "error": "",
        })
        return job["history_id"]


def run_transcribe_job(video_path, download_base, video_name, history_id, model):
    init_job(video_path, model, download_base, video_name, history_id)
    ensure_ollama()
    try:
        with capture_progress_log() as vr:
            vr.OLLAMA_URL = vr.resolve_ollama_url("local")
            txt_path, timestamped = vr.transcribe_video(video_path)
        with job_lock:
            job["transcricao_path"] = txt_path
            job["transcription"] = timestamped
            job["status"] = "transcribed"
        upsert_history_transcription(history_id, video_name, video_path, txt_path)
    except Exception as exc:
        with job_lock:
            job["status"] = "error"
            job["error"] = str(exc)


def run_summarize_job(model):
    with job_lock:
        if not job.get("transcription"):
            job["status"] = "error"
            job["error"] = "Transcrição não disponível"
            return
        transcription = job["transcription"]
        video_path = job.get("video_path") or ""
        history_id = job.get("history_id", "")
        download_base = job.get("download_base", "video")
        job["status"] = "summarizing"
        job["model"] = model

    try:
        with capture_progress_log() as vr:
            vr.OLLAMA_URL = vr.resolve_ollama_url("local")
            base = video_path or os.path.join(UPLOAD_DIR, download_base)
            summary, resumo_path, model_path = vr.summarize_transcription(
                transcription, model, base
            )

        created_at = now_iso()
        summary_entry = {
            "model": model,
            "path": model_path,
            "text": summary,
            "created_at": created_at,
        }

        with job_lock:
            summaries = [s for s in job.get("summaries", []) if s.get("model") != model]
            summaries.append(summary_entry)
            job["summaries"] = summaries
            job["summary"] = summary
            job["resumo_path"] = resumo_path
            job["model"] = model
            job["status"] = "done"

        if history_id:
            add_history_summary(history_id, model, model_path, created_at)
    except Exception as exc:
        with job_lock:
            job["status"] = "transcribed" if job.get("transcription") else "error"
            job["error"] = str(exc)


def run_full_job(video_path, model, download_base, video_name):
    history_id = str(uuid.uuid4())
    run_transcribe_job(video_path, download_base, video_name, history_id, model)
    with job_lock:
        if job["status"] != "transcribed":
            return
    run_summarize_job(model)


def restore_job_from_history(history_id):
    entries = load_history()
    entry = find_history_entry(entries, history_id)
    if not entry:
        return False, "Entrada não encontrada no histórico"

    transcricao_path = entry.get("transcricao_path", "")
    if not transcricao_path or not os.path.isfile(transcricao_path):
        return False, "Arquivo de transcrição não encontrado no disco"

    with open(transcricao_path, encoding="utf-8") as f:
        transcription = f.read()

    summaries = load_summaries_from_disk(entry.get("summaries", []))
    summary = ""
    resumo_path = ""
    model = ""
    if summaries:
        latest = summaries[-1]
        summary = latest.get("text", "")
        resumo_path = latest.get("path", "")
        model = latest.get("model", "")

    video_name = entry.get("video_name", "video")
    download_base = os.path.splitext(video_name)[0] or "video"
    video_path = entry.get("video_path", "")

    with job_lock:
        job.clear()
        job.update({
            "status": "done" if summary else "transcribed",
            "log": [f"Histórico carregado: {video_name}"],
            "summary": summary,
            "transcricao_path": transcricao_path,
            "resumo_path": resumo_path,
            "video_path": video_path,
            "transcription": transcription,
            "download_base": download_base,
            "video_name": video_name,
            "model": model,
            "summaries": summaries,
            "history_id": history_id,
            "chat": [],
            "chat_busy": False,
            "error": "",
        })

    return True, None


HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Resumidor</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: "Segoe UI", system-ui, sans-serif;
      background: #0f1117;
      color: #e8eaed;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 24px;
    }
    .layout { width: min(760px, 100%); }
    .card {
      background: #1a1d27;
      border: 1px solid #2d3142;
      border-radius: 16px;
      padding: 32px;
      box-shadow: 0 20px 60px rgba(0,0,0,.45);
      margin-bottom: 20px;
    }
    h1 { margin: 0 0 8px; font-size: 1.6rem; }
    h3 { margin: 0 0 12px; font-size: 1rem; }
    p.sub { margin: 0 0 24px; color: #9aa0b4; }
    label { display: block; margin-bottom: 8px; color: #9aa0b4; font-size: .9rem; }
    select {
      width: 100%;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid #3d4258;
      background: #12151f;
      color: #e8eaed;
      margin-bottom: 16px;
    }
    #dropzone {
      border: 2px dashed #4f5d8a;
      border-radius: 14px;
      padding: 48px 24px;
      text-align: center;
      cursor: pointer;
      transition: border-color .2s, background .2s;
      background: #12151f;
    }
    #dropzone.dragover {
      border-color: #6c8cff;
      background: #161b2e;
    }
    #dropzone .icon { font-size: 2.4rem; margin-bottom: 12px; }
    #dropzone strong { display: block; font-size: 1.1rem; margin-bottom: 6px; }
    #dropzone span { color: #9aa0b4; font-size: .95rem; }
    #file-name { margin-top: 12px; color: #6c8cff; min-height: 1.2em; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
    #progress, #summary-section { margin-top: 24px; display: none; }
    #chat { margin-top: 24px; }
    #log {
      background: #0a0c12;
      border-radius: 8px;
      padding: 12px;
      font-family: Consolas, monospace;
      font-size: .82rem;
      max-height: 180px;
      overflow-y: auto;
      color: #b8c0d4;
      white-space: pre-wrap;
    }
    #summary {
      background: #0a0c12;
      border-radius: 8px;
      padding: 16px;
      max-height: 360px;
      overflow-y: auto;
      line-height: 1.55;
      white-space: pre-wrap;
    }
    .paths { margin-top: 12px; font-size: .85rem; color: #9aa0b4; }
    .error { color: #ff7b7b; }
    button {
      padding: 10px 18px;
      border: none;
      border-radius: 8px;
      background: #4f6ef7;
      color: white;
      font-weight: 600;
      cursor: pointer;
    }
    button:disabled { opacity: .5; cursor: not-allowed; }
    button.secondary {
      background: #2a3148;
      border: 1px solid #3d4258;
    }
    #chat h3 { margin: 0 0 12px; font-size: 1rem; }
    #chat-messages {
      background: #0a0c12;
      border-radius: 8px;
      padding: 12px;
      max-height: 280px;
      overflow-y: auto;
      margin-bottom: 12px;
    }
    .msg { margin-bottom: 12px; line-height: 1.5; }
    .msg.user strong { color: #6c8cff; }
    .msg.bot strong { color: #7ddea2; }
    .msg p { margin: 4px 0 0; white-space: pre-wrap; }
    #chat-form { display: flex; gap: 8px; }
    #chat-input {
      flex: 1;
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid #3d4258;
      background: #12151f;
      color: #e8eaed;
    }
    #chat-form button { margin-top: 0; white-space: nowrap; }
    .chat-hint { color: #9aa0b4; font-size: .85rem; margin: 0 0 12px; }
    .downloads {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 16px 0 8px;
    }
    .downloads a {
      display: inline-block;
      padding: 10px 16px;
      border-radius: 8px;
      background: #2a3148;
      color: #e8eaed;
      text-decoration: none;
      font-weight: 600;
      border: 1px solid #3d4258;
    }
    .downloads a.disabled {
      opacity: .45;
      pointer-events: none;
      cursor: not-allowed;
    }
    .downloads a:hover { background: #343d5a; }
    #reset { margin-top: 20px; }
    .version { margin-top: 20px; font-size: .75rem; color: #5c6378; text-align: center; }
    #summary-select-wrap { display: none; margin-bottom: 12px; }
    .history-item {
      background: #12151f;
      border: 1px solid #2d3142;
      border-radius: 10px;
      padding: 12px 14px;
      margin-bottom: 10px;
    }
    .history-item strong { display: block; margin-bottom: 4px; }
    .history-item .meta { color: #9aa0b4; font-size: .85rem; margin-bottom: 10px; }
    .history-item .history-actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .status-hint { color: #7ddea2; font-size: .9rem; margin: 12px 0 0; min-height: 1.2em; }
    .tabs {
      display: flex;
      gap: 0;
      margin-bottom: 20px;
      border-bottom: 2px solid #2d3142;
    }
    .tab {
      padding: 10px 22px;
      cursor: pointer;
      color: #9aa0b4;
      font-weight: 600;
      border-bottom: 2px solid transparent;
      margin-bottom: -2px;
      transition: color .2s, border-color .2s;
      font-size: .95rem;
    }
    .tab:hover { color: #c8cde0; }
    .tab.active { color: #6c8cff; border-bottom-color: #6c8cff; }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .prompt-editor { margin-bottom: 20px; }
    .prompt-editor label.prompt-label {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
      color: #c8cde0;
      font-weight: 600;
      font-size: .88rem;
    }
    .prompt-editor textarea {
      width: 100%;
      min-height: 160px;
      padding: 12px;
      border-radius: 8px;
      border: 1px solid #3d4258;
      background: #0a0c12;
      color: #e8eaed;
      font-family: Consolas, monospace;
      font-size: .8rem;
      line-height: 1.45;
      resize: vertical;
    }
    .prompt-editor textarea:focus { border-color: #6c8cff; outline: none; }
    .prompt-editor .hint {
      font-size: .75rem;
      color: #5c6378;
      margin-top: 4px;
    }
    .prompts-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .copy-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 6px 10px;
      border-radius: 6px;
      background: #2a3148;
      border: 1px solid #3d4258;
      color: #9aa0b4;
      font-size: .8rem;
      cursor: pointer;
      transition: background .2s, color .2s;
      margin-left: 8px;
      vertical-align: middle;
    }
    .copy-btn:hover { background: #343d5a; color: #e8eaed; }
    .copy-btn.copied { color: #7ddea2; border-color: #7ddea2; }
    .toast {
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%);
      background: #2a3148;
      color: #e8eaed;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: .9rem;
      border: 1px solid #3d4258;
      z-index: 1000;
      opacity: 0;
      transition: opacity .3s;
      pointer-events: none;
    }
    .toast.show { opacity: 1; }
    code {
      background: #1e2233;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: .85em;
      color: #c8cde0;
    }
    details summary { user-select: none; }
    details summary:hover { color: #c8cde0; }
    .tier-row {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      padding: 6px 0;
      border-bottom: 1px solid #1e2233;
    }
    .tier-row:last-child { border-bottom: none; }
    .tier-icon { font-size: 1.2rem; flex-shrink: 0; }
    .tier-label { color: #c8cde0; font-weight: 600; }
    .tier-models { color: #6c8cff; }
    .model-config-badge {
      display: inline-block;
      background: #1e2233;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: .75rem;
      margin-right: 4px;
      color: #7ddea2;
    }
  </style>
</head>
<body>
  <div class="layout">
    <div class="tabs">
      <div class="tab active" data-tab="process">🎬 Processar</div>
      <div class="tab" data-tab="prompts">📝 Prompts</div>
      <div class="tab" data-tab="history">📋 Histórico</div>
    </div>
    <div class="tab-panel active" id="panel-process">
    <div class="card">
      <h1>🎬 Video Resumidor</h1>
      <p class="sub">Escolha o modelo, selecione o vídeo e clique em Iniciar quando estiver pronto.</p>

      <label for="model">Modelo LLM (Ollama)</label>
      <select id="model"></select>

      <label for="preset-select">Tipo de reunião</label>
      <select id="preset-select"></select>

      <details id="model-tiers-details" style="margin-bottom:16px">
        <summary style="cursor:pointer;color:#9aa0b4;font-size:.9rem;margin-bottom:8px">💡 Modelos recomendados por hardware</summary>
        <div id="model-tiers" style="font-size:.82rem;line-height:1.5;color:#9aa0b4"></div>
      </details>
      <div id="model-info-hint" class="chat-hint" style="margin:-8px 0 12px;min-height:1.2em"></div>

      <div id="dropzone">
        <div class="icon">📁</div>
        <strong>Arraste o vídeo aqui</strong>
        <span>ou clique para selecionar (.mp4, .mkv, .webm, .mov…)</span>
        <div id="file-name"></div>
      </div>
      <input type="file" id="file-input" accept="video/*" hidden>

      <div class="actions">
        <button type="button" id="start" disabled>Iniciar processamento</button>
        <button type="button" id="resummarize" class="secondary" style="display:none" disabled>
          Gerar resumo com este modelo
        </button>
      </div>
      <p class="status-hint" id="status-hint"></p>

      <div id="progress">
        <div id="log"></div>
      </div>

      <div id="summary-section">
        <div id="summary-select-wrap">
          <label for="summary-model-select">Resumos gerados</label>
          <select id="summary-model-select"></select>
        </div>
        <h3>Resumo <button type="button" class="copy-btn" id="copy-summary" style="display:none" title="Copiar resumo">📋 Copiar</button></h3>
        <div id="summary"></div>
      </div>

      <div class="downloads" id="downloads">
        <a id="dl-transcricao" class="disabled" href="/api/download/transcricao" download>Baixar transcrição (.txt)</a>
        <a id="dl-prompt-llm" class="disabled" href="/api/download/prompt-llm" download>Prompt + transcrição (LLM online)</a>
        <a id="dl-resumo" class="disabled" href="/api/download/resumo" download>Baixar resumo (.txt)</a>
      </div>
      <div class="paths" id="paths">Downloads disponíveis após processar o vídeo.</div>

      <div id="chat">
        <h3>Perguntas sobre a reunião</h3>
        <p class="chat-hint">Pergunte o que quiser — as respostas usam só a transcrição.</p>
        <div id="chat-messages"></div>
        <form id="chat-form">
          <input id="chat-input" type="text" placeholder="Ex.: Quais decisões foram tomadas?" autocomplete="off" disabled>
          <button type="submit" id="chat-send" disabled>Enviar</button>
        </form>
      </div>

      <button type="button" id="reset" style="display:none">Processar outro vídeo</button>
      <div class="version" id="ui-version"></div>
    </div>
    </div>

    <div class="tab-panel" id="panel-prompts">
    <div class="card" id="prompts-section">
      <h1>📝 Editar Prompts</h1>
      <p class="sub">Personalize os prompts enviados ao Ollama. Use <code>{variavel}</code> como placeholder.</p>
      <label for="prompts-preset-select">Editar prompts do tipo de reunião</label>
      <select id="prompts-preset-select" style="margin-bottom:16px"></select>
      <div class="prompts-actions">
        <button type="button" id="prompts-save" class="secondary">Salvar alterações</button>
        <button type="button" id="prompts-reset" class="secondary">Restaurar padrões</button>
      </div>
      <div id="prompts-editors"></div>
      <p class="chat-hint" style="margin-top:16px">Dica: os placeholders disponíveis estão listados abaixo de cada campo. Não remova nem renomeie os placeholders entre chaves.</p>
    </div>
    </div>

    <div class="tab-panel" id="panel-history">
    <div class="card" id="history-section">
      <h3>Histórico</h3>
      <p class="chat-hint">Vídeos processados anteriormente — clique para reabrir sem reprocessar.</p>
      <div id="history-list"></div>
    </div>
    </div>

    <div class="toast" id="toast"></div>
  </div>

  <script>
    const dropzone = document.getElementById("dropzone");
    const fileInput = document.getElementById("file-input");
    const modelSelect = document.getElementById("model");
    const fileName = document.getElementById("file-name");
    const startBtn = document.getElementById("start");
    const resummarizeBtn = document.getElementById("resummarize");
    const statusHint = document.getElementById("status-hint");
    const progress = document.getElementById("progress");
    const logEl = document.getElementById("log");
    const summarySection = document.getElementById("summary-section");
    const summaryEl = document.getElementById("summary");
    const summarySelectWrap = document.getElementById("summary-select-wrap");
    const summaryModelSelect = document.getElementById("summary-model-select");
    const pathsEl = document.getElementById("paths");
    const resetBtn = document.getElementById("reset");
    const chatMessages = document.getElementById("chat-messages");
    const chatForm = document.getElementById("chat-form");
    const chatInput = document.getElementById("chat-input");
    const chatSend = document.getElementById("chat-send");
    const dlTranscricao = document.getElementById("dl-transcricao");
    const dlPromptLlm = document.getElementById("dl-prompt-llm");
    const dlResumo = document.getElementById("dl-resumo");
    const uiVersion = document.getElementById("ui-version");
    const historyList = document.getElementById("history-list");
    const presetSelect = document.getElementById("preset-select");

    let polling = null;
    let selectedFile = null;
    let transcricaoReady = false;
    let currentSummaries = [];

    const BUSY = new Set(["transcribing", "summarizing"]);
    const TRANSCRIPTION_READY = new Set(["transcribed", "summarizing", "done"]);

    function isBusy(status) {
      return BUSY.has(status);
    }

    function setDownloadLink(el, enabled, filename) {
      if (!el) return;
      if (enabled) {
        el.classList.remove("disabled");
        if (filename) el.download = filename;
      } else {
        el.classList.add("disabled");
      }
    }

    function setChatEnabled(enabled) {
      chatInput.disabled = !enabled;
      chatSend.disabled = !enabled;
    }

    function updateStartButton() {
      startBtn.disabled = !selectedFile || !modelSelect.value;
    }

    function resetResults() {
      summaryEl.textContent = "";
      summarySection.style.display = "none";
      summarySelectWrap.style.display = "none";
      if (copySummaryBtn) copySummaryBtn.style.display = "none";
      summaryModelSelect.innerHTML = "";
      currentSummaries = [];
      pathsEl.textContent = "Downloads disponíveis após processar o vídeo.";
      setDownloadLink(dlTranscricao, false);
      setDownloadLink(dlPromptLlm, false);
      setDownloadLink(dlResumo, false);
      setChatEnabled(false);
      chatMessages.innerHTML = "";
      resetBtn.style.display = "none";
      resummarizeBtn.style.display = "none";
      resummarizeBtn.disabled = true;
      statusHint.textContent = "";
      transcricaoReady = false;
    }

    function renderSummaries(summaries, selectedModel) {
      currentSummaries = summaries || [];
      if (!currentSummaries.length) {
        summarySelectWrap.style.display = "none";
        summaryEl.textContent = "";
        summarySection.style.display = "none";
        return;
      }

      summarySection.style.display = "block";
      summarySelectWrap.style.display = currentSummaries.length > 1 ? "block" : "none";
      if (copySummaryBtn) copySummaryBtn.style.display = "inline-flex";
      summaryModelSelect.innerHTML = "";

      currentSummaries.forEach((item, index) => {
        const opt = document.createElement("option");
        opt.value = String(index);
        opt.textContent = item.model + (item.created_at ? " — " + new Date(item.created_at).toLocaleString("pt-BR") : "");
        summaryModelSelect.appendChild(opt);
      });

      let index = currentSummaries.length - 1;
      if (selectedModel) {
        const found = currentSummaries.findIndex((s) => s.model === selectedModel);
        if (found >= 0) index = found;
      }
      summaryModelSelect.value = String(index);
      summaryEl.textContent = currentSummaries[index].text || "(resumo vazio)";
    }

    function renderChat(messages) {
      chatMessages.innerHTML = "";
      (messages || []).forEach((item) => {
        const div = document.createElement("div");
        div.className = "msg " + (item.role === "user" ? "user" : "bot");
        const label = item.role === "user" ? "Você" : "Assistente";
        div.innerHTML = "<strong>" + label + "</strong><p></p>";
        div.querySelector("p").textContent = item.text;
        chatMessages.appendChild(div);
      });
      chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function applyStatus(data) {
      const status = data.status;
      const base = data.download_base || "video";

      if (data.log && data.log.length) {
        progress.style.display = "block";
        logEl.textContent = data.log.join("\\n");
        logEl.scrollTop = logEl.scrollHeight;
      }

      if (TRANSCRIPTION_READY.has(status) && data.transcription) {
        if (!transcricaoReady) {
          transcricaoReady = true;
          setDownloadLink(dlTranscricao, true, base + "_transcricao.txt");
          setDownloadLink(dlPromptLlm, true, base + "_prompt_llm.txt");
          setChatEnabled(true);
        }
        resummarizeBtn.style.display = "inline-block";
        resummarizeBtn.disabled = isBusy(status) || !modelSelect.value;

        if (status === "transcribed") {
          statusHint.textContent = "Transcrição pronta. Gerando resumo…";
          pathsEl.textContent = "Transcrição disponível para download. Resumo em andamento…";
        } else if (status === "summarizing") {
          statusHint.textContent = "Gerando resumo com " + (data.model || "modelo") + "…";
          pathsEl.textContent = "Transcrição disponível. Resumo em andamento…";
        }
      }

      if (status === "done") {
        statusHint.textContent = "";
        pathsEl.textContent = "Arquivos prontos para download.";
        setDownloadLink(dlResumo, true, base + "_resumo.txt");
        renderSummaries(data.summaries || [], data.model);
        renderChat(data.chat || []);
        resetBtn.style.display = "inline-block";
        resummarizeBtn.style.display = "inline-block";
        resummarizeBtn.disabled = !modelSelect.value;
      } else if (status === "error" || (status === "transcribed" && data.error)) {
        statusHint.textContent = "";
        if (data.error) {
          logEl.innerHTML += "\\n<span class='error'>❌ " + data.error + "</span>";
        }
        resummarizeBtn.style.display = "inline-block";
        resummarizeBtn.disabled = !modelSelect.value;
      } else if (status === "transcribing") {
        statusHint.textContent = "Transcrevendo vídeo…";
        startBtn.disabled = true;
        resummarizeBtn.disabled = true;
      } else if (status === "summarizing") {
        startBtn.disabled = true;
        resummarizeBtn.disabled = true;
      }

      if (!isBusy(status)) {
        updateStartButton();
      }
    }

    async function loadPresets() {
      const res = await fetch("/api/presets");
      const data = await res.json();
      if (presetSelect) {
        presetSelect.innerHTML = "";
        (data.presets || []).forEach((p) => {
          const opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.icon + " " + p.label;
          if (p.id === data.active) opt.selected = true;
          presetSelect.appendChild(opt);
        });
      }
      // Also update preset selector in Prompts tab if visible
      const promptsPreset = document.getElementById("prompts-preset-select");
      if (promptsPreset) {
        promptsPreset.innerHTML = "";
        (data.presets || []).forEach((p) => {
          const opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.icon + " " + p.label;
          if (p.id === data.active) opt.selected = true;
          promptsPreset.appendChild(opt);
        });
      }
    }

    async function activatePreset(presetId, source) {
      await fetch("/api/presets/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: presetId }),
      });
      // Sync both selectors
      if (source !== "process" && presetSelect) {
        presetSelect.value = presetId;
      }
      if (source !== "prompts" && promptsPresetSelect) {
        promptsPresetSelect.value = presetId;
      }
      showToast("Tipo de reunião: " + presetId);
      // Reload prompts editor if open
      if (document.getElementById("panel-prompts").classList.contains("active")) {
        loadPromptsEditor();
      }
    }

    if (presetSelect) {
      presetSelect.addEventListener("change", () => {
        activatePreset(presetSelect.value, "process");
      });
    }

    async function loadModels() {
      const res = await fetch("/api/models");
      const data = await res.json();
      modelSelect.innerHTML = "";
      if (!data.models.length) {
        modelSelect.innerHTML = '<option value="">Nenhum modelo — inicie o Ollama</option>';
        updateStartButton();
        return;
      }
      data.models.forEach((m, i) => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        if (i === 0) opt.selected = true;
        modelSelect.appendChild(opt);
      });
      updateStartButton();
    }

    function startPolling() {
      progress.style.display = "block";
      resetResults();
      transcricaoReady = false;
      if (polling) clearInterval(polling);
      polling = setInterval(async () => {
        const res = await fetch("/api/status");
        const data = await res.json();
        applyStatus(data);
        if (data.status === "done" || data.status === "error" || (data.error && !isBusy(data.status))) {
          clearInterval(polling);
          polling = null;
          loadHistory();
        }
      }, 800);
    }

    async function upload(file) {
      if (!modelSelect.value) {
        alert("Selecione um modelo Ollama ou inicie o Ollama primeiro.");
        return;
      }
      fileName.textContent = file.name;
      const form = new FormData();
      form.append("video", file);
      form.append("model", modelSelect.value);
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Falha no envio");
        return;
      }
      startPolling();
    }

    async function loadHistory() {
      const res = await fetch("/api/history");
      const data = await res.json();
      renderHistory(data.entries || []);
    }

    function renderHistory(entries) {
      historyList.innerHTML = "";
      if (!entries.length) {
        historyList.innerHTML = '<p class="chat-hint">Nenhum vídeo processado ainda.</p>';
        return;
      }
      entries.forEach((entry) => {
        const div = document.createElement("div");
        div.className = "history-item";
        const models = (entry.summaries || []).map((s) => s.model).join(", ") || "sem resumo";
        const date = entry.created_at ? new Date(entry.created_at).toLocaleString("pt-BR") : "";
        div.innerHTML =
          "<strong>" + entry.video_name + "</strong>" +
          '<div class="meta">' + date + " — " + models + "</div>" +
          '<div class="history-actions">' +
          '<button type="button" class="secondary" data-open="' + entry.id + '">Abrir</button>' +
          '<button type="button" class="secondary" data-remove="' + entry.id + '">Remover</button>' +
          "</div>";
        historyList.appendChild(div);
      });

      historyList.querySelectorAll("[data-open]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const res = await fetch("/api/history/load", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: btn.dataset.open }),
          });
          const data = await res.json();
          if (!res.ok) {
            alert(data.error || "Falha ao carregar histórico");
            return;
          }
          selectedFile = null;
          fileInput.value = "";
          fileName.textContent = data.video_name || "";
          progress.style.display = "block";
          logEl.textContent = (data.log || []).join("\\n");
          applyStatus(data);
          if (polling) clearInterval(polling);
          polling = null;
        });
      });

      historyList.querySelectorAll("[data-remove]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          if (!confirm("Remover este item do histórico?")) return;
          await fetch("/api/history/remove", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: btn.dataset.remove }),
          });
          loadHistory();
        });
      });
    }

    function onFileSelected(file) {
      selectedFile = file;
      fileName.textContent = file.name;
      updateStartButton();
    }

    dropzone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      if (fileInput.files[0]) onFileSelected(fileInput.files[0]);
    });
    modelSelect.addEventListener("change", () => {
      updateStartButton();
      updateModelInfo();
    });

    ["dragenter", "dragover"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (e) => {
      const file = e.dataTransfer.files[0];
      if (file) onFileSelected(file);
    });

    startBtn.addEventListener("click", () => {
      if (selectedFile) upload(selectedFile);
    });

    resummarizeBtn.addEventListener("click", async () => {
      if (!modelSelect.value) {
        alert("Selecione um modelo.");
        return;
      }
      resummarizeBtn.disabled = true;
      const res = await fetch("/api/resummarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelSelect.value }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(data.error || "Falha ao gerar resumo");
        resummarizeBtn.disabled = false;
        return;
      }
      startPolling();
    });

    summaryModelSelect.addEventListener("change", () => {
      const index = Number(summaryModelSelect.value);
      if (currentSummaries[index]) {
        summaryEl.textContent = currentSummaries[index].text || "(resumo vazio)";
      }
    });

    chatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = chatInput.value.trim();
      if (!question) return;

      chatInput.disabled = true;
      chatSend.disabled = true;
      chatSend.textContent = "Pensando...";

      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, model: modelSelect.value }),
      });
      const data = await res.json();

      chatInput.disabled = false;
      chatSend.disabled = false;
      chatSend.textContent = "Enviar";

      if (!res.ok) {
        alert(data.error || "Falha ao enviar pergunta");
        return;
      }

      renderChat(data.chat || []);
      chatInput.value = "";
      chatInput.focus();
    });

    resetBtn.addEventListener("click", () => {
      selectedFile = null;
      fileInput.value = "";
      fileName.textContent = "";
      logEl.textContent = "";
      progress.style.display = "none";
      resetResults();
      updateStartButton();
      fetch("/api/reset", { method: "POST" });
    });

    fetch("/api/status").then((r) => r.json()).then((data) => {
      if (uiVersion) uiVersion.textContent = "Interface v" + (data.ui_version || "?");
      if (TRANSCRIPTION_READY.has(data.status)) {
        applyStatus(data);
        if (data.video_name) fileName.textContent = data.video_name;
      }
    });

    // ── Tabs ──
    const tabPanels = {
      process: document.getElementById("panel-process"),
      prompts: document.getElementById("panel-prompts"),
      history: document.getElementById("panel-history"),
    };
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        Object.values(tabPanels).forEach((p) => p && p.classList.remove("active"));
        const panel = tabPanels[tab.dataset.tab];
        if (panel) panel.classList.add("active");
        if (tab.dataset.tab === "prompts") loadPromptsEditor();
        if (tab.dataset.tab === "history") loadHistory();
      });
    });

    // ── Toast ──
    let toastTimer = null;
    function showToast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.classList.remove("show"), 2500);
    }

    // ── Copy to clipboard ──
    const copySummaryBtn = document.getElementById("copy-summary");
    if (copySummaryBtn) {
      copySummaryBtn.addEventListener("click", () => {
        const text = summaryEl.textContent || "";
        navigator.clipboard.writeText(text).then(() => {
          copySummaryBtn.classList.add("copied");
          copySummaryBtn.textContent = "✅ Copiado";
          showToast("Resumo copiado para a área de transferência!");
          setTimeout(() => {
            copySummaryBtn.classList.remove("copied");
            copySummaryBtn.textContent = "📋 Copiar";
          }, 2000);
        }).catch(() => showToast("Erro ao copiar"));
      });
    }

    // ── Prompts Editor ──
    const PROMPT_META = {
      chunk_prompt: {
        label: "Prompt de trecho (chunk)",
        hint: "Placeholders: {chunk_text}, {chunk_num}, {total}",
      },
      merge_prompt: {
        label: "Prompt de consolidação (merge)",
        hint: "Placeholders: {combined_notes}",
      },
      single_prompt: {
        label: "Prompt de resumo único (lote único)",
        hint: "Placeholders: {transcription_text}",
      },
      chat_prompt: {
        label: "Prompt de chat (perguntas)",
        hint: "Placeholders: {transcription_text}, {question}",
      },
    };

    async function loadPromptsEditor() {
      const container = document.getElementById("prompts-editors");
      if (!container) return;
      try {
        const res = await fetch("/api/prompts");
        const data = await res.json();
        const activePreset = data.active_preset || "general";
        const presets = data.presets || {};
        const presetOverrides = presets[activePreset] || {};
        const isGeneral = activePreset === "general";

        container.innerHTML = "";
        Object.entries(PROMPT_META).forEach(([key, info]) => {
          const div = document.createElement("div");
          div.className = "prompt-editor";
          const overrideLabel = !isGeneral && presetOverrides[key]
            ? ' <span style="color:#6c8cff;font-weight:400">(personalizado para este preset)</span>'
            : ' <span style="color:#5c6378;font-weight:400">(fallback base)</span>';
          div.innerHTML =
            '<label class="prompt-label">' + info.label + overrideLabel + "</label>" +
            '<textarea data-prompt="' + key + '" rows="10"></textarea>' +
            '<div class="hint">' + info.hint + "</div>";
          container.appendChild(div);
        });
        // Preenche com overrides do preset ou base
        Object.entries(PROMPT_META).forEach(([key]) => {
          const ta = container.querySelector('textarea[data-prompt="' + key + '"]');
          if (!ta) return;
          const value = presetOverrides[key] || data[key] || "";
          ta.value = value;
          // Data attribute para saber de onde veio
          ta.dataset.presetSource = presetOverrides[key] ? "preset" : "base";
        });
      } catch (e) {
        container.innerHTML = '<p class="chat-hint">Erro ao carregar prompts.</p>';
      }
    }

    async function savePrompts() {
      const container = document.getElementById("prompts-editors");
      if (!container) return;

      // Get active preset to know where to save
      let activePreset = "general";
      const presetRes = await fetch("/api/presets");
      const presetData = await presetRes.json();
      activePreset = presetData.active || "general";

      if (activePreset === "general") {
        // Save to top-level prompts
        const payload = {};
        container.querySelectorAll("textarea[data-prompt]").forEach((ta) => {
          payload[ta.dataset.prompt] = ta.value;
        });
        try {
          const res = await fetch("/api/prompts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (res.ok) {
            showToast("Prompts salvos com sucesso!");
          } else {
            const data = await res.json();
            showToast("Erro ao salvar: " + (data.error || "desconhecido"));
          }
        } catch (e) {
          showToast("Erro de conexão ao salvar prompts.");
        }
      } else {
        // Save to preset overrides — need full data first, then merge
        const promptsRes = await fetch("/api/prompts");
        const fullData = await promptsRes.json();
        const presets = fullData.presets || {};
        const override = {};
        container.querySelectorAll("textarea[data-prompt]").forEach((ta) => {
          override[ta.dataset.prompt] = ta.value;
        });
        presets[activePreset] = { ...presets[activePreset], ...override };
        try {
          const res = await fetch("/api/prompts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ presets: presets }),
          });
          if (res.ok) {
            showToast("Prompts do preset salvos com sucesso!");
          } else {
            const data = await res.json();
            showToast("Erro ao salvar: " + (data.error || "desconhecido"));
          }
        } catch (e) {
          showToast("Erro de conexão ao salvar prompts.");
        }
      }
    }

    async function resetPrompts() {
      if (!confirm("Restaurar todos os prompts para os valores padrão? Esta ação não pode ser desfeita.")) return;
      try {
        const res = await fetch("/api/prompts/reset", { method: "POST" });
        if (res.ok) {
          const data = await res.json();
          const container = document.getElementById("prompts-editors");
          if (container) {
            Object.entries(data).forEach(([key, value]) => {
              if (key === "_meta") return;
              const ta = container.querySelector('textarea[data-prompt="' + key + '"]');
              if (ta) ta.value = value;
            });
          }
          showToast("Prompts restaurados para os padrões!");
        } else {
          showToast("Erro ao restaurar prompts.");
        }
      } catch (e) {
        showToast("Erro de conexão ao restaurar prompts.");
      }
    }

    document.getElementById("prompts-save").addEventListener("click", savePrompts);
    document.getElementById("prompts-reset").addEventListener("click", resetPrompts);

    const promptsPresetSelect = document.getElementById("prompts-preset-select");
    if (promptsPresetSelect) {
      promptsPresetSelect.addEventListener("change", () => {
        activatePreset(promptsPresetSelect.value, "prompts");
      });
    }

    async function loadModelTiers() {
      const res = await fetch("/api/model-tiers");
      const data = await res.json();
      const container = document.getElementById("model-tiers");
      if (!container || !data.tiers) return;
      container.innerHTML = data.tiers.map((t) =>
        '<div class="tier-row">' +
        '<span class="tier-icon">' + t.icon + '</span>' +
        '<div>' +
        '<span class="tier-label">' + t.label + '</span><br>' +
        '<span>' + t.description + '</span><br>' +
        '<span class="tier-models">Modelos: ' + (t.recommended || []).join(', ') + '</span>' +
        '</div></div>'
      ).join('');
    }

    async function updateModelInfo() {
      const hint = document.getElementById("model-info-hint");
      if (!hint || !modelSelect.value) return;
      const res = await fetch("/api/model-info?model=" + encodeURIComponent(modelSelect.value));
      const data = await res.json();
      if (data.config) {
        const cfg = data.config;
        hint.innerHTML =
          '<span class="model-config-badge">chunk: ' + cfg.chunk_chars + ' chars</span>' +
          '<span class="model-config-badge">merge batch: ' + cfg.merge_batch + '</span>' +
          '<span class="model-config-badge">contexto: ' + cfg.num_ctx + '</span>' +
          '<span class="model-config-badge">timeout: ' + cfg.timeout + 's</span>' +
          (data.tier ? ' <span style="color:#5c6378">(' + data.tier.icon + ' ' + data.tier.label + ')</span>' : '');
      }
    }

    loadModels();
    loadPresets();
    loadModelTiers();
    loadHistory();
    // Update model info after models are loaded
    setTimeout(() => { if (modelSelect.value) updateModelInfo(); }, 300);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text_download(self, content, filename):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _handle_download(self, kind):
        with job_lock:
            status = job["status"]
            if kind == "transcricao":
                if status not in TRANSCRIPTION_READY_STATUSES:
                    self._json(400, {"error": "Transcrição ainda não disponível"})
                    return
                path = job.get("transcricao_path", "")
                content = job.get("transcription", "")
                base = job.get("download_base") or "video"
                filename = f"{base}_transcricao.txt"
            elif kind == "prompt-llm":
                if status not in TRANSCRIPTION_READY_STATUSES:
                    self._json(400, {"error": "Transcrição ainda não disponível"})
                    return
                content = job.get("transcription", "")
                if not content:
                    self._json(404, {"error": "Transcrição não encontrada"})
                    return
                content = build_prompt_llm_content(content)
                base = job.get("download_base") or "video"
                filename = f"{base}_prompt_llm.txt"
                path = ""
            else:
                if status != "done" or not job.get("summary"):
                    self._json(400, {"error": "Resumo ainda não disponível"})
                    return
                path = job.get("resumo_path", "")
                content = job.get("summary", "")
                base = job.get("download_base") or "video"
                filename = f"{base}_resumo.txt"

        if kind != "prompt-llm" and path and safe_upload_path(path) and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                content = f.read()

        if not content:
            self._json(404, {"error": "Arquivo não encontrado"})
            return

        self._send_text_download(content, filename)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/models":
            self._json(200, {"models": ollama_models()})
            return

        if path == "/api/status":
            with job_lock:
                payload = dict(job)
            payload["ui_version"] = UI_VERSION
            self._json(200, payload)
            return

        if path == "/api/history":
            entries = [history_entry_for_api(e) for e in load_history()]
            self._json(200, {"entries": entries})
            return

        if path == "/api/download/transcricao":
            self._handle_download("transcricao")
            return

        if path == "/api/download/prompt-llm":
            self._handle_download("prompt-llm")
            return

        if path == "/api/download/resumo":
            self._handle_download("resumo")
            return

        if path == "/api/prompts":
            data = prompts.get_prompts_meta()
            self._json(200, data)
            return

        if path == "/api/presets":
            presets_list = prompts.get_presets()
            active = prompts.get_active_preset()
            self._json(200, {"presets": presets_list, "active": active})
            return

        if path == "/api/model-tiers":
            tiers = model_config.get_hardware_tiers()
            self._json(200, {"tiers": tiers})
            return

        if path == "/api/model-info":
            model = urlparse(self.path).query
            # Parse ?model=...
            from urllib.parse import parse_qs
            params = parse_qs(urlparse(self.path).query)
            model_name = params.get("model", [""])[0]
            if model_name:
                tier, config = model_config.get_model_info(model_name)
                self._json(200, {"tier": tier, "config": config})
            else:
                self._json(400, {"error": "Parametro model obrigatorio"})
            return

        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/reset":
            with job_lock:
                job["status"] = "idle"
                job["log"].clear()
                job["summary"] = ""
                job["transcription"] = ""
                job["transcricao_path"] = ""
                job["resumo_path"] = ""
                job["video_path"] = ""
                job["download_base"] = ""
                job["video_name"] = ""
                job["model"] = ""
                job["summaries"] = []
                job["history_id"] = ""
                job["chat"] = []
                job["chat_busy"] = False
                job["error"] = ""
            self._json(200, {"ok": True})
            return

        if path == "/api/history/load":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            history_id = (data.get("id") or "").strip()
            if not history_id:
                self._json(400, {"error": "ID obrigatório"})
                return
            ok, err = restore_job_from_history(history_id)
            if not ok:
                self._json(404, {"error": err})
                return
            with job_lock:
                payload = dict(job)
            payload["ui_version"] = UI_VERSION
            self._json(200, payload)
            return

        if path == "/api/history/remove":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            history_id = (data.get("id") or "").strip()
            if not history_id:
                self._json(400, {"error": "ID obrigatório"})
                return
            remove_history_entry(history_id)
            self._json(200, {"ok": True})
            return

        if path == "/api/resummarize":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            model = (data.get("model") or "").strip()
            if not model:
                self._json(400, {"error": "Modelo obrigatório"})
                return

            with job_lock:
                if job["status"] in BUSY_STATUSES:
                    self._json(409, {"error": "Já há um processamento em andamento"})
                    return
                if job["status"] not in TRANSCRIPTION_READY_STATUSES or not job.get("transcription"):
                    self._json(400, {"error": "Transcrição não disponível para resumir"})
                    return

            threading.Thread(target=run_summarize_job, args=(model,), daemon=True).start()
            self._json(200, {"ok": True})
            return

        if path == "/api/chat":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return

            question = (data.get("question") or "").strip()
            if not question:
                self._json(400, {"error": "Pergunta vazia"})
                return

            with job_lock:
                if job["status"] not in TRANSCRIPTION_READY_STATUSES:
                    self._json(400, {"error": "Processe um vídeo antes de perguntar"})
                    return
                if not job["transcription"]:
                    self._json(400, {"error": "Transcrição não disponível"})
                    return
                if job["chat_busy"]:
                    self._json(409, {"error": "Aguarde a resposta anterior"})
                    return
                transcription = job["transcription"]
                model = job["model"] or (data.get("model") or "").strip()
                if not model:
                    self._json(400, {"error": "Nenhum modelo disponível"})
                    return
                job["chat_busy"] = True

            try:
                answer = ask_transcription(transcription, question, model)
            except Exception as exc:
                with job_lock:
                    job["chat_busy"] = False
                self._json(500, {"error": str(exc)})
                return

            with job_lock:
                job["chat"].append({"role": "user", "text": question})
                job["chat"].append({"role": "bot", "text": answer})
                job["chat_busy"] = False
                chat = list(job["chat"])

            self._json(200, {"answer": answer, "chat": chat})
            return

        if path == "/api/presets/activate":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            preset_id = (data.get("preset") or "").strip()
            if not preset_id:
                self._json(400, {"error": "Preset obrigatório"})
                return
            try:
                prompts.set_active_preset(preset_id)
                self._json(200, {"ok": True, "active": preset_id})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if path == "/api/prompts/reset":
            try:
                prompts.reset_to_defaults()
                data = prompts.get_prompts_meta()
                self._json(200, data)
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return

        if path == "/api/prompts":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            # Remove _meta do payload — preservamos o existente
            data.pop("_meta", None)
            try:
                prompts.save_prompts(data)
                self._json(200, {"ok": True})
            except Exception as exc:
                self._json(500, {"error": str(exc)})
            return

        if path == "/api/upload":
            with job_lock:
                if job["status"] in BUSY_STATUSES:
                    self._json(409, {"error": "Já há um vídeo sendo processado"})
                    return

            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                self._json(400, {"error": "Envie multipart/form-data"})
                return

            length = int(self.headers.get("Content-Length", 0))
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": ctype,
                    "CONTENT_LENGTH": str(length),
                },
            )

            if "video" not in form or not form["video"].filename:
                self._json(400, {"error": "Arquivo de vídeo obrigatório"})
                return

            model = form.getvalue("model", "")
            if not model:
                self._json(400, {"error": "Modelo obrigatório"})
                return

            upload = form["video"]
            original = os.path.basename(upload.filename)
            ext = os.path.splitext(original)[1] or ".mp4"
            download_base = os.path.splitext(original)[0] or "video"
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            dest = os.path.join(UPLOAD_DIR, f"upload{ext}")

            with open(dest, "wb") as f:
                f.write(upload.file.read())

            threading.Thread(
                target=run_full_job,
                args=(dest, model, download_base, original),
                daemon=True,
            ).start()
            self._json(200, {"ok": True})
            return

        self.send_error(404)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    url = f"http://127.0.0.1:{PORT}"
    print(f"\nInterface v{UI_VERSION} aberta em {url}")
    print(f"Arquivo: {os.path.abspath(__file__)}")
    print("   Escolha o modelo, selecione o video e clique em Iniciar.\n")
    webbrowser.open(url)
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
