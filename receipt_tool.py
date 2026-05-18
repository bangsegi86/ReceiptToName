#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
영수증 보정 프로그램 (Receipt Correction Tool)
완전 포터블 - 추가 설치 불필요
- 드래그 앤 드롭 / 파일 열기로 이미지 로드
- 자동/수동 원근 보정
- Windows 내장 OCR로 날짜·금액 추출
- [결제일자]_[금액].[확장자] 형식으로 '스캔된_영수증' 폴더에 저장
"""

import sys
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import cv2
import numpy as np
from PIL import Image, ImageTk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


# ──────────────────────────────────────────────
# 색상 테마 (Catppuccin Mocha 기반)
# ──────────────────────────────────────────────
C = {
    'bg':         '#1e1e2e',
    'panel':      '#313244',
    'surface':    '#45475a',
    'button':     '#585b70',
    'accent':     '#89b4fa',
    'green':      '#a6e3a1',
    'red':        '#f38ba8',
    'yellow':     '#f9e2af',
    'peach':      '#fab387',
    'text':       '#cdd6f4',
    'subtext':    '#a6adc8',
    'dim':        '#6c7086',
    'canvas_bg':  '#181825',
}

CORNER_COLORS = ['#f38ba8', '#fab387', '#a6e3a1', '#89b4fa']


# ──────────────────────────────────────────────
# 메인 애플리케이션
# ──────────────────────────────────────────────
class ReceiptApp:

    def __init__(self):
        self._build_root()
        self._build_ui()
        self._bind_events()

        # 상태
        self.orig_img: np.ndarray | None = None
        self.orig_path: str | None = None
        self.warped_img: np.ndarray | None = None
        self.corners: list = []          # [[x,y], ...] 이미지 좌표
        self.mode: str = 'view'          # 'view' | 'manual'
        self.drag_idx: int | None = None
        self.display_scale: float = 1.0
        self.canvas_offset: tuple = (0, 0)
        self._tk_main: ImageTk.PhotoImage | None = None
        self._tk_prev: ImageTk.PhotoImage | None = None

    # ── 루트 윈도우 ──────────────────────────
    def _build_root(self):
        if HAS_DND:
            self.root = TkinterDnD.Tk()
        else:
            self.root = tk.Tk()

        self.root.title("영수증 보정 프로그램")
        self.root.geometry("1280x820")
        self.root.minsize(950, 620)
        self.root.configure(bg=C['bg'])

        # 화면 중앙 배치
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"1280x820+{(sw-1280)//2}+{(sh-820)//2}")

    # ── UI 구성 ──────────────────────────────
    def _build_ui(self):
        self.root.columnconfigure(0, weight=3)
        self.root.columnconfigure(1, weight=0)
        self.root.rowconfigure(0, weight=1)

        # ── 좌측 패널 ──
        left = tk.Frame(self.root, bg=C['panel'])
        left.grid(row=0, column=0, sticky='nsew', padx=(8, 4), pady=8)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        # 좌측 헤더
        hdr = tk.Frame(left, bg=C['panel'])
        hdr.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 4))

        tk.Label(hdr, text="이미지 보정", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 13, 'bold')).pack(side=tk.LEFT)

        self.mode_lbl = tk.Label(hdr, text="", bg=C['panel'], fg=C['yellow'],
                                 font=('맑은 고딕', 9))
        self.mode_lbl.pack(side=tk.LEFT, padx=12)

        # 이미지 캔버스
        cf = tk.Frame(left, bg=C['canvas_bg'])
        cf.grid(row=1, column=0, sticky='nsew', padx=10, pady=4)
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(cf, bg=C['canvas_bg'], highlightthickness=0,
                                cursor='crosshair')
        self.canvas.grid(row=0, column=0, sticky='nsew')

        self.hint_id = self.canvas.create_text(
            400, 300,
            text="이미지를 드래그 앤 드롭하거나\n아래 [파일 열기] 버튼을 클릭하세요",
            fill='#585b70', font=('맑은 고딕', 15), tags='hint', anchor='center'
        )

        # 좌측 버튼
        bf = tk.Frame(left, bg=C['panel'])
        bf.grid(row=2, column=0, sticky='ew', padx=10, pady=(4, 10))

        buttons = [
            ("📂  파일 열기",      self._open_file,        C['accent'],  '#1e1e2e'),
            ("✨  자동 보정",      self._auto_correct,     C['surface'], C['text']),
            ("✏️  수동 조정",      self._toggle_manual,    C['surface'], C['text']),
            ("▶  보정 적용",      self._apply_correction, C['surface'], C['text']),
        ]
        for txt, cmd, bg, fg in buttons:
            tk.Button(bf, text=txt, command=cmd,
                      bg=bg, fg=fg, relief=tk.FLAT,
                      padx=14, pady=7, font=('맑은 고딕', 10),
                      activebackground=C['button'], activeforeground=C['text'],
                      cursor='hand2', bd=0).pack(side=tk.LEFT, padx=3)

        # ── 우측 패널 ──
        right = tk.Frame(self.root, bg=C['panel'], width=370)
        right.grid(row=0, column=1, sticky='nsew', padx=(4, 8), pady=8)
        right.pack_propagate(False)
        right.columnconfigure(0, weight=1)

        row = 0

        # 미리보기
        tk.Label(right, text="보정 미리보기", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 10)).grid(row=row, column=0,
                                              sticky='w', padx=12, pady=(10, 2))
        row += 1

        pf = tk.Frame(right, bg=C['canvas_bg'], height=195)
        pf.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 6))
        pf.pack_propagate(False)
        self.prev_canvas = tk.Canvas(pf, bg=C['canvas_bg'], highlightthickness=0)
        self.prev_canvas.pack(fill=tk.BOTH, expand=True)
        row += 1

        ttk.Separator(right).grid(row=row, column=0, sticky='ew', padx=12, pady=6)
        row += 1

        # OCR 섹션
        ocr_hdr = tk.Frame(right, bg=C['panel'])
        ocr_hdr.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 4))
        tk.Label(ocr_hdr, text="텍스트 인식 (OCR)",
                 bg=C['panel'], fg=C['subtext'], font=('맑은 고딕', 10)).pack(side=tk.LEFT)
        self.ocr_btn = tk.Button(ocr_hdr, text="OCR 실행", command=self._run_ocr,
                                  bg=C['panel'], fg=C['accent'],
                                  relief=tk.FLAT, padx=10, pady=3,
                                  font=('맑은 고딕', 9), cursor='hand2', bd=1,
                                  highlightbackground=C['accent'])
        self.ocr_btn.pack(side=tk.RIGHT)
        row += 1

        self.ocr_status_var = tk.StringVar(value="대기 중")
        tk.Label(right, textvariable=self.ocr_status_var,
                 bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8)
                 ).grid(row=row, column=0, sticky='w', padx=12)
        row += 1

        # OCR 원문
        tf = tk.Frame(right, bg=C['panel'])
        tf.grid(row=row, column=0, sticky='nsew', padx=12, pady=(2, 6))
        right.rowconfigure(row, weight=1)
        self.ocr_text = tk.Text(tf, height=7, bg=C['canvas_bg'], fg=C['subtext'],
                                font=('맑은 고딕', 9), relief=tk.FLAT, bd=4,
                                wrap=tk.WORD, insertbackground=C['text'])
        sb = ttk.Scrollbar(tf, orient=tk.VERTICAL, command=self.ocr_text.yview)
        self.ocr_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ocr_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        row += 1

        ttk.Separator(right).grid(row=row, column=0, sticky='ew', padx=12, pady=6)
        row += 1

        # 날짜 입력
        tk.Label(right, text="결제 일자", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 9, 'bold')).grid(row=row, column=0,
                                                      sticky='w', padx=12, pady=(4, 0))
        row += 1
        self.date_var = tk.StringVar()
        tk.Entry(right, textvariable=self.date_var, font=('맑은 고딕', 13),
                 bg=C['surface'], fg=C['text'], relief=tk.FLAT, bd=6,
                 insertbackground=C['text']
                 ).grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 2))
        row += 1
        tk.Label(right, text="YYYYMMDD 형식  예) 20260518",
                 bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8)
                 ).grid(row=row, column=0, sticky='w', padx=12)
        row += 1

        # 금액 입력
        tk.Label(right, text="합계 금액", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 9, 'bold')).grid(row=row, column=0,
                                                      sticky='w', padx=12, pady=(8, 0))
        row += 1
        self.amount_var = tk.StringVar()
        tk.Entry(right, textvariable=self.amount_var, font=('맑은 고딕', 13),
                 bg=C['surface'], fg=C['text'], relief=tk.FLAT, bd=6,
                 insertbackground=C['text']
                 ).grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 2))
        row += 1
        tk.Label(right, text="숫자만 입력  예) 15500",
                 bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8)
                 ).grid(row=row, column=0, sticky='w', padx=12)
        row += 1

        ttk.Separator(right).grid(row=row, column=0, sticky='ew', padx=12, pady=10)
        row += 1

        # 저장 버튼
        tk.Button(right, text="✅  최종 승인 및 저장",
                  command=self._save,
                  bg='#40a02b', fg='white', relief=tk.FLAT,
                  padx=14, pady=12, font=('맑은 고딕', 12, 'bold'),
                  activebackground=C['green'], activeforeground='#1e1e2e',
                  cursor='hand2', bd=0
                  ).grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 12))

    # ── 이벤트 바인딩 ─────────────────────────
    def _bind_events(self):
        self.canvas.bind('<ButtonPress-1>',   self._on_click)
        self.canvas.bind('<B1-Motion>',       self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Configure>',       lambda _: self._refresh_canvas())

        if HAS_DND:
            for widget in (self.canvas, self.root):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._on_dnd)

    # ──────────────────────────────────────────
    # 파일 로드
    # ──────────────────────────────────────────
    def _on_dnd(self, event):
        raw = event.data.strip()
        # Windows에서 공백 포함 경로는 {}로 감싸짐
        if raw.startswith('{'):
            raw = raw[1:-1]
        # 여러 파일이면 첫 번째만
        paths = re.split(r'\}\s*\{', raw)
        if paths:
            self._load(paths[0].strip('{}').strip())

    def _open_file(self):
        path = filedialog.askopenfilename(
            title="이미지 선택",
            filetypes=[
                ("이미지 파일", "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp"),
                ("모든 파일",   "*.*"),
            ]
        )
        if path:
            self._load(path)

    def _load(self, path: str):
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("이미지를 읽을 수 없습니다.")
        except Exception as e:
            messagebox.showerror("오류", f"파일 로드 실패\n{e}")
            return

        self.orig_path = path
        self.orig_img  = img
        self.warped_img = None
        self.corners    = []
        self.mode       = 'view'
        self.mode_lbl.configure(text="")
        self.canvas.itemconfigure('hint', state='hidden')

        self._refresh_canvas()
        self._auto_correct()

    # ──────────────────────────────────────────
    # 이미지 표시
    # ──────────────────────────────────────────
    def _refresh_canvas(self):
        if self.orig_img is None:
            return

        cw = self.canvas.winfo_width()  or 700
        ch = self.canvas.winfo_height() or 550
        h, w = self.orig_img.shape[:2]

        scale = min(cw / w, ch / h, 1.0)
        self.display_scale = scale
        dw, dh = int(w * scale), int(h * scale)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.canvas_offset = (ox, oy)

        rgb = cv2.cvtColor(self.orig_img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (dw, dh), interpolation=cv2.INTER_AREA)
        self._tk_main = ImageTk.PhotoImage(Image.fromarray(rgb))

        self.canvas.delete('all')
        self.canvas.create_image(ox, oy, anchor=tk.NW, image=self._tk_main, tags='img')
        self._draw_corners()

    def _draw_corners(self):
        self.canvas.delete('overlay')
        if not self.corners:
            return

        pts = [self._i2c(p) for p in self.corners]
        n   = len(pts)

        # 외곽선
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            self.canvas.create_line(x1, y1, x2, y2,
                                    fill=C['accent'], width=2, dash=(7, 3),
                                    tags='overlay')

        # 꼭짓점 원
        r = 9
        for i, (cx, cy) in enumerate(pts):
            col = CORNER_COLORS[i % 4]
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=col, outline='white', width=2, tags='overlay')
            self.canvas.create_text(cx, cy, text=str(i + 1),
                                    fill='#1e1e2e', font=('Arial', 8, 'bold'),
                                    tags='overlay')

    # ── 좌표 변환 ──
    def _i2c(self, pt) -> tuple:
        ox, oy = self.canvas_offset
        return pt[0] * self.display_scale + ox, pt[1] * self.display_scale + oy

    def _c2i(self, cx: float, cy: float) -> tuple:
        ox, oy = self.canvas_offset
        return (cx - ox) / self.display_scale, (cy - oy) / self.display_scale

    # ──────────────────────────────────────────
    # 자동 원근 보정
    # ──────────────────────────────────────────
    def _auto_correct(self):
        if self.orig_img is None:
            return

        corners = self._detect_receipt(self.orig_img)
        h, w = self.orig_img.shape[:2]

        if corners is None:
            corners = [[0.0, 0.0], [float(w), 0.0],
                       [float(w), float(h)], [0.0, float(h)]]

        self.corners = corners
        self._draw_corners()
        self._apply_correction()

    def _detect_receipt(self, img: np.ndarray):
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)

        for lo, hi in [(30, 100), (50, 150), (80, 200)]:
            edges  = cv2.Canny(blur, lo, hi)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

            cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
            cnts = sorted(cnts, key=cv2.contourArea, reverse=True)

            for cnt in cnts[:8]:
                if cv2.contourArea(cnt) < w * h * 0.08:
                    continue
                peri = cv2.arcLength(cnt, True)
                for eps in (0.02, 0.03, 0.05):
                    approx = cv2.approxPolyDP(cnt, eps * peri, True)
                    if len(approx) == 4:
                        pts = approx.reshape(4, 2).astype(float).tolist()
                        return self._order_pts(pts)

        return None

    def _order_pts(self, pts: list) -> list:
        arr = np.array(pts, dtype=np.float32)
        s   = arr.sum(axis=1)
        d   = np.diff(arr, axis=1).flatten()
        out = np.zeros((4, 2), dtype=np.float32)
        out[0] = arr[np.argmin(s)]   # 좌상
        out[1] = arr[np.argmin(d)]   # 우상
        out[2] = arr[np.argmax(s)]   # 우하
        out[3] = arr[np.argmax(d)]   # 좌하
        return out.tolist()

    # ──────────────────────────────────────────
    # 수동 조정 모드
    # ──────────────────────────────────────────
    def _toggle_manual(self):
        if self.orig_img is None:
            return

        if self.mode == 'manual':
            self.mode = 'view'
            self.mode_lbl.configure(text="")
        else:
            self.mode = 'manual'
            self.corners = []
            self._draw_corners()
            self.mode_lbl.configure(
                text="[수동 모드] 꼭짓점 4곳을 순서대로 클릭 ▶ 좌상 → 우상 → 우하 → 좌하"
            )

    # ──────────────────────────────────────────
    # 원근 변환 적용
    # ──────────────────────────────────────────
    def _apply_correction(self):
        if self.orig_img is None or len(self.corners) != 4:
            return

        src = np.float32(self.corners)
        w1  = np.linalg.norm(src[1] - src[0])
        w2  = np.linalg.norm(src[2] - src[3])
        h1  = np.linalg.norm(src[3] - src[0])
        h2  = np.linalg.norm(src[2] - src[1])

        out_w = max(int(max(w1, w2)), 1)
        out_h = max(int(max(h1, h2)), 1)

        dst = np.float32([[0, 0], [out_w - 1, 0],
                          [out_w - 1, out_h - 1], [0, out_h - 1]])
        M = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(self.orig_img, M, (out_w, out_h))
        self.warped_img = warped
        self._update_preview(warped)

    def _update_preview(self, img: np.ndarray):
        self.prev_canvas.update_idletasks()
        pw = self.prev_canvas.winfo_width()  or 346
        ph = self.prev_canvas.winfo_height() or 193

        h, w = img.shape[:2]
        scale = min(pw / w, ph / h)
        nw, nh = max(int(w * scale), 1), max(int(h * scale), 1)

        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        self._tk_prev = ImageTk.PhotoImage(Image.fromarray(rgb))

        self.prev_canvas.delete('all')
        ox, oy = (pw - nw) // 2, (ph - nh) // 2
        self.prev_canvas.create_image(ox, oy, anchor=tk.NW, image=self._tk_prev)

    # ──────────────────────────────────────────
    # 마우스 이벤트
    # ──────────────────────────────────────────
    def _on_click(self, event):
        if self.orig_img is None:
            return

        if self.mode == 'manual':
            ix, iy = self._c2i(event.x, event.y)
            h, w   = self.orig_img.shape[:2]
            if 0 <= ix <= w and 0 <= iy <= h:
                if len(self.corners) >= 4:
                    self.corners = []
                self.corners.append([ix, iy])
                self._draw_corners()
                if len(self.corners) == 4:
                    self._apply_correction()
        else:
            # 꼭짓점 드래그 시작
            self.drag_idx = None
            for i, pt in enumerate(self.corners):
                cx, cy = self._i2c(pt)
                if ((event.x - cx) ** 2 + (event.y - cy) ** 2) ** 0.5 < 15:
                    self.drag_idx = i
                    break

    def _on_drag(self, event):
        if self.drag_idx is None or self.orig_img is None:
            return
        ix, iy = self._c2i(event.x, event.y)
        h, w   = self.orig_img.shape[:2]
        self.corners[self.drag_idx] = [
            max(0.0, min(ix, float(w))),
            max(0.0, min(iy, float(h))),
        ]
        self._draw_corners()

    def _on_release(self, event):
        if self.drag_idx is not None:
            self.drag_idx = None
            self._apply_correction()

    # ──────────────────────────────────────────
    # OCR
    # ──────────────────────────────────────────
    def _run_ocr(self):
        target = self.warped_img if self.warped_img is not None else self.orig_img
        if target is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return

        self.ocr_btn.configure(state=tk.DISABLED)
        self.ocr_status_var.set("OCR 실행 중…")
        self.root.update()

        def worker():
            fd, tmp = tempfile.mkstemp(suffix='.png')
            os.close(fd)
            try:
                ok, buf = cv2.imencode('.png', target)
                if ok:
                    buf.tofile(tmp)
                text = self._windows_ocr(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self.root.after(0, lambda: self._ocr_done(text))

        threading.Thread(target=worker, daemon=True).start()

    def _ocr_done(self, text: str):
        self.ocr_btn.configure(state=tk.NORMAL)
        if not text:
            self.ocr_status_var.set("OCR 실패 — 직접 입력해 주세요")
            return

        self.ocr_status_var.set(f"OCR 완료  ({len(text)}자 인식)")
        self.ocr_text.delete('1.0', tk.END)
        self.ocr_text.insert(tk.END, text)

        date   = self._parse_date(text)
        amount = self._parse_amount(text)
        if date:
            self.date_var.set(date)
        if amount:
            self.amount_var.set(amount)

    # ── Windows 내장 OCR (PowerShell 경유) ──
    def _windows_ocr(self, img_path: str) -> str:
        abs_path = os.path.abspath(img_path).replace('/', '\\')

        ps = (
            "$ErrorActionPreference='Stop'\n"
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime\n"
            "$methods=[System.WindowsRuntimeSystemExtensions].GetMethods()\n"
            "$asTaskGen=($methods|Where-Object{"
            "  $_.Name -eq 'AsTask' -and"
            "  $_.GetParameters().Count -eq 1 -and"
            "  $_.GetParameters()[0].ParameterType.Name -match 'IAsyncOperation'"
            "})[0]\n"
            "function Await($wrt,$t){"
            "  $task=$asTaskGen.MakeGenericMethod($t).Invoke($null,@($wrt));"
            "  $task.Wait(-1)|Out-Null; $task.Result}\n"
            "[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]|Out-Null\n"
            "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null\n"
            "[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime]|Out-Null\n"
            f"$path='{abs_path}'\n"
            "$file=Await([Windows.Storage.StorageFile]::GetFileFromPathAsync($path))"
            "  ([Windows.Storage.StorageFile])\n"
            "$stream=Await($file.OpenAsync([Windows.Storage.FileAccessMode]::Read))"
            "  ([Windows.Storage.Streams.IRandomAccessStream])\n"
            "$decoder=Await([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream))"
            "  ([Windows.Graphics.Imaging.BitmapDecoder])\n"
            "$bitmap=Await($decoder.GetSoftwareBitmapAsync())"
            "  ([Windows.Graphics.Imaging.SoftwareBitmap])\n"
            "$engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()\n"
            "if(-not $engine){"
            "  $lang=[Windows.Globalization.Language]::new('ko');"
            "  $engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)}\n"
            "if(-not $engine){Write-Error 'OCR 엔진 없음'; exit 1}\n"
            "$result=Await($engine.RecognizeAsync($bitmap))([Windows.Media.Ocr.OcrResult])\n"
            "$result.Text\n"
        )

        try:
            r = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass',
                 '-NoProfile', '-NonInteractive', '-Command', ps],
                capture_output=True, text=True, timeout=45,
                encoding='utf-8', errors='replace',
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            return ''
        except Exception:
            return ''

    # ── 날짜 파싱 ──
    def _parse_date(self, text: str) -> str:
        for pat, split in [
            (r'(\d{4})[-./년][ ]?(\d{1,2})[-./월][ ]?(\d{1,2})', True),
            (r'\b(\d{8})\b', False),
        ]:
            for m in re.finditer(pat, text):
                if split:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    s = m.group(1)
                    y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}{mo:02d}{d:02d}"
        return ''

    # ── 금액 파싱 ──
    def _parse_amount(self, text: str) -> str:
        keywords = ['합계', '총액', '결제금액', '총계', '합 계', '결제 금액',
                    '청구금액', '실결제', '실 결제', 'TOTAL', 'Total', '소계']
        lines = text.splitlines()

        for i, line in enumerate(lines):
            if any(kw in line for kw in keywords):
                block = ' '.join(lines[i:i + 3])
                nums = []
                for n in re.findall(r'[\d,]+', block):
                    s = n.replace(',', '')
                    if s.isdigit():
                        v = int(s)
                        if 100 <= v <= 50_000_000:
                            nums.append(v)
                if nums:
                    return str(max(nums))

        # 폴백: 텍스트 전체에서 가장 큰 수
        candidates = []
        for n in re.findall(r'[\d,]+', text):
            s = n.replace(',', '')
            if s.isdigit():
                v = int(s)
                if 100 <= v <= 50_000_000:
                    candidates.append(v)
        return str(max(candidates)) if candidates else ''

    # ──────────────────────────────────────────
    # 저장
    # ──────────────────────────────────────────
    def _save(self):
        date   = self.date_var.get().strip()
        amount = re.sub(r'[^\d]', '', self.amount_var.get().strip())

        if not re.match(r'^\d{8}$', date):
            messagebox.showwarning("입력 오류",
                                   "날짜를 YYYYMMDD 형식으로 입력해 주세요.\n예: 20260518")
            return

        # 날짜 유효성 검사
        try:
            from datetime import datetime
            datetime.strptime(date, '%Y%m%d')
        except ValueError:
            messagebox.showwarning("입력 오류", "올바른 날짜가 아닙니다.")
            return

        if not amount or not (1 <= int(amount) <= 100_000_000):
            messagebox.showwarning("입력 오류", "유효한 금액을 입력해 주세요.")
            return

        # 확장자 결정
        ext = Path(self.orig_path).suffix.lower() if self.orig_path else '.jpg'
        if not ext or ext not in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}:
            ext = '.jpg'
        encode_ext = '.jpg' if ext in {'.jpg', '.jpeg'} else \
                     '.png' if ext in {'.tiff', '.tif'} else ext

        # 저장 폴더: exe(또는 .py) 위치 / 스캔된_영수증
        exe_dir = Path(sys.argv[0]).resolve().parent
        out_dir = exe_dir / '스캔된_영수증'
        out_dir.mkdir(exist_ok=True)

        base     = f"{date}_{amount}"
        out_path = out_dir / f"{base}{ext}"
        counter  = 1
        while out_path.exists():
            out_path = out_dir / f"{base}_{counter}{ext}"
            counter += 1

        target = self.warped_img if self.warped_img is not None else self.orig_img
        if target is None:
            messagebox.showwarning("경고", "저장할 이미지가 없습니다.")
            return

        try:
            ok, buf = cv2.imencode(encode_ext, target)
            if not ok:
                raise RuntimeError("이미지 인코딩 실패")
            buf.tofile(str(out_path))
        except Exception as e:
            messagebox.showerror("저장 오류", f"저장 실패\n{e}")
            return

        messagebox.showinfo("저장 완료",
                            f"파일이 저장되었습니다!\n\n"
                            f"파일명: {out_path.name}\n"
                            f"위치: {out_path.parent}")

        # 탐색기에서 저장 파일 선택
        try:
            subprocess.Popen(f'explorer /select,"{out_path}"', shell=True)
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ──────────────────────────────────────────────
if __name__ == '__main__':
    app = ReceiptApp()
    app.run()
