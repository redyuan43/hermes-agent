# Hermes Clean Migration README

本文档给后续 AI Agent 使用：当主人要求“保存/迁移 Hermes 微信服务到另一台设备”时，优先使用这里的流程。

## 目标

生成一个干净、加密、可恢复的迁移包，用于把当前设备上的 Hermes 用户状态平移到另一台 Linux 设备。

迁移包保留：

- Hermes 配置、记忆、会话、skills、cron
- ChatGPT/Hermes 登录态
- Weixin/iLink 登录态
- gateway systemd 模板
- 代理配置模板

迁移包不保留：

- Hermes 源码目录
- Python venv
- Node、uv、xray 等二进制执行文件
- 日志、缓存、旧备份、request dump
- 历史 WeClaw workspace 和旧账号垃圾数据

## 一键生成迁移包

在源设备上运行：

```bash
~/github/hermes-agent/scripts/hermes-migration-create -o ~
```

脚本会提示输入加密密码，并生成：

```text
~/hermes-migration-<timestamp>.bundle.tar.gz
```

这个 bundle 外层可以查看，敏感 payload 已加密。里面包含：

```text
restore.sh
payload.tar.gz.enc
README.txt
```

## 非交互生成

如果由 AI Agent 自动执行，建议使用临时密码文件：

```bash
printf '强密码放这里\n' > /tmp/hermes-pass.txt
chmod 600 /tmp/hermes-pass.txt

~/github/hermes-agent/scripts/hermes-migration-create \
  -o ~ \
  --passphrase-file /tmp/hermes-pass.txt

rm -f /tmp/hermes-pass.txt
```

注意：不要把密码文件长期保留在磁盘上。

## 传到目标设备

示例：

```bash
scp ~/hermes-migration-*.bundle.tar.gz nx3:/tmp/
```

## 在目标设备恢复

```bash
cd /tmp
tar -xzf hermes-migration-*.bundle.tar.gz
cd hermes-migration-*
./restore.sh
```

如果使用密码文件：

```bash
./restore.sh --passphrase-file /tmp/hermes-pass.txt
```

恢复脚本会：

- 从 GitHub clone/update Hermes
- 创建 venv；如果系统缺 `python3-venv`，会 fallback 到 `uv venv`
- 恢复 `~/.hermes`
- 复用目标设备已有的 `127.0.0.1:10808` 代理；如果没有可用代理，按包内配置恢复 xray
- 安装/更新 `hermes-gateway.service`
- 给 gateway 注入代理环境变量
- 启用 user systemd 服务
- 验证 ChatGPT 后端经代理可达

## 目标设备已有微信，要重新绑定

如果主人说“其余内容保持一致，但微信重新绑定”，恢复时先跳过发送测试：

```bash
./restore.sh --skip-send-test
```

恢复后清理迁移过来的 Weixin 配置，再生成新的绑定链接。

典型步骤：

```bash
systemctl --user stop hermes-gateway.service || true

stamp=$(date +%Y%m%d-%H%M%S)
backup="$HOME/hermes-weixin-rebind-backup-$stamp"
mkdir -p "$backup"

cp ~/.hermes/.env "$backup/.env.before-weixin-rebind"
cp -a ~/.hermes/weixin "$backup/weixin" 2>/dev/null || true

python3 - <<'PY'
from pathlib import Path
p = Path.home() / ".hermes/.env"
lines = p.read_text().splitlines()
lines = [line for line in lines if not line.startswith("WEIXIN_")]
p.write_text("\n".join(lines) + "\n")
p.chmod(0o600)
PY

rm -rf ~/.hermes/weixin/accounts
mkdir -p ~/.hermes/weixin/accounts
```

然后生成新的 Weixin 绑定链接。最稳妥的做法是启动一个后台轮询脚本：用户扫码确认后，自动保存新账号、写 `.env` 并重启 gateway。

如果只是要先拿链接，可以用 Hermes 的 Weixin API 直接取：

```bash
cd ~/github/hermes-agent
env \
  http_proxy=http://127.0.0.1:10808 \
  https_proxy=http://127.0.0.1:10808 \
  all_proxy=socks5h://127.0.0.1:10808 \
  HERMES_HOME="$HOME/.hermes" \
  "$HOME/github/hermes-agent/venv/bin/python" - <<'PY'
import asyncio, json
import aiohttp
from gateway.platforms.weixin import (
    _api_get,
    _make_ssl_connector,
    ILINK_BASE_URL,
    EP_GET_BOT_QR,
    QR_TIMEOUT_MS,
)

async def main():
    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        resp = await _api_get(
            session,
            base_url=ILINK_BASE_URL,
            endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
            timeout_ms=QR_TIMEOUT_MS,
        )
    print(resp.get("qrcode_img_content") or json.dumps(resp, ensure_ascii=False))

asyncio.run(main())
PY
```

重要：只生成链接不等于完成绑定。必须继续轮询 `EP_GET_QR_STATUS`，拿到 `status=confirmed` 后保存：

- `WEIXIN_ACCOUNT_ID`
- `WEIXIN_TOKEN`
- `WEIXIN_HOME_CHANNEL`
- `WEIXIN_ALLOWED_USERS`
- `~/.hermes/weixin/accounts/<account_id>.json`

## 验证恢复是否成功

恢复后执行：

```bash
systemctl --user status hermes-gateway.service --no-pager -l
systemctl --user show hermes-gateway.service -p Environment -p DropInPaths --no-pager
tail -n 120 ~/.hermes/logs/gateway.log
```

必须看到：

- `hermes-gateway.service` 是 `active`
- gateway 环境里有：
  - `http_proxy=http://127.0.0.1:10808`
  - `https_proxy=http://127.0.0.1:10808`
  - `all_proxy=socks5h://127.0.0.1:10808`
- Weixin 日志里有：
  - `Connected account=...`
  - `✓ weixin connected`

发送测试：

```bash
~/github/hermes-agent/venv/bin/python -m hermes_cli.main send \
  --to "weixin:<WEIXIN_HOME_CHANNEL>" \
  "微信目前连接正常，可以继续使用。"
```

## 临时文件清理

确认目标设备稳定后，可以删除：

```bash
rm -f /tmp/hermes-migration-*.bundle.tar.gz
rm -f /tmp/passphrase.txt
rm -rf /tmp/hermes-restore-test
```

源设备上的迁移包如果不再需要，也可以删除；如果要留档，必须加密保存。

## 安全提醒

迁移包包含：

- 微信 token
- ChatGPT/Hermes 登录态
- 记忆
- 会话历史
- 用户配置

必须当作敏感密钥包处理，不要明文长期保存，不要发到不可信渠道。
