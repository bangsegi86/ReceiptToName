@echo off
chcp 65001 > nul
echo.
echo ============================================================
echo   영수증 보정 프로그램  빌드 스크립트
echo   Python + PyInstaller 가 설치된 Windows 환경에서 실행
echo ============================================================
echo.

:: Python 존재 확인
python --version > nul 2>&1
if errorlevel 1 (
    echo [오류] Python 이 설치되어 있지 않거나 PATH 에 없습니다.
    echo        https://www.python.org 에서 Python 3.10 이상을 설치하세요.
    pause
    exit /b 1
)

echo [1/3] 의존 패키지 설치 중...
pip install opencv-python-headless Pillow tkinterdnd2 pyinstaller --quiet --upgrade
if errorlevel 1 (
    echo [오류] 패키지 설치 실패. 인터넷 연결 또는 pip 상태를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [2/3] PyInstaller 로 단일 .exe 빌드 중 (수 분 소요)...

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "영수증보정프로그램" ^
    --hidden-import cv2 ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageTk ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.filedialog ^
    --hidden-import tkinter.messagebox ^
    --collect-all tkinterdnd2 ^
    --collect-all cv2 ^
    receipt_tool.py

if errorlevel 1 (
    echo.
    echo [오류] 빌드 실패. 위 오류 메시지를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [3/3] 빌드 완료!
echo.
echo   실행 파일 위치: dist\영수증보정프로그램.exe
echo.
echo   이 파일 하나만 복사해서 사용하세요.
echo   (Python / OCR 엔진 별도 설치 불필요)
echo.
pause
