# -*- coding: utf-8 -*-
"""
영수증 보정 프로그램 빌드 스크립트
실행: python build.py
"""
import subprocess
import sys
import os

def run(cmd):
    print(f"\n>>> {' '.join(str(c) for c in cmd)}\n")
    subprocess.check_call(cmd)

def main():
    print("=" * 60)
    print("  영수증 보정 프로그램  빌드 시작")
    print("=" * 60)

    # 1. 의존 패키지 설치
    print("\n[1/2] 패키지 설치 중...")
    run([
        sys.executable, "-m", "pip", "install",
        "opencv-python-headless",
        "Pillow",
        "tkinterdnd2",
        "pyinstaller",
        "--upgrade", "--quiet",
    ])

    # 2. PyInstaller 빌드
    print("\n[2/2] PyInstaller 빌드 중 (수 분 소요)...")
    run([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "영수증보정프로그램",
        "--hidden-import", "cv2",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        "--collect-all", "tkinterdnd2",
        "--collect-all", "cv2",
        "receipt_tool.py",
    ])

    print("\n" + "=" * 60)
    print("  빌드 완료!")
    print("  실행 파일: dist\\영수증보정프로그램.exe")
    print("  이 파일 하나만 복사해서 사용하세요.")
    print("=" * 60)

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"\n[오류] 빌드 실패 (종료 코드 {e.returncode})")
        sys.exit(1)
    except FileNotFoundError:
        print("\n[오류] Python 또는 pip 를 찾을 수 없습니다.")
        print("       https://www.python.org 에서 Python 3.10+ 를 설치하세요.")
        sys.exit(1)
