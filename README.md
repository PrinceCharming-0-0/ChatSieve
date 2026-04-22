# ChatSieve

从指定微信群提取消息，调用 AI 生成摘要，通过 Server酱turbo 推送至手机。支持定时自动摘要（`main.py`）和按需导出（`export_summary.py`）两种模式。

## 脚本说明

| 脚本 | 说明 | 触发方式 |
|------|------|---------|
| `main.py` | 定时摘要，推送纯文本摘要到手机 | crontab 定时任务 |
| `export_summary.py` | 按时间段导出，生成 Markdown 日报 | 手动执行 |
| `image_analyzer.py` | 图片分析器，按需识别群聊图片中的文字 | 作为模块导入使用 |

## 功能特性

- **多 AI 服务商**：DeepSeek / SiliconFlow / 智谱 / 通义千问 / MiniMax，一行配置切换
- **消息预处理**：过滤短消息（<2字符）、纯表情、连续重复内容、系统通知（撤回、加群等）
- **图片分析**（可选）：过滤表情包 / 合并连续批次 / 语义触发判断 / 调用视觉模型 OCR
- **Token 用量追踪**：日/月累计统计，支持自定义预警阈值
- **Server酱turbo 推送**：指数退避重试，Markdown 段落级分片
- **日志切分**：按日归档，ERROR 级别独立文件，保留 30 天

## 文件结构

```
wechat_daily_summary/
├── main.py              # 定时摘要主脚本
├── export_summary.py     # 按时间段导出脚本
├── wechat_client.py      # wechat-cli 封装
├── preprocessor.py       # 消息预处理（过滤/合并/去系统通知）
├── ai_client.py          # 多 AI 服务商客户端
├── image_analyzer.py     # 图片分析器（可选模块）
├── token_tracker.py      # Token 用量追踪
├── pusher.py             # Server酱turbo 推送
├── requirements.txt      # 依赖列表
├── .env.example          # 配置模板（复制为 .env 后填入）
├── logs/                 # 日志目录（自动创建）
├── tmp/                  # 图片分析临时文件（自动创建）
├── token_state.json      # Token 状态文件（自动创建，不提交）
└── run_state.json        # 运行状态文件（自动创建，不提交）
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 .env

```bash
cp .env .env.example   # 如有需要备份默认配置
# 编辑 .env，填入配置（见下节）
```

### 3. 初始化 wechat-cli

> wechat-cli 是 macOS 本地工具，需提前完成初始化。

```bash
wechat-cli init
wechat-cli sessions          # 验证是否正常工作
```

### 4. 测试运行

```bash
python main.py              # 定时摘要模式
python export_summary.py    # 按时间段导出模式
```

### 5. 配置定时任务（macOS crontab）

```bash
crontab -e
# 每天早上 9:00 执行
0 9 * * * cd /path/to/wechat_daily_summary && python3 main.py >> logs/cron.log 2>&1
```

### 6. 确保 Mac 在定时任务前被唤醒

`cron` 在 Mac 睡眠时无法执行任务，因此需要配置系统定时唤醒，让电脑在任务执行前几分钟自动醒来。

#### 6.1 设置系统唤醒时间

```bash
# 例如每天 08:58 唤醒（任务在 09:00）
sudo pmset repeat wake MTWRFSU 08:58:00
```

pmset repeat 只能设置一对重复事件（一个开机/一个关机）。如需设置两个唤醒时间，建议编写一次性唤醒脚本。

#### 6.2 授予 cron 全磁盘访问权限

macOS 对 cron 有严格的沙盒限制，需手动授权：

1. 打开 系统设置 → 隐私与安全性 → 全磁盘访问权限
2. 点击 +，按 Command + Shift + G 输入 /usr/sbin/cron，添加并勾选
3. 重启 Mac 使权限生效

#### 6.3 验证唤醒计划

```bash
pmset -g sched
```

如果输出中有 wake at MM/dd/yy HH:mm:ss 字样，表示唤醒计划已生效。

#### 6.4 唤醒失效排查

· 确保 Mac 接通电源：电池供电时，部分唤醒计划可能被系统忽略。
· 确保 Mac 未处于深度休眠：合盖且电池供电时可能无法唤醒。
· 检查唤醒计划是否被覆盖：某些系统更新或节能设置可能清除计划，可定期运行 pmset -g sched 确认。

```

