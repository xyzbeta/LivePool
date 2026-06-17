#!/bin/bash
# =============================================================================
# LivePool 构建与部署脚本
# 使用方式: ./build.sh [服务器IP] [SSH端口]
# 示例:     ./build.sh 10.192.172.2 40022
#
# 前提: 服务器已安装 docker + docker compose
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
$SCP_CMD docker-compose.yml config.yaml root@$SERVER_IP:/serverhub/livepool/

echo ""
echo "========================================"
echo " 4/4  服务器加载镜像并重启"
echo "========================================"
$SSH_CMD root@$SERVER_IP "
  cd /serverhub/livepool
  docker load -i livepool-image.tar
  rm -f livepool-image.tar
  docker compose up -d
  echo ''
  echo '等待健康检查...'
  for i in \$(seq 1 10); do
    sleep 3
    STATUS=\$(curl -sf http://localhost:8008/api/health 2>/dev/null && echo ok || echo pending)
    echo \"  [\${i}] health: \$STATUS\"
    [ \"\$STATUS\" = \"ok\" ] && break
  done
  echo ''
  docker ps --filter name=livepool --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
"

rm -f /tmp/livepool-image.tar
echo ""
echo "✅ 部署完成"
