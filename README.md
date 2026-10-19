# Woow_podman_auth_gateway

本 repo 只負責 H618 容器式機器上 **Pi Agent** 與 **Tailscale GUI** 的驗證與反向代理：`woow-auth-gateway` 檢查 Home Assistant 簽發的一次性 ticket，通過後才把請求轉給只聽 loopback 的上游。HA 側配套的 `woow_ingress` 元件與 `woow-auth-iframe-card.js` 卡片也放在這裡（`ha/`）。

它**和 HA ingress 不是同一件事**。HA ingress 是 Supervisor 的 add-on 機制，由 HA 本身代理 add-on 的網頁；容器式機器沒有 Supervisor，本機制是另外做的：HA 只簽發 ticket，瀏覽器直接連 gateway 的埠（`:8088`、`:30142`），由 gateway 驗證並代理。元件名稱 `woow_ingress` 是歷史名稱，不代表它跟 HA ingress 有關；改名為 `woow_auth_gateway` 見 pipeline issue #61。

設計決定見 [`docs/adr/0001-containerized-auth-gateway.md`](docs/adr/0001-containerized-auth-gateway.md)。

## 流程

1. 管理員在 HA 開啟用 `woow-auth-iframe-card` 的儀表板，卡片以 WebSocket 呼叫 `woow_ingress/create_ticket`（只限 admin），拿到 45 秒、一次性的 ticket 與目標埠。
2. 卡片在 iframe 開 `http://<同一主機>:<埠>/auth/launch?ticket=…`。gateway 只接受 Referer 為同主機 `:8123`、非頂層導覽的請求，驗證 HMAC、時效與 `jti`（防重放紀錄只在記憶體，重啟後清空），換發 HttpOnly 的簽章 session cookie（預設 12 小時）。
3. 之後的請求帶 cookie，gateway 代理 HTTP 與 WebSocket 到上游；HTML 回應加 `frame-ancestors <HA 來源>:8123`。直接開、頂層導覽、跨站、沒有或偽造 ticket 的請求一律回 **404**。

| Target | gateway（對外） | 上游（loopback） | cookie | 出現在 |
|---|---:|---|---|---|
| `tailscale-web` | `:8088` | `127.0.0.1:8089` | `woow_ts_session` | H618 基底 |
| `pi-agent` | `:30142` | `127.0.0.1:30141` | `woow_pi_session` | H4G 疊加層 |

## 內容

```
gateway/woow_auth_gateway.py     gateway 程式（Python 3.11 + aiohttp 3.8）
Containerfile                    arm64 image：debian:bookworm-slim（釘 digest）+ python3-aiohttp，USER 10001:10001
quadlet/woow-auth-gateway.container                                 基底：rootful quadlet，只開 Tailscale Web
quadlet/overlays/h4g/woow-auth-gateway.container.d/pi-agent.conf    H4G：加開 Pi Agent
ha/custom_components/woow_ingress/   HA 元件：簽發 ticket（TARGETS 決定 target 與埠）
ha/www/woow-auth-iframe-card.js      HA 卡片：取 ticket、開 iframe
tests/                           CLI 測試、quadlet dry-run、image smoke test
archive/pre-container-service/   容器化之前 .25 上的 systemd service（只供參考，不再安裝）
```

本 repo 不放 install、upgrade 這類腳本。安裝由 `woow_ha_version_control` 的 `restore.sh` 負責：quadlet 放進 `/etc/containers/systemd/`，H4G 另放 drop-in 到 `/etc/containers/systemd/woow-auth-gateway.container.d/`。

## gateway 參數

| 參數 | 預設 | 說明 |
|---|---|---|
| `--secret-file` | image 內為 `/run/secrets/ingress-secret` | hex 編碼，至少 32 bytes |
| `--listen` | `0.0.0.0` | |
| `--tailscale-port` / `--tailscale-upstream` | `8088` / `http://127.0.0.1:8089` | 埠設 `0` 表示不開、不代理 |
| `--pi-port` / `--pi-upstream` | `30142` / `http://127.0.0.1:30141` | 埠設 `0` 表示不開、不代理 |
| `--session-hours` | `12` | |

兩個 target 都設成 `0` 是錯誤，程式直接報錯退出。

## 密鑰

