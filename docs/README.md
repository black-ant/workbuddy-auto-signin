# 文档索引

| 文档 | 内容 | 谁该读 |
| --- | --- | --- |
| [../README.md](../README.md) | 上游项目介绍、Windows / macOS 一键安装 | 上游用户 |
| [../AGENTS.md](../AGENTS.md) | 仓库结构、硬性约束、代码约定、AI 协作规则 | **AI 编码助手 / 贡献者** |
| [quickstart-linux.md](quickstart-linux.md) | 四步跑起来（可直接转发） | 急着部署的人 |
| [linux-deploy.md](linux-deploy.md) | Linux 完整部署：依赖、`env.sh`、systemd、验收、排错、安全边界；附录 B 为 Windows 适配层 | 部署运维者 |
| [codearts-signin.md](codearts-signin.md) | 华为 CodeArts Agent 签到协议：端点、签名、凭据、状态映射 | 想改 CodeArts 实现的人 |
| [../local/DEPLOY-RESULT.md](../local/DEPLOY-RESULT.md) | 一次真实部署的实测记录与踩坑复盘 | 排错时对照 |
| [../local/REFERENCE-IMPLEMENTATION.md](../local/REFERENCE-IMPLEMENTATION.md) | 同类开源项目的源码分析（借鉴了什么、刻意不做什么） | 想扩展功能的人 |
| [plans/](plans/) | 上游的历史设计文档 | 了解上游演进 |

## 三条最常见的路径

**我要在一台 Linux 上跑起来**
→ [quickstart-linux.md](quickstart-linux.md)，或 [linux-deploy.md](linux-deploy.md) 第 1 → 4 → 7 → 8 节。

**我要理解 CodeArts（华为）那条路怎么实现的**
→ [codearts-signin.md](codearts-signin.md)。

**我是 AI，要在本仓库改代码**
→ [../AGENTS.md](../AGENTS.md)，重点看第 2 节「硬性约束」和第 4 节「代码约定」。
