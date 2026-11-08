"""Cross-platform business labels must resolve to native codes, never raw Chinese."""
import pytest
from utils import boss_filters, zhilian_filters


@pytest.mark.parametrize('label,zhilian,boss', [
    ('机械设备/机电/重工', '500140000,500220000', '100901'),
    ('仪器仪表/工业自动化', '500180000,500230000', '100902'),
    ('原材料及加工/模具', '500090000', '100903'),
    ('汽车研发/制造', '500110000', '100801'),
    ('汽车零部件', '1600020000', '100802'),
    ('电子/半导体/集成电路', '100010000,100000240,100000230', '101407'),
    ('计算机服务', '100040000', '100023'),
    ('电力/热力/燃气/水利', '1100010000', '101205'),
    ('人工智能', '100080000', '100028'),
])
def test_equipment_industry_native_codes(label, zhilian, boss):
    assert zhilian_filters.build_filters({'industry': label}) == {'S_SOU_JD_INDUSTRY_LEVEL': zhilian}
    assert boss_filters.build_filters({'industry': label}) == {'industry': boss}


def test_ai_and_hardware_filters_keep_both_sources_native():
    labels = '人工智能,仪器仪表/工业自动化,计算机软件,计算机服务,计算机硬件'
    zp = zhilian_filters.build_filters({'industry': labels})['S_SOU_JD_INDUSTRY_LEVEL']
    boss = boss_filters.build_filters({'industry': labels})['industry']
    assert set(zp.split(',')) == {'100080000','500180000','500230000','100040000','100060000'}
    assert len(zp.split(',')) == len(set(zp.split(',')))
    assert set(boss.split(',')) == {'100028','100902','100021','100023','101404'}
    assert all(code.isdigit() for code in zp.split(',') + boss.split(','))
