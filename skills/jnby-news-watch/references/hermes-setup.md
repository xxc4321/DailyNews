# Hermes 0.18 + 飞书私聊接入

以下命令面向 Hermes 0.18。先运行健康检查，确认只显示“已配置/未配置”，不会显示密钥值：

```powershell
python <skill-root>\scripts\run.py --home <runtime-home> health --json
```

将整个 `jnby-news-watch` 目录复制或链接到 Hermes 的 Skills 目录，并把定时脚本放进 Hermes 允许的 `scripts` 目录。推荐让无 Agent cron 执行固定脚本并投递 stdout，避免早报正文再经过一层 Agent 改写。

正式任务要求：

- cron：`0 8 * * *`
- 时区：`Asia/Shanghai`
- 命令：`digest --scheduled --news 10 --reviews 5 --cost-mode immediate`
- 目标：Joe 已批准的飞书私聊 home channel
- 创建前按名称检查已有任务，只允许一个正式任务；更新匹配项，不触碰其他 cron。

Hermes CLI 的典型形式：

```powershell
hermes cron create "0 8 * * *" --name "JNBY Daily News" --script <script-name> --no-agent --deliver
```

实际参数以本机 `hermes cron create --help` 为准。首次上线顺序是：离线报告 → DeepSeek 小批量 → 飞书测试文本 → 小型测试日报 → 检查唯一 cron 和下一次运行时间。只有飞书适配器返回成功后，才调用 `Pipeline.confirm_delivery` 写入成功状态。
