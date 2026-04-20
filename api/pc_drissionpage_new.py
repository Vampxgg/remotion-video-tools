import json
import random
import time
from DrissionPage import ChromiumPage
from DrissionPage.common import By
from DrissionPage.common import Keys
import csv

from utils.settings import settings as _settings


class Zhilian():
    def __init__(self):

        self.url = _settings.ZHAOPIN_LIST_URL  # 访问页面

        self.page = ChromiumPage(_settings.DRISSION_BROWSER_HOST_PORT)
        self.api_url = '/c/i/search/positions?'  ##接口监听接口

        '''drissionpage 监听接口'''
        self.page.listen.start(self.api_url)

        self.keyword_list = ['大数据']  # 搜索关键词
        '''创建csv文件'''
        self.csv_file = open('智联招聘.csv', 'w', encoding='utf-8-sig', newline='')
        self.csv_writer = csv.DictWriter(
            self.csv_file,
            fieldnames=['岗位名称', '岗位薪资', '岗位标签', '岗位福利', '工作省份', '工作地点', '工作经验', '学历要求',
                        '公司名称', '公司规模', '公司类型', '公司行业'])
        self.csv_writer.writeheader()

    def goto_html(self):
        self.page.get(self.url)

    def input_keyword(self, keyword):
        search_input_obj = self.page.ele((By.XPATH, '//div[@class="query-search__content-input__wrap"]/input'))
        search_input_obj.clear()
        time.sleep(random.uniform(0.5, 1))
        search_input_obj.input(keyword)
        time.sleep(random.uniform(0.5, 1))
        search_input_obj.input(Keys.ENTER)

    def get_province(self, province):
        area_obj = self.page.ele((By.XPATH, '//div[@class="content-s"]/div[1]'))
        area_obj.click()
        time.sleep(random.uniform(0.5, 1))
        area_input_obj = self.page.ele((By.XPATH, '//div[@class="query-other-city"]/input'))

        area_input_obj.input(province)

        if province in ['吉林', '海南']:
            self.page.ele((By.XPATH, f'//ul[@class="query-other-city__list"]/li[contains(.,"{province}")]'),
                          timeout=10).click()
        else:
            self.page.ele((By.XPATH, '//ul[@class="query-other-city__list"]/li[1]'), timeout=10).click()
            # area_input_obj.input(Keys.ENTER)  # 执行回车

    def drop_down(self):
        for x in range(1, 10, 3):  # 1, 3, 5, 7, 9
            j = x / 9  # 计算滚动比例
            js = 'document.documentElement.scrollTop = document.documentElement.scrollHeight * %f' % j
            self.page.run_js(js)  # 执行 JavaScript 滑动操作
            time.sleep(random.uniform(1, 2))  # 等待页面加载

    def get_data(self, item, province):
        json_data = item
        print(json_data)
        if json_data:

            try:
                for data in json_data['data']['list']:
                    job_name = data.get('name')  # 岗位名称
                    salary = data.get('salary60')  # 岗位薪资
                    jobSkillTags = [i.get('name') for i in data.get('jobSkillTags')] if data.get(
                        'jobSkillTags') else []  # 岗位标签
                    job_WelfareTags = data.get('jobKnowledgeWelfareFeatures')  # 岗位福利

                    work_province = province  # 工作省份
                    work_area = json.loads(data.get('cardCustomJson'))['address']  # 工作地点

                    work_experience = data.get('workingExp')  # 工作经验
                    work_education = data.get('education')  # 学历要求
                    company_name = data.get('companyName')  # 公司名称
                    company_size = data.get('companySize')  # 公司规模
                    company_type = data.get('propertyName')  # 公司类型
                    company_industry = data.get('industryName')  # 公司行业

                    dic = {
                        '岗位名称': job_name,
                        '岗位薪资': salary,
                        '岗位标签': jobSkillTags,
                        '岗位福利': job_WelfareTags,
                        '工作省份': work_province,
                        '工作地点': work_area,
                        '工作经验': work_experience,
                        '学历要求': work_education,
                        '公司名称': company_name,
                        '公司规模': company_size,
                        '公司类型': company_type,
                        '公司行业': company_industry,
                    }
                    self.csv_writer.writerow(dic)
                    # print(dic)
            except Exception as e:
                print('json解析错误', e)

    def get_next(self):
        next_flag = self.page.wait.eles_loaded((By.XPATH, '//a[@class="btn soupager__btn"]'), timeout=2)
        if next_flag:
            self.page.ele((By.XPATH, '//a[@class="btn soupager__btn"]')).click()
            time.sleep(random.uniform(1, 2))
            return True
        else:
            return False

    def main(self):
        self.goto_html()
        time.sleep(1)
        for keyword in self.keyword_list:
            for province in province_list:
                # 先选省份（确保搜索会带上省份条件）
                self.get_province(province)
                time.sleep(1)

                # 清除之前的监听记录，避免取到历史请求
                try:
                    self.page.listen.clear()
                except Exception:
                    pass

                # 触发搜索（输入关键词并回车）
                self.input_keyword(keyword)
                time.sleep(0.5)

                current_page = 1
                # 使用 steps() 遍历新产生的请求，注意只处理匹配 self.api_url 的项
                for item in self.page.listen.steps():
                    # 过滤：只关心目标接口
                    if self.api_url not in item.url:
                        continue

                    # 确保有响应体
                    body = item.response.body
                    if not body:
                        continue

                    # body 可能已经是 dict，也可能是 str/bytes
                    if isinstance(body, dict):
                        json_data = body
                    elif isinstance(body, (bytes, bytearray)):
                        json_data = json.loads(body.decode('utf-8', errors='ignore'))
                    else:
                        json_data = json.loads(body)

                    # 解析并写入 CSV
                    self.get_data(json_data, province)

                    print(f'-----------------采集完成--{keyword}--{province}--{current_page}页-------------')
                    break
                    # 处理完当前页后尝试翻页
                    current_page += 1
                    if self.get_next():
                        # 等待下一页接口并继续监听（清空旧的监听记录以免重复）
                        try:
                            self.page.listen.clear()
                        except Exception:
                            pass
                        # 等待下一页的接口响应（你也可以再次使用 steps() 循环）
                        continue
                    else:
                        break  # 没有下一页，退出分页

        print('采集结束')
        self.csv_file.close()


if __name__ == '__main__':
    province_list = [
        "深圳"
    ]

    zhilian = Zhilian()
    zhilian.main()
