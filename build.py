# -*- coding: utf-8 -*-
"""
영수증 보정 프로그램 빌드 스크립트
실행: python build.py

자동으로 수행하는 작업
  1. Python 패키지 설치 (pytesseract, OpenCV, Pillow, PyInstaller 등)
  2. Tesseract OCR 엔진 위치 탐색
     - 없으면 winget 으로 자동 설치 시도
  3. 한국어 tessdata 없으면 자동 다운로드
  4. PyInstaller 로 단일 .exe 빌드 (Tesseract 번들 포함)
"""

import subprocess
import sys
import os
import shutil
import urllib.request
from pathlib import Path

# ── 상수 ─────────────────────────────────────
KOR_DATA_URL = (
    "https://github.com/tesseract-ocr/tessdata/raw/main/kor.traineddata"
)
ENG_DATA_URL = (
    "https://github.com/tesseract-ocr/tessdata/raw/main/eng.traineddata"
)
TESS_COMMON_PATHS = [
    r"C:\Program Files\Tesseract-OCR",
    r"C:\Program Files (x86)\Tesseract-OCR",
]


# ── 유틸 ─────────────────────────────────────
def run(cmd, **kw):
    print(f"\n>>> {' '.join(str(c) for c in cmd)}\n")
    subprocess.check_call(cmd, **kw)


def find_tesseract_dir() -> str | None:
    tess = shutil.which("tesseract")
    if tess:
        return str(Path(tess).parent)
    for p in TESS_COMMON_PATHS:
        if Path(p, "tesseract.exe").exists():
            return p
    return None


def install_tesseract_winget() -> str | None:
    print("[알림] Tesseract 를 찾을 수 없습니다. winget 으로 설치 시도...")
    try:
        subprocess.run(
            [
                "winget", "install", "-e", "--id",
                "UB-Mannheim.TesseractOCR",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ],
            check=True, timeout=300,
        )
        print("Tesseract 설치 완료. 경로를 다시 탐색합니다...")
        return find_tesseract_dir()
    except Exception as e:
        print(f"자동 설치 실패: {e}")
        return None


def ensure_traineddata(tess_dir: str):
    tessdata = Path(tess_dir) / "tessdata"
    tessdata.mkdir(exist_ok=True)

    for lang, url in [("kor", KOR_DATA_URL), ("eng", ENG_DATA_URL)]:
        fp = tessdata / f"{lang}.traineddata"
        if not fp.exists():
            print(f"  [{lang}] 언어 데이터 다운로드 중... ({url})")
            urllib.request.urlretrieve(url, str(fp))
            print(f"  [{lang}] 완료")
        else:
            print(f"  [{lang}] 이미 있음")


def collect_tess_files(tess_dir: str):
    """PyInstaller --add-binary / --add-data 목록 반환"""
    tp = Path(tess_dir)
    add_binary, add_data = [], []

    # tesseract.exe
    add_binary.append(f"{tp / 'tesseract.exe'};.")

    # DLL 전체 (Tesseract 구동에 필요)
    for dll in tp.glob("*.dll"):
        add_binary.append(f"{dll};.")

    # tessdata (kor + eng 만)
    for lang in ("kor", "eng"):
        fp = tp / "tessdata" / f"{lang}.traineddata"
        if fp.exists():
            add_data.append(f"{fp};tessdata")

    return add_binary, add_data


# ── 메인 ─────────────────────────────────────
def main():
    print("=" * 60)
    print("  영수증 보정 프로그램  빌드 시작")
    print("=" * 60)

    # 1. Python 패키지
    print("\n[1/4] Python 패키지 설치 중...")
    run([
        sys.executable, "-m", "pip", "install",
        "opencv-python-headless",
        "Pillow",
        "pytesseract",
        "tkinterdnd2",
        "paddlepaddle",
        "paddleocr",
        "pyinstaller",
        "--upgrade", "--quiet",
    ])

    # 2. Tesseract 확인
    print("\n[2/4] Tesseract 위치 탐색...")
    tess_dir = find_tesseract_dir()

    if not tess_dir:
        tess_dir = install_tesseract_winget()

    if not tess_dir:
        print()
        print("=" * 60)
        print("  [오류] Tesseract 를 찾을 수 없습니다.")
        print()
        print("  수동 설치 방법:")
        print("  1. https://github.com/UB-Mannheim/tesseract/wiki")
        print("     에서 Windows installer 다운로드 및 설치")
        print("  2. 설치 후 이 스크립트를 다시 실행하세요.")
        print("=" * 60)
        sys.exit(1)

    print(f"  Tesseract 위치: {tess_dir}")

    # 3. 언어 데이터
    print("\n[3/4] 언어 데이터 확인...")
    ensure_traineddata(tess_dir)

    # 4. PyInstaller 빌드
    print("\n[4/4] PyInstaller 빌드 중 (수 분 소요)...")
    add_binary, add_data = collect_tess_files(tess_dir)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed",
        "--name", "영수증보정프로그램",
        "--hidden-import", "cv2",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "pytesseract",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--collect-all", "tkinterdnd2",
        "--collect-all", "cv2",
        "--collect-all", "paddleocr",
        "--collect-all", "paddle",
        "--hidden-import", "paddleocr",
    ]
    for b in add_binary:
        cmd += ["--add-binary", b]
    for d in add_data:
        cmd += ["--add-data", d]
    cmd.append("receipt_tool.py")

    try:
        run(cmd)
    except subprocess.CalledProcessError:
        print("\n[오류] 빌드 실패. 위 오류 메시지를 확인하세요.")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  빌드 완료!")
    print(f"  실행 파일: dist\\영수증보정프로그램.exe")
    print("  이 파일 하나만 복사해서 사용하세요.")
    print("=" * 60)


if __name__ == "__main__":
    main()
