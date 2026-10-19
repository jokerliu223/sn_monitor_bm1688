#!/bin/bash
# SN 抓拍服务 — SE9 一键部署脚本
# 用法: git clone https://github.com/jokerliu223/sn_monitor_bm1688.git /data/soph_SN/
#       cd /data/soph_SN && sudo ./deploy/init.sh
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ENV_FILE="$(dirname "$0")/sn.env"
SERVICE_DIR="/etc/systemd/system"

echo "============================================"
echo "  SN 抓拍服务 — SE9 一键部署"
echo "============================================"
echo ""

# ── Phase 0: 网络配置与自检 ──
echo "── Phase 0: 网络检查 ──"
ETH0_IP=$(ip -4 -br addr show eth0 2>/dev/null | awk '{print $3}')
ETH1_IP=$(ip -4 -br addr show eth1 2>/dev/null | awk '{print $3}')
ETH0_DHCP=$(ip -4 addr show eth0 2>/dev/null | grep -q dynamic && echo "DHCP" || echo "静态")
echo "  eth0: ${ETH0_IP:-未配置} (${ETH0_DHCP}) — 公司网"
echo "  eth1: ${ETH1_IP:-未配置} — 相机网"

if [ "$ETH0_DHCP" = "DHCP" ]; then
    echo ""
    echo -e "  ${YELLOW}建议配置静态 IP 以确保服务稳定可达。${NC}"
    read -rp "  是否需要配置静态 IP？[Y/n] " cfg_ip
    if [ "${cfg_ip:-y}" != "n" ] && [ "${cfg_ip:-y}" != "N" ]; then
        GW=$(ip route show default 2>/dev/null | awk '{print $3}')
        DNS=$(grep nameserver /etc/resolv.conf 2>/dev/null | awk '{print $2}' | head -1)
        read -rp "  eth0 IP/CIDR [${ETH0_IP:-10.80.40.68/24}]: " e0_ip; e0_ip=${e0_ip:-${ETH0_IP:-10.80.40.68/24}}
        read -rp "  eth0 网关 [${GW:-10.80.40.254}]: " e0_gw; e0_gw=${e0_gw:-${GW:-10.80.40.254}}
        read -rp "  eth0 DNS [${DNS:-10.80.10.10}]: " e0_dns; e0_dns=${e0_dns:-${DNS:-10.80.10.10}}
        read -rp "  eth1 IP/CIDR [${ETH1_IP:-192.168.1.200/24}]: " e1_ip; e1_ip=${e1_ip:-${ETH1_IP:-192.168.1.200/24}}
        echo "  正在写入 /etc/netplan/01-netcfg.yaml ..."
        sudo tee /etc/netplan/01-netcfg.yaml > /dev/null <<NPEOF
network:
  version: 2
  renderer: networkd
  ethernets:
    eth0:
      dhcp4: no
      addresses: [${e0_ip}]
      routes:
        - to: default
          via: ${e0_gw}
      nameservers:
        addresses: [${e0_dns}, 114.114.114.114]
      optional: yes
    eth1:
      dhcp4: no
      addresses: [${e1_ip}]
      optional: yes
NPEOF
        sudo netplan apply 2>/dev/null || echo -e "  ${YELLOW}netplan apply 失败, 请手动检查${NC}"
        echo -e "  ${YELLOW}⚠ 如修改了 eth0, SSH 可能断开, 请用新 IP 重连${NC}"
    fi
fi

echo ""
echo "  自检连通性..."
ping -c1 -W2 192.168.1.9 >/dev/null 2>&1 && echo -e "  ${GREEN}✅ 正面相机(192.168.1.9)${NC}" || echo -e "  ${YELLOW}⚠ 正面相机不通${NC}"
ping -c1 -W2 192.168.1.8 >/dev/null 2>&1 && echo -e "  ${GREEN}✅ 背面相机(192.168.1.8)${NC}" || echo -e "  ${YELLOW}⚠ 背面相机不通(单路可忽略)${NC}"
ping -c1 -W2 10.80.40.57 >/dev/null 2>&1 && echo -e "  ${GREEN}✅ webserver(10.80.40.57)${NC}" || echo -e "  ${YELLOW}⚠ webserver 不通${NC}"

