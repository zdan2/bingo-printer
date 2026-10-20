"""ビンゴ抽選の中継サーバー。

タブレットのブラウザに抽選画面 (index.html) を配信し、
POST /print で受け取った番号を ESC/POS に変換してサーマルプリンターへ送る。
LAN 接続 (TCP 9100) と USB 接続 (/dev/usb/lp0) の両方に対応。標準ライブラリだけで動く。

  python server.py --printer 192.168.1.50
  python server.py --usb /dev/usb/lp0 --http-port 80
  python server.py --printer 192.168.1.50 --paper 58 --no-cut
"""

import argparse
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

# Font A の1文字幅 (dots)。倍率計算に使う
FONT_A_WIDTH = 12
PAPER_DOTS = {58: 384, 80: 576}
LINE_DOTS = 30  # 1行の紙送り量 (標準の行間 1/6 インチ ≒ 30 dots)。画像の下余白に換算する

# 席ごとの画面 (/parent, /camera)。紙には番号の上に席名を印字して見分ける。
# 画像印字では日本語、Pillow がないときの文字印字では英字を使う
SEATS = {"parent": ("保護者席", "PARENTS"), "camera": ("カメラ席", "CAMERA")}
DEFAULT_FONT = "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"

ESC = b"\x1b"
GS = b"\x1d"

config = argparse.Namespace()
print_lock = threading.Lock()

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None


