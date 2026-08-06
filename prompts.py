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

# Chaves de prompt que podem ter versão específica por preset
_PROMPT_KEYS = ("chunk_prompt", "merge_prompt", "single_prompt", "chat_prompt")

# Prompts padrão específicos de cada tipo de reunião.
# - "general" não define prompts: usa as chaves de nível superior (base editável).
# - Os demais presets têm defaults próprios, para que cada tipo de reunião
#   gere um resumo com formato adequado desde o início.
_PRESET_PROMPT_DEFAULTS = {
    "standup": {
        "chunk_prompt": (
            "Você analisa o TRECHO {chunk_num}/{total} de uma transcrição automática de uma REUNIÃO DIÁRIA (daily/standup) (podem haver erros de voz).\n"
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
            "- Atualizações de cada pessoa (o que fez / está fazendo)\n"
            "- Bloqueios ou impedimentos citados\n"
            "- Decisões explícitas (se houver)\n"
            "- Ações e próximos passos combinados (só se falados)\n"
            "\n"
            "Se uma categoria não aparecer neste trecho, omita-a (não escreva \"não mencionado\").\n"
            "\n"
            "TRECHO DA TRANSCRIÇÃO:\n"
            "{chunk_text}"
        ),
        "merge_prompt": (
            "Você monta o RESUMO FINAL de uma REUNIÃO DIÁRIA (daily/standup) a partir de notas parciais (já extraídas da transcrição).\n"
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
            "## Atualizações por pessoa\n"
            "Bullets: [timestamp] Nome — o que fez / o que está fazendo\n"
            "\n"
            "## Bloqueios e impedimentos\n"
            "Só bloqueios citados + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Próximos passos\n"
            "Só o combinado explicitamente + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "NOTAS PARCIAIS DA REUNIÃO:\n"
            "{combined_notes}"
        ),
        "single_prompt": (
            "Você analisa uma REUNIÃO DIÁRIA (daily/standup) a partir de transcrições automáticas de áudio (podem conter erros de reconhecimento de voz).\n"
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
            "- Use EXATAMENTE os títulos de seção abaixo, sem renomear\n"
            "- Seja objetivo; NÃO repita a mesma informação em seções diferentes\n"
            "- NÃO escreva despedidas nem comentários sobre você ser uma IA\n"
            "\n"
            "FORMATO (markdown) — exemplo de item correto:\n"
            "- [04:12] Maria: revisão do PR #42 quase concluída\n"
            "\n"
            "## Resumo executivo\n"
            "2 a 4 frases apenas com o que foi dito na reunião.\n"
            "\n"
            "## Atualizações por pessoa\n"
            "Bullets: [timestamp] Nome — o que fez / o que está fazendo\n"
            "\n"
            "## Bloqueios e impedimentos\n"
            "Só bloqueios citados + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Próximos passos\n"
            "Só o combinado explicitamente + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "TRANSCRIÇÃO DA REUNIÃO:\n"
            "{transcription_text}\n"
            "\n"
            "LEMBRETE FINAL: siga o formato acima. Cada bullet precisa de [MM:SS] ou [HH:MM:SS] tirado da transcrição."
        ),
        "chat_prompt": (
            "Você responde perguntas sobre uma REUNIÃO DIÁRIA (daily/standup) usando SOMENTE a transcrição abaixo.\n"
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
    },
    "retrospective": {
        "chunk_prompt": (
            "Você analisa o TRECHO {chunk_num}/{total} de uma transcrição automática de uma REUNIÃO DE RETROSPECTIVA (podem haver erros de voz).\n"
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
            "- O que foi bem / pontos positivos citados\n"
            "- O que pode melhorar / pontos de atenção\n"
            "- Decisões explícitas (se houver)\n"
            "- Ações e responsáveis combinados (só se falados)\n"
            "\n"
            "Se uma categoria não aparecer neste trecho, omita-a (não escreva \"não mencionado\").\n"
            "\n"
            "TRECHO DA TRANSCRIÇÃO:\n"
            "{chunk_text}"
        ),
        "merge_prompt": (
            "Você monta o RESUMO FINAL de uma REUNIÃO DE RETROSPECTIVA a partir de notas parciais (já extraídas da transcrição).\n"
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
            "## O que foi bem\n"
            "Bullets: [timestamp] ponto positivo citado\n"
            "\n"
            "## O que pode melhorar\n"
            "Bullets: [timestamp] ponto de melhoria citado. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Ações e responsáveis\n"
            "Formato: [timestamp] Responsável — ação — prazo (só se nas notas). Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Próximos passos\n"
            "Só o combinado explicitamente + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "NOTAS PARCIAIS DA REUNIÃO:\n"
            "{combined_notes}"
        ),
        "single_prompt": (
            "Você analisa uma REUNIÃO DE RETROSPECTIVA (retro) de equipe a partir de transcrições automáticas de áudio (podem conter erros de reconhecimento de voz).\n"
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
            "- Use EXATAMENTE os títulos de seção abaixo, sem renomear\n"
            "- Seja objetivo; NÃO repita a mesma informação em seções diferentes\n"
            "- NÃO escreva despedidas nem comentários sobre você ser uma IA\n"
            "\n"
            "FORMATO (markdown) — exemplo de item correto:\n"
            "- [06:40] Melhoramos muito a cobertura de testes\n"
            "\n"
            "## Resumo executivo\n"
            "2 a 4 frases apenas com o que foi dito na reunião.\n"
            "\n"
            "## O que foi bem\n"
            "Bullets: [timestamp] ponto positivo citado\n"
            "\n"
            "## O que pode melhorar\n"
            "Bullets: [timestamp] ponto de melhoria citado. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Ações e responsáveis\n"
            "Formato: [timestamp] Responsável — ação — prazo (só se falado). Senão: \"Não mencionado na reunião\"\n"
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
            "Você responde perguntas sobre uma REUNIÃO DE RETROSPECTIVA usando SOMENTE a transcrição abaixo.\n"
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
    },
    "planning": {
        "chunk_prompt": (
            "Você analisa o TRECHO {chunk_num}/{total} de uma transcrição automática de uma reunião de SPRINT PLANNING (podem haver erros de voz).\n"
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
            "- Objetivos e escopo definidos para a sprint\n"
            "- Itens/tarefas planejados (só se citados)\n"
            "- Estimativas, prazos ou capacidade citados\n"
            "- Riscos ou dependências levantados\n"
            "- Decisões explícitas (se houver)\n"
            "\n"
            "Se uma categoria não aparecer neste trecho, omita-a (não escreva \"não mencionado\").\n"
            "\n"
            "TRECHO DA TRANSCRIÇÃO:\n"
            "{chunk_text}"
        ),
        "merge_prompt": (
            "Você monta o RESUMO FINAL de uma reunião de SPRINT PLANNING a partir de notas parciais (já extraídas da transcrição).\n"
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
            "## Escopo e objetivos\n"
            "Bullets: [timestamp] objetivo ou escopo definido\n"
            "\n"
            "## Itens planejados\n"
            "Bullets: [timestamp] item/tarefa planejado (responsável, se citado)\n"
            "\n"
            "## Estimativas e prazos\n"
            "Só estimativas, capacidade ou prazos citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Riscos e dependências\n"
            "Só riscos ou dependências citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Próximos passos\n"
            "Só o combinado explicitamente + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "NOTAS PARCIAIS DA REUNIÃO:\n"
            "{combined_notes}"
        ),
        "single_prompt": (
            "Você analisa uma reunião de SPRINT PLANNING a partir de transcrições automáticas de áudio (podem conter erros de reconhecimento de voz).\n"
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
            "- Use EXATAMENTE os títulos de seção abaixo, sem renomear\n"
            "- Seja objetivo; NÃO repita a mesma informação em seções diferentes\n"
            "- NÃO escreva despedidas nem comentários sobre você ser uma IA\n"
            "\n"
            "FORMATO (markdown) — exemplo de item correto:\n"
            "- [03:10] Definido o objetivo da sprint: entregar a v2 do portal\n"
            "\n"
            "## Resumo executivo\n"
            "2 a 4 frases apenas com o que foi dito na reunião.\n"
            "\n"
            "## Escopo e objetivos\n"
            "Bullets: [timestamp] objetivo ou escopo definido\n"
            "\n"
            "## Itens planejados\n"
            "Bullets: [timestamp] item/tarefa planejado (responsável, se citado)\n"
            "\n"
            "## Estimativas e prazos\n"
            "Só estimativas, capacidade ou prazos citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Riscos e dependências\n"
            "Só riscos ou dependências citados + [timestamp]. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Decisões\n"
            "Bullets: [timestamp] Decisão — somente acordos explícitos. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "## Próximos passos\n"
            "Só o combinado explicitamente + [timestamp] e responsável. Senão: \"Não mencionado na reunião\"\n"
            "\n"
            "TRANSCRIÇÃO DA REUNIÃO:\n"
            "{transcription_text}\n"
            "\n"
            "LEMBRETE FINAL: siga o formato acima. Cada bullet precisa de [MM:SS] ou [HH:MM:SS] tirado da transcrição."
        ),
        "chat_prompt": (
            "Você responde perguntas sobre uma reunião de SPRINT PLANNING usando SOMENTE a transcrição abaixo.\n"
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
    },
}

# IDs dos presets padrão (não podem ser removidos pelo usuário)
_BUILTIN_PRESET_IDS = frozenset({"general", "standup", "retrospective", "planning"})

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

    # Presets: substitui o dict de cada preset enviado no payload.
    # O editor sempre envia o conjunto completo (label/icon + overrides),
    # então isso permite também REMOVER overrides (chaves ausentes somem).
    if "presets" in data:
        current_presets = current.get("presets", {})
        for pid, preset_data in data["presets"].items():
            current_presets[pid] = preset_data
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
    # active_preset
    data["active_preset"] = _DEFAULT_PROMPTS.get("active_preset", "general")
    # Presets: apenas label/icon no JSON. Os prompts padrão de cada tipo
    # ficam em código (_PRESET_PROMPT_DEFAULTS) — assim "restaurar padrões"
    # não vira um monte de overrides no arquivo.
    # Preserva presets personalizados (só label/icon, sem overrides),
    # mas recria os built-in com os valores padrão
    current_presets = _load().get("presets", {})
    custom_presets = {
        pid: {"label": p.get("label", pid), "icon": p.get("icon", "📝")}
        for pid, p in current_presets.items()
        if pid not in _BUILTIN_PRESET_IDS
    }
    data["presets"] = {
        pid: {"label": p.get("label", pid), "icon": p.get("icon", "📋")}
        for pid, p in _DEFAULT_PROMPTS.get("presets", {}).items()
    }
    data["presets"].update(custom_presets)
    # Prompts de nível superior (base do preset "general")
    for key in _PROMPT_KEYS:
        if key in _DEFAULT_PROMPTS:
            data[key] = _DEFAULT_PROMPTS[key]
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _prompts_cache, _prompts_mtime
    _prompts_cache = None
    _prompts_mtime = 0


def get_all_preset_defaults():
    """Retorna os prompts padrão de cada tipo de reunião (definidos em código).

    Formato: {preset_id: {chunk_prompt: ..., merge_prompt: ..., ...}}.
    Presets sem prompts próprios (ex.: general) não aparecem ou vêm vazios.
    """
    result = {}
    for pid, p in _PRESET_PROMPT_DEFAULTS.items():
        prompts_for_preset = {k: p[k] for k in _PROMPT_KEYS if p.get(k)}
        if prompts_for_preset:
            result[pid] = prompts_for_preset
    return result


def get_presets():
    """Retorna lista de presets disponíveis (label, icon, id)."""
    data = _load()
    presets = data.get("presets", {}) or _DEFAULT_PROMPTS.get("presets", {})
    return [
        {
            "id": pid,
            "label": p.get("label", pid),
            "icon": p.get("icon", "📋"),
            "builtin": pid in _BUILTIN_PRESET_IDS,
        }
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


def add_preset(preset_id, label, icon):
    """Adiciona um novo tipo de reunião personalizado.

    Cria o preset com apenas label/icon (sem prompts customizados).
    O novo preset herda a base geral (top-level prompts) até ser personalizado.
    """
    preset_id = preset_id.strip().lower()
    if not preset_id or not preset_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("ID do preset inválido: use apenas letras, números, _ ou -")
    if not label.strip():
        raise ValueError("O label não pode ficar vazio")

    data = _load()
    presets = data.get("presets", {})
    if preset_id in presets or preset_id in _BUILTIN_PRESET_IDS:
        raise ValueError(f"Já existe um preset com o id \"{preset_id}\"")

    presets[preset_id] = {
        "label": label.strip(),
        "icon": (icon or "📝").strip(),
    }
    data["presets"] = presets

    meta = data.get("_meta", {"version": 2})
    data["_meta"] = meta

    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _prompts_cache, _prompts_mtime
    _prompts_cache = None
    _prompts_mtime = 0


def remove_preset(preset_id):
    """Remove um tipo de reunião personalizado (presets padrão são protegidos)."""
    if preset_id in _BUILTIN_PRESET_IDS:
        raise ValueError(f"O preset \"{preset_id}\" é padrão e não pode ser removido")

    data = _load()
    presets = data.get("presets", {})
    if preset_id not in presets:
        raise ValueError(f"Preset \"{preset_id}\" não encontrado")

    del presets[preset_id]
    data["presets"] = presets

    # Se o preset removido estava ativo, volta para general
    if data.get("active_preset") == preset_id:
        data["active_preset"] = "general"

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
    1. Override salvo pelo usuário no preset ativo (ex: presets.standup.single_prompt)
    2. Prompt padrão específico deste tipo de reunião (definido em código)
    3. Chave de nível superior (ex: single_prompt) — base do preset "general"
    4. _DEFAULT_PROMPTS (fallback se JSON não existir)
    """
    data = _load()
    active = data.get("active_preset", "general")
    presets = data.get("presets", {})

    # 1. Override salvo pelo usuário no preset ativo
    override = presets.get(active, {}).get(key, "")
    if override:
        return override

    # 2. Prompt padrão específico deste tipo de reunião
    preset_default = _PRESET_PROMPT_DEFAULTS.get(active, {}).get(key, "")
    if preset_default:
        return preset_default

    # 3. Chave de nível superior (base editável do preset "general")
    tmpl = data.get(key, "")
    if tmpl:
        return tmpl

    # 4. Fallback final: defaults em código
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
