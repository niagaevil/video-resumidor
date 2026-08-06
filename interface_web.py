"""Interface web local com arrastar-e-soltar para o Video Resumidor."""

import cgi
import json
import os
import re
import threading
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

import hardware_detect
import model_config
import prompts

PORT = int(os.environ.get("VIDEO_RESUMIDOR_PORT", "8765"))
UI_VERSION = 8
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
    "compare": [],         # resultados de comparacao multi-modelo
    "compare_progress": "", # status atual da comparacao
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


def ollama_models_detail():
    """Lista modelos com detalhes (nome, tamanho, data)."""
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
        bad = ("embed", "vision", "clip")
        result = []
        for m in models:
            name = m.get("name", "")
            if any(b in name for b in bad):
                continue
            size_bytes = m.get("size", 0)
            if size_bytes >= 1_000_000_000:
                size_str = f"{size_bytes / 1_000_000_000:.1f} GB"
            elif size_bytes >= 1_000_000:
                size_str = f"{size_bytes / 1_000_000:.0f} MB"
            else:
                size_str = f"{size_bytes / 1_000:.0f} KB"
            result.append({
                "name": name,
                "size_bytes": size_bytes,
                "size": size_str,
                "modified_at": m.get("modified_at", ""),
            })
        return result
    except Exception:
        return []


def ollama_delete_model(model_name):
    """Deleta um modelo do Ollama."""
    try:
        r = requests.delete(
            "http://localhost:11434/api/delete",
            json={"model": model_name},
            timeout=30,
        )
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, str(e)


def ollama_pull_model(model_name):
    """Inicia pull de um modelo (roda em thread separada)."""
    import threading as thr
    def _pull():
        try:
            r = requests.post(
                "http://localhost:11434/api/pull",
                json={"model": model_name, "stream": False},
                timeout=1800,
            )
            r.raise_for_status()
        except Exception:
            pass
    thr.Thread(target=_pull, daemon=True).start()
    return True


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


