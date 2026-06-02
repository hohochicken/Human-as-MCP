# HumanMCP

将人类操作员桥接到 MCP (Model Context Protocol) 生态系统的服务器。当 AI Agent 遇到能力边界或权限边界时，通过标准化的 MCP 工具接口将任务委托给人类执行。

## 核心设计哲学

```
AI 是编排者 + 验证者，人是 AI 工具箱中的一个"工具"

✅ AI 做它能做的所有事 → 遇到能力/权限边界 → 调用人类工具
✅ AI 审核人类返回的结果 → 验证合理性 → 整合到工作流
✅ 人类只负责提供 AI 确实没有的能力
❌ 人类不审核 AI 的日常产出
```

## 快速开始

### 环境要求

- Python 3.10+
- Windows 10/11（通知功能需要 PowerShell；非 Windows 系统自动降级）
- 现代浏览器（Dashboard）

### 安装

```bash
cd H:\Human
pip install -r requirements.txt
```

### 启动

```bash
python server/main.py
```

启动后：
- **Dashboard**: http://localhost:4350/dashboard
- **MCP 端点**: http://localhost:4350/mcp
- **健康检查**: http://localhost:4350/health

---

## 部署指南

### 方式一：Claude Code（推荐）

在 Claude Code 中注册此 MCP 服务器：

**1. 找到 Claude Code 的 MCP 配置文件：**

```bash
# Windows
%APPDATA%\Claude\mcp.json

# macOS / Linux
~/.claude/mcp.json
```

**2. 添加 HumanMCP 配置：**

```json
{
  "mcpServers": {
    "human-as-mcp": {
      "command": "python",
      "args": ["-m", "server.main"],
      "cwd": "H:\\Human",
      "env": {}
    }
  }
}
```

如果希望 AI 以特定 agent 身份运行（用于频率限制区分）：

```json
{
  "mcpServers": {
    "human-as-mcp": {
      "command": "python",
      "args": ["-m", "server.main"],
      "cwd": "H:\\Human",
      "env": {
        "HUMAN_MCP_AGENT_ID": "claude-code-01"
      }
    }
  }
}
```

**3. 可选：注册 Skill（让 AI 深入理解使用模式）**

将 `SKILL.md` 注册为 Claude Code 的 Skill：

```bash
# 在项目目录下
mkdir -p .claude/skills
cp SKILL.md .claude/skills/human-as-mcp.md
```

或者直接告诉 Claude Code：`/skill 加载 H:\Human\SKILL.md 作为 human-as-mcp 技能`

**4. 重启 Claude Code**，AI 应该能看到 6 个 `human_*` 工具。

**5. 验证：** 在 Claude Code 中输入：
```
调用 human_list_tasks 查看当前队列
```

---

### 方式二：其他 MCP 客户端（Cursor、Continue、自定义 Agent 等）

**1. 作为 HTTP 端点运行：**

```bash
cd H:\Human
python server/main.py
```

服务器在 `http://127.0.0.1:4350/mcp` 上监听 HTTP 请求。

**2. 配置 MCP 客户端连接到此端点：**

```json
{
  "mcpServers": {
    "human-as-mcp": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:4350/mcp"
    }
  }
}
```

**3. （可选）作为后台服务运行：**

使用任务计划程序 (Windows) 或 systemd (Linux) 确保服务器开机自启：

```bash
# Windows: 创建计划任务
schtasks /create /tn "HumanMCP" /tr "python H:\Human\server\main.py" /sc onlogon /rl highest

# 或者直接双击 start.bat（启动后保持在后台运行）
```

---

### 方式三：作为 Python 模块嵌入

```python
import asyncio
from server.app import init, create_app

async def start():
    await init()  # 初始化数据库、运行迁移
    app = create_app()  # 构建 FastMCP 应用
    app.run(transport="streamable-http", host="127.0.0.1", port=4350)

asyncio.run(start())
```

---

## MCP 工具

### 域工具（3 个）

| 工具 | 用途 | AI 做不到的原因 |
|------|------|---------------|
| `human_action` | 执行手动操作、运行受限命令、人际协调 | AI 没有操作真实软件/设备的能力，无法主动联系人 |
| `human_information` | 提供未文档化的知识、查询封闭系统 | 信息只存在人脑中，或系统没有对 AI 开放的 API |
| `human_decision` | 拍板重大方向 | 涉及商业判断和风险承担 |

### 基础设施工具（3 个）

| 工具 | 用途 |
|------|------|
| `human_poll` | 查询任务结果（单个或批量） |
| `human_cancel` | 取消/修改未完成的任务 |
| `human_list_tasks` | 查看任务队列 |

所有域工具采用**同步优先**策略：AI 调用后阻塞等待最多 180 秒，人类即时响应则同步返回；超时后回退为异步模式。

各工具的详细触发条件、反触发条件、拒绝处理规则已嵌入工具本身的描述中——AI 连接后无需额外配置即可获得基本的行为指导。完整的使用模式（Fan-Out / Pipeline / Loop-Until-Clear / Adversarial Verify）见 `SKILL.md`。

### 任务拒绝理由

人类可以在 Dashboard 以 4 种理由拒绝任务，每种理由 AI 有不同的响应策略：

| 理由 | 含义 | AI 必须 |
|------|------|---------|
| `ai_can_do` | AI 自己能做 | 自己完成，不重试 |
| `unclear` | 指令不清 | 补充上下文，新建 task |
| `out_of_scope` | 超出职责/权限 | 协调或升级，不重试 |
| `invalid_task` | 前提有误 | 修正前提，不重试 |

---

## 配置

编辑 `config/server_config.yaml`：

```yaml
server:
  host: "127.0.0.1"       # 仅本地访问
  port: 4350

rate_limits:
  per_agent_per_hour: 30  # 单 Agent 每小时最大任务数
  global_per_hour: 100    # 全局每小时最大任务数

websocket:
  auth_enabled: false      # 设为 true 启用 token 认证
  shared_secret: ""         # WebSocket 连接令牌

storage:
  db_path: "data/tasks.db" # SQLite 数据库路径
```

---

## 项目结构

```
H:\Human\
├── server/
│   ├── main.py              # 入口
│   ├── app.py               # FastMCP 应用 + HTTP/WS 路由
│   ├── constants.py         # 共享常量
│   ├── models.py            # 数据模型 (Task, 枚举)
│   ├── task_manager.py      # 任务生命周期管理
│   ├── task_pipeline.py     # 任务创建共享流水线
│   ├── storage.py           # SQLite 持久化（异步）
│   ├── boundary_gate.py     # 频率限制
│   ├── notification.py      # Windows Toast 通知
│   └── tools/               # MCP 工具实现 (6个)
│       ├── action.py
│       ├── decision.py
│       ├── information.py
│       └── infrastructure.py
├── static/
│   └── dashboard.html       # Web Dashboard
├── config/
│   └── server_config.yaml   # 服务器配置
├── tests/                   # 测试套件
├── DESIGN.md                # 设计文档
├── SKILL.md                 # AI Agent 使用指南
├── README.md                # 本文件
└── requirements.txt
```

## 运行测试

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -v
```
