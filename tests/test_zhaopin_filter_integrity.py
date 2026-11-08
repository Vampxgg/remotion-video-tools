"""Do not turn a filtered search failure into unfiltered jobs or a successful empty result."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from services import zhaopin_client as module


def client(monkeypatch, result=None, error=None, *, direct=True, fallback=True):
    monkeypatch.setattr(module._settings, 'JOB_SEARCH_V2_DIRECT_ENABLED', direct)
    monkeypatch.setattr(module._settings, 'JOB_SEARCH_V2_BROWSER_FALLBACK_ENABLED', fallback)
    browser=SimpleNamespace(navigate_and_listen=AsyncMock(return_value={'data':{'list':[{'name':'unfiltered poison'}]}}))
    c=module.ZhaopinSearchClient(browser)
    c._direct=SimpleNamespace(search_positions=AsyncMock(return_value=result,side_effect=error))
    return c,browser


@pytest.mark.asyncio
@pytest.mark.parametrize('result,error',[
    (None,RuntimeError('transport failed')),
    (([],{'count':None,'isEndPage':None,'isVerification':0}),None),
    (([{'name':'partial'}],{'count':1,'isEndPage':1,'isVerification':1}),None),
])
async def test_filtered_failure_never_returns_browser_jobs(monkeypatch,result,error):
    c,b=client(monkeypatch,result,error)
    with pytest.raises(RuntimeError,match='丢失过滤条件'):
        await c.search_positions('工程师','765',filters={'S_SOU_JD_INDUSTRY_LEVEL':'500230000'})
    b.navigate_and_listen.assert_not_awaited()


@pytest.mark.asyncio
async def test_filtered_browser_only_mode_fails_closed(monkeypatch):
    c,b=client(monkeypatch,direct=False)
    with pytest.raises(RuntimeError,match='丢失过滤条件'):
        await c.search_positions('工程师','765',filters={'S_SOU_JD_INDUSTRY_LEVEL':'500230000'})
    b.navigate_and_listen.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('meta',[
    {'count':0,'isEndPage':0,'isVerification':0},
    {'count':'0','isEndPage':'0','isVerification':'0'},
    {'count':None,'isEndPage':1,'isVerification':0},
])
async def test_confirmed_empty_result_keeps_filter_and_does_not_fallback(monkeypatch,meta):
    c,b=client(monkeypatch,([],meta))
    assert await c.search_positions('工程师','765',filters={'S_SOU_JD_INDUSTRY_LEVEL':'500230000'})==([],meta)
    b.navigate_and_listen.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_fallback_does_not_mask_transport_error(monkeypatch):
    c,b=client(monkeypatch,error=RuntimeError('network unavailable'),fallback=False)
    with pytest.raises(RuntimeError,match='回退已关闭'):
        await c.search_positions('工程师','765')
    b.navigate_and_listen.assert_not_awaited()


@pytest.mark.asyncio
async def test_unfiltered_browser_fallback_still_works(monkeypatch):
    c,b=client(monkeypatch,error=RuntimeError('network unavailable'))
    jobs,_=await c.search_positions('工程师','765')
    assert jobs==[{'name':'unfiltered poison'}]
    b.navigate_and_listen.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_propagates_through_region_pipeline(monkeypatch):
    from api import jobs_region
    c,b=client(monkeypatch,error=RuntimeError('network unavailable'))
    monkeypatch.setattr(jobs_region,'get_zhilian_client',lambda:c)
    payload=jobs_region.RegionJobSearchPayload.model_validate({
        'region':{'city':'深圳'},'query':{'keywords':['工程师'],'filters':{'industry':'自动化设备'}},
        'sources':['zhilian'],'collection':{'max_pages_per_source':1,'detail_level':'summary'},
    })
    result=await jobs_region._run_zhilian(payload)
    assert result.ok is False and result.error_code=='zhilian_all_failed' and result.jobs==[]
    b.navigate_and_listen.assert_not_awaited()
