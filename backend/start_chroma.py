"""作为独立进程启动 Chroma HTTP 服务，目录和监听端口来自本工程配置。"""
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from .config import Settings

if __name__ == '__main__':
    settings = Settings()
    address = urlsplit(settings.chroma_url)
    binary = Path(sys.executable).with_name('chroma.exe' if sys.platform == 'win32' else 'chroma')
    subprocess.run([str(binary), 'run', '--host', address.hostname, '--port', str(address.port or 80),
                    '--path', str(settings.chroma_dir.resolve())], check=True)
