# Woow_podman_auth_gateway

本 repo 只負責 H618 容器式機器上的兩個**受保護網頁應用**：**Pi Agent** 與 **Tailscale GUI**。一組機制有兩端：

- **`woow-auth-gateway`**（容器）：檢查 Home Assistant 簽發的一次性 ticket，通過後才把請求轉給只聽 loopback 的上游。
- **`woow_protected_apps`**（HA 元件，`ha/`）：簽發 ticket，並把每個啟用的應用註冊成 HA 側邊欄的 panel。

從 2.1.0 起，側邊欄使用 HA Core 自訂整合提供的同網域 ingress：瀏覽器只連 HA 網址，HA 代理 loopback 上游，因此 Cloudflare Tunnel 只需轉發 8123。它不需要 Supervisor。原本的 gateway 容器與 ticket/port 回應保留給舊版客戶端；新版 panel 使用回應中的相對 `url`。（`2.0.0-r1` 以前元件的舊名是 `woow_ingress`，見 ADR-0002。）

設計決定見 [`docs/adr/`](docs/adr/)：0001 容器化；0002 HA 元件改名、改用 panel、協定版本的範圍與 release asset。

## 流程

1. `woow_protected_apps` 依 `configuration.yaml` 的 `targets:` 為每個應用註冊一個只限 admin 的側邊欄 panel（`url_path` 等於 target 名稱）。panel 自己畫 toolbar，只有 HA 的選單按鈕與應用名稱。不用 Lovelace dashboard，因為 dashboard 的 toolbar 由 HA 繪製，HA 的按鈕藏不掉。
2. 管理員透過 HA WebSocket 呼叫 `woow_protected_apps/create_ticket`，取得 45 秒、一次性的 ingress launch URL。
3. panel 的 iframe 開啟此同網域 URL，交換限於該應用路徑、有效一小時的 HttpOnly session cookie。HTTPS 下帶 Secure，並使用 SameSite=Strict。
4. HA 在代理每個 HTTP 請求／WebSocket 握手前檢查 session 與管理員狀態，將請求送到固定 loopback 上游。未認證為 401、跨來源為 403、未知 target 為 404。這些狀態與舊版 gateway 的統一 404 規則不同。

| Target | HA ingress path | 上游（loopback） | 出現在 |
|---|---|---|---|
| `tailscale-web` | `/api/woow_protected_apps/tailscale-web/` | `127.0.0.1:8089` | H618 基底（H2G/H4G） |
| `pi-agent` | `/api/woow_protected_apps/pi-agent/` | `127.0.0.1:30141` | H4G 疊加層 |

舊 gateway 仍在 `:8088`／`:30142` 使用原本的 HMAC ticket 與 12 小時簽章 cookie；Cloudflare 新版側邊欄不經過這兩個埠。

## 內容

```
gateway/woow_auth_gateway.py     gateway 程式（Python 3.11 + aiohttp 3.8）
Containerfile                    arm64 image：debian:bookworm-slim（釘 digest）+ python3-aiohttp，USER 10001:10001
quadlet/woow-auth-gateway.container                                 基底：rootful quadlet，只開 Tailscale Web
quadlet/overlays/h4g/woow-auth-gateway.container.d/pi-agent.conf    H4G：加開 Pi Agent
ha/custom_components/woow_protected_apps/            HA 元件：簽發 ticket、註冊 panel（TARGETS：名稱、埠、標題、圖示）
ha/custom_components/woow_protected_apps/frontend/   panel 的 web component
ha/build-asset.sh                打包元件成 release asset
tests/                           CLI 測試、quadlet dry-run、image smoke test、asset 檢查
tests/ha/                        元件在 HA core（與主機同版）裡的測試
archive/pre-container-service/   容器化之前 .25 上的 systemd service（只供參考，不再安裝；內容是舊名 woow_ingress、ingress-secret）
```

本 repo 不放 install、upgrade 這類腳本。安裝由 `woow_ha_version_control` 的 `restore.sh` 負責：quadlet 放進 `/etc/containers/systemd/`，H4G 另放 drop-in 到 `/etc/containers/systemd/woow-auth-gateway.container.d/`；元件從同一個 release 的 asset 裝進 `/opt/homeassistant/custom_components/`（見「版本與發佈」）。

## HA 元件設定

```yaml
woow_protected_apps:
  targets: [pi-agent, tailscale-web]   # 省略時只有 tailscale-web
```

列出的名稱要在元件的 `TARGETS` 裡，否則設定失敗。H618 基底只開 `tailscale-web`，H4G 兩個都開。

## gateway 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `--secret-file` | image 內為 `/run/secrets/ticket-secret` | hex 編碼，至少 32 bytes |
| `--listen` | `0.0.0.0` | |
| `--tailscale-port` / `--tailscale-upstream` | `8088` / `http://127.0.0.1:8089` | 埠設 `0` 表示不開、不代理 |
| `--pi-port` / `--pi-upstream` | `30142` / `http://127.0.0.1:30141` | 埠設 `0` 表示不開、不代理 |
| `--session-hours` | `12` | |

