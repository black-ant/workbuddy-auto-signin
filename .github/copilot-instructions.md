# 仓库协作说明（Copilot / IDE 内置助手）

本文件的权威版本是根目录 [`AGENTS.md`](../AGENTS.md)，本文件只是让只读
`.github/copilot-instructions.md` 的工具也能拿到关键约束。

## 必须遵守

1. **不改上游文件**：`signin.py`、`qoder_signin.py`、`README.md`、`install-*.sh`、
   `tests/`。所有本机适配只写在 `local/`。
2. **凭据不落盘、不打印、不进仓库**：token、AK/SK、Cookie、密码、
   `auth.v1.dat`、`workbuddy-desktop.info`、`machine_token.json`、`state.vscdb`。
3. **文档不写真实信息**：路径用 `<CLONE_DIR>`，主机名用 `<HOSTNAME>`，
   账号用 `<ACCOUNT_ID>`，邮箱不写。
4. **stdout 只输出一行 JSON**，诊断走 stderr；退出码 `0/1/2/3`。

## 关键文档

- 部署：`docs/linux-deploy.md`
- 华为 CodeArts 签到协议：`docs/codearts-signin.md`
- 实测记录：`local/DEPLOY-RESULT.md`

## 改完必须自检

```bash
python3 -m py_compile local/*.py
bash -n local/*.sh
systemd-analyze verify local/systemd/*.service local/systemd/*.timer
```

## 常见错误

- `OnCalendar=*-*-* 00:20,10:20,15:20,20:20:00` —— 非法写法，
  应为 `OnCalendar=*-*-* 00,10,15,20:20:00`
- CodeArts 把签名用的 canonical URI（末尾补 `/`）当真实 URL 发 → `404`
- 漏 `Agent-Type: PromptCenter` → `TM.00001001`
- `claimable == false` 时仍调 `POST /v1/ops/claim`
- 用 `systemctl show -p Result` 判断任务成功（`Result` 初始值就是 `success`）
