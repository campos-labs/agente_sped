"""Optional routing uses synthetic credentials and a simulated SDK only."""

import json
import sys
from contextvars import ContextVar
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core import globals as langchain_globals
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.tracers.context import tracing_v2_callback_var
from langsmith.run_helpers import get_tracing_context
from pydantic import SecretStr, ValidationError

from agente_sped.domain.routing import RoutingDecision
from agente_sped.infra.gemini import (
    GeminiRoutingProvider,
    ProviderFailure,
    ensure_private_runtime,
)
from agente_sped.infra.settings import Settings

_SYNTHETIC_KEY = 'synthetic-key-never-sent'
_DEFAULT_TIMEOUT = 30
_CUSTOM_TIMEOUT = 12.5


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    for name in (
        'AI_ROUTING_ENABLED',
        'AI_NARRATIVE_ENABLED',
        'EXPLORATORY_SQL_ENABLED',
        'GEMINI_API_KEY',
        'GEMINI_MODEL',
        'GEMINI_TIMEOUT_SECONDS',
        'GOOGLE_API_KEY',
        'GOOGLE_GENAI_USE_ENTERPRISE',
        'GOOGLE_GENAI_USE_VERTEXAI',
        'LANGCHAIN_TRACING',
        'LANGCHAIN_HANDLER',
        'LANGCHAIN_TRACING_V2',
        'LANGSMITH_TRACING',
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def catalog_projection():
    return [
        {
            'analysis_id': 'supplier_concentration',
            'title': 'Concentração por fornecedor',
            'description': 'Concentração das entradas regulares no período.',
            'criteria': ['Documentos regulares de entrada.'],
            'period_scope': 'document',
            'parameters': {
                'start_date': {
                    'type': 'date',
                    'minimum': None,
                    'maximum': None,
                },
                'top_n': {
                    'type': 'integer',
                    'minimum': '1',
                    'maximum': '20',
                },
                'threshold_percent': {
                    'type': 'decimal',
                    'minimum': '0',
                    'maximum': '100',
                },
            },
        }
    ]


@pytest.fixture
def sdk(monkeypatch):
    state = SimpleNamespace(
        creations=[],
        schema=None,
        structured_options=None,
        invocations=[],
        closed=0,
        vertexai=False,
        hook=None,
        response={
            'status': 'matched',
            'analysis_id': 'supplier_concentration',
            'parameters': {'top_n': 5, 'threshold_percent': '12.50'},
            'reason': 'catalog_match',
        },
    )

    class FakeModel:
        def __init__(self, **kwargs):
            state.creations.append(kwargs)
            self.client = SimpleNamespace(
                vertexai=state.vertexai, close=self.close
            )

        def with_structured_output(self, schema, **kwargs):
            state.schema = schema
            state.structured_options = kwargs
            return self

        def invoke(self, messages, *, config):
            state.invocations.append((messages, config))
            if state.hook is not None:
                state.hook()
            return state.response

        def close(self):
            state.closed += 1

    monkeypatch.setitem(
        sys.modules,
        'langchain_google_genai',
        SimpleNamespace(ChatGoogleGenerativeAI=FakeModel),
    )
    return state


def _provider(**overrides):
    values = {
        'ai_routing_enabled': True,
        'gemini_api_key': SecretStr(_SYNTHETIC_KEY),
    }
    values.update(overrides)
    return GeminiRoutingProvider(Settings(_env_file=None, **values))


def test_settings_allow_local_execution_and_opt_in_without_credentials():
    local = Settings(_env_file=None)
    assert not local.ai_routing_enabled
    assert local.gemini_api_key is None
    assert local.gemini_model == 'gemini-3.5-flash-lite'
    assert local.gemini_timeout_seconds == _DEFAULT_TIMEOUT
    opt_in = Settings(_env_file=None, ai_routing_enabled=True)
    assert opt_in.ai_routing_enabled
    assert opt_in.gemini_api_key is None


def test_settings_only_use_gemini_key_and_keep_secret_out_of_repr(monkeypatch):
    monkeypatch.setenv('GOOGLE_API_KEY', 'ignored-synthetic-google-key')
    assert Settings(_env_file=None).gemini_api_key is None
    monkeypatch.setenv('GEMINI_API_KEY', _SYNTHETIC_KEY)
    settings = Settings(_env_file=None)
    assert settings.gemini_api_key.get_secret_value() == _SYNTHETIC_KEY
    assert _SYNTHETIC_KEY not in repr(settings)
    assert _SYNTHETIC_KEY not in settings.model_dump_json()
    monkeypatch.setenv('GEMINI_API_KEY', '')
    assert Settings(_env_file=None).gemini_api_key is None


@pytest.mark.parametrize(
    'flag', ['ai_narrative_enabled', 'exploratory_sql_enabled']
)
def test_settings_still_reject_unavailable_capabilities(flag):
    with pytest.raises(ValidationError, match='ainda indisponíveis'):
        Settings(_env_file=None, **{flag: True})


@pytest.mark.parametrize('timeout', [0, 61, float('inf'), float('nan'), True])
def test_settings_reject_invalid_timeout_without_disclosing_input(timeout):
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            gemini_api_key=_SYNTHETIC_KEY,
            gemini_timeout_seconds=timeout,
        )
    assert _SYNTHETIC_KEY not in str(error.value)


