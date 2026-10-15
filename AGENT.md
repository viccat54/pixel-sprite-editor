# 給 agent 的使用說明

像素修圖台的專案檔是 `<名稱>.pixelfixer.json`（手機上「匯出專案」得到的檔）。
`tools/pixelfixer.py` 讓 agent 讀、畫、預覽它。需要 Python 3.9+ 與 Pillow。

## 鐵則

- **不能直接改專案正本。** 先 `propose` 開提案，所有繪圖都進提案。
- 畫完用 `render --proposal` 看結果、`show` 看摘要，交給使用者確認。
- 只有使用者明確說要接受，才跑 `accept`（可用 `--cells` 只接受部分格子）。
- 鎖定的圖層不能畫；不要動使用者沒提到的格子。
- 用了專案裡沒有的顏色，`show` 會列在 `newColors`，回報時要提。

## 座標

- 整張圖座標：左上角 (0,0)，x 往右、y 往下。
- 格子：`欄,列`，從 0 起算。每格 `cell` 像素（`info` 看）。
- 操作加 `"cell":[欄,列]` 時，x、y 改成格子內座標，而且畫不出那一格。
- 圖層：編號（0＝最底層）或名稱。

## 流程

```bash
T=tools/pixelfixer.py; P=專案.pixelfixer.json
python3 $T info $P                                # 大小、格子、圖層、提案
python3 $T dump $P --cell 2,0 --layer 頭髮        # 一格印成文字＋圖例（讀精確座標用）
python3 $T render $P --cell 2,0 --ref hi,prev,next -o view.png   # 放大加格線，並排參考圖
ID=$(python3 $T propose $P --note "補頭髮輪廓")
python3 $T edit $P $ID --layer 頭髮 --ops-file ops.json   # 或 --ops '<JSON>'、或 stdin
python3 $T render $P --cell 2,0 --proposal $ID -o after.png  # 現況｜提案後｜改了哪些像素
python3 $T show $P $ID
# 使用者同意後：
python3 $T accept $P $ID [--cells "2,0;3,0"]
```

`render` 輸出的 PNG 用看圖工具讀；格線每 8 像素加深，紅線是格子邊界。

## 操作（edit 的 JSON，一個物件或陣列）

共同欄位：`layer`（不寫就用 `--layer`）、`cell`（選填）。

| op | 欄位 | 說明 |
|---|---|---|
| `set` | `x,y` 或 `points:[[x,y],…]`、`color` | 點 |
| `line` | `x0,y0,x1,y1,color` | 直線（Bresenham） |
| `rect` | `x,y,w,h,color`、`fill` | 矩形框，`fill:true` 實心 |
| `fill` | `x,y,color` | 油漆桶：只在那一格、只看這一層、四方向同色相連 |
| `replace` | `from,to`、範圍 | 範圍內某色全部換成另一色 |
| `clear` | 範圍 | 清成透明 |
| `grid` | `x,y,rows:[…],legend:{字元:顏色}` | 用文字一次畫一塊。`.`＝透明，空白或 `?`＝不改 |
| `flip` | 範圍、`axis:"h"\|"v"` | 範圍內左右／上下翻轉 |
| `shift` | 範圍、`dx,dy` | 範圍內的不透明像素平移 |
| `copy` | 範圍、`to:[x,y]` 或 `to:{"cell":[c,r]}`、`from_layer`、`mode:"replace"\|"over"` | 複製，`over` 不覆蓋透明 |
| `move_to_layer` | 範圍或 `points:[[x,y],…]`、`to` | 不透明像素搬到別層（蓋掉目標、清掉來源） |
| `revert` | 範圍 | 把提案在這範圍的改動撤掉 |
| `add_layer` | `name`、`below`／`above`（圖層）或 `position:"bottom"` | 新增空白圖層（預設放最上面），之後的操作就能用這個名稱。accept 時才真的建立 |
| `place_layer` | `name`、`below`／`above`／`position` | 改這個提案新增圖層的位置 |

範圍：`x,y,w,h`；有 `cell` 而沒寫 `w,h` 就是整格。
顏色：`#rgb`、`#rrggbb`、`#rrggbbaa`、`transparent`。

`grid` 跟 `dump` 用同一套字元，可以先 dump、改文字、再整塊畫回去：

```json
{"op":"grid","cell":[2,0],"x":12,"y":8,"legend":{"a":"#1a1c2c","b":"#f7cd52"},
 "rows":["..aaaa..",
         ".abbbba.",
         "?abbbba?"]}
```

一次 `edit` 裡任何一個操作失敗，整批都不會存。

## 其他指令

- `list`：列出提案；`reject <id>`：退回
- `history`／`restore <快照>`：每次 accept 前都會存快照（最多 30 份，在 `<名稱>.pfdata/history/`）
- `new --pixel a.png [--hi b.png] [--base c.png] --cell 32`：用 PNG 建專案
- `export -o out.png [--layers-dir 資料夾]`：匯出合併圖或每層一張

## 手機來回

1. 手機「匯出專案」→ 儲存到檔案 → iCloud 雲碟 / ProjectNine-Pixel
2. agent 在 Mac 上對那個檔開提案、畫、給使用者看；使用者同意後 accept
3. 手機「匯入專案」選同一個檔 → 會更新原本那份專案（不會多一份）

注意：第 3 步會整份蓋掉手機上的版本，匯出之後在手機上又改的部分會不見。
