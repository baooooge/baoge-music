# BaoGe Music Bot

A production-grade, high-fidelity Discord music bot built for studio-grade audio quality, multi-platform streaming, and intelligent autoplay.

Official Site: [https://bybaoge.com](https://bybaoge.com)
Discord Community: [https://discord.gg/92BB9zGRmS](https://discord.gg/92BB9zGRmS)
License: AGPL-3.0

---

## Language Navigation / 語言導覽 / 语言导航 / 言語ナビゲーション

* [English](#english)
* [繁體中文](#繁體中文)
* [简体中文](#简体中文)
* [日本語](#日本語)

---

<a id="english"></a>
## English

### Overview
BaoGe Music Bot is a self-hosted Discord music bot engineered for stability, multi-platform support, dynamic DSP filtering, and studio audio purity.

### Key Features
* Multi-Platform Support: YouTube, Bilibili, StreetVoice (HLS direct extraction), KKBOX (Widget DOM parsing), Spotify, Apple Music.
* Audio Purity Filter: Automatically excludes live recordings, unofficial covers, and low-quality audio to ensure studio-grade playback.
* Smart Autoplay: Automatically transitions into official radio recommendations when the queue ends.
* Dynamic DSP Equalizer: Real-time EQ presets (Flat, Bass Boost, Bass Extreme, Vocal Boost, Treble Boost) and seamless 15-second seeking.
* Internationalization (i18n): Automatic guild language detection and manual switching via `/language` or `/lg` (en_US, zh_TW, zh_CN, ja_JP).
* Interactive Control Panel: In-message real-time progress bar, volume controls, and playback management buttons.

### Quick Start (Docker)
1. Clone the repository:
   git clone https://github.com/your-username/baoge-music.git
   cd baoge-music

2. Configure environment:
   cp .env.example .env
   (Edit .env and enter your DISCORD_TOKEN)

3. Run the container:
   docker compose up -d --build

### Command Reference
* `/play <search>`: Stream audio from title or supported URL.
* `/language` (`/lg`): Change server interface language.
* `/nowplaying`: Display current audio track information.
* `/queue`: View paginated playback queue.
* `/seek <timestamp>`: Seek playback position (e.g. `1:30` or `90`).
* `/equalizer`: Select real-time DSP equalizer presets.
* `/skip`: Skip current track (supports vote skip and administrator override).
* `/skipto <index>`: Jump directly to a specific track in the queue.
* `/loop`: Toggle between off, single-track loop, and queue loop.
* `/shuffle`: Randomize current queue order.
* `/autoleave`: Toggle automatic disconnect when voice channel is empty (24/7 mode toggle).
* `/volume <1-200>`: Adjust playback volume percentage.
* `/stop`: Clear queue, stop playback, and disconnect.

### License & Attribution
Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
* Original Author: BaoGe
* Website: https://bybaoge.com
* Community: https://discord.gg/92BB9zGRmS
Under AGPL-3.0 Section 7, all distributions, forks, and deployments must retain the original author credits, website links, and community links.

[Back to Top](#language-navigation--語言導覽--语言导航--言語ナビゲーション)

---

<a id="繁體中文"></a>
## 繁體中文

### 專案簡介
BaoGe Music Bot 是一款專為錄音室級高保真音質設計的 Discord 音樂機器人，具備全平台解析、原音純化演算法與動態控制面板。

### 核心功能
* 全平台音訊支援：原生解析 YouTube、Bilibili、StreetVoice（街聲 HLS 直鏈）、KKBOX（Widget 穿透）、Spotify 與 Apple Music。
* 原音純化過濾器：自動過濾現場演唱（Live）、非官方翻唱（Cover）與雜音，確保輸出最高音質母帶。
* 智慧電台續播：播放清單播畢後，自動銜接官方廣播推薦曲目，音樂不中斷。
* 動態 DSP 等化器：即時套用等化器（原音、重低音、極限低音、人聲清晰、通透高音）與無縫 ±15 秒快進快退。
* 多國語言架構：自動偵測伺服器預設語系，並支援使用 `/language` 或 `/lg` 即時切換（繁中、簡中、英文、日文）。
* 視覺化控制面板：點歌自動喚出動態進度條與全功能按鈕操控介面。

### 快速部署 (Docker)
1. 下載專案原始碼：
   git clone https://github.com/your-username/baoge-music.git
   cd baoge-music

2. 設定環境變數：
   cp .env.example .env
   (編輯 .env 並填入你的 DISCORD_TOKEN)

3. 啟動服務：
   docker compose up -d --build

### 指令清單
* `/play <歌名/網址>`：播放指定歌曲或平台播放清單。
* `/language` (`/lg`)：設定伺服器介面語言。
* `/nowplaying`：查看當前播放曲目詳細資訊。
* `/queue`：查看分頁待播清單。
* `/seek <時間>`：跳轉播放進度（例：`1:30` 或 `90`）。
* `/equalizer`：切換即時 DSP 等化器效果。
* `/skip`：跳過當前歌曲（支援投票與管理員覆寫）。
* `/skipto <序號>`：跳轉至清單中指定編號的歌曲。
* `/loop`：切換關閉、單曲循環或清單循環。
* `/shuffle`：隨機打亂目前待播隊列。
* `/autoleave`：切換無人自動離線功能（24/7 常駐掛台開關）。
* `/volume <1-200>`：調整即時播放音量。
* `/stop`：清空隊列、停止播放並離開語音頻道。

### 授權協議與署名規範
本專案採用 GNU Affero General Public License v3.0 (AGPL-3.0) 授權。
* 原創作者：BaoGe
* 官方網站：https://bybaoge.com
* 官方社群：https://discord.gg/92BB9zGRmS
依據 AGPL-3.0 第 7 條規定，任何修改、衍生版本或伺服器託管均必須完整保留原作者署名、官方網站與社群連結。

[回到頂部](#language-navigation--語言導覽--语言导航--言語ナビゲーション)

---

<a id="简体中文"></a>
## 简体中文

### 项目简介
BaoGe Music Bot 是一款专为录音室级高保真音质设计的 Discord 音乐机器人，具备全平台解析、原音纯化算法与动态控制面板。

### 核心功能
* 全平台音频支持：原生解析 YouTube、Bilibili、StreetVoice（街声 HLS 直链）、KKBOX（Widget 穿透）、Spotify 与 Apple Music。
* 原音纯化过滤器：自动过滤现场演唱（Live）、非官方翻唱（Cover）与杂音，确保输出高音质母带。
* 智能电台续播：播放列表播毕后，自动衔接官方广播推荐曲目，音乐不中断。
* 动态 DSP 均衡器：实时套用均衡器（原音、重低音、极限低音、人声清晰、通透高音）与无缝 ±15 秒快进快退。
* 多语言架构：自动检测服务器默认语系，并支持使用 `/language` 或 `/lg` 实时切换（繁中、简中、英文、日文）。
* 可视化控制面板：点歌自动唤出动态进度条与全功能按钮操作界面。

### 快速部署 (Docker)
1. 下载项目源码：
   git clone [https://github.com/your-username/baoge-music.git](https://github.com/your-username/baoge-music.git)
   cd baoge-music

2. 配置环境变量：
   cp .env.example .env
   (编辑 .env 并填入你的 DISCORD_TOKEN)

3. 启动服务：
   docker compose up -d --build

### 指令列表
* `/play <歌名/网址>`：播放指定歌曲或平台播放列表。
* `/language` (`/lg`)：设置服务器界面语言。
* `/nowplaying`：查看当前播放曲目详细信息。
* `/queue`：查看分页待播列表。
* `/seek <时间>`：跳转播放进度（例如：`1:30` 或 `90`）。
* `/equalizer`：切换实时 DSP 均衡器效果。
* `/skip`：跳过当前歌曲（支持投票与管理员覆盖）。
* `/skipto <序号>`：跳转至列表中指定编号的歌曲。
* `/loop`：切换关闭、单曲循环或列表循环。
* `/shuffle`：随机打乱当前待播队列。
* `/autoleave`：切换无人自动离线功能（24/7 常驻挂台开关）。
* `/volume <1-200>`：调整实时播放音量。
* `/stop`：清空队列、停止播放并离开语音频道。

### 开源协议与署名规范
本项目采用 GNU Affero General Public License v3.0 (AGPL-3.0) 授权。
* 原创作者：BaoGe
* 官方网站：[https://bybaoge.com](https://bybaoge.com)
* 官方社群：[https://discord.gg/92BB9zGRmS](https://discord.gg/92BB9zGRmS)
依据 AGPL-3.0 第 7 条规定，任何修改、衍生版本或服务器托管均必须完整保留原作者署名、官方网站与社群链接。

[回到顶部](#language-navigation--語言導覽--语言导航--言語ナビゲーション)

---

<a id="日本語"></a>
## 日本語

### プロジェクト概要
BaoGe Music Bot は、スタジオ品質の高音質再生、マルチプラットフォーム解析、および直感的な操作パネルを備えた Discord 音楽ボットです。

### 主な機能
* マルチプラットフォーム対応: YouTube、Bilibili、StreetVoice（HLS 直リンク抽出）、KKBOX（Widget 解析）、Spotify、Apple Music。
* ピュアオーディオフィルター: ライブ録音（Live）や非公式カバー（Cover）を自動除外し、スタジオマスター音源を再生。
* 自動連続再生: キュー終了時に公式ラジオミックスから推薦曲を自動再生。
* リアルタイム DSP イコライザー: EQ プリセット（原音、低音強化、超重低音、ボーカル強調、高音明瞭）と ±15 秒のシームレススキップ。
* 多言語サポート: サーバーの言語設定を自動認識。`/language` または `/lg` で手動切り替え可能（日本語、英語、繁体字、簡体字）。
* インタラクティブ操作パネル: 再生時に視覚的な進行バーと操作ボタンを自動生成。

### クイックスタート (Docker)
1. リポジトリのクローン:
   git clone [https://github.com/your-username/baoge-music.git](https://github.com/your-username/baoge-music.git)
   cd baoge-music

2. 環境変数の設定:
   cp .env.example .env
   (.env を編集して DISCORD_TOKEN を入力)

3. コンテナの起動:
   docker compose up -d --build

### コマンド一覧
* `/play <曲名/URL>`: 曲やプレイリストの再生。
* `/language` (`/lg`): サーバー言語の設定。
* `/nowplaying`: 現在再生中の曲情報の表示。
* `/queue`: 待機キューの確認。
* `/seek <時間>`: 再生位置の指定（例: `1:30` または `90`）。
* `/equalizer`: イコライザープリセットの切り替え。
* `/skip`: 現在の曲をスキップ（投票および管理者特権に対応）。
* `/skipto <番号>`: キュー内の指定曲へジャンプ。
* `/loop`: ループなし、1曲ループ、全体ループの切り替え。
* `/shuffle`: キュー内の曲順をシャッフル。
* `/autoleave`: ボイスチャンネル無人時の自動退出切り替え（24/7 常駐対応）。
* `/volume <1-200>`: 音量の調整。
* `/stop`: 再生を停止し、キューを削除してボイスチャンネルから退出。

### ライセンスと帰属表示
本プロジェクトは GNU Affero General Public License v3.0 (AGPL-3.0) に基づいて公開されています。
* 原作者: BaoGe
* 公式サイト: [https://bybaoge.com](https://bybaoge.com)
* 公式コミュニティ: [https://discord.gg/92BB9zGRmS](https://discord.gg/92BB9zGRmS)
AGPL-3.0 第 7 条に基づき、本ソフトウェアの改変、再配布、およびホスティングにおいては、原作者の帰属表示、公式サイト、およびコミュニティリンクの維持が義務付けられています。

[トップへ戻る](#language-navigation--語言導覽--语言导航--言語ナビゲーション)