class ImageTickets:
    """番号と席名を日本語フォントで画像にし、ラスター印字 (GS v 0) のバイト列にする。
    1枚目の印字を待たせないよう、作ったものは覚えておき、起動時に裏でよく使う番号を作っておく。"""

    LABEL_SIZE = 56
    NUMBER_SIZE = 300
    BAND = 64  # 一度に送る行数。大きすぎると受け付けない機種がある

    def __init__(self, font_path: str):
        self.font_path = font_path
        self.label_font = ImageFont.truetype(font_path, self.LABEL_SIZE)
        self.number_fonts = {}
        self.cache = {}

    def number_font(self, text: str, width: int):
        # 桁が多くて用紙幅を超えるときは文字を小さくする
        size = self.NUMBER_SIZE
        while True:
            font = self.number_fonts.get(size) or self.number_fonts.setdefault(
                size, ImageFont.truetype(self.font_path, size))
            box = font.getbbox(text, stroke_width=6)
            if box[2] - box[0] <= width - 16 or size <= 60:
                return font, box
            size -= 20

    def render(self, text: str, label: str, width: int, bottom: int = 0):
        """bottom は下余白 (dots)。紙送り命令は次の券が続けて届くと効かないことがあるので、白い行として画像に含める。"""
        nfont, nb = self.number_font(text, width)
        num_h = nb[3] - nb[1]
        lab_h, gap = 0, 0
        if label:
            lb = self.label_font.getbbox(label)
            lab_h, gap = lb[3] - lb[1], 20
        img = Image.new("1", (width, lab_h + gap + num_h + 8 + bottom), 1)
        d = ImageDraw.Draw(img)
        if label:
            d.text(((width - (lb[2] - lb[0])) // 2 - lb[0], -lb[1]), label, font=self.label_font, fill=0)
        d.text(((width - (nb[2] - nb[0])) // 2 - nb[0], lab_h + gap - nb[1]), text,
               font=nfont, fill=0, stroke_width=6, stroke_fill=0)
        return img

    def raster(self, img) -> bytes:
        wb = (img.width + 7) // 8
        out = bytearray()
        for y0 in range(0, img.height, self.BAND):
            part = img.crop((0, y0, img.width, min(img.height, y0 + self.BAND)))
            # Pillow の 1bit 画像は 1=白、プリンターは 1=黒 なので反転する
            data = bytes(b ^ 0xFF for b in part.tobytes())
            out += GS + b"v0\x00" + bytes([wb & 0xFF, wb >> 8, part.height & 0xFF, part.height >> 8]) + data
        return bytes(out)

    def get(self, text: str, label: str, width: int) -> bytes:
        key = (text, label, width)
        body = self.cache.get(key)
        if body is None:
            bottom = config.feed * LINE_DOTS
            body = self.cache[key] = self.raster(self.render(text, label, width, bottom))
        return body

    def warm_up(self, width: int, upto: int = 99) -> None:
        labels = [""] + [jp for jp, _ in SEATS.values()]
        for n in range(1, upto + 1):
            for label in labels:
                self.get(str(n), label, width)


images = None  # ImageTickets。使えないときは None で文字印字にする


def build_ticket(text: str, seat: str = "") -> bytes:
    """番号1つを用紙幅いっぱいの大きさで中央に印字するバイト列を作る。seat は番号の上に添える席。"""
    if images:
        label = SEATS[seat][0] if seat in SEATS else ""
        out = bytearray()
        # ESC @ (初期化) は送らない。前の券の印字中に届くと、未印字分が消えて途中で切れる
        out += ESC + b"a\x01"             # 中央寄せ (画像は用紙幅いっぱいなので念のため)
        out += images.get(text, label, PAPER_DOTS[config.paper])  # 下余白は画像に含まれる
        if config.cut:
            out += GS + b"V\x42\x00"
        return bytes(out)
    return build_text_ticket(text, SEATS[seat][1] if seat in SEATS else "")


def build_text_ticket(text: str, label: str = "") -> bytes:
    """プリンター内蔵フォントで印字する (Pillow がないとき用)。"""
    dots = PAPER_DOTS[config.paper]
    # 横倍率は用紙幅に収まる最大値 (1〜8)。縦も同じ倍率にする
    mag = max(1, min(8, dots // (FONT_A_WIDTH * max(1, len(text)))))
    size = ((mag - 1) << 4) | (mag - 1)

    out = bytearray()
    # ESC @ (初期化) は送らない。前の券の印字中に届くと、未印字分が消えて途中で切れる。
    # 代わりに使う設定を毎回指定し直す
    out += ESC + b"a\x01"             # 中央寄せ
    out += ESC + b"E\x01"             # 太字
    if label:
        out += GS + b"!\x11"          # 縦横2倍
        out += label.encode("ascii") + b"\n"
    out += GS + b"!" + bytes([size])  # 文字倍率
    out += text.encode("ascii") + b"\n"
    out += GS + b"!\x00"
    out += ESC + b"E\x00"
    out += ESC + b"d" + bytes([config.feed])  # 紙送り
    if config.cut:
        out += GS + b"V\x42\x00"      # 紙送りしてパーシャルカット
    return bytes(out)


def send_to_printer(data: bytes) -> None:
    # 連打されても印字が混ざらないよう、1枚ずつ順番に送る
    with print_lock:
        if config.usb:
            with open(config.usb, "wb", buffering=0) as f:
                f.write(data)
        else:
            with socket.create_connection((config.printer, config.port), timeout=config.timeout) as s:
                s.sendall(data)


def check_printer() -> None:
    """プリンターに届くか確認する。届かなければ OSError。"""
    # 印刷中の接続とぶつからないようロックを取ってから確認する
    with print_lock:
        if config.usb:
            if not os.path.exists(config.usb):
                raise OSError(f"{config.usb} がありません（USBケーブル・電源を確認）")
            if not os.access(config.usb, os.W_OK):
                raise OSError(f"{config.usb} に書き込み権限がありません")
        else:
            with socket.create_connection((config.printer, config.port), timeout=1.5):
                pass


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.strip("/") in SEATS:
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            try:
                check_printer()
                self.reply(200, {"ok": True})
            except OSError as e:
                self.reply(503, {"ok": False, "error": str(e)})
        else:
            self.reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/print":
            self.reply(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            number = int(payload["number"])
            seat = payload.get("seat") or ""
        except (ValueError, KeyError, TypeError):
            self.reply(400, {"ok": False, "error": "number が必要です"})
            return
        try:
            send_to_printer(build_ticket(str(number), seat))
            self.reply(200, {"ok": True})
        except OSError as e:
            self.log_message("print failed: %s", e)
            self.reply(502, {"ok": False, "error": str(e)})

    def reply(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    p = argparse.ArgumentParser(description="ビンゴ抽選の中継サーバー")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--printer", help="LAN接続: プリンターのIPアドレス")
    target.add_argument("--usb", help="USB接続: デバイスファイル (例 /dev/usb/lp0)")
    p.add_argument("--port", type=int, default=9100, help="プリンターのRAWポート (既定 9100)")
    p.add_argument("--paper", type=int, choices=(58, 80), default=80, help="用紙幅 mm (既定 80)")
    p.add_argument("--feed", type=int, default=4, help="印字後の紙送り行数 (既定 4)")
    p.add_argument("--no-cut", dest="cut", action="store_false", help="オートカットしない")
    p.add_argument("--timeout", type=float, default=3.0, help="プリンター接続のタイムアウト秒")
    p.add_argument("--listen", default="0.0.0.0", help="待ち受けアドレス")
    p.add_argument("--http-port", type=int, default=8080, help="画面配信ポート (既定 8080)")
    p.add_argument("--font", default=DEFAULT_FONT, help="画像印字に使う日本語フォント")
    p.add_argument("--text-mode", action="store_true", help="画像を使わずプリンター内蔵フォントで印字")
    p.parse_args(namespace=config)

    global images
    if config.text_mode:
        print("文字印字モード (内蔵フォント)", flush=True)
    elif Image is None:
        print("Pillow がないので文字印字モードで動きます", flush=True)
    elif not os.path.exists(config.font):
        print(f"フォント {config.font} がないので文字印字モードで動きます", flush=True)
    else:
        images = ImageTickets(config.font)
        threading.Thread(target=images.warm_up, args=(PAPER_DOTS[config.paper],), daemon=True).start()
        print(f"画像印字モード ({config.font})", flush=True)

    server = ThreadingHTTPServer((config.listen, config.http_port), Handler)
    dest = config.usb or f"{config.printer}:{config.port}"
    print(f"抽選画面: http://<このPCのIP>:{config.http_port}/  →  プリンター {dest}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