def test_settings_accept_bounded_timeout_and_model_from_environment(
    monkeypatch,
):
    monkeypatch.setenv('GEMINI_TIMEOUT_SECONDS', '12.5')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
    assert Settings(_env_file=None).gemini_timeout_seconds == _CUSTOM_TIMEOUT
    monkeypatch.setenv('GEMINI_MODEL', 'https://untrusted.invalid/model')
    with pytest.raises(ValidationError, match='gemini_model|GEMINI_MODEL'):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    'overrides',
    [
        {'ai_routing_enabled': False},
        {'gemini_api_key': None},
        {'gemini_api_key': SecretStr('   ')},
    ],
)
def test_disabled_or_unconfigured_provider_does_not_construct_client(
    catalog_projection, sdk, overrides
):
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider(**overrides).route(
            'concentração fornecedor', catalog_projection
        )
    assert sdk.creations == []


def test_sdk_receives_explicit_bounded_configuration_and_public_input(
    catalog_projection, sdk
):
    provider = _provider(gemini_timeout_seconds=12.5)
    assert sdk.creations == []
    result = provider.route('concentração fornecedor', catalog_projection)
    assert result.analysis_id == 'supplier_concentration'
    assert result.parameters.threshold_percent == '12.50'
    kwargs = sdk.creations[0]
    assert kwargs == {
        'model': 'gemini-3.5-flash-lite',
        'api_key': SecretStr(_SYNTHETIC_KEY),
        'vertexai': False,
        'base_url': 'https://generativelanguage.googleapis.com/',
        'client_args': {'trust_env': False},
        'thinking_level': 'minimal',
        'timeout': 12.5,
        'max_retries': 1,
        'max_output_tokens': 1024,
        'callbacks': [],
        'verbose': False,
        'cache': False,
    }
    assert sdk.schema is RoutingDecision
    assert sdk.structured_options == {
        'method': 'json_schema',
        'include_raw': False,
    }
    messages, config = sdk.invocations[0]
    assert config == {'callbacks': []}
    assert json.loads(messages[1][1]) == {
        'question': 'concentração fornecedor',
        'catalog': catalog_projection,
    }
    assert _SYNTHETIC_KEY not in str(messages)
    assert sdk.closed == 1


@pytest.mark.parametrize(
    'flag',
    [
        'GOOGLE_GENAI_USE_ENTERPRISE',
        'GOOGLE_GENAI_USE_VERTEXAI',
        'LANGCHAIN_TRACING',
        'LANGCHAIN_HANDLER',
    ],
)
def test_ambient_backend_and_legacy_tracing_flags_fail_before_client(
    monkeypatch, catalog_projection, sdk, flag
):
    monkeypatch.setenv(flag, 'true')
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', catalog_projection)
    assert sdk.creations == []


