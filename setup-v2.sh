#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $1"; }
success() { echo -e "${GREEN}[OK]${NC}    $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error()   { echo -e "${RED}[ERR]${NC}   $1"; exit 1; }
step()    { echo -e "\n${BOLD}${CYAN}▶ $1${NC}"; }

COMPOSE_CMD="docker compose"
ENV_FILE=".env"

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   DashScope Proxy V2 — 独立目录部署     ║${NC}"
echo -e "${BLUE}║   （不影响已运行的旧版本服务）           ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════╝${NC}"
echo ""

[ -f "docker-compose.yml" ] || error "请在项目根目录（含 docker-compose.yml）下运行此脚本"

# ── 检查系统依赖 ────────────────────────────────
step "检查系统依赖"
command -v docker &>/dev/null || error "未安装 Docker"
success "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+' | head -1) 已就绪"
$COMPOSE_CMD version &>/dev/null || error "未安装 Docker Compose V2"
command -v curl &>/dev/null || error "未安装 curl"

# ── 检查端口 ────────────────────────────────────
if ss -tlnp 2>/dev/null | grep -q ':8001 ' || netstat -tlnp 2>/dev/null | grep -q ':8001 '; then
  warn "端口 8001 已被占用"
  read -p "  继续部署？[y/N] " _ans
  [[ "$_ans" =~ ^[Yy]$ ]] || error "已取消"
fi

# ── 选择操作模式 ────────────────────────────────
echo ""
if [ -f "$ENV_FILE" ]; then
  echo -e "  检测到已有 ${YELLOW}.env${NC}，请选择操作："
  echo -e "  ${BOLD}1)${NC} 更新代码并重新构建（保留 V2 已有数据）"
  echo -e "  ${BOLD}2)${NC} 重新生成配置（删除 .env 从头开始）"
  echo -e "  ${BOLD}3)${NC} 仅重启 V2 服务"
  echo -e "  ${BOLD}4)${NC} 查看 V2 Key 信息"
  echo -e "  ${BOLD}q)${NC} 退出"
  echo ""
  read -p "  请选择 [1/2/3/4/q]: " MODE
  echo ""
  case "$MODE" in
    1) MODE="update" ;;
    2) rm -f "$ENV_FILE"; MODE="install" ;;
    3) MODE="restart" ;;
    4) MODE="showkeys" ;;
    q|Q) echo "已退出"; exit 0 ;;
    *) error "无效选项" ;;
  esac
else
  MODE="install"
fi

# ── 查看 Key 信息 ────────────────────────────────
if [ "$MODE" = "showkeys" ]; then
  step "V2 当前配置信息"
  source "$ENV_FILE"
  echo -e "  ${YELLOW}API 地址:${NC}     http://localhost:8001"
  echo -e "  ${YELLOW}Admin Token:${NC}  ${GREEN}${ADMIN_TOKEN}${NC}"
  echo -e "  ${YELLOW}管理后台:${NC}     http://localhost:8001/_panel/admin"
  exit 0
fi

# ── 仅重启 ─────────────────────────────────────
if [ "$MODE" = "restart" ]; then
  step "重启 V2 服务"
  $COMPOSE_CMD restart proxy
  success "V2 服务已重启"
  exit 0
fi

