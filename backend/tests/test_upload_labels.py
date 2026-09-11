"""Label reservation uses fake Redis and public metadata, never real accounts."""
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis
import pytest
from app import upload_templates
from app.models_channels import Task
from app.upload_templates import batch_group_tag, new_upload_group_tag, upload_type_code


@pytest.mark.parametrize(('family', 'kind', 'wire', 'expected'), [
    ('AWS', 'aws_bedrock', 33, 'AWS-bedrock'),
    ('AWS', 'aws_ak_sk', 33, 'AWS-bedrock'),
    ('AWS', 'aws_api_key', 33, 'AWS-bedrock'),
    ('AWS', 'aws_claude', 14, 'AWS-claude'),
    ('Azure', 'azure_gpt', 3, 'Azure-openai'),
    ('Azure', 'api_key', 3, 'Azure-openai'),
    ('Azure', 'azure_claude', 14, 'Azure-claude'),
    ('Google', 'api_key', 24, 'Google-ai-studio'),
    ('Google', 'vertex_gemini', 41, 'Google-vertex-gemini'),
    ('Google', 'vertex_claude', 41, 'Google-vertex-claude'),
    ('Anthropic', 'api_key', 14, 'Anthropic-api-key'),
    ('OpenAI', 'api_key', 1, 'OpenAI-api-key'),
    ('OpenRouter', 'api_key', 20, 'OpenRouter-api-key'),
    ('OpenCode', 'api_key', 14, 'OpenCode-api-key'),
])
def test_type_code_uses_business_service(family, kind, wire, expected):
    assert upload_type_code(SimpleNamespace(family=family),
                            SimpleNamespace(schema_config={'type': kind, 'remote_type': wire})) == expected


@pytest.fixture
def label_state(monkeypatch):
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('app.auth.get_redis', lambda: redis)
    monkeypatch.setattr(upload_templates, 'utcnow', lambda: datetime(2026, 9, 9, 16, 0, 1, tzinfo=UTC).replace(tzinfo=None))
    return SimpleNamespace(redis=redis, db=SimpleNamespace(scalar=lambda query: None),
                           user=SimpleNamespace(id='user-id', display_id=12, username='never-in-label'),
                           category=SimpleNamespace(id='category-id', family='AWS'),
                           fmt=SimpleNamespace(schema_config={'type': 'aws_claude', 'remote_type': 14}))


def test_label_freezes_beijing_time_and_is_cached_across_days(label_state, monkeypatch):
    case = label_state
    monkeypatch.setattr(upload_templates.secrets, 'choice', lambda alphabet: 'z')
    first = batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt)
    assert first == '12-AWS-claude-20260910-00:00:01-zzzz'
    monkeypatch.setattr(upload_templates, 'utcnow', lambda: datetime(2026, 10, 1, tzinfo=UTC).replace(tzinfo=None))
    case.user.username = 'renamed'
    assert batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt) == first
    cache_key = next(case.redis.scan_iter('upload-label:v3:*'))
    assert 0 < case.redis.ttl(cache_key) <= upload_templates.LABEL_CACHE_SECONDS
    assert len(first) <= 80


def test_reservation_retries_existing_and_concurrent_tag_collisions(label_state, monkeypatch):
    case = label_state
    suffixes = iter('aaaabbbbcccc')
    monkeypatch.setattr(upload_templates.secrets, 'choice', lambda alphabet: next(suffixes))
    case.db.scalar = lambda query: 'existing-group-id' if query.whereclause.right.value.endswith('aaaa') else None
    case.redis.set('upload-label-tag:v3:12-AWS-claude-20260910-00:00:01-bbbb', 'someone-else', ex=60)
    assert new_upload_group_tag(case.db, case.user, case.category, case.fmt).endswith('-cccc')
    assert case.redis.get('upload-label-tag:v3:12-AWS-claude-20260910-00:00:01-bbbb') == 'someone-else'


def test_same_batch_race_returns_existing_winner_without_overwriting(label_state, monkeypatch):
    case = label_state
    winner = '12-AWS-claude-20260910-00:00:01-win1'
    def racing_call(method, *args, **kwargs):
        if method == 'set' and args[0].startswith('upload-label:v3:'):
            case.redis.set(args[0], winner, ex=60)
            return False
        return getattr(case.redis, method)(*args, **kwargs)
    monkeypatch.setattr(upload_templates, 'redis_call', racing_call)
    assert batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt) == winner
    assert case.redis.get(next(case.redis.scan_iter('upload-label:v3:*'))) == winner


def test_submitted_legacy_tag_precedes_cache_and_new_format(label_state, monkeypatch):
    case = label_state
    def prior(query):
        assert query.column_descriptions[0]['entity'] is Task
        return SimpleNamespace(snapshot={'group_tag': 'original-user-AWS-old-hash'})
    case.db.scalar = prior
    monkeypatch.setattr(upload_templates, 'redis_call', lambda *args, **kwargs: pytest.fail('No cache needed for history'))
    assert batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt) == 'original-user-AWS-old-hash'


def test_expired_unsubmitted_cache_regenerates_without_database_writes(label_state, monkeypatch):
    case = label_state
    suffixes = iter('aaaabbbb')
    monkeypatch.setattr(upload_templates.secrets, 'choice', lambda alphabet: next(suffixes))
    first = batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt)
    case.redis.delete(*case.redis.scan_iter('upload-label:v3:*'))
    second = batch_group_tag(case.db, case.user, case.category, 'batch-token', case.fmt)
    assert first.endswith('-aaaa') and second.endswith('-bbbb')