- **產生**：`restore.sh` 產生 `/opt/homeassistant/.woow_ingress_secret`（32 bytes 的 hex，`0640 root:root`），HA 的 `woow_ingress` 元件讀這個檔；接著 `podman secret create woow-ingress-secret /opt/homeassistant/.woow_ingress_secret`，quadlet 以 `Secret=woow-ingress-secret,type=mount,target=ingress-secret,uid=10001,gid=10001,mode=0400` 掛進容器的 `/run/secrets/ingress-secret`。
- **輪換**：HA 讀的檔案和 podman secret **要一起換**，任何一邊沒換，ticket 都驗不過（gateway 回 404）。
  ```sh
  python3 -c 'import secrets; print(secrets.token_hex(32))' > /opt/homeassistant/.woow_ingress_secret.new
  chmod 0640 /opt/homeassistant/.woow_ingress_secret.new
  mv /opt/homeassistant/.woow_ingress_secret.new /opt/homeassistant/.woow_ingress_secret
  podman secret rm woow-ingress-secret
  podman secret create woow-ingress-secret /opt/homeassistant/.woow_ingress_secret
  systemctl restart woow-auth-gateway.service   # 讀新的 secret
  # 再重啟 HA 容器：元件只在啟動時讀密鑰
  ```
  輪換後，所有既有 session 失效，使用者重新從 HA 開啟即可。
- 密鑰是單台身分：不進 image、不進本 repo、不進出貨備份。

## Tailscale Web 必須聽 `127.0.0.1:8089`

gateway 自己佔 `:8088`，轉給 `127.0.0.1:8089`。`woow-tailscale` 的 quadlet 要設 `TS_WEB_LISTEN=127.0.0.1:8089`（`Woow_podman_vpn_tailscale_package` 1.102.3-r2 起是預設）。Tailscale Web 是沒有驗證、可寫入的管理介面，絕不能聽在 loopback 以外。舊版聽 `127.0.0.1:8088` 會和 gateway 撞埠，gateway 起不來。

## 新增 target 要同步改的五個地方

1. `gateway/woow_auth_gateway.py`：在 `run()` 的 target 清單加一個 `Target`（名稱、cookie 名稱），並加 `--<name>-port`／`--<name>-upstream` 參數。
2. `ha/custom_components/woow_ingress/__init__.py`：`TARGETS` 加 `"<name>": <gateway 埠>`。
3. `ha/www/woow-auth-iframe-card.js`：`setConfig` 允許的 target 清單。
4. `quadlet/`：基底或疊加層 drop-in 的 `Exec=` 開那個埠（Containerfile 的 `EXPOSE` 一併更新）。
5. `woow_ha_version_control`：儀表板裡用這張卡片的 `target:` 設定，以及 `restore.sh` 安裝對應的 quadlet／drop-in。

另外記得補 `tests/` 的檢查。

## 版本與發佈

tag 規則是 `<協定版本>-r<N>`，例如 `1.0.0-r1`：

- **協定版本**指 ticket 協定（ticket／session 格式、cookie 名稱、target 集合與埠、404、Referer 與 CSP 規則）。改了協定，`woow_ingress` 元件與卡片要跟著改，協定版本就要升。
- **`-r<N>`** 是同一協定下的打包修訂：gateway 的修正、基底 image 更新等，HA 側不用動。

發佈步驟：

1. 在 PR 裡把 `quadlet/woow-auth-gateway.container` 的 `Image=` 改成新 tag（先不帶 digest），合併。
2. 在 main 上打 tag 並推：`git tag 1.0.0-r2 && git push origin 1.0.0-r2`。`publish` workflow 在 `ubuntu-24.04-arm` 建置、跑完 smoke test 後推到 `ghcr.io/woowtech/woow-auth-gateway:<tag>`，用 skopeo 確認是 arm64，GitHub release 記錄 `IMAGE:TAG@digest`。tag 和 quadlet 不一致、或 ghcr 已有這個 tag，都會拒絕。
3. 把 release 記錄的 digest 補進 `Image=`（`…:<tag>@sha256:…`）再 commit。主機一律依 tag 加 digest 拉取。

已發佈的 tag 不會被覆寫；要重發就升 `-rN`。

## 測試

```sh
python -m unittest discover -s tests       # 需要 aiohttp
tests/quadlet-dryrun.sh                    # 需要 podman 的 quadlet generator
tests/container-smoke.sh <image>           # 會用到本機 :8088 與 :30142，只在 CI 跑
```