# ── 生成 .env ─────────────────────────────────
if [ "$MODE" = "install" ]; then
  step "配置 V2 API Key"
  echo -e "  请输入阿里云 Coding Plan API Key"
  echo -e "  ${CYAN}格式为 sk-sp- 开头，在百炼控制台 → API-KEY 页面获取${NC}"
  echo ""

  while true; do
    read -p "  API Key: " ALIYUN_KEY
    [ -z "$ALIYUN_KEY" ] && echo -e "  ${RED}不能为空${NC}" && continue
    if [[ ! "$ALIYUN_KEY" =~ ^sk-sp- ]]; then
      warn "Key 格式不符（期望 sk-sp- 开头）"
      read -p "  确认使用此 Key 继续？[y/N] " _c
      [[ "$_c" =~ ^[Yy]$ ]] && break || continue
    else
      break
    fi
  done

  # 月度配额重置日
  echo ""
  echo -e "  ${BOLD}月度配额重置日${NC}"
  while true; do
    read -p "  重置日 [1-28，默认 1]: " _rd_input
    if [ -z "$_rd_input" ]; then
      MONTHLY_RESET_DAY=1; break
    elif [[ "$_rd_input" =~ ^[0-9]+$ ]] && [ "$_rd_input" -ge 1 ] && [ "$_rd_input" -le 28 ]; then
      MONTHLY_RESET_DAY=$_rd_input; break
    else
      warn "请输入 1-28 之间的数字"
    fi
  done
  success "月度重置日设为每月第 ${MONTHLY_RESET_DAY} 日"
  echo ""

  # 生成随机凭据
  ADMIN_TOKEN=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
  REDIS_PASS=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)

  cat > "$ENV_FILE" <<ENVEOF
# ── DashScope Proxy V2 ─────────────────────────────
# 注意：此配置与旧版本完全独立，互不影响

# ── 阿里云 ──────────────────────────────────────────
ALIYUN_API_KEY=${ALIYUN_KEY}
ALIYUN_BASE_URL=https://coding.dashscope.aliyuncs.com/v1

# ── Redis ────────────────────────────────────────────
REDIS_URL=redis://:${REDIS_PASS}@127.0.0.1:16379
REDIS_PASSWORD=${REDIS_PASS}

# ── 管理员 Token ──────────────────────────────────────
ADMIN_TOKEN=${ADMIN_TOKEN}

# ── 月度配额重置日（1-28，可在管理后台在线修改）──────────
MONTHLY_RESET_DAY=${MONTHLY_RESET_DAY}
ENVEOF

  success ".env 已生成"
  echo ""
  echo -e "  ${RED}${BOLD}⚠ 以下信息只显示一次，请立即保存！${NC}"
  echo ""
  echo -e "  ${YELLOW}Admin Token:${NC}"
  echo -e "  ${GREEN}${BOLD}${ADMIN_TOKEN}${NC}"
  echo ""
  echo -e "  ${CYAN}访问地址:${NC}"
  echo -e "  管理后台: ${BLUE}http://localhost:8001/_panel/admin${NC}"
  echo -e "  用量面板: ${BLUE}http://localhost:8001/_panel/usage${NC}"
  echo -e "  API 地址: ${BLUE}http://localhost:8001${NC}"
  echo ""
  read -p "  已保存以上信息，按 Enter 继续部署..."
fi

# ── 构建镜像 ────────────────────────────────────
step "构建 V2 Docker 镜像"
$COMPOSE_CMD build --progress=plain
success "V2 镜像构建完成"

# ── 启动服务 ────────────────────────────────────
step "启动 V2 服务"
$COMPOSE_CMD up -d

# ── 健康检查 ─────────────────────────────────────
step "V2 健康检查"
info "等待 V2 服务就绪..."
MAX=30; WAITED=0
while true; do
  if curl -sf http://localhost:8001/_panel/usage > /dev/null 2>&1; then
    success "V2 服务已就绪（等待 ${WAITED}s）"
    break
  fi
  WAITED=$((WAITED+2))
  if [ $WAITED -ge $MAX ]; then
    error "V2 服务在 ${MAX}s 内未就绪，请检查日志：docker compose logs --tail=50 proxy"
  fi
  printf "  等待中... (%ds)\r" "$WAITED"
  sleep 2
done

# ── 部署完成 ─────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   V2 部署成功 ✓                          ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}双服务并行运行中：${NC}"
echo -e "  旧版本: ${YELLOW}http://localhost:8000${NC}"
echo -e "  V2 版本: ${GREEN}http://localhost:8001${NC}"
echo ""
echo -e "  ${BOLD}V2 常用命令${NC}"
echo -e "  查看日志: $COMPOSE_CMD logs -f proxy"
echo -e "  重启服务: $COMPOSE_CMD restart proxy"
echo -e "  停止 V2:   $COMPOSE_CMD down"
echo ""
