import json
from pathlib import Path
import pytest
from utils import boss_filters,zhilian_filters
from utils.filter_errors import InvalidIndustryFilter

FIXTURE=json.loads((Path(__file__).parent/'fixtures/industry_dictionary_20261108.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('source,mapper',[('boss',boss_filters),('zhilian',zhilian_filters)])
def test_every_current_industry_label_and_code_matches_browser(source,mapper):
    for label,code in FIXTURE['labels'][source].items():
        assert mapper.map_value('industry',label)==code
        assert mapper.map_value('industry',code)==code
    for label,codes in mapper._load()['industry']['options'].items():
        assert set(codes.split(',')) <= set(FIXTURE['labels'][source].values())

@pytest.mark.parametrize('code',FIXTURE['zhilian_previous_unlisted_codes'])
def test_obsolete_zhilian_codes_are_rejected(code):
    with pytest.raises(InvalidIndustryFilter):
        zhilian_filters.map_value('industry',code)

@pytest.mark.parametrize('label',FIXTURE['zhilian_retired_labels'])
def test_unproven_historical_label_is_not_guessed(label):
    with pytest.raises(InvalidIndustryFilter):
        zhilian_filters.map_value('industry',label)
