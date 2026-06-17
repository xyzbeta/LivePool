#!/bin/bash
# =============================================================================
# LivePool 构建与部署脚本
# 使用方式: ./build.sh [服务器IP] [SSH端口]
# 示例:     ./build.sh 10.192.172.2 40022
#
# 前提:
#   本地: 已安装 docker
#   服务器: 已安装 docker，/serverhub/livepool/ 下有 config.yaml + data/
# =============================================================================
set -e

SERVER_IP="${1:-10.192.172.2}"
SSH_PORT="${2:-40022}"
SSH_CMD="ssh -p $SSH_PORT -o StrictHostKeyChecking=no"
SCP_CMD="scp -P $SSH_PORT -o StrictHostKeyChecking=no"

echo "========================================"
echo " 1/4  构建 Docker 镜像"
echo "========================================"
docker build -t livepool:latest .

echo ""
echo "========================================"
echo " 2/4  导出镜像并传输到服务器"
echo "========================================"
docker save livepool:latest -o /tmp/livepool-image.tar
echo "镜像大小: $(du -h /tmp/livepool-image.tar | cut -f1)"
$SCP_CMD /tmp/livepool-image.tar root@$SERVER_IP:/serverhub/livepool/

echo ""
echo "========================================"
echo " 3/4  同步配置文件"
echo "========================================"
$SCP_CMD config.yaml run.sh root@$SERVER_IP:/serverhub/livepool/

echo ""
echo "========================================"
echo " 4/4  服务器加载镜像并重启"
echo "========================================"
$SSH_CMD root@$SERVER_IP "
  set -e
  cd /serverhub/livepool

  # 迁移旧数据到扁平结构
  if [ -f data/output/live.m3u8 ] && [ ! -f data/live.m3u8 ]; then
    echo '迁移数据到扁平结构...'
    [ -f data/logs/app.log ] && mv data/logs/app.log data/app.log 2>/dev/null || true
    [ -d data/output/by_group ] && mv data/output/by_group data/by_group 2>/dev/null || true
    [ -d data/output/logos ] && mv data/output/logos data/logos 2>/dev/null || true
    [ -f data/output/live.m3u8 ] && mv data/output/live.m3u8 data/live.m3u8 2>/dev/null || true
    rm -rf data/output data/logs 2>/dev/null || true
    mkdir -p data/by_group data/logos data/sources
  fi

  # 加载新镜像
  docker load -i livepool-image.tar
  rm -f livepool-image.tar

  # 停止旧容器（兼容 docker-compose 遗留）
  docker stop livepool 2>/dev/null || true
  docker rm livepool 2>/dev/null || true

  # 启动新容器
  chmod +x run.sh
  JWT_SECRET=\$(cat .env 2>/dev/null | grep JWT_SECRET | cut -d= -f2) ./run.sh start
"

rm -f /tmp/livepool-image.tar
echo ""
echo "✅ 部署完成"
