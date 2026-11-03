"""Conservative catalog routing with a minimized optional provider input."""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from agente_sped.domain.routing import (
    AnalysisRequest,
    ProviderFailure,
    RoutingDecision,
)
from agente_sped.infra.analysis_catalog import AnalysisCatalog

_GENERIC_PHRASES = {
    'creditos em uso ou consumo': 'possible_credit_on_consumption_cfop',
    'creditos de icms em uso ou consumo': (
        'possible_credit_on_consumption_cfop'
    ),
    'itens com ncm generico': 'generic_ncm_high_value',
    'itens de alto valor com ncm generico': 'generic_ncm_high_value',
}


class RoutingProvider(Protocol):
    def route(
        self, question: str, catalog: list[dict[str, object]]
    ) -> RoutingDecision: ...


@dataclass(frozen=True)
class RouteResolution:
    decision: RoutingDecision
    source: Literal['manual', 'deterministic', 'provider', 'none']
    message: str
    options: tuple[str, ...] = ()


def normalize_question(question: str) -> str:
    plain = unicodedata.normalize('NFKD', question.casefold())
    return ' '.join(
        re.findall(
            r'[a-z0-9]+',
            ''.join(
                character
                for character in plain
                if not unicodedata.combining(character)
            ),
        )
    )


def is_simple_action(question: str | None) -> bool:
    return normalize_question(question or '') in {
        'oi',
        'ola',
        'obrigado',
        'obrigada',
        'bom dia',
        'boa tarde',
        'boa noite',
        'copiar',
        'copie',
    }


def catalog_projection(catalog: AnalysisCatalog) -> list[dict[str, object]]:
    """Expose only public intent metadata, never SQL or reference results."""
    return [
        {
            'analysis_id': analysis_id,
            'title': definition.title,
            'description': definition.description,
            'criteria': list(definition.criteria),
            'period_scope': definition.execution.period_scope,
            'parameters': {
                name: {
                    'type': parameter.type,
                    'minimum': str(parameter.constraints.minimum)
                    if parameter.constraints.minimum is not None
                    else None,
                    'maximum': str(parameter.constraints.maximum)
                    if parameter.constraints.maximum is not None
                    else None,
                }
                for name, parameter in definition.parameters.items()
            },
        }
        for analysis_id, definition in catalog.analyses.items()
    ]


def sanitize_question(question: str, catalog: AnalysisCatalog) -> str:
    """Keep public catalog vocabulary and short numeric parameter proposals.

    Unknown terms are removed rather than guessing which names are personal.
    This deliberately trades some routing recall for data minimization.
    """
    public_text = ' '.join(
        ' '.join(
            (
                definition.title,
                definition.description,
                *definition.criteria,
                *definition.routing.utterances,
            )
        )
        for definition in catalog.analyses.values()
    )
    vocabulary = set(normalize_question(public_text).split()) | {
        'analisar',
        'analise',
        'encontrar',
        'localizar',
        'buscar',
        'compras',
        'vendas',
        'cancelamentos',
        'duplicadas',
        'duplicados',
        'producao',
        'concentracao',
        'periodo',
        'top',
        'de',
        'ate',
        'entre',
        'janeiro',
        'fevereiro',
        'marco',
        'abril',
        'maio',
        'junho',
        'julho',
        'agosto',
        'setembro',
        'outubro',
        'novembro',
        'dezembro',
    }
    question = re.sub(
        r'https?://\S+|[\w.+-]+@[\w.-]+|[A-Za-z]:[\\/]\S+',
        ' termo ',
        question,
    )
    question = re.sub(
        r'\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b'
        r'|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b',
        ' termo ',
        question,
    )
    # Numeric fragments of phones or documents are not routing parameters.
    proposed_numbers: dict[str, str] = {}

    def keep_parameter(match: re.Match[str]) -> str:
        key = 'parameter' + chr(ord('a') + len(proposed_numbers))
        proposed_numbers[key] = match.group()
        return ' ' + key + ' '

    question = normalize_question_preserving_dates(question)
    question = re.sub(
        r'\b\d{4}-\d{2}-\d{2}\b'
        r'|\btop\s+\d{1,4}(?![\d.,])\b'
        r'|\b\d{1,4}\s+(?:maiores|principais)\b'
        r'|(?<![\d.,])\b\d{1,6}(?:[.,]\d{1,6})?\s*%'
        r'|\b(?:acima de|limite de)\s+'
        r'\d{1,6}(?:[.,]\d{1,6})?(?![\d.,])',
        keep_parameter,
        question,
    )
    tokens = []
    for word in normalize_question(question).split():
        safe = word in vocabulary and not word.isdecimal()
        token = proposed_numbers.get(word, word if safe else '[termo]')
        if not tokens or token != '[termo]' or tokens[-1] != token:
            tokens.append(token)
    return ' '.join(tokens)


def normalize_question_preserving_dates(question: str) -> str:
    plain = unicodedata.normalize('NFKD', question.casefold())
    return ''.join(
        character
        for character in plain
        if not unicodedata.combining(character)
    )