## 配置说明

所有配置通过 `.env` 文件或环境变量传入。

### 必需配置

| 变量 | 说明 |
|------|------|
| `WECHAT_GROUP_NAME` | 目标微信群名称，支持逗号分隔多个群 |
| `AI_PROVIDER` | AI 服务商：`deepseek` / `siliconflow` / `zhipu` / `qwen` / `minimax` |
| `<PROVIDER>_API_KEY` | 对应服务商的 API Key |

### 可选配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `WECHAT_DAYS` | `1` | 拉取消息的天数范围 |
| `WECHAT_MESSAGE_LIMIT` | `200` | 每次拉取消息上限 |
| `TEXT_TOKEN_TOTAL_LIMIT` | `1000000` | 文本模型 token 总额度（`TEXT_WARNING_MODE=token` 时生效） |
| `VISION_TOKEN_LIMIT` | `1000000` | 视觉模型 token 总额度（`VISION_WARNING_MODE=token` 时生效） |
| `TOKEN_WARNING_RATIO` | `0.9` | 预警触发比例（超出该比例时推送警告） |
| `DEEPSEEK_MODEL` | `deepseek-v3-0324` | DeepSeek 模型名称 |
| `SILICONFLOW_MODEL` | `glm-5.1` | SiliconFlow 模型名称 |
| `ZHIPU_MODEL` | `glm-4.7` | 智谱模型名称 |
| `QWEN_MODEL` | `qwen3.6-plus` | 通义千问模型名称 |
| `MINIMAX_MODEL` | `claude-sonnet-4-6` | MiniMax 模型名称 |
| `VISION_API_KEY` | — | 视觉模型 API Key（图片分析功能必需） |
| `VISION_MODEL` | `claude-sonnet-4-6` | 视觉模型名称 |
| `VISION_BASE_URL` | — | 视觉模型 API 地址（可选） |

## AI 服务商配置示例

```env
# DeepSeek（推荐）
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-xxx

# SiliconFlow
AI_PROVIDER=siliconflow
SILICONFLOW_API_KEY=sk-xxx

# 智谱
AI_PROVIDER=zhipu
ZHIPU_API_KEY=xxx

# 通义千问
AI_PROVIDER=qwen
QWEN_API_KEY=sk-xxx

# MiniMax
AI_PROVIDER=minimax
MINIMAX_API_KEY=xxx
```

## 消息预处理流程

`preprocessor.py` 对每条消息依次执行：

1. **长度过滤**：去除空白后不足 2 字符的消息
2. **表情过滤**：去除纯 emoji 消息（包括 `[表情]` 标记）
3. **连续合并**：同一发送者连续发送相同内容 → 合并为一条，标注发送次数
4. **系统消息过滤**（`export_summary.py` 额外执行）：剔除撤回、加群、离群等系统通知

## 图片分析（image_analyzer.py）

图片分析是独立模块，需手动调用，不在定时摘要流程中自动执行。

### 流程

1. **表情包过滤**：文件名含 `emoji/sticker/CustomEmotions` 或宽高均 < 240px → 跳过
2. **连续批次合并**：间隔 < 30 秒且中间无有效文本的图片 → 合并为一个批次
3. **上下文附加**：取批次前后各 3 条文本消息（限 5 分钟内），作为 AI 判断依据
4. **语义触发判断**：将上下文发给文本 AI，判断"用户是否需要图片才能理解对话" → 返回 NO 则跳过整批
5. **截图 + 视觉分析**：调用 `wechat-cli preview` 打开预览，`screencapture -l <WindowID>` 截图，Base64 送视觉模型提取文字

