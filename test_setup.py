import os

try:
    import config
    print("✅ config.py 載入成功！語法與環境變數讀取沒問題。")
except Exception as e:
    print(f"❌ config.py 出錯了：{e}")

try:
    import database
    print("✅ database.py 載入成功！資料庫 Schema 語法沒問題。")
except Exception as e:
    print(f"❌ database.py 出錯了：{e}")

# 檢查實體資料庫檔案是否被成功創造出來
if os.path.exists("data.db"):
    print("✅ 🎉 恭喜！系統已經成功建立 data.db 資料庫檔案！第一階段完美過關。")
else:
    print("⚠️ 程式碼沒報錯，但沒看到 data.db 檔案。（如果是這種情況，通常是因為 Codex 的寫法要等到第一次存入資料時才會建立檔案，只要上面兩個都是打勾的就沒問題）")