兩個 target 都設成 `0` 是錯誤，程式直接報錯退出。

## 密鑰

- **產生**：`restore.sh` 產生 `/opt/homeassistant/.woow_ticket_secret`（32 bytes 的 hex，`0640 root:root`），HA 的 `woow_protected_apps` 元件讀這個檔；接著 `podman secret create woow-ticket-secret /opt/homeassistant/.woow_ticket_secret`，quadlet 以 `Secret=woow-ticket-secret,type=mount,target=ticket-secret,uid=10001,gid=10001,mode=0400` 掛進容器的 `/run/secrets/ticket-secret`。
- **輪換**：HA 讀的檔案和 podman secret **要一起換**，任何一邊沒換，ticket 都驗不過（gateway 回 404）。
  ```sh
  python3 -c 'import secrets; print(secrets.token_hex(32))' > /opt/homeassistant/.woow_ticket_secret.new
  chmod 0640 /opt/homeassistant/.woow_ticket_secret.new
  mv /opt/homeassistant/.woow_ticket_secret.new /opt/homeassistant/.woow_ticket_secret
  podman secret rm woow-ticket-secret
  podman secret create woow-ticket-secret /opt/homeassistant/.woow_ticket_secret
  systemctl restart woow-auth-gateway.service   # 讀新的 secret
  # 再重啟 HA 容器：元件只在啟動時讀密鑰
  ```
  輪換後，所有既有 session 失效，使用者重新從 HA 開啟即可。
- 密鑰是單台身分：不進 image、不進本 repo、不進出貨備份。

## Tailscale Web 必須聽 `127.0.0.1:8089`

gateway 自己佔 `:8088`，轉給 `127.0.0.1:8089`。H618 的 Tailscale 裝在主機上（`woow_ha_version_control` ADR-0006，待演練），由 `tailscale-web.service` 聽這個位址；其他機器若用 `woow-tailscale` 容器，要設 `TS_WEB_LISTEN=127.0.0.1:8089`（`Woow_podman_vpn_tailscale_package` 1.102.3-r2 起是預設）。Tailscale Web 的節點登入介面沒有額外平台驗證，必須只聽 loopback；登入後完整管理仍受 Tailscale 原生權限限制。舊版聽 `127.0.0.1:8088` 會和 gateway 撞埠，gateway 起不來。

## 新增 target 要同步改的四個地方

1. `gateway/woow_auth_gateway.py`：在 `run()` 的 target 清單加一個 `Target`（名稱、cookie 名稱），並加 `--<name>-port`／`--<name>-upstream` 參數。
2. `ha/custom_components/woow_protected_apps/__init__.py`：`TARGETS` 加 `"<name>": Target(<gateway 埠>, "<標題>", "<mdi 圖示>")`。panel 不用改。
3. `quadlet/`：基底或疊加層 drop-in 的 `Exec=` 開那個埠（Containerfile 的 `EXPOSE` 一併更新）。
4. `woow_ha_version_control`：要開這個應用的機器，在 `configuration.yaml` 的 `woow_protected_apps: targets:` 加上名稱；`restore.sh` 安裝對應的 quadlet／drop-in。

另外記得補 `tests/` 的檢查。新增 target 會改協定，協定版本要升。

## 版本與發佈

tag 規則是 `<協定版本>-r<N>`，例如 `1.0.0-r1`：

- **協定版本**涵蓋整組機制兩端的介面，任一邊改了都要升：
  - gateway 側：ticket／session 格式、cookie 名稱、target 集合與埠、404、Referer 與 CSP 規則；
  - HA 側：WebSocket 指令（名稱、參數、回應）、元件的 domain 與設定（`targets:`）；
  - 兩端共用密鑰的名稱與位置。

  不相容的改變升主版號，例如 `2.0.0-r1` 把 `woow_ingress/create_ticket`（舊名）改成 `woow_protected_apps/create_ticket`。
- **`-r<N>`** 是同一協定下的打包修訂：gateway 的修正、基底 image 更新、元件不影響介面的修正等。

每個 tag 發佈兩樣東西，掛在同一個 GitHub release：image（記錄 `IMAGE:TAG@digest`）與元件 asset `woow_protected_apps-<tag>.tar.gz`（記錄 sha256；解開是 `custom_components/woow_protected_apps/`，不含 `__pycache__`；同一個 commit 打包出來的 sha256 相同）。`restore.sh` 用同一個 tag 拉 image、下載 asset，兩者都核對過才動主機。

發佈步驟：

發佈在 release PR 的分支上完成，main 上的 `Image=` 永遠是 tag 加 digest：