def _restricted_scope(question: str) -> bool:
    return bool(
        re.search(
            r'\b(?:cfop|ncm)\s+(?:de\s+)?\d+'
            r'|\b(?:somente|apenas|exclusivamente|especificamente)\b'
            r'|\b(?:cnpj|cpf|cod part|participante especifico)\b'
            r'|\b(?:uf|estado|municipio|cliente)\s+\w+',
            question,
        )
    ) or ('extemporaneamente' in question and 'comum ou' not in question)


def _generic_candidates(question: str) -> tuple[str, ...]:
    patterns = {
        'movement_by_cfop': r'(?:saida.*cfop|cfop.*saida)',
        'cancelled_documents': r'cancelad|cancelamento',
        'block_k_activity': r'bloco k\b|k230\b|ordens.*producao',
        'supplier_concentration': (
            r'concentra.*fornecedor|fornecedor.*concentra'
        ),
        'possible_duplicate_documents': r'duplicad|duplicidade',
        'document_sequence_gaps': r'lacuna|sequencial|numeracao.*ausente',
        'possible_credit_on_consumption_cfop': (
            r'credit\w*.*(?:uso|consumo)|(?:uso|consumo).*credit'
        ),
        'generic_ncm_high_value': r'ncm.*generic|generic.*ncm',
    }
    return tuple(
        analysis_id
        for analysis_id, pattern in patterns.items()
        if re.search(pattern, question)
    )


def _scope_options(question: str, catalog: AnalysisCatalog) -> tuple[str, ...]:
    candidates = _generic_candidates(question)
    if not candidates and re.search(r'\bncm\b', question):
        candidates = ('generic_ncm_high_value',)
    if not candidates and re.search(r'\bcfop\b', question):
        candidates = ('possible_credit_on_consumption_cfop',)
    return candidates or catalog.analysis_ids


def resolve_route(
    request: AnalysisRequest,
    catalog: AnalysisCatalog,
    *,
    provider: RoutingProvider | None = None,
) -> RouteResolution:
    if request.analysis_id is not None:
        known = request.analysis_id in catalog.analyses
        return RouteResolution(
            RoutingDecision(
                status='matched' if known else 'out_of_catalog',
                analysis_id=request.analysis_id if known else None,
                reason='catalog_match' if known else 'unsupported',
            ),
            'manual',
            'Análise selecionada.' if known else 'Análise fora do catálogo.',
        )
    question = normalize_question(request.question or '')
    # A canonical utterance may itself ask for a narrower fiscal scope.
    if _restricted_scope(question):
        return RouteResolution(
            RoutingDecision(
                status='needs_clarification', reason='scope_confirmation'
            ),
            'deterministic',
            'O catálogo não oferece esse filtro específico. '
            'Confira os critérios da análise completa e selecione seu ID '
            'para confirmar a execução.',
            _scope_options(question, catalog),
        )
    matches = tuple(
        analysis_id
        for analysis_id, definition in catalog.analyses.items()
        if question
        in {normalize_question(text) for text in definition.routing.utterances}
    )
    generic = _generic_candidates(question)
    if not matches and question in _GENERIC_PHRASES:
        matches = (_GENERIC_PHRASES[question],)
    if len(generic) > 1:
        return RouteResolution(
            RoutingDecision(status='needs_clarification', reason='ambiguous'),
            'deterministic',
            'Escolha uma análise por execução.',
            generic,
        )
    if len(matches) == 1:
        analysis_id = matches[0]
        if not catalog.analyses[analysis_id].routing.clarification_required:
            return RouteResolution(
                RoutingDecision(
                    status='matched',
                    analysis_id=analysis_id,
                    reason='catalog_match',
                ),
                'deterministic',
                'Intenção identificada no catálogo.',
            )
    if provider is not None:
        decision = provider.route(
            sanitize_question(request.question or '', catalog),
            catalog_projection(catalog),
        )
        if not isinstance(decision, RoutingDecision):
            raise ProviderFailure('Resposta de roteamento inválida.')
        if decision.analysis_id not in (None, *catalog.analysis_ids):
            raise ProviderFailure('Resposta de roteamento inválida.')
        return RouteResolution(
            decision,
            'provider',
            'Proposta de análise recebida.'
            if decision.status == 'matched'
            else 'Confirme uma análise e seus critérios no catálogo.',
            () if decision.status == 'matched' else catalog.analysis_ids,
        )
    return RouteResolution(
        RoutingDecision(status='needs_clarification', reason='ambiguous'),
        'none',
        'Pergunta recebida, mas não identificada com segurança sem IA. '
        'Selecione uma análise abaixo e confira seus critérios '
        'para confirmar a execução.',
        matches or generic or catalog.analysis_ids,
    )


def merged_parameters(
    request: AnalysisRequest, decision: RoutingDecision
) -> Mapping[str, object]:
    """Explicit values take precedence; all values still require validation."""
    return {
        **decision.parameters.model_dump(exclude_none=True),
        **request.parameters,
    }
