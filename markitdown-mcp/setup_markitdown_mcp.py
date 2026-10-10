#!/usr/bin/env python3
"""MarkItDown MCP'ni o'rnatib, Claude Desktop'ga ulaydi va sinab ko'radi.

Ishga tushirish:
  macOS/Linux:  python3 setup_markitdown_mcp.py
  Windows:      py -3 setup_markitdown_mcp.py

Mavjud MCP serverlar saqlanadi; config o'zgartirilishidan oldin zaxira nusxa olinadi.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SERVER_NAME = "markitdown"
IS_WIN = os.name == "nt"


def fail(msg):
    print(f"\n[XATO] {msg}")
    sys.exit(1)


def venv_dir():
    if IS_WIN:
        return Path(os.environ["LOCALAPPDATA"]) / "markitdown-mcp" / "venv"
    return Path.home() / ".local" / "share" / "markitdown-mcp" / "venv"


def venv_paths(v):
    if IS_WIN:
        return v / "Scripts" / "python.exe", v / "Scripts" / "markitdown-mcp.exe"
    return v / "bin" / "python", v / "bin" / "markitdown-mcp"


def desktop_config_path():
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return Path(os.environ["APPDATA"]) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def mcp_test(server_cmd, timeout=60):
    """stdio orqali initialize -> tools/list -> convert_to_markdown chaqiradi."""
    tmp = Path(tempfile.mkdtemp())
    test_file = tmp / "markitdown_test.html"
    test_file.write_text(
        "<html><body><h1>Salom MarkItDown</h1><p>Bu <b>test</b> fayli.</p>"
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>",
        encoding="utf-8",
    )
    p = subprocess.Popen(server_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8")
    deadline = time.time() + timeout

    def send(m):
        p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()

    def recv():
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("server javob bermadi: " + p.stderr.read()[-500:])
            m = json.loads(line)
            if "id" in m:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m["result"]
        raise RuntimeError("vaqt tugadi")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "setup-test", "version": "1"}}})
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = [t["name"] for t in recv()["tools"]]
        if "convert_to_markdown" not in tools:
            raise RuntimeError(f"convert_to_markdown topilmadi, tool'lar: {tools}")
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "convert_to_markdown", "arguments": {"uri": test_file.as_uri()}}})
        res = recv()
        text = res["content"][0]["text"]
        if res.get("isError") or "Salom MarkItDown" not in text:
            raise RuntimeError("kutilmagan natija: " + text[:300])
        return tools, text
    finally:
        p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    # 1. OS va Python
    print(f"OS: {platform.system()} {platform.release()}, Python: {platform.python_version()}")
    if not ((3, 10) <= sys.version_info[:2] <= (3, 14)):
        fail("MarkItDown uchun Python 3.10-3.14 kerak. Mos versiyani o'rnatib, skriptni o'sha bilan ishga tushiring.")

    cfg_path = desktop_config_path()
    cfg = {}
    if cfg_path.exists():
        raw = cfg_path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                cfg = json.loads(raw)
            except json.JSONDecodeError as e:
                fail(f"{cfg_path} yaroqli JSON emas ({e}). Faylga tegmadim, avval uni tuzating.")
    servers = cfg.get("mcpServers") or {}
    print(f"Claude Desktop config: {cfg_path} ({'bor' if cfg_path.exists() else 'yo`q, yaratiladi'})")
    print(f"Mavjud MCP serverlar: {list(servers) or 'yo`q'}")

    # Avval ulangan markitdown ishlayotgan bo'lsa, qayta o'rnatmaymiz
    existing = servers.get(SERVER_NAME)
    if existing:
        cmd = [existing.get("command", "")] + list(existing.get("args", []))
        try:
            mcp_test(cmd)
            print(f"\n'{SERVER_NAME}' allaqachon ulangan va ishlayapti: {cmd[0]}. Hech narsa o'zgartirilmadi.")
            return
        except Exception as e:
            print(f"Mavjud '{SERVER_NAME}' ishlamayapti ({e}); qayta o'rnataman.")

    # 2. Alohida venv + o'rnatish
    v = venv_dir()
    py, server = venv_paths(v)
    if not py.exists():
        print(f"Virtual muhit yaratilmoqda: {v}")
        subprocess.run([sys.executable, "-m", "venv", str(v)], check=True)
    print("markitdown-mcp o'rnatilmoqda...")
    subprocess.run([str(py), "-m", "pip", "install", "-q", "--upgrade", "pip", "markitdown-mcp"], check=True)
    if not server.exists():
        fail(f"Server fayli topilmadi: {server}")
    server = server.resolve()
    print(f"Server yo'li: {server}")

    # 4 (oldindan). Serverni configga yozishdan oldin sinab ko'ramiz
    tools, md = mcp_test([str(server)])
    print(f"MCP test OK. Tool'lar: {tools}\n--- natija ---\n{md}\n--------------")

    # 3. Faqat markitdown'ni qo'shamiz, qolganini saqlaymiz
    if cfg_path.exists():
        backup = cfg_path.with_name(cfg_path.name + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(cfg_path, backup)
        print(f"Zaxira nusxa: {backup}")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    servers[SERVER_NAME] = {"command": str(server), "args": []}
    cfg["mcpServers"] = servers
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, cfg_path)

    # Tekshiruv: yozilgan configni qayta o'qiymiz
    check = json.loads(cfg_path.read_text(encoding="utf-8"))["mcpServers"]
    print(f"Configdagi serverlar endi: {list(check)}")

    print("\nTAYYOR. Claude Desktop'ni to'liq yoping (tray/menyu bar'dan Quit) va qayta oching.")
    print("Keyin yangi chatda: 'convert_to_markdown bilan file:///yo'l/fayl.pdf ni Markdown'ga aylantir' deb so'rang.")


if __name__ == "__main__":
    main()
