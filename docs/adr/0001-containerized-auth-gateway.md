# 0001 woow-auth-gateway 容器化：rootful quadlet、host 網路、podman secret、target 可關

- 狀態：已採用（2026-10-19）
- 來源：pipeline issue container-machines/59（2026-10-15 triage 定案）與 /60；`woow_ha_version_control` ADR-0005

## 背景

`woow-auth-gateway` 原本只存在第 9 台參考實機 .25 的 `/opt/woow-deploy/overlays/woow-auth-gateway/`，沒有版本控制。它是 rootful 主機上的 systemd service，以 `woow-gateway` 帳號執行，用 `LoadCredential` 取得 `/opt/homeassistant/.woow_ingress_secret`，在 `:30142`（Pi Agent）與 `:8088`（Tailscale Web）前面驗證 HA `woow_ingress` 元件簽發的 HMAC ticket。兩個 target 寫死不能關，pi 的預設上游 `30143` 已過時（.25 實際以參數改成 `30141`）。HA 側的元件與卡片也沒有 repo。

ADR-0005 把 H618 規格改成「基底 + H4G 疊加層」，由 `restore.sh` 從乾淨 Armbian 建出，自建 image 發佈到 ghcr.io/woowtech 並依 arm64 manifest digest 拉取。基底只有 Tailscale，Pi Agent 只在 H4G。這個 gateway 要跟著變成 image，而且要能只開其中一個 target。

## 決定

1. **範圍與名稱**：repo `WOOWTECH/Woow_podman_auth_gateway`，image `ghcr.io/woowtech/woow-auth-gateway`。repo 收整套機制：gateway、`woow_ingress` 元件、iframe card。名稱刻意不用 ingress、sidebar 這類字眼，避免被誤會成管理所有 HA ingress 或側邊欄；README 寫明只負責 Pi Agent 與 Tailscale GUI。元件名稱 `woow_ingress` 暫不改，改名與移進客製層見 #61。
2. **第一個 commit 是 .25 的原樣**（gateway、README、unit 與 drop-in、元件、卡片；不含 `__pycache__` 與密鑰），之後的修改另外 commit，可以從歷史看出容器化改了什麼。
3. **target 可關**：`--pi-port 0`／`--tailscale-port 0` 表示不開那個埠、不代理那個 target；兩個都關是錯誤。pi 預設上游改成 `http://127.0.0.1:30141`。其餘參數名稱不變。
4. **協定不變**：ticket／session 格式、cookie 名稱、404 行為、Referer 與 CSP 規則都不動，所以 `woow_ingress` 元件與卡片不用改，HA 的 `TARGETS` 也不變。
5. **image**：`debian:bookworm-slim` 釘 digest（與 `Woow_podman_pi_agent_package` 同一個），apt 裝 `python3-aiohttp`，和 .25 現行的 Python 3.11／aiohttp 3.8.4 一致，不另外打包 Python 相依。`USER 10001:10001`，image 內不含任何密鑰，預設讀 `/run/secrets/ingress-secret`。
6. **rootful quadlet，`Network=host`**：上游分屬不同的使用者命名空間——Tailscale Web 在 rootful `woow-tailscale` 的 `127.0.0.1:8089`，pi-web 是 `pi` 帳號的 rootless 容器，經 rootlessport 聽 `127.0.0.1:30141`。只有 host 網路能同時連到兩邊的 loopback，也能直接在主機上開 HA 卡片指定的 `:8088`、`:30142`。容器另設 `ReadOnly=true`、`NoNewPrivileges=true`、`DropCapability=all`。
7. **基底只開 Tailscale，H4G 用 drop-in 加 Pi Agent**：基底 `woow-auth-gateway.container` 的 `Exec=--pi-port 0`；H4G 疊加層的 `woow-auth-gateway.container.d/pi-agent.conf` 以 `Exec=--pi-port 30142 --pi-upstream http://127.0.0.1:30141` 取代（quadlet 取最後一個 `Exec=`）。
8. **密鑰用 podman secret**：`Secret=woow-ingress-secret,type=mount,target=ingress-secret,uid=10001,gid=10001,mode=0400`，取代 `LoadCredential`。`restore.sh` 產生 `.woow_ingress_secret` 後 `podman secret create`；輪換時 HA 讀的檔案和 podman secret 一起換。密鑰是單台身分，不進 image、repo 或出貨備份。
9. **啟動順序只設 `After=`／`Wants=network-online.target`**。原本的 `After=tailscale-web.service` 拿掉：pi-web 是 rootless 的使用者 unit，system unit 不能對它設 `After=`；上游沒起來時 gateway 回 502，上游起來後自然恢復，不需要排序。
10. **Tailscale Web 改聽 `127.0.0.1:8089`**，gateway 對外維持 `:8088`。`woow-tailscale` quadlet 原本的 `127.0.0.1:8088` 會和 gateway 撞埠（`Woow_podman_vpn_tailscale_package` 1.102.3-r2 已改，#39）。
11. **tag 是 `<協定版本>-r<N>`**，第一版 `1.0.0-r1`。協定變了才升協定版本，打包修訂升 `-rN`。主機依 tag 加 arm64 manifest digest 拉取；已發佈的 tag 不覆寫。
12. **CI**：PR 與 main 在 `ubuntu-24.04-arm` 建置並檢查、不推；推 `*-r*` tag 時建置、檢查後推到 ghcr，以 skopeo 確認 arm64，GitHub release 記錄 `IMAGE:TAG@digest`，已存在的 tag 拒推（照 `Woow_podman_vpn_tailscale_package` 的 publish workflow）。檢查包括 image 內沒有密鑰、以非 root 執行、用測試密鑰起容器後未帶 ticket 存取開著的埠回 404、關掉的埠沒有在聽。
13. **不放 install、upgrade 腳本**，安裝由 `woow_ha_version_control` 的 `restore.sh` 負責（#50）。

## 後果

- `restore.sh`（#50）要依這組名稱建立 podman secret、安裝基底 quadlet，H4G 再裝 drop-in；主機不再需要 `woow-gateway` 帳號與 `/opt/woow-deploy/overlays/woow-auth-gateway/`。
- quadlet 的 `Image=` 在發佈後才補得上 digest，所以每次發佈是三步，都在 release PR 的分支上：改 tag、在分支上打 git tag 發佈、補 digest 後才合併。main 上不會出現只有 tag 的 `Image=`，`image` workflow 也以此為合併門檻。
- ticket 的防重放紀錄只在記憶體，容器重啟後清空；ticket 只有 45 秒，可以接受。要在重啟後保留不在本 ADR 範圍。
- 新增 target 要同步改五個地方（README 列出），協定版本隨之升級。