# ── Phase 1: 环境安装 ──
echo ""
echo "── Phase 1: 环境安装 ──"
if [ ! -f "sn_monitor.py" ]; then
    echo -e "  ${RED}sn_monitor.py 不存在，请先 git clone 到 /data/soph_SN/${NC}"
    exit 1
fi

# 检查互联网
ping -c1 -W2 github.com >/dev/null 2>&1 && HAS_NET=1 || { echo -e "  ${YELLOW}⚠ 无互联网，跳过下载步骤${NC}"; HAS_NET=0; }

# opencv-python-headless
pip3 show opencv-python-headless >/dev/null 2>&1 && echo "  opencv-python-headless: 已安装" || {
    echo "  安装 opencv-python-headless ..."
    pip3 install opencv-python-headless || { echo -e "  ${RED}失败，请检查 eth0 是否已联网${NC}"; exit 1; }
}

# sophon-demo + 模型
if [ -d "/data/soph_SN/sophon-demo" ]; then
    echo "  sophon-demo: 已存在, 跳过 clone"
else
    echo "  克隆 sophon-demo ..."
    git clone https://github.com/sophgo/sophon-demo.git /data/soph_SN/sophon-demo || { echo -e "  ${RED}失败，请检查 eth0 是否已联网${NC}"; exit 1; }
fi
MODEL_DIR="/data/soph_SN/sophon-demo/sample/PP-OCR/models/BM1688"
if [ -f "$MODEL_DIR/ch_PP-OCRv4_det_int8_2core.bmodel" ]; then
    echo "  OCR 模型: 已下载"
else
    echo "  下载 OCR 模型 ..."
    cd /data/soph_SN/sophon-demo/sample/PP-OCR
    chmod -R +x scripts/ 2>/dev/null
    ./scripts/download.sh || { echo -e "  ${RED}模型下载失败，请检查 eth0 是否已联网${NC}"; exit 1; }
    cd /data/soph_SN
fi

# Py 依赖
echo "  安装 Python 依赖 ..."
cd /data/soph_SN/sophon-demo/sample/PP-OCR
pip3 install -r python/requirements.txt 2>/dev/null || true
pip3 install dfss 2>/dev/null || true
python3 -m dfss --install sail 2>/dev/null || echo -e "  ${YELLOW}sail 安装跳过(可能已存在)${NC}"
grep -q "sophon-sail" ~/.bashrc 2>/dev/null || echo 'export LD_LIBRARY_PATH=/opt/sophon/sophon-sail/lib/:$LD_LIBRARY_PATH' >> ~/.bashrc
export LD_LIBRARY_PATH=/opt/sophon/sophon-sail/lib/:$LD_LIBRARY_PATH
cd /data/soph_SN

echo -e "  ${GREEN}✅ 环境安装完成${NC}"

# ── Phase 2: 配置 sn.env ──
echo ""
echo "── Phase 2: 配置确认 ──"
if [ -f "$ENV_FILE" ]; then
    echo "  sn.env 已存在，当前配置:"
    cat "$ENV_FILE" | grep -v "^#" | grep -v "^$"
    read -rp "  是否修改？[y/N] " mod
    if [ "${mod:-n}" != "y" ] && [ "${mod:-n}" != "Y" ]; then
        echo "  保持现有配置。"; source "$ENV_FILE" 2>/dev/null
    else
        rm -f "$ENV_FILE"
    fi
