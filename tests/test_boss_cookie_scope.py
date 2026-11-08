from unittest.mock import Mock
import httpx
from services.boss_zhipin_client import _DirectBossSession


def test_session_only_requests_cookies_for_current_boss_page(monkeypatch):
    monkeypatch.setattr('services.boss_zhipin_client.time.sleep',lambda _:None)
    tab=Mock()
    tab.cookies.return_value=[{'name':'__zp_stoken__','value':'dummy','domain':'.zhipin.com'}]
    tab.run_js.return_value='test-agent'
    with httpx.Client() as http:
        session=_DirectBossSession(tab,http)
        session._refresh_cookies()
        tab.cookies.assert_called_once_with(all_domains=False)
        assert http.cookies.get('__zp_stoken__')=='dummy'