### 使用方式

```python
from image_analyzer import ImageAnalyzer
from ai_client import create_ai_client

ai_client = create_ai_client("minimax", api_key=os.environ["MINIMAX_API_KEY"], model="claude-sonnet-4-6")
analyzer = ImageAnalyzer(group_name="我的群", days=1, limit=200)
result_text = analyzer.analyze(all_text_messages, ai_client)
# result_text 可追加到摘要末尾，随 Server酱turbo 一起推送
```

### 输出格式

单张：`[图片分析] 张三 在 2026-04-12 22:00 发送了一张图片：图片中的文字是...`

多张批次：
```
【图片批次】（共 3 张，李四 于 22:05 发送）
[图片分析] 李四 在 22:05 发送了第 1/3 张图片：...
[图片分析] 李四 在 22:06 发送了第 2/3 张图片：...
[图片分析] 李四 在 22:06 发送了第 3/3 张图片：...
```

### 屏幕录制权限

`screencapture -l <WindowID>` 需要 macOS 屏幕录制权限。首次运行时会弹窗申请授权，验证方法：

```bash
# 测试截图
screencapture -x /tmp/test.png && ls -la /tmp/test.png
```

> 授权路径：**系统设置 → 隐私与安全性 → 屏幕录制** → 确认终端或 Python 已获得授权。
> **注意**：截图时需确保微信窗口**可见且未最小化**，否则截图可能为空白。

## export_summary.py — 按时间段导出

支持指定任意时间段，生成结构化 Markdown 日报。

```bash
# 命令行指定时间段
python export_summary.py --start "2026-04-13 09:00" --end "2026-04-13 18:00"

# 交互式输入（省略参数时）
python export_summary.py
```

报告输出至 `summary_report_YYYYMMDD_HHMM.md`，包含：
- 群名、时间范围、消息条数、活跃人数
- 核心主题、关键决策、待办清单、成员活跃度表格
- 可折叠的原始聊天记录

### 报告结构

```markdown
# 微信群聊日报
## 群名A
### 元数据（表格）
### 摘要（核心主题 + 一段话概括）
### 待办清单
### 消息总结
### 原始聊天记录（可折叠）

## 群名B
...
## 失败记录（如有）
```

## 日志文件

| 文件 | 说明 | 保留 |
|------|------|------|
| `logs/wechat_summary.log` | 主日志（INFO+） | 30 天 |
| `logs/error.log` | ERROR 级别独立归档 | 30 天 |
| `logs/image_analyzer.log` | 图片分析日志 | 14 天 |
| `logs/image_error.log` | 图片分析 ERROR 日志 | 14 天 |

## 常见问题

**Q: `wechat-cli: command not found`**
> 确认 wechat-cli 已安装并加入 PATH，或在 `.env` 中指定完整路径。

**Q: Server酱turbo 推送失败**
> 检查 SendKey 是否正确，确认未达到当日推送上限（免费版 500 条/天）。

**Q: AI 摘要为空或质量差**
> 尝试增加 `WECHAT_MESSAGE_LIMIT`，或调整 AI 服务商的 `model` 参数。

**Q: Token 预警频繁触发**
> 提高 `DAILY_TOKEN_LIMIT` 或降低 `TOKEN_WARNING_RATIO`。

**Q: 图片分析失败**
> 确认已授予终端/Python 屏幕录制权限，并已配置 `VISION_API_KEY`。

**Q: 定时任务未执行（日志为空或无新记录）**
> 1. 检查 Mac 是否接通电源，电池供电可能阻止唤醒。
> 2. 执行 `pmset -g sched` 查看唤醒计划是否存在。
> 3. 确认 `cron` 已获得全磁盘访问权限（系统设置 → 隐私与安全性）。
> 4. 检查 `logs/cron.log` 中是否有错误输出。