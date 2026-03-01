# import requests
# import threading
# import time
# import json
# import uuid
#
# # --- 1. 配置参数 (请根据您的需求修改这里) ---
#
# # API接口信息
# API_URL = "http://119.45.167.133:5125/v1/chat-messages"
# API_KEY = "app-b10Kwt4ONZIzBmMbM90gTdrA"
#
# # 并发测试参数
# TOTAL_REQUESTS = 50  # 总共要发送的请求数
# CONCURRENCY = 50  # 并发数 (同时运行的线程数)
#
# # --- 2. 请求数据定义 ---
#
# # HTTP Headers
# headers = {
#     'Authorization': f'Bearer {API_KEY}',
#     'Content-Type': 'application/json'
# }
#
#
# # 定义一个函数来生成每次请求的数据负载 (Payload)
# # 这样可以确保每个请求的数据可以是动态的，例如拥有不同的 user_id
# def generate_payload(user_id):
#     """
#     生成API请求的JSON数据。
#     请在这里填入 `inputs` 对象的真实有效值。
#     """
#     payload = {
#         # inputs 对象是您应用的核心输入参数
#         "inputs": {
#             # --- 必填参数 ---
#             "select_knowledge_unit": "理想L9安装维修教程",  # (必填) 请替换为有效的知识库单元ID
#             "is_single_file_processing": False,
#             "smart_search": True,  # (必填) 布尔值，根据您的逻辑填写 True 或 False
#
#             # --- 可选参数 (如果不需要可以留空或删除) ---
#             "prompt": "图片多一点",
#             "database_id": "c3517663-17cc-43ce-8c14-873ac6d7c9f4",
#             "docCategory": "electronics",
#             "all_knowledge_units": "",
#             "docTemplate": "template_A",
#             "large_text": "",
#             "career_positions": ""
#         },
#         "query": "测试_001",
#         "response_mode": "streaming",  # Dify接口支持 streaming(流式) 或 blocking(阻塞)
#         "conversation_id": "",  # 如果需要连续对话，请传入之前的对话ID
#         "user": user_id,  # 使用动态生成的user_id来模拟不同用户
#         "files": [
#             # 如果您的场景需要文件，请保留。如果不需要，可以将 "files": [] 或完全删除此字段
#         ]
#     }
#     return payload
#
#
# # --- 3. 执行并发请求的核心逻辑 ---
#
# # 存储成功和失败的请求计数
# success_count = 0
# failure_count = 0
# # 线程锁，用于安全地更新计数器
# lock = threading.Lock()
#
#
# def send_request(session):
#     """
#     单个请求的发送函数，由每个线程执行。
#     """
#     global success_count, failure_count
#
#     # 为每个请求生成一个唯一的用户ID，模拟真实场景
#     user_id = f"test_user_{uuid.uuid4()}"
#     request_data = generate_payload(user_id)
#
#     try:
#         # 使用 session 对象发送请求可以复用TCP连接，性能更好
#         response = session.post(API_URL, headers=headers, json=request_data, timeout=60)  # 设置60秒超时
#
#         # 对于流式响应，我们只需要检查状态码，并可以读取部分内容来确认
#         if response.status_code == 200:
#             # 流式响应需要迭代处理
#             # for chunk in response.iter_lines():
#             #     pass # 在压测中，我们通常不关心完整内容，只关心连接是否成功
#             with lock:
#                 success_count += 1
#             # print(f"请求成功: User={user_id}, Status={response.status_code}")
#         else:
#             with lock:
#                 failure_count += 1
#             print(f"请求失败: User={user_id}, Status={response.status_code}, Response={response.text[:200]}")
#
#     except requests.exceptions.RequestException as e:
#         with lock:
#             failure_count += 1
#         print(f"请求异常: User={user_id}, Error={e}")
#
#
# def main():
#     """
#     主函数，用于启动并发测试
#     """
#     print(f"开始压力测试...")
#     print(f"总请求数: {TOTAL_REQUESTS}, 并发数: {CONCURRENCY}")
#
#     start_time = time.time()
#
#     threads = []
#     # 使用 requests.Session() 来管理连接池
#     with requests.Session() as session:
#         for i in range(TOTAL_REQUESTS):
#             # 创建线程
#             thread = threading.Thread(target=send_request, args=(session,))
#             threads.append(thread)
#
#             # 启动线程
#             thread.start()
#
#             # 控制并发数：当达到并发上限时，等待一个线程结束后再启动下一个
#             if len(threads) % CONCURRENCY == 0:
#                 for t in threads:
#                     t.join()  # 等待这一批线程全部执行完毕
#                 threads = []
#
#     # 等待最后一批不足并发数的线程执行完毕
#     for thread in threads:
#         thread.join()
#
#     end_time = time.time()
#     duration = end_time - start_time
#
#     print("\n--- 测试结果 ---")
#     print(f"测试总耗时: {duration:.2f} 秒")
#     print(f"成功请求数: {success_count}")
#     print(f"失败请求数: {failure_count}")
#     if duration > 0:
#         rps = TOTAL_REQUESTS / duration
#         print(f"平均每秒请求数 (RPS): {rps:.2f}")
#     print("----------------")
#
#
# if __name__ == "__main__":
#     main()
