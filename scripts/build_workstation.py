"""Build the Windows x64 workstation kit on Windows with 64-bit Python 3.14."""

import hashlib
import io
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PYTHON = "https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip"
LLAMA_TAG = "b12589"
LLAMA = (f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/"
         f"llama-{LLAMA_TAG}-bin-win-cpu-x64.zip")


def download(url: str, digest: str, target: Path) -> None:
    with urlopen(url, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise RuntimeError(f"Upstream digest mismatch for {target.name}")


def add_edge_integration(kit: Path) -> None:
    from pip._vendor import distlib

    shutil.copytree(ROOT / "src/cisco_vmanage_mcp/edge_addon", kit / "Edge extension")
    shutil.copy2(ROOT / "scripts" / "Set up Edge.cmd", kit)
    shutil.copy2(ROOT / "scripts" / "Set up Edge.cmd", kit / "Set up BlairScope.cmd")
    # distlib's console launcher resolves this interpreter relative to itself after extraction.
    vendor = Path(distlib.__file__).parent
    stub = (vendor / "t64.exe").read_bytes()
    if "<launcher_dir>".encode("utf-16le") not in stub:
        raise RuntimeError("This pip distribution lacks a relocatable Windows launcher.")
    script = io.BytesIO()
    with ZipFile(script, "w") as archive:
        archive.writestr("__main__.py", "from cisco_vmanage_mcp.edge_host import main\nraise SystemExit(main())\n")
    (kit / "sdwan-edge-host.exe").write_bytes(
        stub + b"#!<launcher_dir>\\python\\python.exe -B -X utf8\n" + script.getvalue()
    )
    shutil.copy2(vendor / "LICENSE.txt", kit / "LICENSE-distlib.txt")


def build(stage: Path) -> Path:
    kit = stage / "kit"
    python_dir = kit / "python"
    runtime = kit / "llama"
    runtime.mkdir(parents=True)
    download(PYTHON, "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159", stage / "python.zip")
    with ZipFile(stage / "python.zip") as archive:
        archive.extractall(python_dir)
    (python_dir / "python314._pth").write_text(
        "python314.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii",
    )
    download(LLAMA, "ddd62ebd0a77c1f6bb0f3d595f816a987bcec5e0a75689534f52232ebb529edb", stage / "llama.zip")
    with ZipFile(stage / "llama.zip") as archive:
        for entry in archive.infolist():
            name = Path(entry.filename).name
            if name.endswith(".dll") and name.endswith("-impl.dll") and name != "llama-server-impl.dll":
                continue
            if name == "llama-server.exe" or name.endswith(".dll") or name.startswith("LICENSE"):
                (runtime / name).write_bytes(archive.read(entry))
    license_url = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_TAG}/LICENSE"
    with urlopen(license_url, timeout=30) as response:
        (runtime / "LICENSE-llama.cpp").write_bytes(response.read())
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(stage), str(ROOT)], check=True)
    packages = python_dir / "Lib" / "site-packages"
    wheel = next(stage.glob("cisco_vmanage_mcp-*.whl"))
    subprocess.run([
        sys.executable, "-m", "pip", "install", "--no-compile", "--ignore-installed",
        "--only-binary=:all:", "--target", str(packages), str(wheel),
    ], check=True)
    shutil.copy2(ROOT / "LICENSE", kit / "LICENSE")
    shutil.copy2(ROOT / "docs" / "WORKSTATION.md", kit / "README.md")
    shutil.copy2(ROOT / "scripts" / "Start SD-WAN.cmd", kit)
    add_edge_integration(kit)
    subprocess.run([str(python_dir / "python.exe"), "-m", "cisco_vmanage_mcp.workstation", "--help"],
                   cwd=kit, check=True)
    target = ROOT / "dist" / "SD-WAN-Workstation-Windows-x64.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(stage / "kit.zip", "w", ZIP_DEFLATED, compresslevel=4) as archive:
        for file in sorted(kit.rglob("*")):
            if file.is_file() and "__pycache__" not in file.parts:
                archive.write(file, file.relative_to(kit))
    shutil.copy2(stage / "kit.zip", target)
    return target


if __name__ == "__main__":
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14) or platform.machine() != "AMD64":
        raise SystemExit("Use 64-bit Python 3.14 on Windows x64 to build this kit.")
    with TemporaryDirectory(prefix="sdwan-build-") as temporary:
        print(build(Path(temporary)))
