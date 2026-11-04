# 快速开始（Linux）

四步跑起三平台自动领取。完整版见 [linux-deploy.md](linux-deploy.md)。

```bash
# 1. 拿代码
git clone https://github.com/black-ant/workbuddy-auto-signin.git
cd workbuddy-auto-signin

# 2. 装依赖（目录名与 env.sh 的 SIGNIN_PYTHON 默认值保持一致）
python3 -m venv "$HOME/.venvs/workbuddy-signin"
"$HOME/.venvs/workbuddy-signin/bin/pip" install cryptography dbus-next

# 3. 把示例路径换成自己的
sed -i "s#^export SIGNIN_PYTHON=.*#export SIGNIN_PYTHON=\"$HOME/.venvs/workbuddy-signin/bin/python\"#" local/env.sh
CLONE_DIR="$(pwd)"
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$CLONE_DIR#" local/systemd/*.service
sed -i "s#^ExecStart=.*/local/#ExecStart=$CLONE_DIR/local/#" local/systemd/*.service
sed -i "s#^Environment=HOME=.*#Environment=HOME=$HOME#" local/systemd/*.service

# 核对：这三行应都是你机器的真实值
grep -n "SIGNIN_PYTHON" local/env.sh
grep -H "WorkingDirectory\|ExecStart\|Environment" local/systemd/*.service

# 4. 验证 → 装定时 → 开 linger
./local/run.sh doctor
./local/run-qoder.sh doctor
./local/run-codearts.sh doctor

mkdir -p "$HOME/.config/systemd/user"
cp local/systemd/*.service local/systemd/*.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now workbuddy-signin.timer workbuddy-growth-poll.timer \
                                 qoder-claim.timer codearts-claim.timer
sudo loginctl enable-linger "$USER"

# 确认实际生效的触发时刻
systemctl --user list-timers --all | grep -E 'workbuddy|qoder|codearts'
```

## 前提：客户端必须先登录

三个平台都是从本机客户端的加密登录态里解凭据，**不是脚本自己登录**：

- WorkBuddy 桌面端登录一次
- Qoder 桌面端登录一次
- CodeArts Agent（码道 IDE）登录一次

## 三个 `doctor` 的期望输出

```
{"result": "AUTH_READY", ...}      # WorkBuddy
{"result": "...", ...}            # Qoder 能解出登录态即可
{"result": "AUTH_READY", ...}     # CodeArts
```

## 手工执行报 D-Bus 错？

```
AuthError: ... EXTERNAL ... REJECTED
```

说明你不在用户会话里。改用：

```bash
systemd-run --user --wait --pipe --collect ./local/run-codearts.sh doctor
```

## 排错

见 [linux-deploy.md 第 11 节](linux-deploy.md#11-排错)。
CodeArts（华为）的协议细节见 [codearts-signin.md](codearts-signin.md)。
