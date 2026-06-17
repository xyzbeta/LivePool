#!/bin/bash
# =============================================================================
# LivePool 生产部署脚本
# 使用方式: ./run.sh [命令]
#   启动:   ./run.sh start
#   停止:   ./run.sh stop
#   重启:   ./run.sh restart
#   日志:   ./run.sh logs
# =============================================================================
set -e

NAME="livepool"
IMAGE="livepool:latest"
CONFIG_DIR="/serverhub/livepool"
PORT="8008"

case "${1:-start}" in
  start)
    echo "启动 ${NAME}..."
    docker run -d \
      --name ${NAME} \
      --restart unless-stopped \
      -p ${PORT}:${PORT} \
      -v ${CONFIG_DIR}/config.yaml:/app/config.yaml:ro \
      -v ${CONFIG_DIR}/data:/app/data \
      -e TZ=Asia/Shanghai \
      -e JWT_SECRET="${JWT_SECRET:-}" \
      ${IMAGE}
    echo "等待健康检查..."
    for i in $(seq 1 10); do
      sleep 3
      STATUS=$(curl -sf http://localhost:${PORT}/api/health 2>/dev/null && echo ok || echo pending)
      echo "  [$i] health: $STATUS"
      [ "$STATUS" = "ok" ] && break
    done
    docker ps --filter name=${NAME} --format 'table {{.Names}}\t{{.Status}}'
    ;;
  stop)
    echo "停止 ${NAME}..."
    docker stop ${NAME} 2>/dev/null || true
    docker rm ${NAME} 2>/dev/null || true
    echo "已停止"
    ;;
  restart)
    $0 stop
    $0 start
    ;;
  logs)
    docker logs -f ${NAME}
    ;;
  *)
    echo "用法: $0 {start|stop|restart|logs}"
    exit 1
    ;;
esac
