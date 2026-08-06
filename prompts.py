"""Gerenciamento de templates de prompt para o Video Resumidor.

Carrega e salva prompts do arquivo prompts.json. As funções de formatação
recebem os valores de runtime e retornam o prompt completo pronto para enviar
ao Ollama.

Os templates usam placeholders {variavel} que são substituídos pelas funções.
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROMPTS_FILE = os.path.join(SCRIPT_DIR, "prompts.json")

# Cache em memória (recarregado a cada get_prompts / format_*)
_prompts_cache = None
_prompts_mtime = 0

_DEFAULT_PROMPTS = {
    "active_preset": "general",
    "presets": {
        "general": {"label": "Reunião Geral", "icon": "📋"},
        "standup": {"label": "Daily / Standup", "icon": "☀️"},
        "retrospective": {"label": "Retrospectiva", "icon": "🔄"},
        "planning": {"label": "Sprint Planning", "icon": "📐"},
    },
    "chunk_prompt": (
        "Você analisa o TRECHO {chunk_num}/{total} de uma transcrição automática de REUNIÃO (podem haver erros de voz).\n"
        "\n"
        "Linhas no formato [MM:SS] ou [HH:MM:SS] + texto falado. COPIE os timestamps — não invente.\n"
        "\n"
        "REGRAS:\n"
        "- PROIBIDO inventar ou supor informação que não esteja neste trecho\n"
        "- Só inclua itens com [timestamp] correspondente na transcrição\n"
        "- Responda em português do Brasil, de forma objetiva\n"
        "- NÃO escreva despedidas nem comentários sobre ser IA\n"
        "\n"
        "Extraia SOMENTE deste trecho (use bullets com [timestamp]):\n"
        "- Tópicos discutidos\n"
        "- Decisões explícitas (se houver)\n"
        "- Ações, responsáveis e prazos (só se falados)\n"
        "- Pendências ou riscos citados (se houver)\n"
        "\n"
        "Se uma categoria não aparecer neste trecho, omita-a (não escreva \"não mencionado\").\n"
        "\n"
        "TRECHO DA TRANSCRIÇÃO:\n"
        "{chunk_text}"
    ),
    "merge_prompt": (
        "Você monta o RESUMO FINAL de uma reunião a partir de notas parciais (já extraídas da transcrição).\n"
        "\n"
        "As notas abaixo vieram de trechos diferentes da mesma reunião. Cada item já traz [timestamp].\n"
        "PROIBIDO inventar informação que não esteja nas notas.\n"
        "\n"
        "REGRAS:\n"
        "- Responda SOMENTE em português do Brasil\n"
        "- OBRIGATÓRIO: todo item em lista deve manter [MM:SS] ou [HH:MM:SS] das notas\n"
        "- Use EXATAMENTE os títulos de seção abaixo, sem renomear\n"
        "- Seja objetivo; NÃO repita a mesma informação em seções diferentes\n"
        "- Preserve decisões, ações, responsáveis, prazos, pendências e timestamps; só remova duplicatas claras\n"
        "- NÃO escreva despedidas nem comentários sobre você ser uma IA\n"
        "- Se uma seção não tiver conteúdo nas notas, escreva: \"Não mencionado na reunião\"\n"
        "\n"
        "FORMATO (markdown):\n"
        "\n"
        "## Resumo executivo\n"
        "2 a 4 frases apenas com o que foi dito na reunião.\n"
        "\n"
        "## Tópicos discutidos\n"
        "4 a 10 itens: [timestamp] Assunto\n"
        "\n"
        "## Decisões\n"
        "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Ações e responsáveis\n"
        "Formato: [timestamp] Responsável — ação — prazo (só se nas notas). Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Pendências e riscos\n"
        "Só bloqueios, dúvidas ou riscos citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Próximos passos\n"
        "Só o combinado explicitamente + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "NOTAS PARCIAIS DA REUNIÃO:\n"
        "{combined_notes}"
    ),
    "single_prompt": (
        "Você analisa REUNIÕES a partir de transcrições automáticas de áudio (podem conter erros de reconhecimento de voz).\n"
        "\n"
        "A transcrição abaixo tem linhas no formato [MM:SS] ou [HH:MM:SS] seguido do texto falado.\n"
        "COPIE esses timestamps para o resumo — não invente horários.\n"
        "\n"
        "REGRA MAIS IMPORTANTE — FIDELIDADE À TRANSCRIÇÃO:\n"
        "- PROIBIDO inventar, supor ou completar lacunas\n"
        "- PROIBIDO criar decisões, ações, prazos, responsáveis, nomes ou conclusões que não estejam na transcrição\n"
        "- Só inclua um item se puder apontar o trecho correspondente (use o [timestamp] desse trecho)\n"
        "- Na dúvida, OMITA — não chute\n"
        "- Paráfrase permitida, mas o sentido deve estar explícito na fala transcrita\n"
        "- Corrija apenas erros óbvios de transcrição (nomes/termos); se não tiver certeza, mantenha o original e marque \"(?)\"\n"
        "- Se uma seção não tiver conteúdo na transcrição, escreva exatamente: \"Não mencionado na reunião\"\n"
        "\n"
        "OUTRAS REGRAS:\n"
        "- Responda SOMENTE em português do Brasil\n"
        "- OBRIGATÓRIO: todo item em lista deve começar com [MM:SS] ou [HH:MM:SS] copiado da transcrição\n"
        "- PROIBIDO entregar resumo genérico de \"vídeo\" — é uma reunião transcrita\n"
        "- Use EXATAMENTE os títulos de seção abaixo, sem renomear\n"
        "- Seja objetivo; NÃO repita a mesma informação em seções diferentes\n"
        "- NÃO escreva despedidas nem comentários sobre você ser uma IA\n"
        "\n"
        "FORMATO (markdown) — exemplo de item correto:\n"
        "- [04:12] Definido que o deploy será na sexta\n"
        "\n"
        "## Resumo executivo\n"
        "2 a 4 frases apenas com o que foi dito na reunião.\n"
        "\n"
        "## Tópicos discutidos\n"
        "4 a 10 itens: [timestamp] Assunto (só assuntos realmente falados)\n"
        "\n"
        "## Decisões\n"
        "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Ações e responsáveis\n"
        "Formato: [timestamp] Responsável — ação — prazo (só se falado). Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Pendências e riscos\n"
        "Só bloqueios, dúvidas ou riscos citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "## Próximos passos\n"
        "Só o combinado explicitamente + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
        "\n"
        "TRANSCRIÇÃO DA REUNIÃO:\n"
        "{transcription_text}\n"
        "\n"
        "LEMBRETE FINAL: siga o formato acima. Cada bullet precisa de [MM:SS] ou [HH:MM:SS] tirado da transcrição."
    ),
    "chat_prompt": (
        "Você responde perguntas sobre uma REUNIÃO usando SOMENTE a transcrição abaixo.\n"
        "\n"
        "REGRAS:\n"
        "- PROIBIDO inventar ou supor qualquer informação\n"
        "- Se não estiver na transcrição, responda: \"Não foi mencionado na reunião.\"\n"
        "- Cite [timestamp] ao referenciar trechos\n"
        "- Responda em português do Brasil, de forma objetiva\n"
        "\n"
        "TRANSCRIÇÃO:\n"
        "{transcription_text}\n"
        "\n"
        "PERGUNTA:\n"
        "{question}"
    ),
}


def _load():
    """Carrega prompts do JSON, com cache baseado em mtime."""
    global _prompts_cache, _prompts_mtime
    try:
        mtime = os.path.getmtime(PROMPTS_FILE)
    except OSError:
        mtime = 0

    if _prompts_cache is not None and mtime == _prompts_mtime:
        return _prompts_cache

    if not os.path.isfile(PROMPTS_FILE):
        _prompts_cache = {}
        _prompts_mtime = 0
        return _prompts_cache

    with open(PROMPTS_FILE, encoding="utf-8") as f:
        _prompts_cache = json.load(f)
    _prompts_mtime = mtime
    return _prompts_cache


def get_prompts():
    """Retorna dict com todos os templates (sem o _meta). Útil para a API."""
    data = dict(_load())
    data.pop("_meta", None)
    return data


def get_prompts_meta():
    """Retorna dict completo incluindo _meta, para o editor da interface."""
    return dict(_load())


def save_prompts(data):
    """Salva o dict de prompts no JSON. Faz merge profundo para presets."""
    current = _load()
    meta = data.pop("_meta", None) or current.get("_meta", {"version": 2})
    current["_meta"] = meta

    # Deep merge para presets (preserva outros prompts)
    if "presets" in data:
        current_presets = current.get("presets", {})
        for pid, overrides in data["presets"].items():
            current_presets[pid] = {**current_presets.get(pid, {}), **overrides}
        current["presets"] = current_presets
        del data["presets"]

    # Shallow merge para chaves de nível superior
    current.update(data)

    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    # Invalida cache
    global _prompts_cache, _prompts_mtime
    _prompts_cache = None
    _prompts_mtime = 0


def reset_to_defaults():
    """Restaura os prompts padrão (recria prompts.json do zero)."""
    _meta = {
        "version": 2,
        "description": (
            "Templates de prompt editáveis para o Video Resumidor. "
            "Use {variavel} para placeholders que serão substituídos em runtime."
        ),
    }
    data = {"_meta": _meta}
    # Copia apenas os prompts (não active_preset/presets que são metadados)
    for key in ("active_preset", "presets", "chunk_prompt", "merge_prompt", "single_prompt", "chat_prompt"):
        if key in _DEFAULT_PROMPTS:
            data[key] = _DEFAULT_PROMPTS[key]
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _prompts_cache, _prompts_mtime
    _prompts_cache = None
    _prompts_mtime = 0


def get_presets():
    """Retorna lista de presets disponíveis (label, icon, id)."""
    data = _load()
    presets = data.get("presets", {}) or _DEFAULT_PROMPTS.get("presets", {})
    return [
        {"id": pid, "label": p.get("label", pid), "icon": p.get("icon", "📋")}
        for pid, p in presets.items()
    ]


def get_active_preset():
    """Retorna o id do preset ativo."""
    return _load().get("active_preset", "general") or "general"


def set_active_preset(preset_id):
    """Define o preset ativo e salva no JSON."""
    data = _load()
    presets = data.get("presets", {})
    if preset_id not in presets:
        default_presets = _DEFAULT_PROMPTS.get("presets", {})
        if preset_id not in default_presets:
            raise ValueError(f"Preset desconhecido: {preset_id}")
    data["active_preset"] = preset_id
    # Preserva _meta
    meta = data.get("_meta", {"version": 2})
    data["_meta"] = meta
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _prompts_cache, _prompts_mtime
    _prompts_cache = None
    _prompts_mtime = 0


def _get_template(key):
    """Obtém template pelo nome, com fallback para _DEFAULT_PROMPTS.

    Ordem de prioridade:
    1. Override do preset ativo (ex: presets.standup.single_prompt)
    2. Chave de nível superior (ex: single_prompt)
    3. _DEFAULT_PROMPTS (fallback se JSON não existir)
    """
    data = _load()
    active = data.get("active_preset", "general")
    presets = data.get("presets", {})

    # 1. Active preset override
    override = presets.get(active, {}).get(key, "")
    if override:
        return override

    # 2. Top-level key
    tmpl = data.get(key, "")
    if tmpl:
        return tmpl

    # 3. Python defaults: try general preset first, then top-level
    default_presets = _DEFAULT_PROMPTS.get("presets", {})
    fallback = default_presets.get("general", {}).get(key, "")
    if fallback:
        return fallback
    return _DEFAULT_PROMPTS.get(key, "")


def _safe_format(tmpl, key, **kwargs):
    """Formata template com fallback para default em caso de erro."""
    try:
        return tmpl.format(**kwargs)
    except (KeyError, ValueError):
        # Placeholder quebrado — usa o default
        fallback = _DEFAULT_PROMPTS.get(key, "")
        try:
            return fallback.format(**kwargs)
        except (KeyError, ValueError):
            return ""


def format_chunk_prompt(chunk_text, chunk_num, total):
    """Prompt para análise de um trecho da transcrição."""
    tmpl = _get_template("chunk_prompt")
    return _safe_format(tmpl, "chunk_prompt", chunk_text=chunk_text, chunk_num=chunk_num, total=total)


def format_merge_prompt(combined_notes):
    """Prompt para consolidar múltiplos resumos parciais em um final."""
    tmpl = _get_template("merge_prompt")
    return _safe_format(tmpl, "merge_prompt", combined_notes=combined_notes)


def format_single_prompt(transcription_text):
    """Prompt para resumir transcrição inteira em lote único."""
    tmpl = _get_template("single_prompt")
    return _safe_format(tmpl, "single_prompt", transcription_text=transcription_text)


def format_chat_prompt(transcription_text, question):
    """Prompt para responder pergunta sobre a transcrição."""
    tmpl = _get_template("chat_prompt")
    return _safe_format(tmpl, "chat_prompt", transcription_text=transcription_text, question=question)
