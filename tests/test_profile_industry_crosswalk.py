"""Industry mappings checked against browser categories and authenticated searches."""
import json
from pathlib import Path
import pytest
from utils import boss_filters, zhilian_filters

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/industry_dictionary_20261106.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('label,expected', [
    ('新能源汽车', '500110000'),
    ('汽车智能网联', '1600000400'),
    ('汽车后市场', '1600000500'),
    ('汽车经销商', '1600010000'),
    ('动力电池', '1900020000'),
    ('物联网', '100180000'),
    ('通信/网络设备', '100090000'),
    ('财务/审计/税务', '800010000'),
    ('电子商务', '100020000'),
    ('食品/饮料/烟酒', '500100000'),
    ('检测/认证/知识产权', '800060000'),
])
def test_missing_zhilian_labels_use_documented_industry_codes(label, expected):
    actual = zhilian_filters.build_filters({'industry': label})
    assert actual == {'S_SOU_JD_INDUSTRY_LEVEL': expected}
    assert set(expected.split(',')) <= set(FIXTURE['labels']['zhilian'].values())
    assert boss_filters.map_value('industry', label) == FIXTURE['labels']['boss'][label]

@pytest.mark.parametrize('label,targets', [
    ('机械设备/机电/重工', ['通用设备', '专用设备']),
    ('仪器仪表/工业自动化', ['仪器仪表', '自动化设备']),
    ('原材料及加工/模具', ['金属制品']),
    ('电子/半导体/集成电路', ['半导体/芯片','电子/硬件开发','计算机/通信/其他电子设备']),
    ('4S店/后市场', ['汽车经销商','汽车后市场']),
])
def test_original_boss_categories_keep_their_native_codes(label, targets):
    assert boss_filters.map_value('industry', label) == FIXTURE['labels']['boss'][label]
    result = boss_filters.map_value('industry', ','.join([label, *targets]))
    assert result.split(',') == [FIXTURE['labels']['boss'][label], *[FIXTURE['labels']['boss'][target] for target in targets]]

@pytest.mark.parametrize('source,mapper,field', [
    ('boss',boss_filters,'industry'),
    ('zhilian',zhilian_filters,'S_SOU_JD_INDUSTRY_LEVEL'),
])
def test_configured_profiles_only_emit_dictionary_native_codes(source, mapper, field):
    for industry in FIXTURE['profile_industries']:
        result = mapper.build_filters({'industry': industry})
        assert set(result) == {field}
        codes = result[field].split(',')
        assert len(codes) == len(set(codes))
        assert set(codes) <= set(FIXTURE['labels'][source].values())
        assert all(mapper._load()['industry']['options'].get(label) for label in industry.split(','))