fi
if [ ! -f "$ENV_FILE" ]; then
    read -rp "  正面相机 IP [192.168.1.9]: " cam_f; cam_f=${cam_f:-192.168.1.9}
    read -rp "  背面相机 IP (留空=单路) [192.168.1.8]: " cam_b; cam_b=${cam_b:-192.168.1.8}
    read -rp "  webserver IP [10.80.40.57]: " ws_ip; ws_ip=${ws_ip:-10.80.40.57}
    read -rp "  webserver 端口 [8099]: " ws_port; ws_port=${ws_port:-8099}
    read -rp "  预览端口 (子码流 live1 显示) [8091]: " pv_port; pv_port=${pv_port:-8091}
    read -rp "  板型 big/small [big]: " bd_type; bd_type=${bd_type:-big}
    cat > "$ENV_FILE" <<EOF
# SN 抓拍服务 — SE9 侧配置（由 init.sh 生成）
SN_CAM_FRONT=${cam_f}
SN_CAM_BACK=${cam_b}
SN_WEBSERVER_IP=${ws_ip}
SN_WEBSERVER_PORT=${ws_port}
SN_PREVIEW_PORT=${pv_port}
SN_BOARD_TYPE=${bd_type}
EOF
    echo -e "  ${GREEN}已写入 $ENV_FILE${NC}"
fi

# ── Phase 3: 拉流检查 ──
echo ""
echo "── Phase 3: 拉流检查 ──"
source "$ENV_FILE" 2>/dev/null
check_rtsp() {
    local ip=$1 name=$2
    timeout 3 bash -c "cat < /dev/null > /dev/tcp/$ip/8554" 2>/dev/null && \
        echo -e "  ${GREEN}✅ ${name}(${ip}) RTSP 可达${NC}" || \
        echo -e "  ${RED}❌ ${name}(${ip}) RTSP 不可达 — 请确认摄像头 IP/供电，重启摄像头后重试${NC}"
}
check_rtsp "${SN_CAM_FRONT:-192.168.1.9}" "正面"
[ -n "${SN_CAM_BACK:-}" ] && check_rtsp "${SN_CAM_BACK}" "背面"

# ── Phase 4: 安装 unit + 启动 ──
echo ""
echo "── Phase 4: 安装 systemd 单元 ──"
DEPLOY_DIR="$(dirname "$0")"
for f in "$DEPLOY_DIR"/*.service; do
    sudo cp "$f" "$SERVICE_DIR/"
    echo "  安装: $(basename $f)"
done
sudo systemctl daemon-reload

if [ "${SN_BOARD_TYPE:-big}" = "big" ]; then
    UNIT="sn-monitor-big"
else
    UNIT="sn-monitor"
fi
echo "  启动 $UNIT ..."
sudo systemctl enable --now "$UNIT" 2>/dev/null
sleep 5
if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    echo -e "  ${GREEN}✅ $UNIT 已启动${NC}"
else
    echo -e "  ${RED}❌ $UNIT 启动失败${NC}"
    systemctl status "$UNIT" --no-pager -l 2>/dev/null | tail -10
    journalctl -u "$UNIT" --no-pager -n 10 2>/dev/null
fi

# ── Phase 5: 验证 ──
echo ""
echo "── Phase 5: 验证 ──"
sleep 3
PV_PORT="${SN_PREVIEW_PORT:-8091}"
curl -s -m5 -o /dev/null -w "  预览 :${PV_PORT}/status → HTTP %{http_code}\n" "http://127.0.0.1:${PV_PORT}/status" 2>/dev/null || echo -e "  ${YELLOW}⚠ 预览端口未响应${NC}"
echo ""
echo "============================================"
echo -e "  ${GREEN}部署完成！${NC}"
echo "  后续操作:"
echo "    · 更新代码:  cd /data/soph_SN && git pull && sudo systemctl restart $UNIT"
echo "    · 查看日志:  journalctl -u $UNIT -f"
echo "    · 修改配置:  sudo vi $ENV_FILE"
echo "============================================"