from unittest.mock import AsyncMock
import pytest
from utils import boss_filters, zhilian_filters
from utils.filter_errors import InvalidIndustryFilter
from api import jobs_region


@pytest.mark.parametrize('mapper',[boss_filters,zhilian_filters])
@pytest.mark.parametrize('value',['9999999999','不存在的行业','人工智能,不存在的行业'])
def test_unknown_industry_is_not_silently_forwarded(mapper,value):
    with pytest.raises(InvalidIndustryFilter):
        mapper.build_filters({'industry':value})


def test_zhilian_native_semicolon_multiselect_is_normalized():
    assert zhilian_filters.map_value('industry','500230000;500180000,500230000')=='500230000,500180000'
    assert zhilian_filters.map_value('industry','-1') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('source,runner',[
    ('zhilian',jobs_region._run_zhilian),('boss_zhipin',jobs_region._run_boss),
])
async def test_invalid_filter_is_nonretryable_and_does_not_touch_pipeline(monkeypatch,source,runner):
    def forbidden():
        pytest.fail('must validate before initializing the source client')
    proxy=AsyncMock(side_effect=AssertionError('must not call source proxy'))
    monkeypatch.setattr(jobs_region,'get_zhilian_client',forbidden)
    monkeypatch.setattr(jobs_region,'_get_boss_region_client',forbidden)
    monkeypatch.setattr(jobs_region,'_run_boss_via_service',proxy)
    p=jobs_region.RegionJobSearchPayload.model_validate({
        'region':{'city':'深圳'},'query':{'keywords':['工程师'],'filters':{'industry':'9999999999'}},
        'sources':[source],
    })
    result=await runner(p)
    assert not result.ok and result.retryable is False
    assert result.error_code=='invalid_industry_filter' and result.jobs==[]
    proxy.assert_not_awaited()
