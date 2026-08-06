"""
Configuracoes recomendadas por modelo e tier de hardware.

Cada entrada em MODEL_TIERS define:
- chunk_chars: tamanho maximo de cada chunk da transcricao
- merge_batch: quantos resumos parciais consolidar por vez
- num_ctx: tamanho da janela de contexto (passado ao Ollama)

Modelos menores precisam de chunks menores para nao se perderem.
Modelos maiores podem lidar com chunks maiores e mais consolidacoes.
"""

# Tiers de hardware com modelos recomendados e range de parametros
HARDWARE_TIERS = [
    {
        "id": "low",
        "label": "PC Basico (8GB RAM, sem GPU)",
        "icon": "\U0001f5a5\ufe0f",
        "description": "Modelos ate 3B parametros, rodam via CPU. Funcional, mas mais lento.",
        "recommended": ["llama3.2:3b", "qwen2.5:3b", "phi3:mini"],
        "default_config": {"chunk_chars": 4000, "merge_batch": 2, "num_ctx": 4096, "timeout": 900},
    },
    {
        "id": "mid",
        "label": "PC Medio (16GB RAM, GPU 6-8GB VRAM)",
        "icon": "\U0001f4bb",
        "description": "Modelos 7-8B parametros. Melhor custo-beneficio para sumarizacao.",
        "recommended": ["qwen2.5:7b", "qwen2.5:7b-instruct", "llama3.1:8b", "mistral:7b"],
        "default_config": {"chunk_chars": 8000, "merge_batch": 4, "num_ctx": 8192, "timeout": 600},
    },
    {
        "id": "high",
        "label": "PC Avancado (32GB RAM, GPU 12GB+ VRAM)",
        "icon": "\U0001f680",
        "description": "Modelos 14B+. Resumos mais precisos e capacidade de analise profunda.",
        "recommended": ["qwen2.5:14b", "phi4:14b", "llama3.1:70b"],
        "default_config": {"chunk_chars": 12000, "merge_batch": 6, "num_ctx": 16384, "timeout": 600},
    },
]

# Configuracoes especificas por modelo (match parcial no nome)
# Se um modelo nao estiver aqui, usa o default do tier detectado
MODEL_CONFIGS = {
    "phi3:mini":       {"chunk_chars": 3000, "merge_batch": 2, "num_ctx": 4096,  "timeout": 900},
    "llama3.2:3b":     {"chunk_chars": 4000, "merge_batch": 2, "num_ctx": 4096,  "timeout": 900},
    "qwen2.5:3b":      {"chunk_chars": 4000, "merge_batch": 2, "num_ctx": 4096,  "timeout": 900},

    "mistral:7b":      {"chunk_chars": 7000, "merge_batch": 4, "num_ctx": 8192,  "timeout": 600},
    "llama3.1:8b":     {"chunk_chars": 8000, "merge_batch": 4, "num_ctx": 8192,  "timeout": 600},
    "qwen2.5:7b":      {"chunk_chars": 8000, "merge_batch": 4, "num_ctx": 8192,  "timeout": 600},

    "phi4:14b":        {"chunk_chars": 10000, "merge_batch": 4, "num_ctx": 16384, "timeout": 600},
    "qwen2.5:14b":     {"chunk_chars": 12000, "merge_batch": 6, "num_ctx": 16384, "timeout": 600},
    "qwen2.5:32b":     {"chunk_chars": 14000, "merge_batch": 6, "num_ctx": 32768, "timeout": 900},
    "llama3.1:70b":    {"chunk_chars": 16000, "merge_batch": 8, "num_ctx": 32768, "timeout": 1200},
}


def _model_size_tier(model_name):
    """Detecta o tier pelo tamanho em B no nome do modelo."""
    import re
    match = re.search(r"(\d+)b", model_name.lower())
    if not match:
        return "mid"
    size = int(match.group(1))
    if size <= 3:
        return "low"
    elif size <= 8:
        return "mid"
    else:
        return "high"


def get_model_config(model_name):
    """
    Retorna config otimizada para o modelo.
    Prioridade: match exato > match parcial (prefixo) > tier por tamanho.
    """
    name = model_name.lower().strip()

    # 1. Match parcial (prefixo do modelo)
    for prefix, config in MODEL_CONFIGS.items():
        if name.startswith(prefix.lower()):
            return dict(config)

    # 2. Tier por tamanho
    tier = _model_size_tier(name)
    for t in HARDWARE_TIERS:
        if t["id"] == tier:
            return dict(t["default_config"])

    # 3. Fallback seguro
    return {"chunk_chars": 6000, "merge_batch": 3, "num_ctx": 8192, "timeout": 600}


def get_hardware_tiers():
    """Retorna lista de tiers para exibicao na UI."""
    return HARDWARE_TIERS


def get_model_info(model_name):
    """
    Retorna (tier_dict, config_dict) para exibir na UI.
    """
    config = get_model_config(model_name)
    name = model_name.lower().strip()

    # Encontra em qual tier o modelo se encaixa
    for tier in HARDWARE_TIERS:
        for rec in tier["recommended"]:
            if name.startswith(rec.lower()):
                return tier, config

    # Fallback: tier por tamanho
    tier_id = _model_size_tier(name)
    tier = next((t for t in HARDWARE_TIERS if t["id"] == tier_id), HARDWARE_TIERS[1])
    return tier, config
