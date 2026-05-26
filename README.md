# DashScope Proxy

阿里云 DashScope API 代理服务，支持多子 Key 管理、配额控制、用量统计。

同时兼容 **OpenAI 协议**（openclaw 等工具）和 **Anthropic 协议**（Claude Code），共用同一套子 Key 和配额体系。

## 界面预览

| 管理后台 | 用户面板 |
|---------|---------|
| ![管理后台](docs/dashboard.png) | ![用户面板](docs/user-detail.png) |

## 功能特性

- **双协议支持**：OpenAI 协议 + Anthropic 协议，自动识别，无需额外配置
- **多子 Key 管理**：自动生成 4 个子 Key，支持动态管理
- **固定周期配额**：5H（整点对齐）/ 自然周 / 自然月
- **模型白名单**：只有 Coding Plan 包含的模型才计入配额
- **管理后台**：可视化查看用量、管理 Key
- **用户面板**：用户自助查看自己的配额使用情况
- **安全特性**：Redis 密码不泄露到日志，API Key 脱敏显示，Pydantic 输入校验

## 支持的模型

以下 9 个模型计入配额，其他模型直接透传不扣额：

| 模型 | 适用场景 |
|------|----------|
| `qwen3.5-plus` | 通用编程，超长上下文（1M） |
| `qwen3-max-2026-01-23` | 复杂推理 |
| `qwen3-coder-next` | 代码专精 |
| `qwen3-coder-plus` | 代码专精，超长上下文（1M） |
| `MiniMax-M2.5` | 通用 |
| `glm-5` | 通用 |
| `glm-4.7` | 通用 |
| `kimi-k2.5` | 通用 |
| `qwen3.6-plus` | 通用编程，超长上下文（1M） |

## 部署步骤

### 1. 克隆项目到服务器

```bash
git clone https://github.com/Peters-Pans/dashscope-proxy.git
cd dashscope-proxy
```

### 2. 一键部署

```bash
# 全新安装（旧版）
bash setup.sh

# 或部署 V2 版（与旧版并行运行，不冲突）
git clone https://github.com/Peters-Pans/dashscope-proxy.git dashscope-proxy-v2
cd dashscope-proxy-v2
bash setup-v2.sh
```

脚本会自动：

- 检查 Docker 是否安装
- 引导你输入阿里云 Coding Plan API Key（`sk-sp-` 开头）
- 自动生成随机 Admin Token、Redis 密码、4 个子 Key（只显示一次，记得保存）
- 构建镜像、启动服务、健康检查

### 3. 绑定域名 + HTTPS（可选）

```bash
# 安装 Nginx
apt install nginx certbot python3-certbot-nginx -y

# 复制配置，改成你的域名
cp nginx.conf /etc/nginx/sites-available/dashproxy
vim /etc/nginx/sites-available/dashproxy  # 改 server_name
ln -s /etc/nginx/sites-available/dashproxy /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 申请 SSL 证书
certbot --nginx -d your-domain.com
```

## 客户端接入

### openclaw / OpenAI 兼容工具

```
Base URL:  https://your-domain.com/v1
API Key:   sk-sub-xxxxx（你的子 Key）
```

### Claude Code

编辑 `~/.claude/settings.json`（不存在则新建）：

```json
{
    "env": {
        "ANTHROPIC_AUTH_TOKEN": "sk-sub-xxxxx",
        "ANTHROPIC_BASE_URL": "https://your-domain.com",
        "ANTHROPIC_MODEL": "qwen3.5-plus"
    }
}
```

同时确保 `~/.claude.json` 中有：

```json
{
  "hasCompletedOnboarding": true
}
```

**切换模型：**

```bash
# 启动时指定模型
claude --model qwen3-coder-next

# 会话中切换
/model qwen3-max-2026-01-23
```

> 配额与 OpenAI 协议共享，模型名称与 openclaw 中使用的完全一致。

## 协议识别规则

代理通过请求路径和请求头自动判断协议类型：

