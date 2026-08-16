"""雷达服务入口。

与主项目 backend/main.py 保持相同形态（python main.py 直接启动），
这样部署脚本和排障习惯不用为这个服务单独记一套。
"""

from __future__ import annotations

import logging

import uvicorn

from radar.api import create_app
from radar.service import RadarService
from radar.settings import load_settings


def main() -> None:
    settings = load_settings()
    service = RadarService(settings)
    app = create_app(service)

    uvicorn.run(
        app,
        host=str(settings.service.get("host", "0.0.0.0")),
        port=int(settings.service.get("port", 8000)),
        log_level=str(settings.observability.get("log_level", "INFO")).lower(),
        # 关掉 access log：采集器每分钟几十条请求日志会把真正有用的
        # 业务日志淹没，而请求指标已经在 /diagnostics 里有结构化统计
        access_log=False,
    )

    # 配置页"保存并重启"：优雅停机已在 lifespan 里完成（写队列排空），
    # 这里用非零码退出，compose 的 on-failure 策略会拉起新进程并
    # 加载新的 overrides。容器健康运行 10 秒后失败计数自动归零，
    # 不会耗尽 on-failure:5 的重启预算
    if service.restart_requested:
        logging.getLogger("radar.main").warning("按管理端请求重启进程（exit 3）")
        raise SystemExit(3)


if __name__ == "__main__":
    logging.captureWarnings(True)
    main()