1. 開 release 分支，把 `quadlet/woow-auth-gateway.container` 的 `Image=` 改成新 tag（這時還沒有 digest），推上去開 PR。此時 `image` workflow 的「The quadlet pins the image by tag and digest」會失敗，這是預期的，它就是合併的門檻。
2. 在這個分支的 HEAD 打 tag 並推：`git tag 1.0.0-r2 && git push origin 1.0.0-r2`。`publish` workflow 在 `ubuntu-24.04-arm` 建置、跑完 smoke test、打包並檢查元件 asset 後推到 `ghcr.io/woowtech/woow-auth-gateway:<tag>`，用 skopeo 確認是 arm64；GitHub release 記錄 `IMAGE:TAG@digest` 與 asset 的 sha256，並附上 asset。tag 和 quadlet 不一致、ghcr 已有這個 tag、或查不出 ghcr 上有沒有這個 tag，都會拒絕。
3. 在同一個分支把 release 記錄的 digest 補進 `Image=`（`…:<tag>@sha256:…`），CI 轉綠後合併。主機一律依 tag 加 digest 拉取。

已發佈的 tag 不會被覆寫；要重發就升 `-rN`。如果推上 ghcr 之後建立 release 失敗，重跑會被「tag 已存在」擋下，這時依 run 紀錄裡的 digest 手動 `gh release create`。

## 測試

```sh
python -m unittest discover -s tests       # 需要 aiohttp
pip install -r tests/ha/requirements.txt   # Linux、Python 3.14；HA core 與主機同版
python -m pytest -c tests/ha/pytest.ini tests/ha
tests/ha-asset-check.sh "$(ha/build-asset.sh dev /tmp)"
tests/quadlet-dryrun.sh                    # 需要 podman 的 quadlet generator
tests/container-smoke.sh <image>           # 會用到本機 :8088 與 :30142，只在 CI 跑
```


## HA 同網域 ingress（2.1.0）

側邊欄透過 HA 的 `/api/woow_protected_apps/pi-agent/` 與
`/api/woow_protected_apps/tailscale-web/` 代理本機服務。Cloudflare 只需將 HA
網域轉到 HA 的 8123，無須另開 Pi Agent 或 Tailscale 連接埠。
這是 Core 自訂整合提供的代理，不依賴 HAOS Supervisor。

HA 必須能連到本機 `127.0.0.1:30141`（Pi Agent）與
`127.0.0.1:8089`（Tailscale Web）；目前部署使用 host networking。
HTTPS 反向代理需正確傳遞 forwarded headers，並在 HA 設定可信任代理。

管理員從已認證的 HA WebSocket API 取得 45 秒、一次性啟動票券，
換取限於該應用路徑的 HttpOnly / SameSite=Strict session cookie；
HTTPS 下另加 Secure。每次請求重新檢查使用者仍為有效管理員；已建立的 WebSocket 與 SSE
每 15 秒內重查撤權／登出，並在 session 到期時關閉。
Session 一小時到期，重新開啟側邊欄頁面可取得新票券。
HTTP、SSE、WebSocket 走同一代理；HA Authorization 不會傳給後端。

前端 bridge 會改寫應用的根路徑資源與 API 請求。此相容層已針對
目前的 Pi Web 與 Tailscale Web 測試；更新應用後需再次驗證。
Tailscale 本身的登入流程仍保留。

可獨立執行 `python -m pytest tests/ingress` 驗證權限、票券重放、
跨應用隔離、跨來源拒絕、串流與 WebSocket。此測試以假的 HA user store
搭配真的 aiohttp transport 執行，請與 `tests/ha` 分開啟動 pytest。

Tailscale Web 1.104.5 啟動時必須加上
`--prefix /api/woow_protected_apps/tailscale-web`。非 CGI 模式的 HTTP 上游
仍收到去掉 prefix 的路徑；這個旗標讓登入後的前端 Router 使用正確的 base。
沒有它時，未登入畫面正常，但登入 tailnet 後會出現 `Page not found`。
原生 `tailscale web --readonly=false` 仍是 LoginServerMode：CF 可顯示裝置、
登入節點及瀏覽設定，但完整管理模式需要 tailnet 的來源身分與授權，
不能宣稱 HA 管理員驗證已取代 Tailscale 的管理授權。

Pi Web 的 Next.js 會將根路徑移除尾端斜線；bridge 必須將有／無斜線的
掛載根視為同一路徑，否則選擇專案或對話時會重複 prefix 而回傳 404。
CSS custom property 的圖示 URL 也需改寫。`node --test tests/frontend/*.cjs`
涵蓋這兩個由 CF 實際操作發現的回歸案例。

同網域 iframe 的兩個應用視為受信任的管理程式：其 JavaScript 與 HA 共用
origin，可存取同來源的父頁面與瀏覽器儲存。路徑 cookie 隔離防止憑證被
代理到錯誤後端，並不提供惡意應用程式之間的瀏覽器安全隔離；不要把任意
第三方網頁加入 target。若需要互不信任的應用隔離，應改用獨立 origin。

2026-11-15 的 H4G 實機 CF 驗收（HA 2026.7.2／Pi Web 0.10.0／Tailscale 1.104.5）：
實際瀏覽器已驗證專案選擇、既有對話開啟與重新整理、新對話與切回、檔案
瀏覽與圖示、側邊欄往返、Tailscale 詳情與重新整理、SSE，以及兩個 ingress
未登入回 401。另以既有模型送出一筆短提示並收到回覆。這不代表 Tailscale
完整管理、iOS 推播、所有外部模型／MCP 服務均已驗收；H2G 乾淨實機還原
也需另外演練。
