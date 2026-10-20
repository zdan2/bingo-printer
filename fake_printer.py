"""実機なしで試すための疑似プリンター。TCP 9100 で受けた ESC/POS から印字文字だけを表示する。

  python fake_printer.py            # 別ウィンドウで起動しておき
  python server.py --printer 127.0.0.1
"""

import re
import socketserver


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        data = b""
        while chunk := self.request.recv(4096):
            data += chunk
        if not data:
            return  # /status の接続確認
        # 制御コードを除いて印字テキストだけ取り出す
        text = re.sub(rb"\x1b[@]|\x1b[aEd].|\x1d!.|\x1dV..", b"", data, flags=re.S).strip()
        cut = b"\x1dV" in data
        print(f"[印刷] {text.decode('ascii', 'replace')}  ({len(data)} bytes{', カット' if cut else ''})", flush=True)


if __name__ == "__main__":
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", 9100), Handler) as s:
        print("疑似プリンター待ち受け中 127.0.0.1:9100", flush=True)
        s.serve_forever()
