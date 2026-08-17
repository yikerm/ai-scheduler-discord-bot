# AI Scheduler Discord Bot

整合 Discord、Google Calendar、Gemini、OR-Tools 與機器學習的個人排程助理。

## 主要功能

- Discord Slash Commands 與互動式選單
- Google Calendar 行程建立、更新與增量同步
- 依截止時間、優先度、作息與空檔進行滾動排程
- 長任務分段與每段獨立效率／精神評分
- Random Forest 時段預測與 OR-Tools 最佳化
- 以 Calendar 最終時間及實際分鐘作為訓練資料

## 快速開始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

在 `.env` 填入 Discord、Gemini 與 Google Calendar 設定，並把 Google Service Account JSON 放在本機。請勿提交 `.env` 或憑證 JSON。

```bash
.venv/bin/python main.py
```

## 測試

```bash
DATABASE_URL=sqlite:////tmp/ai_scheduler_bot_tests.db \
  .venv/bin/python -m unittest discover -s tests -v
```

## 文件

- [Discord 指令手冊](BOT_COMMANDS.md)
- [Oracle VM 部署手冊](ORACLE_DEPLOYMENT.md)

## 安全

`.gitignore` 會排除環境變數、憑證、資料庫、虛擬環境、備份與私人開發紀錄。公開前仍應檢查 Git 待提交清單。
