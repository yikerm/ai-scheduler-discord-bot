# Oracle VM 升級、回復與首次設定

本手冊適用於 `~/ai_scheduler_bot` 的 `ai-scheduler-bot.service`。所有 Oracle Cloud 選單與操作說明使用中文名稱。

## 重要原則

- 不上傳本機 `.env`、Service Account JSON、`.venv` 或任何 `data*.db`。
- 正式資料庫在 VM 原地遷移，先使用 SQLite `.backup` 建立一致快照。
- 更新期間先安裝套件，真正切換程式前才停止服務，縮短停機時間。
- 啟動失敗時先停止服務，再還原程式備份與資料庫備份。

## 1. 本機驗證

在本機專案目錄執行：

```bash
.venv/bin/python -m py_compile availability.py bot.py config.py database.py feedback_service.py gcal_service.py main.py ml_engine.py nlp_parser.py nlp_router.py planning_service.py recurrence_service.py rolling_optimizer.py scheduler.py segment_feedback.py settings_parser.py structured_add.py structured_delete.py structured_fixed.py structured_plan.py structured_repeat.py temporal_parser.py
```

```bash
DATABASE_URL=sqlite:////tmp/ai_scheduler_bot_upgrade_tests.db .venv/bin/python -m unittest discover -s tests -v
```

必須顯示 `Ran 74 tests` 與 `OK`。

## 2. VM 備份目前程式

先在 VM 建立可回復的舊版封存檔；此步驟不停止服務：

```bash
cd ~
tar --exclude="ai_scheduler_bot/.venv" --exclude="ai_scheduler_bot/__pycache__" --exclude="ai_scheduler_bot/tests/__pycache__" -czf ai_scheduler_bot-code-before-v7-segment-feedback.tar.gz ai_scheduler_bot
ls -lh ai_scheduler_bot-code-before-v7-segment-feedback.tar.gz
```


## 3. 上傳程式但排除正式資料

在本機終端機執行，將 IP 與金鑰路徑換成實際內容：

```bash
rsync -avP \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='*.json' \
  --exclude='data.db' \
  --exclude='data*.db' \
  --exclude='oracle-data*.db' \
  -e "ssh -i /完整路徑/ssh-private-key.key" \
  /本機專案路徑/ai_scheduler_bot/ \
  <VM_USER>@<VM_PUBLIC_IP>:~/ai_scheduler_bot/
```

## 4. VM 安裝新版依賴

在 VM 執行：

```bash
cd ~/ai_scheduler_bot
.venv/bin/pip install -r requirements.txt
.venv/bin/pip check
```

`pip check` 必須顯示 `No broken requirements found.`。

## 5. 停止服務並備份資料庫

```bash
sudo systemctl stop ai-scheduler-bot
systemctl is-active ai-scheduler-bot
```

狀態必須是 `inactive`。再執行：

```bash
cd ~/ai_scheduler_bot
sqlite3 data.db ".backup 'data.before-v7-segment-feedback.db'"
ls -lh data.db data.before-v7-segment-feedback.db
```

不要刪除既有的資料庫備份。

## 6. 執行資料庫遷移

```bash
cd ~/ai_scheduler_bot
.venv/bin/python -c "import database; print('資料庫遷移完成')"
```

驗證：

```bash
sqlite3 data.db "PRAGMA integrity_check; SELECT MAX(version) FROM schema_migrations; SELECT COUNT(*) FROM feedback; SELECT COUNT(*) FROM pragma_table_info('feedback') WHERE name='segment_id'; SELECT COUNT(*) FROM pragma_table_info('feedback') WHERE name='actual_minutes'; SELECT COUNT(*) FROM pragma_table_info('task_segments') WHERE name='status'; SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='segment_feedback_drafts';"
```

預期依序看到：

```text
ok
6
<應與升級前的 Feedback 筆數相同>
1
1
1
1
```

這表示 SQLite 完整、已套用 schema v6、舊回饋沒有減少，且分段回饋需要的欄位與暫存表都存在。

## 7. 啟動與 Log 驗證

```bash
sudo systemctl start ai-scheduler-bot
systemctl is-active ai-scheduler-bot
```

狀態必須是 `active`。接著：

```bash
sudo journalctl -u ai-scheduler-bot -n 80 --no-pager
```

必須看到：

- `Scheduler started`
- `已登入 Discord：<你的 Bot 名稱>`
- 新增 `schedule_failure_notification_job`
- 新增 `recurrence_maintenance_job`

不能出現新的 `Traceback` 或 `ERROR`。`PyNaCl` 與 `davey` 只是語音功能警告，不影響文字排程。

## 8. 部署後首次設定

資料庫原地遷移會保留目前模式與作息。先輸入：

```text
@Bot 查看目前作息
```

依自己的需求設定平日與週末作息，例如：

```text
@Bot 未開學時星期一到星期五早上九點到晚上十點，星期六日早上十點到晚上八點
```

正式切換模式前，請先設定各模式的平日與週末時間，再指定生效日；所有修改都應核對確認卡片。

如需重建舊版重複任務，先使用 `/delete` 結束舊系列，再建立新系列：

```text
/repeat title:每日閱讀
```

在選單中設定每天、每次一小時，並確認到期方式顯示「每 14 天詢問是否延長」。若要指定期限，在第二頁選擇指定結束日期。

接著執行冒煙測試：

1. `@Bot 新增測試報告` 只開啟 `/add` 選單，不直接建立；在選單完成設定後才寫入。
2. `@Bot 下星期四下午兩點安排 meeting 一小時` 只開啟 `/fixed` 選單，日期、時間與時長維持未選擇。
3. `@Bot 每天閱讀` 只開啟 `/repeat` 選單，完成兩頁設定後才建立系列。
4. `/tasks` 與 `/plan` 顯示新行程。
5. 建立一般任務，收到回饋邀請後先在 Calendar 移動或調整時長，再完成效率與精神評分；資料庫應保存移動後的實際開始、結束與分鐘數，不必等待 `00/30` 分同步。
6. 建立允許分割的長任務，確認每一段結束後都會各自收到效率與精神評分；只完成第一段時，後續分段仍保留。
7. 對其中一段測試「未完成」、原因選單與重新排程；該段 Calendar 應標紅，其他段不受影響。
8. 對測試重複行程執行刪除，確認出現「只刪除此行程／刪除整個系列／取消」。
9. 確認 `journalctl` 中 `recurrence_maintenance_job` 已註冊且沒有錯誤。
10. 用 `/add` 建立一個「緊急」任務，確認卡片顯示優先度；若需要移動既有彈性任務，應先出現「未來 7 天重排建議」，Calendar 在按下確認前不得改變。
11. 以 SQLite 查詢最新 Feedback，確認分段資料具有 `segment_id`、`actual_minutes`，且 `scheduled_start` 是 Calendar 最終時間；完整與未完成混合時父任務狀態為 `partially_completed`。

## 9. 回復舊版

只有新版無法啟動且短時間內不能修復時才使用：

```bash
sudo systemctl stop ai-scheduler-bot
```

將程式還原為升級前備份，並用 SQLite 備份還原 `data.db`。還原資料庫會覆蓋新版上線後產生的資料，因此操作前必須再次建立最新故障快照。

還原後：

```bash
sudo systemctl start ai-scheduler-bot
sudo journalctl -u ai-scheduler-bot -n 50 --no-pager
```