def test_public_privacy_guard_also_protects_offline_graph(monkeypatch):
    monkeypatch.setattr(langchain_globals, '_debug', True)
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        ensure_private_runtime()


def test_fresh_context_disables_tracing_and_inherited_callbacks(
    monkeypatch, catalog_projection, sdk
):
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.setenv('LANGCHAIN_TRACING_V2', 'true')
    inherited = ContextVar('private_routing_test_context', default=None)
    inherited_token = inherited.set('synthetic-parent-context')
    callback_token = tracing_v2_callback_var.set(object())

    def inspect_context():
        assert inherited.get() is None
        assert tracing_v2_callback_var.get() is None
        context = get_tracing_context()
        assert context['enabled'] is False
        assert context['parent'] is None
        callbacks = CallbackManager.configure(inheritable_callbacks=[])
        assert callbacks.handlers == []

    sdk.hook = inspect_context
    try:
        _provider().route('concentração fornecedor', catalog_projection)
        assert inherited.get() == 'synthetic-parent-context'
        assert tracing_v2_callback_var.get() is not None
    finally:
        inherited.reset(inherited_token)
        tracing_v2_callback_var.reset(callback_token)
    assert sdk.closed == 1


@pytest.mark.parametrize(
    'extra',
    [
        {'sql': 'SELECT synthetic_secret'},
        {'execution': {'sql': 'SELECT synthetic_secret'}},
        {'workspace': 'synthetic-private-path'},
    ],
)
def test_raw_catalog_or_local_data_is_rejected_before_send(
    catalog_projection, sdk, extra
):
    catalog_projection[0].update(extra)
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', catalog_projection)
    assert sdk.creations == []


def test_unexpected_nested_parameter_metadata_and_duplicate_ids_are_rejected(
    catalog_projection, sdk
):
    invalid = deepcopy(catalog_projection)
    invalid[0]['parameters']['top_n']['sql'] = 'synthetic-secret'
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', invalid)
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', catalog_projection * 2)
    assert sdk.creations == []


@pytest.mark.parametrize(
    'response',
    [
        {
            'status': 'matched',
            'analysis_id': 'invented_analysis',
            'reason': 'catalog_match',
        },
        {
            'status': 'matched',
            'analysis_id': 'supplier_concentration',
            'parameters': {'top_n': True},
            'reason': 'catalog_match',
        },
        {
            'status': 'out_of_catalog',
            'analysis_id': 'supplier_concentration',
            'reason': 'unsupported',
        },
        {
            'status': 'matched',
            'analysis_id': 'supplier_concentration',
            'reason': 'catalog_match',
            'sql': 'SELECT synthetic_secret',
        },
    ],
)
def test_invalid_or_unknown_provider_proposals_fail_closed(
    catalog_projection, sdk, response
):
    sdk.response = response
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', catalog_projection)
    assert sdk.closed == 1


def test_sdk_timeout_is_sanitized_and_client_is_closed(
    catalog_projection, sdk, capsys, caplog
):
    def timeout():
        raise TimeoutError('synthetic-private-prompt ' + _SYNTHETIC_KEY)

    sdk.hook = timeout
    with pytest.raises(
        ProviderFailure, match='provedor de roteamento'
    ) as error:
        _provider().route('concentração fornecedor', catalog_projection)
    assert 'synthetic-private-prompt' not in str(error.value)
    assert _SYNTHETIC_KEY not in str(error.value)
    assert error.value.__suppress_context__
    assert sdk.closed == 1
    assert capsys.readouterr().out == ''
    assert caplog.records == []


def test_backend_recheck_rejects_unexpected_enterprise_client(
    catalog_projection, sdk
):
    sdk.vertexai = True
    with pytest.raises(ProviderFailure, match='provedor de roteamento'):
        _provider().route('concentração fornecedor', catalog_projection)
    assert sdk.invocations == []
    assert sdk.closed == 1
