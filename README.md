# 像素修圖台 (Pixel Sprite Editor)

專為修整 AI 產生或自動縮小的**像素角色行走圖（Sprite Sheet）**設計的純靜態網頁工具。上方顯示高解析原圖或人偶作為對照，下方逐格（動畫幀）進行像素級修整與預覽。

## 特色

- **行動優先**：針對 iPhone Safari 直向操作最佳化，支援虛擬十字鍵連塗、雙指同步縮放平移、三指拖曳圈選與半像素鏡像線。
- **智慧取色與還原**：自動對高解析原圖對應區域進行色彩分群排序，支援一鍵還原參考色與選色即塗。
- **自動儲存與離線可用**：專案直接儲存在瀏覽器 IndexedDB，支援匯出／匯入 `.pixelfixer.json` 與透過 iOS 分享表直接將 PNG 存入 iCloud 雲碟。
- **零外部相依**：單一 `index.html`，無建置步驟、無外部網路請求、無追蹤。

## 給 AI agent 用

`tools/pixelfixer.py`（Python＋Pillow）可以讓 Claude、Codex 等 agent 讀取、預覽、繪製 `.pixelfixer.json` 專案。agent 只能送「提案」，使用者確認後才寫回。用法見 [AGENT.md](AGENT.md)。

## 授權

MIT License © viccat54
