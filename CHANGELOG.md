# 更新日誌 (Changelog)

本專案遵循語意化版本（Semantic Versioning）。

---

## [v1.1.7] - 2026-10-08

### ⚡ 效能與架構極限優化 (Performance & Reliability)
- **Discord Gateway 速率限制防禦**：
  - 實作指令指紋雜湊快取 (`COMMANDS_HASH_FILE`)，啟動時比對斜線指令變更，無異動自動略過全域 `tree.sync()`，避免浪費 API 與 WebSocket 配額。
  - 平滑化語音狀態重連（Staggered Reconnect），於 `restore_playback_state` 將併發信號量縮減為 2，並引入 0.4 秒階梯式間隔與 10 秒連線超時防護，消除重啟時觸發「waiting 59s ratelimit」卡頓。
  - 智慧狀態更新（Presence Throttling），於動態狀態循環中加入內容變更判定與 300 秒心跳防禦，減少無謂的 WebSocket 廣播封包。
  - 強化 `_delayed_seek` 異常攔截機制，杜絕未捕獲 Task 異常。

---

## [v1.1.6] - 2026-10-08

### 🐛 錯誤修復 (Bug Fixes)
- **消除 10062 Unknown Interaction 逾時異常**：
  - 重構 `/stop` 斜線指令與控制面板「停止」按鈕的回應執行順序。
  - 改採優先回傳 Discord 交互確認訊息（耗時 < 50ms），隨後再非同步執行語音頻道中斷連線，徹底根除語音斷線握手延遲導致超過 3 秒交互過期問題。

---

## [v1.1.5] - 2026-10-08

### ✨ 新增功能 (Features)
- **Bilibili 合集與多 P 歌單完整解析**：
  - 新增 `_resolve_bilibili` 解析管線，支援追蹤 `b23.tv` 短連結。
  - 透過 Gzip 解壓直接提取 `window.__INITIAL_STATE__`，完整解析 `ugc_season`（合集）與多 P 列表，支援一次將整張合集（如 30 首）全數排入待播隊列。
- **自動續播容錯與種子連續性強化**：
  - 在 `GuildPlayer` 新增 `last_played_meta` 常駐種子保存，隊列播畢時依序自 `current -> current_meta -> last_played_meta -> history[-1]` 提取推薦種子，避免推薦抖動時續播中斷。
  - 增強推薦標題特殊符號（`【】《》『』` 等）清洗與官方版本雙重 fallback 檢索機制。

### 🧪 壓力測試 (Stress Testing)
- 通過 100 輪、300 伺服器全矩陣併發壓力測試，平均吞吐量達 5,746 req/s，Event Loop 延遲 8.29ms，零記憶體洩漏。

---

## [v1.1.4] - 2026-10-07

### ⚡ 效能與高併發優化
- 拔除 `on_ready` 中逐伺服器 `tree.sync()` 迴圈，杜絕 Discord API 429 限流風險。
- 優化搜尋引擎評分模型，優先選擇官方釋出版本（Official Music Video、Lyric Video、Topic、工作室版本），嚴格過濾第三方翻唱與 Remix。
- 修正 `seek()` `try...finally` 死鎖防禦。
- 修正投票跳歌人數計算與顯示邏輯。
- 修正 FFmpeg 終止時的 Broken Pipe 異常與日誌壓制。
- 實作單主機 4C8G 極限最佳化（64k 音質、FFmpeg 串流單執行緒、記憶體硬限制）。
