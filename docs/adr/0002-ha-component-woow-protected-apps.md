# 0002 HA 元件改名 `woow_protected_apps`、改用 panel、協定版本涵蓋兩端、以 release asset 發佈

- 狀態：已採用（2026-10-19）
- 來源：pipeline issue container-machines/61（2026-10-19 triage 定案，併入 #67）；`woow_ha_version_control` ADR-0005 的 2026-10-19 修訂

## 背景

ADR-0001 把 gateway 容器化時，HA 側原樣收進 repo：元件 `woow_ingress` 只註冊 WebSocket 指令 `woow_ingress/create_ticket`，讀 `/config/.woow_ingress_secret`；卡片 `woow-auth-iframe-card.js` 用這個指令取 ticket 再開 iframe。.25 上兩個應用各是一個 YAML 模式的 Lovelace dashboard，裡面放一張卡片。

幾個問題：

- `woow_ingress` 容易和 HA ingress（Supervisor 讓 HA 介面開 add-on 的機制）混淆，但兩者無關。
- dashboard 的 toolbar 由 HA 繪製，除了名稱還有搜尋、Assist、編輯等 HA 按鈕，卡片管不到（#67）。
- 協定版本只涵蓋 gateway 側，WebSocket 指令改名這種不相容的變更不在定義裡。
- 元件與卡片沒有發佈方式，`restore.sh`（#50）沒有東西可以依版本安裝。
- 密鑰以 `ingress` 命名，與內容無關。

## 決定

1. **元件改名 `woow_protected_apps`**，WebSocket 指令改成 `woow_protected_apps/create_ticket`。不用 `woow_auth_gateway`：它和 gateway 容器 `woow-auth-gateway` 幾乎同名，但兩者職責相反——元件簽發 ticket、註冊 panel，gateway 驗證 ticket、代理請求。名稱取自 version_control `CONTEXT.md` 的詞條「受保護網頁應用（Protected Web App）」。gateway 與 image 維持 `woow-auth-gateway`。
2. **改用元件註冊的 panel，刪除卡片**：
   - `configuration.yaml` 的 `woow_protected_apps: targets: [...]` 列出這台開哪些應用，預設只有 `tailscale-web`。名稱不在元件的 `TARGETS` 裡，設定就失敗。`TARGETS` 是 HA 側唯一的名稱、埠、標題、圖示清單。
   - setup 時對每個啟用的 target 以 `panel_custom` 註冊 panel：`url_path` 等於 target 名稱，`require_admin`，顯示在側邊欄。panel 的 web component 放在元件目錄，由元件以 static path 提供，不需要 `www/` 檔案或 Lovelace resource。
   - panel 自己畫 toolbar，只有 HA 的 `ha-menu-button` 與應用名稱，下方 iframe。取 ticket、組網址、`allow`、`referrerPolicy` 與舊卡片相同。
   - `create_ticket` 只接受啟用的 target。
   - 不用 dashboard 的理由：dashboard 的 toolbar 由 HA 繪製，HA 按鈕藏不掉。用 kiosk-mode 這類前端套件，多一個跟 HA 版本走的依賴；從卡片鑽進 HA 的 shadow DOM 藏按鈕，HA 前端一改版就壞。
3. **協定版本涵蓋整組機制兩端的介面**：gateway 側（ticket／session 格式、cookie、target 與埠、404、Referer 與 CSP），HA 側（WebSocket 指令、元件 domain 與設定），以及共用密鑰的名稱與位置。這次指令改名不相容，發 `2.0.0-r1`。ticket 線上格式與 gateway 行為不變。
4. **密鑰以內容命名**：HA 檔 `/opt/homeassistant/.woow_ticket_secret`，podman secret `woow-ticket-secret`，容器內 `/run/secrets/ticket-secret`（uid/gid 10001）。
5. **元件以 release asset 發佈**：推 tag 時 publish workflow 另外打包 `woow_protected_apps-<tag>.tar.gz`（解開是 `custom_components/woow_protected_apps/`，不含 `__pycache__`，可重現），掛在 image 的同一個 GitHub release，release 記錄 image digest 與 asset sha256。`restore.sh` 依同一個 tag 拉 image、下載 asset 並核對 sha256，與 image digest 釘在一起；元件不走 Config 層（白名單排除它的目錄）。
6. **元件在真的 HA core 裡測試**：CI 以 `pytest-homeassistant-custom-component` 跑元件，HA core 版本與主機相同（目前 2026.7.2）；ticket 由 gateway 程式自己的 `verify()` 檢查。

## 後果

- 主機上 HA 側只有元件與 `configuration.yaml` 的一段設定，沒有 dashboard、`www/` 檔案或 Lovelace resource。panel 與 dashboard 不能同時佔用同一個 `url_path`，從舊做法切換時要在同一次變更裡完成。
- 新增 target 要同步改的地方從五個變成四個（README）：gateway 的 target、元件 `TARGETS`、quadlet、各機器的 `targets:`。
- 主機升級 HA core 時，`tests/ha/requirements.txt` 要跟著改，確認元件與 panel 在新版仍可用。
- `archive/pre-container-service/` 保留舊名，作為 .25 容器化之前的紀錄。