| 路径 | 协议 | 上游地址 |
|------|------|---------|
| `/v1/messages` | Anthropic | `coding.dashscope.aliyuncs.com/apps/anthropic` |
| `/v1/chat/completions` 等 | OpenAI | `coding.dashscope.aliyuncs.com/v1` |

认证方式：`x-api-key` 或 `Authorization: Bearer` 均支持，代理自动转换为上游所需格式。

## 常用命令

```bash
docker compose logs -f proxy
docker compose restart proxy
docker compose down
docker compose build && docker compose up -d
```

## API 使用

### OpenAI 协议代理请求

```bash
curl https://your-domain.com/v1/chat/completions \
  -H "Authorization: Bearer sk-sub-xxxxx" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5-plus", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Anthropic 协议代理请求

```bash
curl https://your-domain.com/v1/messages \
  -H "x-api-key: sk-sub-xxxxx" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.5-plus", "max_tokens": 1024, "messages": [{"role": "user", "content": "Hello"}]}'
```

### 查看用量（用户端）

```bash
curl https://your-domain.com/_usage \
  -H "Authorization: Bearer sk-sub-xxxxx"
```

## 管理后台

### 访问地址

- 管理后台：`https://your-domain.com/_panel/admin`
- 用户面板：`https://your-domain.com/_panel/usage`

### 管理员 API

需要携带 `X-Admin-Token` 头：

```bash
# 查看所有 Key 状态
curl https://your-domain.com/_admin/keys \
  -H "X-Admin-Token: your-admin-token"

# 禁用/启用 Key
curl -X POST https://your-domain.com/_admin/keys/k1/toggle \
  -H "X-Admin-Token: your-admin-token"

# 修改配额
curl -X PUT https://your-domain.com/_admin/keys/k1/limits \
  -H "X-Admin-Token: your-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"month": 50000, "week": 20000, "5h": 3000}'

# 修改用量
curl -X PUT https://your-domain.com/_admin/keys/k1/usage \
  -H "X-Admin-Token: your-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"month": 1000, "week": 500, "5h": 100}'

# 重新生成 Key
curl -X POST https://your-domain.com/_admin/keys/k1/regenerate \
  -H "X-Admin-Token: your-admin-token"

# 获取完整 Secret
curl https://your-domain.com/_admin/keys/k1/secret \
  -H "X-Admin-Token: your-admin-token"
```

## 配额说明

### 默认配额

| 周期 | 默认值 | 重置时间 |
|------|--------|----------|
| 5小时 | 1,500 次 | 每 5 小时整点重置（00:00, 05:00, 10:00, 15:00, 20:00） |
| 自然周 | 11,250 次 | 每周一 00:00 |
| 自然月 | 22,500 次 | 每月 1 日 00:00 |

### 配额校验规则

- 任意周期达到上限都会拒绝请求（429）
- OpenAI 和 Anthropic 协议共用同一套配额计数
- 流式请求失败会自动回滚配额
- 客户端主动断开不回滚（上游已处理）

## 项目结构

```
.
├── main.py              # FastAPI 主程序
├── static/
│   ├── admin.html       # 管理后台
│   └── user.html        # 用户面板
├── Dockerfile
├── docker-compose.yml
├── nginx.conf           # Nginx 反向代理配置
├── setup.sh             # 一键部署脚本
└── requirements.txt
```

## 更新日志

### 2026-03-26
- 新增 Anthropic 协议支持（Claude Code 接入）
- 路径 + 请求头组合自动识别协议类型，`x-api-key` / `Authorization: Bearer` 均兼容
- OpenAI 和 Anthropic 共用同一套子 Key 和配额体系
- 支持 Claude Code 会话内 `/model` 切换及启动时 `--model` 指定模型

### 2026-03-24
- Redis 密码不再泄露到日志
- API Key 脱敏显示，需单独接口获取完整密钥
- 添加 Pydantic 输入校验
- 修复 API 路径转发问题（baseUrl 与官方一致，含 /v1）
- 完善 README 文档

## License

MIT


