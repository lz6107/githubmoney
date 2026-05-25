# GitHub 项目赚钱分享频道机器人

第一版目标：

- 自动从 GitHub 搜索候选项目
- AI 自动判断哪个项目适合做“开源项目变现”频道内容
- AI 自动生成 Telegram 长文
- AI 自动生成配图提示词
- 草稿只发到你的私聊审核
- 只有你手动 `/publish 草稿ID` 才会发到频道

---

## 1. 文件说明

```text
github_money_channel_bot/
├─ bot.py                 # 主程序
├─ requirements.txt       # Python依赖
├─ .env.example           # 配置模板
├─ README.md              # 说明
└─ data/
   ├─ drafts.json         # 草稿记录，自动生成/更新
   └─ seen_repos.json     # 已用过项目，避免重复
```

---

## 2. 准备条件

### 必须准备

1. Telegram Bot Token  
   找 BotFather 创建机器人，拿到 `TELEGRAM_BOT_TOKEN`。

2. 你的 Telegram 用户数字 ID  
   私聊 `@userinfobot` 或 `@RawDataBot` 获取，填到 `TELEGRAM_OWNER_ID`。

3. OpenAI API Key  
   填到 `OPENAI_API_KEY`。

4. 频道 ID 或频道用户名  
   例如：`@your_channel_username`。  
   如果要让机器人发频道，必须把机器人加为频道管理员。

### 可选准备

GitHub Token：

- 不填也能搜索 GitHub。
- 填了以后频率限制更宽松，更适合长期跑。

---

## 3. 本地 Windows 运行

进入文件夹后执行：

```bash
pip install -r requirements.txt
```

复制配置文件：

```bash
copy .env.example .env
```

编辑 `.env`，填好：

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_ID=
TELEGRAM_CHANNEL_ID=
OPENAI_API_KEY=
OPENAI_MODEL=
GITHUB_TOKEN=
```

启动：

```bash
python bot.py
```

---

## 4. Railway 部署

1. 上传整个项目到 GitHub 仓库。
2. Railway 新建项目，连接这个仓库。
3. 添加环境变量：

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_OWNER_ID
TELEGRAM_CHANNEL_ID
OPENAI_API_KEY
OPENAI_MODEL
GITHUB_TOKEN
CHANNEL_NAME
```

4. Start Command 填：

```bash
python bot.py
```

---

## 5. 常用命令

### 自动找项目并生成草稿

```text
/find
```

带关键词：

```text
/find automation
/find dashboard
/find telegram bot
/find self-hosted
```

机器人会返回：

- 草稿 ID
- GitHub 项目
- 项目地址
- 图片文件名
- 图片提示词
- TG 正文
- 发送命令

---

### 只看候选项目，不生成草稿

```text
/candidates
/candidates automation
```

---

### 指定 GitHub 项目生成草稿

```text
/draft https://github.com/n8n-io/n8n
```

或者：

```text
/draft n8n-io/n8n
```

---

### 查看最新草稿

```text
/last
```

---

### 只看图片提示词

```text
/image 草稿ID
```

如果不带草稿 ID，则默认看最新草稿：

```text
/image
```

---

### 只看 TG 正文

```text
/post 草稿ID
```

不带草稿 ID，则默认看最新草稿：

```text
/post
```

---

### 手动确认发送到频道

```text
/publish 草稿ID
```

注意：只有执行这个命令，才会发频道。

---

### 丢弃草稿

```text
/discard 草稿ID
```

---

## 6. 每日自动生成草稿

默认关闭。

如果你想每天固定时间自动找项目并生成草稿到私聊，不自动发频道，可以在 `.env` 里设置：

```text
AUTO_DRAFT_ENABLED=true
AUTO_DRAFT_HOUR=9
AUTO_DRAFT_MINUTE=0
BOT_TIMEZONE=Asia/Shanghai
```

它只会把草稿发给你审核，不会自动发频道。

---

## 7. 注意事项

1. 第一版不会自动生成图片，只生成图片提示词。你复制提示词到 GPT 生成图。
2. 第一版不会自动发布，必须你手动 `/publish 草稿ID`。
3. 如果 OpenAI 报 `model not found`，把 `.env` 里的 `OPENAI_MODEL` 换成你控制台可用模型。
4. 如果 GitHub 搜索频繁失败，建议填写 `GITHUB_TOKEN`。
5. 机器人会过滤色情、赌博、恶意软件、盗版、灰产等高风险关键词。
6. 文案会刻意避免“稳赚”“暴富”“保证赚钱”等说法，走长期合规路线。
