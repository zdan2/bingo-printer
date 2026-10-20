#!/bin/bash
# ラズパイを「ビンゴ専用機」にする。Raspberry Pi OS Bookworm 用。
#  - 抽選サーバーを /opt/bingo に置き、起動時に自動で立ち上げる (USB プリンター /dev/usb/lp0)
#  - ラズパイ自身を Wi-Fi 親機にする (次回再起動から有効)
#
# 使い方 (bingo-printer フォルダの中で、インターネットにつながった状態で):
#   sudo ./setup_pi.sh <Wi-Fiパスワード8文字以上> [SSID=BINGO] [帯域 bg=2.4GHz / a=5GHz]
#   親機を wlan0 以外にするとき: sudo AP_IF=wlan1 ./setup_pi.sh ...
#   別の Wi-Fi で外につながっていれば (ドングル2本構成)、再起動なしで親機がすぐ立ち上がる
set -euo pipefail

PSK="${1:-}"
SSID="${2:-BINGO}"
BAND="${3:-bg}"
APP=/opt/bingo
AP_ADDR=192.168.50.1
AP_IF="${AP_IF:-wlan0}"   # 親機にするインターフェース (AP モード対応のもの)

if [ "$(id -u)" != 0 ]; then echo "sudo で実行してください"; exit 1; fi
if [ ${#PSK} -lt 8 ]; then echo "Wi-Fiパスワードを8文字以上で指定してください"; exit 1; fi
cd "$(dirname "$0")"

echo "== アプリを $APP に配置"
install -d "$APP"
install -m 644 server.py index.html "$APP/"

echo "== 必要なパッケージ"
command -v dnsmasq >/dev/null || apt-get install -y dnsmasq-base
# 席名(日本語)と番号を画像にして印字するため
apt-get install -y python3-pil fonts-ipafont-gothic
raspi-config nonint do_wifi_country JP || true

echo "== 自動起動サービス"
cat > /etc/systemd/system/bingo.service <<EOF
[Unit]
Description=Bingo draw + thermal printer relay
After=network.target

[Service]
ExecStart=/usr/bin/python3 $APP/server.py --usb /dev/usb/lp0 --http-port 80
Restart=always
RestartSec=2
DynamicUser=yes
SupplementaryGroups=lp
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable bingo.service
systemctl restart bingo.service

echo "== Wi-Fi 親機 ($SSID) を $AP_IF に"
# USB ドングルは再起動で wlan0/wlan1 の名前が入れ替わることがあるので MAC で固定する
AP_MAC=$(cat "/sys/class/net/$AP_IF/address")
nmcli con delete bingo-ap >/dev/null 2>&1 || true
nmcli con add type wifi ifname "*" con-name bingo-ap ssid "$SSID" \
  802-11-wireless.mac-address "$AP_MAC" \
  802-11-wireless.mode ap 802-11-wireless.band "$BAND" \
  ipv4.method shared ipv4.addresses "$AP_ADDR/24" ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PSK" \
  connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null

# 別の Wi-Fi で外につながっていれば (ドングル2本構成)、その接続もいまの MAC に固定し、
# 親機はすぐ立ち上げる。SSH は外向きの Wi-Fi 経由なので切れない
UPLINK=$(nmcli -t -f NAME,TYPE,DEVICE con show --active | awk -F: -v ap="$AP_IF" '$2=="802-11-wireless" && $3!=ap {print $1 ":" $3; exit}')
if [ -n "$UPLINK" ]; then
  UP_CON=${UPLINK%:*}; UP_IF=${UPLINK##*:}
  nmcli con mod "$UP_CON" 802-11-wireless.mac-address "$(cat "/sys/class/net/$UP_IF/address")"
  echo "== 外向き接続 $UP_CON を $UP_IF に固定し、親機を起動"
  nmcli con up bingo-ap
  NOW=1
fi

if [ -n "${NOW:-}" ]; then
  cat <<EOF

完了しました。Wi-Fi「$SSID」が出ています（$AP_IF）。外向きの Wi-Fi は $UP_IF のまま。
  タブレットを Wi-Fi「$SSID」につなぎ http://$AP_ADDR/ を開く
  親機を止める: sudo nmcli con down bingo-ap
EOF
  exit 0
fi

cat <<EOF

完了しました。
  sudo reboot で Wi-Fi 親機に切り替わります（今の Wi-Fi・Tailscale からは切れます）。
  再起動後: タブレットを Wi-Fi「$SSID」につなぎ http://$AP_ADDR/ を開く
  保守用SSH: 同じ Wi-Fi から ssh <ユーザー名>@$AP_ADDR
  元に戻す:   sudo nmcli con mod bingo-ap connection.autoconnect no && sudo reboot
EOF
