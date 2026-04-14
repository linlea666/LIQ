"""
polls — 按领域组织的 Coinglass 数据轮询模块。

每个子模块导出独立的 async 函数，接收 cg_client + state(s) 参数，
不依赖 Engine 实例，可独立测试。
"""