def run_compare_job(models):
    """Roda summarization em varios modelos sequencialmente."""
    with job_lock:
        if not job.get("transcription"):
            job["compare_progress"] = "Erro: transcricao nao disponivel"
            return
        transcription = job["transcription"]
        video_path = job.get("video_path") or ""
        download_base = job.get("download_base", "video")
        job["compare"] = []
        job["compare_progress"] = f"0/{len(models)}"

    for idx, model in enumerate(models):
        with job_lock:
            job["compare_progress"] = f"{idx}/{len(models)} — {model}"
        try:
            with capture_progress_log() as vr:
                vr.OLLAMA_URL = vr.resolve_ollama_url("local")
                base = video_path or os.path.join(UPLOAD_DIR, download_base)
                summary, _, _ = vr.summarize_transcription(transcription, model, base)
            with job_lock:
                job["compare"].append({
                    "model": model,
                    "summary": summary,
                    "error": None,
                })
        except Exception as exc:
            with job_lock:
                job["compare"].append({
                    "model": model,
                    "summary": "",
                    "error": str(exc),
                })

    with job_lock:
        done = len(job["compare"])
        errors = sum(1 for c in job["compare"] if c["error"])
        job["compare_progress"] = f"Concluido: {done - errors}/{done} com sucesso"


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
    .layout { width: 100%; max-width: 1400px; }
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
    .restore-field-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 26px;
      height: 26px;
      border-radius: 50%;
      background: transparent;
      border: 1px solid #3d4258;
      color: #9aa0b4;
      font-size: .9rem;
      cursor: pointer;
      transition: background .2s, color .2s, border-color .2s;
      margin-left: 4px;
      flex-shrink: 0;
      line-height: 1;
      padding: 0;
    }
    .restore-field-btn:hover { background: #2a3148; color: #7ddea2; border-color: #7ddea2; }
    .restore-field-btn.restored { color: #7ddea2; border-color: #7ddea2; }
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
    .model-item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      background: #12151f;
      border: 1px solid #2d3142;
      border-radius: 10px;
      margin-bottom: 8px;
    }
    .model-item .model-icon { font-size: 1.3rem; flex-shrink: 0; }
    .model-item .model-info { flex: 1; min-width: 0; }
    .model-item .model-name { color: #e8eaed; font-weight: 600; }
    .model-item .model-meta { color: #9aa0b4; font-size: .8rem; margin-top: 2px; }
    .model-item .model-delete {
      padding: 6px 12px;
      border-radius: 6px;
      background: transparent;
      border: 1px solid #5c2a2a;
      color: #ff7b7b;
      cursor: pointer;
      font-size: .8rem;
      font-weight: 600;
      flex-shrink: 0;
      transition: background .2s;
    }
    .compare-card {
      background: #0a0c12;
      border-radius: 10px;
      padding: 16px;
      border: 1px solid #2d3142;
    }
    .compare-card.error { border-color: #5c2a2a; }
    .compare-card strong { display: block; margin-bottom: 10px; font-size: .95rem; }
    .compare-card pre {
      white-space: pre-wrap;
      font-size: .78rem;
      line-height: 1.5;
      color: #b8c0d4;
      max-height: 550px;
      overflow-y: auto;
      margin: 0;
      font-family: inherit;
    }
  </style>
</head>
<body>
  <div class="layout">
    <div class="tabs">
      <div class="tab active" data-tab="process">🎬 Processar</div>
      <div class="tab" data-tab="history">📋 Histórico</div>
      <div class="tab" data-tab="models">🧠 Modelos</div>
      <div class="tab" data-tab="prompts">📝 Prompts</div>
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
        <button type="button" id="compare-btn" class="secondary" style="display:none" disabled>
          🔬 Comparar modelos
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

      <div id="compare-section" style="display:none;margin-top:24px">
        <h3>🔬 Comparação de modelos</h3>
        <div id="compare-progress" class="chat-hint" style="color:#7ddea2;min-height:1.2em"></div>
        <div id="compare-grid" style="display:grid;grid-template-columns:repeat(2,1fr);gap:16px;padding-bottom:8px"></div>
      </div>

      <div id="compare-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center">
        <div style="background:#1a1d27;border:1px solid #3d4258;border-radius:16px;padding:28px;max-width:500px;width:90%">
          <h3 style="margin:0 0 8px">Selecionar modelos para comparar</h3>
          <p class="chat-hint">Escolha 2 ou mais modelos. Cada um gerará um resumo da mesma transcrição.</p>
          <div id="compare-model-list" style="max-height:260px;overflow-y:auto;margin:12px 0"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button type="button" class="secondary" id="compare-cancel">Cancelar</button>
            <button type="button" id="compare-start">Iniciar comparação</button>
          </div>
        </div>
      </div>

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
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:16px">
        <select id="prompts-preset-select" style="margin-bottom:0;flex:1"></select>
        <button type="button" id="prompts-add" class="secondary" style="white-space:nowrap;font-size:.85rem;margin-bottom:0;padding:8px 14px">+ Novo tipo</button>
        <button type="button" id="prompts-remove" class="secondary" style="display:none;white-space:nowrap;font-size:.85rem;margin-bottom:0;padding:8px 14px;border-color:#5c2a2a;color:#ff7b7b">🗑️ Remover</button>
      </div>
      <div class="prompts-actions">
        <button type="button" id="prompts-save" class="secondary">Salvar alterações</button>
        <button type="button" id="prompts-reset" class="secondary">Restaurar padrões</button>
      </div>
      <div id="prompts-editors"></div>
      <p class="chat-hint" style="margin-top:16px">Dica: os placeholders disponíveis estão listados abaixo de cada campo. Não remova nem renomeie os placeholders entre chaves.</p>
    </div>
    </div>

    <div class="tab-panel" id="panel-models">
    <div class="card" id="models-section">
      <h1>🧠 Gerenciar Modelos</h1>
      <p class="sub">Modelos instalados no Ollama. Baixe novos ou remova os que não usa mais.</p>

      <div id="models-list"></div>

      <div style="margin-top:24px;padding-top:20px;border-top:1px solid #2d3142">
        <h3>Baixar novo modelo</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <input id="pull-model-input" type="text" placeholder="Ex.: qwen2.5:7b" style="flex:1;min-width:200px;padding:10px 12px;border-radius:8px;border:1px solid #3d4258;background:#12151f;color:#e8eaed">
          <button type="button" id="pull-model-btn">Baixar</button>
        </div>
        <p class="chat-hint" style="margin-top:8px">Digite o nome exato do modelo (ex: qwen2.5:7b, llama3.2:3b, mistral:7b). O download roda em segundo plano.</p>
        <div id="pull-status" class="chat-hint" style="color:#7ddea2;min-height:1.2em;margin-top:4px"></div>
      </div>
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
    const compareBtn2 = document.getElementById("compare-btn");
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
      if (compareBtn2) compareBtn2.style.display = "none";
      if (compareSection) compareSection.style.display = "none";
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
        if (compareBtn2) {
          compareBtn2.style.display = "inline-block";
          compareBtn2.disabled = isBusy(status);
        }

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
        if (compareBtn2) { compareBtn2.style.display = "inline-block"; compareBtn2.disabled = false; }
      } else if (status === "error" || (status === "transcribed" && data.error)) {
        statusHint.textContent = "";
        if (data.error) {
          logEl.innerHTML += "\\n<span class='error'>❌ " + data.error + "</span>";
        }
        resummarizeBtn.style.display = "inline-block";
        resummarizeBtn.disabled = !modelSelect.value;
        if (compareBtn2) { compareBtn2.style.display = "inline-block"; compareBtn2.disabled = false; }
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
      window._presetsData = data.presets || [];  // cache for add/remove
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
        updateRemoveButton(data.active, data.presets);
      }
    }

    function updateRemoveButton(activeId, presetsData) {
      const removeBtn = document.getElementById("prompts-remove");
      if (!removeBtn) return;
      const info = (presetsData || window._presetsData || []).find((p) => p.id === activeId);
      if (info && !info.builtin) {
        removeBtn.style.display = "inline-block";
        removeBtn.textContent = "🗑️ Remover \"" + info.label + "\"";
      } else {
        removeBtn.style.display = "none";
      }
    }

    async function activatePreset(presetId, source, silent) {
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
      if (!silent) showToast("Tipo de reunião: " + presetId);
      updateRemoveButton(presetId);
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
          // Switch to Processar tab so user sees the results
          const processTab = document.querySelector('.tab[data-tab="process"]');
          if (processTab) processTab.click();
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
      models: document.getElementById("panel-models"),
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
        if (tab.dataset.tab === "models") loadModelsTab();
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
        const presetDefaults = (data.preset_defaults || {})[activePreset] || {};
        const presetOverrides = presets[activePreset] || {};
        const overrideKeys = Object.keys(presetOverrides).filter((k) => PROMPT_META[k]);
        const isGeneral = activePreset === "general";

        container.innerHTML = "";

        // Aviso: preset ainda sem personalização
        if (!isGeneral && overrideKeys.length === 0) {
          const banner = document.createElement("div");
          banner.style.cssText = "padding:10px 14px;border-radius:8px;background:#161b2e;border:1px solid #2d3142;color:#9aa0b4;font-size:.85rem;margin-bottom:16px;line-height:1.5";
          banner.innerHTML = "ℹ️ Este tipo de reunião ainda usa os prompts <strong>padrão</strong> (diferentes de outros tipos). Edite abaixo e clique em salvar para personalizar <strong>apenas este tipo</strong>.";
          container.appendChild(banner);
        }

        Object.entries(PROMPT_META).forEach(([key, info]) => {
          const div = document.createElement("div");
          div.className = "prompt-editor";
          let badge;
          if (presetOverrides[key]) {
            badge = ' <span style="color:#6c8cff;font-weight:400">(personalizado para este preset)</span>';
          } else if (presetDefaults[key]) {
            badge = ' <span style="color:#7ddea2;font-weight:400">(padrão deste tipo de reunião)</span>';
          } else {
            badge = ' <span style="color:#5c6378;font-weight:400">(base geral)</span>';
          }
          const innerLabel = '<span>' + info.label + '</span>' + badge;
          div.innerHTML =
            '<label class="prompt-label">' + innerLabel +
            '<button type="button" class="restore-field-btn" title="Restaurar padrão" data-prompt="' + key + '">↺</button></label>' +
            '<textarea data-prompt="' + key + '" rows="10"></textarea>' +
            '<div class="hint">' + info.hint + "</div>";
          container.appendChild(div);
        });
        // Preenche com override do preset, padrão do tipo ou base geral
        Object.entries(PROMPT_META).forEach(([key]) => {
          const ta = container.querySelector('textarea[data-prompt="' + key + '"]');
          if (!ta) return;
          const value = presetOverrides[key] || presetDefaults[key] || data[key] || "";
          ta.value = value;
          // Valor carregado — usado para detectar o que foi alterado
          ta.dataset.defaultValue = value;
          // Valor padrão sem override — usado para remover personalizações
          ta.dataset.baseValue = presetDefaults[key] || data[key] || "";
          ta.dataset.presetSource = presetOverrides[key] ? "preset" : (presetDefaults[key] ? "default" : "base");

          // Botão ↺ restaurar padrão
          const restoreBtn = container.querySelector('.restore-field-btn[data-prompt="' + key + '"]');
          if (restoreBtn) {
            restoreBtn.addEventListener("click", () => {
              ta.value = ta.dataset.baseValue || "";
              ta.dataset.defaultValue = ta.value;
              ta.dataset.presetSource = (presetDefaults[key] ? "default" : "base");
              // Feedback visual
              restoreBtn.classList.add("restored");
              setTimeout(() => restoreBtn.classList.remove("restored"), 1200);
            });
          }
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
        // Save to top-level prompts (base geral)
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
            loadPromptsEditor();
          } else {
            const data = await res.json();
            showToast("Erro ao salvar: " + (data.error || "desconhecido"));
          }
        } catch (e) {
          showToast("Erro de conexão ao salvar prompts.");
        }
      } else {
        // Save to preset overrides — salva apenas os campos alterados,
        // para não congelar todos os prompts do preset de uma vez
        const promptsRes = await fetch("/api/prompts");
        const fullData = await promptsRes.json();
        const presets = fullData.presets || {};
        const override = {};
        const removals = [];
        container.querySelectorAll("textarea[data-prompt]").forEach((ta) => {
          const key = ta.dataset.prompt;
          const val = ta.value;
          const loaded = ta.dataset.defaultValue || "";
          const base = ta.dataset.baseValue || "";
          if (val === loaded) return; // nada mudou
          if (ta.dataset.presetSource === "preset" && val === base) {
            removals.push(key); // voltou ao padrão → remove a personalização
          } else {
            override[key] = val;
          }
        });
        if (!Object.keys(override).length && !removals.length) {
          showToast("Nenhuma alteração para salvar neste tipo de reunião.");
          return;
        }
        const presetObj = { ...(presets[activePreset] || {}) };
        removals.forEach((k) => { delete presetObj[k]; });
        presets[activePreset] = { ...presetObj, ...override };
        try {
          const res = await fetch("/api/prompts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ presets: presets }),
          });
          if (res.ok) {
            showToast("Prompts do preset salvos com sucesso!");
            loadPromptsEditor();
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
          showToast("Prompts restaurados para os padrões!");
          loadPromptsEditor();
        } else {
          showToast("Erro ao restaurar prompts.");
        }
      } catch (e) {
        showToast("Erro de conexão ao restaurar prompts.");
      }
    }

    document.getElementById("prompts-save").addEventListener("click", savePrompts);
    document.getElementById("prompts-reset").addEventListener("click", resetPrompts);
    document.getElementById("prompts-add").addEventListener("click", addPreset);
    document.getElementById("prompts-remove").addEventListener("click", removeCurrentPreset);

    const promptsPresetSelect = document.getElementById("prompts-preset-select");
    if (promptsPresetSelect) {
      promptsPresetSelect.addEventListener("change", () => {
        activatePreset(promptsPresetSelect.value, "prompts");
      });
    }

    async function addPreset() {
      const label = prompt("Nome do novo tipo de reunião:", "");
      if (!label || !label.trim()) return;
      const icon = (prompt("Ícone (emoji):", "📝") || "📝").trim();
      if (!icon) return;
      try {
        const res = await fetch("/api/presets/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label: label.trim(), icon: icon }),
        });
        const data = await res.json();
        if (res.ok) {
          window._presetsData = data.presets || [];
          await loadPresets();
          // Ativa o novo preset (silencioso para não duplicar toast)
          if (data.id) {
            activatePreset(data.id, "prompts", true);
            showToast("Novo tipo criado: \"" + label.trim() + "\"!");
          }
        } else {
          showToast("Erro ao criar tipo: " + (data.error || "desconhecido"));
        }
      } catch (e) {
        showToast("Erro de conexão ao criar tipo de reunião.");
      }
    }

    async function removeCurrentPreset() {
      const active = promptsPresetSelect ? promptsPresetSelect.value : "";
      if (!active) return;
      const info = (window._presetsData || []).find((p) => p.id === active);
      if (!info || info.builtin) {
        showToast("Este tipo de reunião é padrão e não pode ser removido.");
        return;
      }
      if (!confirm("Remover o tipo \"" + info.label + "\"? Seus prompts personalizados serão perdidos.")) return;
      try {
        const res = await fetch("/api/presets/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: active }),
        });
        const data = await res.json();
        if (res.ok) {
          window._presetsData = data.presets || [];
          await loadPresets();
          updateRemoveButton(data.active, data.presets);
          loadPromptsEditor();
          showToast("Tipo \"" + info.label + "\" removido.");
        } else {
          showToast("Erro ao remover tipo: " + (data.error || "desconhecido"));
        }
      } catch (e) {
        showToast("Erro de conexão ao remover tipo de reunião.");
      }
    }

    async function loadModelTiers() {
      // Detect hardware first
      let hw = null;
      try {
        const hwRes = await fetch("/api/hardware");
        hw = await hwRes.json();
      } catch (e) { /* silent */ }

      const res = await fetch("/api/model-tiers");
      const data = await res.json();
      const container = document.getElementById("model-tiers");
      if (!container || !data.tiers) return;

      const detectedTier = hw ? hw.tier : null;
      const ramInfo = hw && hw.ram_gb ? hw.ram_gb + ' GB RAM' : '';
      const gpuInfo = hw && hw.gpus && hw.gpus.length
        ? hw.gpus[0].name + (hw.total_vram_gb ? ' (' + hw.total_vram_gb + ' GB VRAM)' : '')
        : 'GPU nao detectada';

      // Header com info do hardware
      let html = '';
      if (hw && detectedTier) {
        html += '<div style="margin-bottom:10px;padding:8px 12px;background:#12151f;border-radius:8px;display:flex;align-items:center;gap:8px">' +
          '<span style="font-size:1.2rem">🖥️</span>' +
          '<span style="color:#c8cde0"><strong>Seu PC:</strong> ' + ramInfo +
          (hw.gpus && hw.gpus.length ? ' • ' + gpuInfo : '') +
          '</span></div>';
      }

      html += data.tiers.map((t) => {
        const isMe = t.id === detectedTier;
        const badge = isMe ? ' <span style="background:#4f6ef7;color:#fff;padding:1px 8px;border-radius:10px;font-size:.7rem;font-weight:600">✅ SEU PC</span>' : '';
        const borderStyle = isMe ? 'border:1px solid #4f6ef7;border-radius:8px;padding:8px;margin:-8px;background:#161b2e' : '';
        return '<div class="tier-row" style="' + borderStyle + '">' +
          '<span class="tier-icon">' + t.icon + '</span>' +
          '<div>' +
          '<span class="tier-label">' + t.label + badge + '</span><br>' +
          '<span>' + t.description + '</span><br>' +
          '<span class="tier-models">Modelos: ' + (t.recommended || []).join(', ') + '</span>' +
          '</div></div>';
      }).join('');
      container.innerHTML = html;
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

    // ── Models Tab ──
    async function loadModelsTab() {
      const container = document.getElementById("models-list");
      if (!container) return;
      try {
        const res = await fetch("/api/models/detail");
        const data = await res.json();
        const models = data.models || [];
        if (!models.length) {
          container.innerHTML = '<p class="chat-hint">Nenhum modelo instalado. Use o campo abaixo para baixar.</p>';
          return;
        }
        container.innerHTML = models.map((m) =>
          '<div class="model-item">' +
          '<span class="model-icon">🧠</span>' +
          '<div class="model-info">' +
          '<div class="model-name">' + m.name + '</div>' +
          '<div class="model-meta">' + m.size +
          (m.modified_at ? ' • ' + new Date(m.modified_at).toLocaleString("pt-BR") : '') +
          '</div></div>' +
          '<button class="model-delete" data-model="' + m.name + '">Remover</button>' +
          '</div>'
        ).join('');

        container.querySelectorAll(".model-delete").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const model = btn.dataset.model;
            if (!confirm("Remover " + model + "? Esta ação não pode ser desfeita.")) return;
            btn.disabled = true;
            btn.textContent = "...";
            const delRes = await fetch("/api/models/delete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ model: model }),
            });
            if (delRes.ok) {
              showToast("Modelo " + model + " removido!");
              loadModelsTab();
              loadModels(); // refresh the main model selector too
            } else {
              const err = await delRes.json();
              showToast("Erro: " + (err.error || "falha ao remover"));
              btn.disabled = false;
              btn.textContent = "Remover";
            }
          });
        });
      } catch (e) {
        container.innerHTML = '<p class="chat-hint">Erro ao carregar modelos. Ollama está rodando?</p>';
      }
    }

    document.getElementById("pull-model-btn").addEventListener("click", async () => {
      const input = document.getElementById("pull-model-input");
      const status = document.getElementById("pull-status");
      const model = input.value.trim();
      if (!model) {
        showToast("Digite o nome do modelo.");
        return;
      }
      const btn = document.getElementById("pull-model-btn");
      btn.disabled = true;
      btn.textContent = "Baixando...";
      status.textContent = "Iniciando download de " + model + "...";
      const res = await fetch("/api/models/pull", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: model }),
      });
      if (res.ok) {
        showToast("Baixando " + model + " em segundo plano. Atualize a lista em alguns minutos.");
        status.textContent = "✅ Download de " + model + " iniciado! Atualize a lista para ver o progresso.";
        input.value = "";
      } else {
        const err = await res.json();
        status.textContent = "❌ " + (err.error || "Falha ao iniciar download");
        showToast("Erro ao baixar modelo.");
      }
      btn.disabled = false;
      btn.textContent = "Baixar";
    });

    // ── Compare Models ──
    const compareBtn = document.getElementById("compare-btn");
    const compareModal = document.getElementById("compare-modal");
    const compareSection = document.getElementById("compare-section");
    const compareGrid = document.getElementById("compare-grid");
    const compareProgress = document.getElementById("compare-progress");
    let comparePolling = null;

    compareBtn.addEventListener("click", async () => {
      // Populate modal with installed models
      const list = document.getElementById("compare-model-list");
      const res = await fetch("/api/models");
      const data = await res.json();
      list.innerHTML = (data.models || []).map((m) =>
        '<label style="display:flex;align-items:center;gap:8px;padding:6px 0;cursor:pointer;color:#c8cde0">' +
        '<input type="checkbox" value="' + m + '" class="compare-check"> ' + m +
        '</label>'
      ).join('');
      compareModal.style.display = "flex";
    });

    document.getElementById("compare-cancel").addEventListener("click", () => {
      compareModal.style.display = "none";
    });

    document.getElementById("compare-start").addEventListener("click", async () => {
      const checks = document.querySelectorAll(".compare-check:checked");
      const models = Array.from(checks).map((c) => c.value);
      if (models.length < 2) {
        showToast("Selecione pelo menos 2 modelos.");
        return;
      }
      compareModal.style.display = "none";
      compareSection.style.display = "block";
      compareGrid.innerHTML = "";
      compareProgress.textContent = "Iniciando...";

      const res = await fetch("/api/compare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: models }),
      });
      if (!res.ok) {
        const err = await res.json();
        showToast(err.error || "Falha ao iniciar");
        return;
      }

      // Poll status for compare progress
      if (comparePolling) clearInterval(comparePolling);
      comparePolling = setInterval(async () => {
        const s = await fetch("/api/status");
        const d = await s.json();
        const items = d.compare || [];
        const prog = d.compare_progress || "";
        compareProgress.textContent = prog;

        // Render grid
        compareGrid.innerHTML = items.map((item) => {
          if (item.error) {
            return '<div class="compare-card error">' +
              '<strong style="color:#ff7b7b">❌ ' + item.model + '</strong>' +
              '<p style="color:#ff7b7b;font-size:.85rem">Erro: ' + item.error + '</p></div>';
          }
          return '<div class="compare-card">' +
            '<strong style="color:#6c8cff">🧠 ' + item.model + '</strong>' +
            '<pre>' + (item.summary || "(aguardando...)") + '</pre></div>';
        }).join('');

        if (prog.indexOf("Concluido") === 0) {
          clearInterval(comparePolling);
          comparePolling = null;
        }
      }, 1000);
    });

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
            data["preset_defaults"] = prompts.get_all_preset_defaults()
            self._json(200, data)
            return

        if path == "/api/presets":
            presets_list = prompts.get_presets()
            active = prompts.get_active_preset()
            self._json(200, {"presets": presets_list, "active": active})
            return

        if path == "/api/models/detail":
            self._json(200, {"models": ollama_models_detail()})
            return

        if path == "/api/hardware":
            try:
                hw = hardware_detect.detect_tier()
                self._json(200, hw)
            except Exception as exc:
                self._json(200, {"tier": "unknown", "error": str(exc)})
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
                job["compare"] = []
                job["compare_progress"] = ""
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

        if path == "/api/compare":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            models = data.get("models") or []
            if not models or len(models) < 2:
                self._json(400, {"error": "Selecione pelo menos 2 modelos"})
                return
            with job_lock:
                if job["status"] not in TRANSCRIPTION_READY_STATUSES or not job.get("transcription"):
                    self._json(400, {"error": "Transcrição não disponível"})
                    return
                if job.get("compare_progress", "") and "Concluido" not in job.get("compare_progress", ""):
                    self._json(409, {"error": "Comparação já em andamento"})
                    return
            threading.Thread(target=run_compare_job, args=(models,), daemon=True).start()
            self._json(200, {"ok": True})
            return

        if path == "/api/models/delete":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            model = (data.get("model") or "").strip()
            if not model:
                self._json(400, {"error": "Modelo obrigatório"})
                return
            ok, err = ollama_delete_model(model)
            if ok:
                self._json(200, {"ok": True})
            else:
                self._json(500, {"error": err or "Falha ao deletar"})
            return

        if path == "/api/models/pull":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            model = (data.get("model") or "").strip()
            if not model:
                self._json(400, {"error": "Modelo obrigatório"})
                return
            ollama_pull_model(model)
            self._json(200, {"ok": True, "message": f"Baixando {model} em segundo plano..."})
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

        if path == "/api/presets/add":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            preset_id = (data.get("id") or "").strip()
            label = (data.get("label") or "").strip()
            icon = (data.get("icon") or "📝").strip()
            if not preset_id:
                # Auto-generate from label
                preset_id = re.sub(r"[^a-z0-9_\-]", "", label.lower().replace(" ", "_").replace("ç", "c").replace("ã", "a").replace("õ", "o").replace("é", "e").replace("á", "a").replace("í", "i").replace("ú", "u")) or "custom"
                # Ensure unique
                existing = set(prompts.get_prompts_meta().get("presets", {}).keys()) | set(prompts._BUILTIN_PRESET_IDS)
                base = preset_id
                n = 2
                while preset_id in existing:
                    preset_id = f"{base}{n}"
                    n += 1
            try:
                prompts.add_preset(preset_id, label, icon)
                presets_list = prompts.get_presets()
                active = prompts.get_active_preset()
                self._json(200, {"ok": True, "id": preset_id, "presets": presets_list, "active": active})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if path == "/api/presets/remove":
            data = self._read_json_body()
            if data is None:
                self._json(400, {"error": "JSON inválido"})
                return
            preset_id = (data.get("id") or "").strip()
            if not preset_id:
                self._json(400, {"error": "ID do preset obrigatório"})
                return
            try:
                prompts.remove_preset(preset_id)
                presets_list = prompts.get_presets()
                active = prompts.get_active_preset()
                self._json(200, {"ok": True, "presets": presets_list, "active": active})
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return

        if path == "/api/prompts/reset":
            try:
                prompts.reset_to_defaults()
                data = prompts.get_prompts_meta()
                data["preset_defaults"] = prompts.get_all_preset_defaults()
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
