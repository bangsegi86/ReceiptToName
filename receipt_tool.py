#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
영수증 보정 프로그램 (Receipt Correction Tool)
완전 포터블 - 추가 설치 불필요
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

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False


C = {
    'bg':        '#1e1e2e',
    'panel':     '#313244',
    'surface':   '#45475a',
    'button':    '#585b70',
    'accent':    '#89b4fa',
    'green':     '#a6e3a1',
    'red':       '#f38ba8',
    'yellow':    '#f9e2af',
    'text':      '#cdd6f4',
    'subtext':   '#a6adc8',
    'dim':       '#6c7086',
    'canvas_bg': '#181825',
}
CORNER_COLORS = ['#f38ba8', '#fab387', '#a6e3a1', '#89b4fa']
IMG_MARGIN    = 40


class ReceiptApp:

    def __init__(self):
        self._build_root()
        self._build_ui()
        self._bind_events()

        self.orig_img:   np.ndarray | None = None
        self.orig_path:  str | None        = None
        self.warped_img: np.ndarray | None = None
        self.corners:    list              = []
        self.mode:       str               = 'view'
        self.drag_idx:   int | None        = None
        self.display_scale:  float         = 1.0
        self.canvas_offset:  tuple         = (0, 0)
        self._tk_main:   ImageTk.PhotoImage | None = None
        self._tk_prev:   ImageTk.PhotoImage | None = None

        self.file_queue:   list[str]  = []
        self.queue_idx:    int        = 0
        self.receipt_data: list[dict] = []
        self._loading:     bool       = False

        # 목록 ↔ 우측 패널 동기화 트레이스
        self.date_var.trace_add('write',   self._on_field_change)
        self.amount_var.trace_add('write', self._on_field_change)

        self._tess_cmd:  str | None = None
        self._tess_data: str | None = None
        self._init_tesseract()

    # ── 루트 ──────────────────────────────────
    def _build_root(self):
        self.root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        self.root.title("영수증 보정 프로그램")
        self.root.configure(bg=C['bg'])

        # ttk 다크 테마 (Treeview 포함)
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('Treeview',
            background=C['surface'],
            fieldbackground=C['surface'],
            foreground=C['text'],
            rowheight=26,
            font=('맑은 고딕', 9),
            borderwidth=0,
        )
        style.configure('Treeview.Heading',
            background=C['button'],
            foreground=C['subtext'],
            font=('맑은 고딕', 9, 'bold'),
            relief='flat',
            padding=4,
        )
        style.map('Treeview',
            background=[('selected', C['accent'])],
            foreground=[('selected', '#1e1e2e')],
        )

        self.root.geometry("1560x820")
        self.root.minsize(1100, 620)
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"1560x820+{(sw-1560)//2}+{(sh-820)//2}")

    # ── UI ────────────────────────────────────
    def _build_ui(self):
        self.root.columnconfigure(0, weight=0, minsize=270)  # 목록 패널
        self.root.columnconfigure(1, weight=3)               # 이미지 캔버스
        self.root.columnconfigure(2, weight=0)               # 우측 패널
        self.root.rowconfigure(0, weight=1)

        self._build_list_panel()

        # ─ 이미지 보정 패널 ─
        left = tk.Frame(self.root, bg=C['panel'])
        left.grid(row=0, column=1, sticky='nsew', padx=4, pady=8)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        hdr = tk.Frame(left, bg=C['panel'])
        hdr.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 4))

        tk.Label(hdr, text="이미지 보정", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 13, 'bold')).pack(side=tk.LEFT)

        self.queue_lbl = tk.Label(hdr, text="", bg=C['panel'], fg=C['accent'],
                                  font=('맑은 고딕', 10, 'bold'))
        self.queue_lbl.pack(side=tk.LEFT, padx=10)

        self.mode_lbl = tk.Label(hdr, text="", bg=C['panel'], fg=C['yellow'],
                                 font=('맑은 고딕', 9))
        self.mode_lbl.pack(side=tk.LEFT)

        cf = tk.Frame(left, bg=C['canvas_bg'])
        cf.grid(row=1, column=0, sticky='nsew', padx=10, pady=4)
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(cf, bg=C['canvas_bg'], highlightthickness=0,
                                cursor='crosshair')
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.canvas.create_text(400, 300,
            text="이미지를 드래그 앤 드롭하거나\n아래 [파일 열기] 버튼을 클릭하세요",
            fill='#585b70', font=('맑은 고딕', 15), tags='hint', anchor='center')

        bf = tk.Frame(left, bg=C['panel'])
        bf.grid(row=2, column=0, sticky='ew', padx=10, pady=(4, 10))

        def btn(parent, text, cmd, bg=C['surface'], fg=C['text']):
            return tk.Button(parent, text=text, command=cmd,
                             bg=bg, fg=fg, relief=tk.FLAT,
                             padx=12, pady=7, font=('맑은 고딕', 10),
                             activebackground=C['button'], activeforeground=C['text'],
                             cursor='hand2', bd=0)

        btn(bf, "📂  파일 열기",  self._open_file,   C['accent'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "✨  자동 보정",  self._auto_correct).pack(side=tk.LEFT, padx=2)
        btn(bf, "✏️  수동 조정",  self._toggle_manual).pack(side=tk.LEFT, padx=2)

        tk.Frame(bf, bg=C['panel'], width=12).pack(side=tk.LEFT)
        self.prev_btn = btn(bf, "◀ 이전", self._prev_file)
        self.prev_btn.pack(side=tk.LEFT, padx=2)
        self.next_btn = btn(bf, "다음 ▶", self._next_file)
        self.next_btn.pack(side=tk.LEFT, padx=2)

        # ─ 우측 패널 ─
        right = tk.Frame(self.root, bg=C['panel'], width=370)
        right.grid(row=0, column=2, sticky='nsew', padx=(4, 8), pady=8)
        right.pack_propagate(False)
        right.columnconfigure(0, weight=1)

        row = 0

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

        ocr_hdr = tk.Frame(right, bg=C['panel'])
        ocr_hdr.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 4))
        tk.Label(ocr_hdr, text="텍스트 인식 (OCR)", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 10)).pack(side=tk.LEFT)
        self.ocr_btn = tk.Button(ocr_hdr, text="OCR 실행", command=self._run_ocr,
                                  bg=C['panel'], fg=C['accent'],
                                  relief=tk.FLAT, padx=10, pady=3,
                                  font=('맑은 고딕', 9), cursor='hand2', bd=1)
        self.ocr_btn.pack(side=tk.RIGHT)
        row += 1

        self.ocr_status_var = tk.StringVar(value="대기 중")
        tk.Label(right, textvariable=self.ocr_status_var, bg=C['panel'],
                 fg=C['dim'], font=('맑은 고딕', 8)
                 ).grid(row=row, column=0, sticky='w', padx=12)
        row += 1

        self.ocr_progress = ttk.Progressbar(right, mode='indeterminate')
        self.ocr_progress.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 4))
        self.ocr_progress.grid_remove()
        row += 1

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

        tk.Button(right, text="✅  최종 승인 및 저장",
                  command=self._save,
                  bg='#40a02b', fg='white', relief=tk.FLAT,
                  padx=14, pady=12, font=('맑은 고딕', 12, 'bold'),
                  activebackground=C['green'], activeforeground='#1e1e2e',
                  cursor='hand2', bd=0
                  ).grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 12))

    # ── 영수증 목록 패널 ──────────────────────
    def _build_list_panel(self):
        lp = tk.Frame(self.root, bg=C['panel'])
        lp.grid(row=0, column=0, sticky='nsew', padx=(8, 4), pady=8)
        lp.rowconfigure(1, weight=1)
        lp.columnconfigure(0, weight=1)
        lp.columnconfigure(1, weight=0)

        tk.Label(lp, text="영수증 목록", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 11, 'bold')
                 ).grid(row=0, column=0, columnspan=2, sticky='w',
                        padx=10, pady=(10, 6))

        cols = ('file', 'date', 'amount')
        self.receipt_tree = ttk.Treeview(lp, columns=cols, show='headings',
                                          selectmode='extended')
        self.receipt_tree.heading('file',   text='파일명')
        self.receipt_tree.heading('date',   text='결제일자')
        self.receipt_tree.heading('amount', text='합계금액')
        self.receipt_tree.column('file',   width=105, minwidth=60, stretch=True)
        self.receipt_tree.column('date',   width=78,  minwidth=70, stretch=False, anchor='center')
        self.receipt_tree.column('amount', width=72,  minwidth=55, stretch=False, anchor='e')

        # 인식 상태별 색상
        self.receipt_tree.tag_configure('done',    foreground=C['green'])   # 날짜+금액 모두
        self.receipt_tree.tag_configure('partial', foreground=C['yellow'])  # 하나만
        self.receipt_tree.tag_configure('none',    foreground=C['red'])     # 미인식
        self.receipt_tree.tag_configure('saved',   foreground=C['dim'])     # 저장 완료

        vsb = ttk.Scrollbar(lp, orient='vertical', command=self.receipt_tree.yview)
        self.receipt_tree.configure(yscrollcommand=vsb.set)

        self.receipt_tree.grid(row=1, column=0, sticky='nsew',
                               padx=(8, 0), pady=(0, 0))
        vsb.grid(row=1, column=1, sticky='ns', padx=(0, 8), pady=(0, 0))

        # 선택 항목 OCR 실행 버튼
        self.batch_ocr_btn = tk.Button(
            lp, text="선택 항목 OCR 실행",
            command=self._run_ocr_batch,
            bg=C['accent'], fg='#1e1e2e', relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['button'], activeforeground=C['text'],
            cursor='hand2', bd=0,
        )
        self.batch_ocr_btn.grid(row=2, column=0, columnspan=2,
                                sticky='ew', padx=8, pady=(6, 8))
        lp.rowconfigure(2, weight=0)

        self.receipt_tree.bind('<<TreeviewSelect>>', self._on_list_select)

    # ── 이벤트 바인딩 ─────────────────────────
    def _bind_events(self):
        self.canvas.bind('<ButtonPress-1>',   self._on_click)
        self.canvas.bind('<B1-Motion>',       self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Configure>',       lambda _: self._refresh_canvas())

        if HAS_DND:
            for w in (self.canvas, self.root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self._on_dnd)

    # ──────────────────────────────────────────
    # 목록 데이터 관리
    # ──────────────────────────────────────────
    def _init_receipt_data(self, paths: list):
        self.receipt_data = [
            {'date': '', 'amount': '', 'ocr_done': False, 'saved': False}
            for _ in paths
        ]
        self._refresh_list()

    def _refresh_list(self):
        self.receipt_tree.delete(*self.receipt_tree.get_children())
        for i, (path, data) in enumerate(zip(self.file_queue, self.receipt_data)):
            fname = Path(path).name
            if len(fname) > 15:
                fname = fname[:12] + '...'
            date_disp   = data['date'] if data['date'] else '-'
            amount_disp = self._fmt_amount(data['amount'])
            tag = self._row_tag(data)
            self.receipt_tree.insert('', 'end', iid=str(i),
                                     values=(fname, date_disp, amount_disp),
                                     tags=(tag,))
        self._select_list_row(self.queue_idx)

    def _update_list_row(self, idx: int):
        if idx >= len(self.receipt_data) or idx >= len(self.file_queue):
            return
        data  = self.receipt_data[idx]
        fname = Path(self.file_queue[idx]).name
        if len(fname) > 15:
            fname = fname[:12] + '...'
        date_disp   = data['date'] if data['date'] else '-'
        amount_disp = self._fmt_amount(data['amount'])
        tag = self._row_tag(data)
        try:
            self.receipt_tree.item(str(idx),
                                   values=(fname, date_disp, amount_disp),
                                   tags=(tag,))
        except tk.TclError:
            pass

    def _fmt_amount(self, amount: str) -> str:
        if not amount:
            return '-'
        try:
            return f"{int(amount):,}"
        except ValueError:
            return amount

    def _row_tag(self, data: dict) -> str:
        if data.get('saved'):
            return 'saved'
        has_date   = bool(data['date'])
        has_amount = bool(data['amount'])
        if has_date and has_amount:
            return 'done'
        if has_date or has_amount:
            return 'partial'
        return 'none'

    def _select_list_row(self, idx: int):
        iid = str(idx)
        if self.receipt_tree.exists(iid):
            self.receipt_tree.selection_set(iid)
            self.receipt_tree.see(iid)

    def _on_list_select(self, _event):
        # 다중 선택 모드: 네비게이션은 포커스 아이템(마지막 클릭)만 따라감
        focused = self.receipt_tree.focus()
        if not focused:
            return
        idx = int(focused)
        if idx != self.queue_idx:
            self.queue_idx = idx
            self._load_current()

    def _on_field_change(self, *_):
        if self._loading:
            return
        if not self.receipt_data or self.queue_idx >= len(self.receipt_data):
            return
        data = self.receipt_data[self.queue_idx]
        data['date']   = self.date_var.get()
        data['amount'] = self.amount_var.get()
        self._update_list_row(self.queue_idx)

    # ──────────────────────────────────────────
    # 파일 로드
    # ──────────────────────────────────────────
    def _on_dnd(self, event):
        raw = event.data.strip()
        if raw.startswith('{'):
            raw = raw[1:-1]
        paths = [p.strip('{}').strip() for p in re.split(r'\}\s*\{', raw)]
        valid = [p for p in paths if p]
        if valid:
            self.file_queue = valid
            self.queue_idx  = 0
            self._init_receipt_data(valid)
            self._load_current()

    def _open_file(self):
        paths = filedialog.askopenfilenames(
            title="이미지 선택 (여러 장 선택 가능)",
            filetypes=[
                ("이미지 파일", "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp"),
                ("모든 파일",   "*.*"),
            ]
        )
        if paths:
            self.file_queue = list(paths)
            self.queue_idx  = 0
            self._init_receipt_data(list(paths))
            self._load_current()

    def _load_current(self):
        if not self.file_queue:
            return
        path = self.file_queue[self.queue_idx]
        try:
            data = np.fromfile(path, dtype=np.uint8)
            img  = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("이미지를 읽을 수 없습니다.")
        except Exception as e:
            messagebox.showerror("오류", f"파일 로드 실패\n{e}")
            return

        self.orig_path  = path
        self.orig_img   = img
        self.warped_img = None
        self.corners    = []
        self.mode       = 'view'
        self.mode_lbl.configure(text="")

        # OCR UI 초기화 (이전 파일의 진행 상태 제거)
        self.ocr_progress.stop()
        self.ocr_progress.grid_remove()
        self.ocr_btn.configure(state=tk.NORMAL)

        self._loading = True
        rd = self.receipt_data[self.queue_idx] if self.queue_idx < len(self.receipt_data) else None
        if rd and rd['ocr_done']:
            # 이미 OCR 완료된 파일 → 저장된 값 복원
            self.date_var.set(rd['date'])
            self.amount_var.set(rd['amount'])
            self.ocr_status_var.set("OCR 완료 (캐시)")
        else:
            self.date_var.set("")
            self.amount_var.set("")
            self.ocr_status_var.set("대기 중")
        self.ocr_text.delete('1.0', tk.END)
        self._loading = False

        self.canvas.itemconfigure('hint', state='hidden')
        self._update_nav_ui()
        self._select_list_row(self.queue_idx)
        self._refresh_canvas()
        self._auto_correct()

    def _update_nav_ui(self):
        n = len(self.file_queue)
        if n > 1:
            self.queue_lbl.configure(text=f"파일 {self.queue_idx + 1} / {n}")
        else:
            self.queue_lbl.configure(text="")

        self.prev_btn.configure(
            state=tk.NORMAL if self.queue_idx > 0 else tk.DISABLED)
        self.next_btn.configure(
            state=tk.NORMAL if self.queue_idx < n - 1 else tk.DISABLED)

    def _prev_file(self):
        if self.queue_idx > 0:
            self.queue_idx -= 1
            self._load_current()

    def _next_file(self):
        if self.queue_idx < len(self.file_queue) - 1:
            self.queue_idx += 1
            self._load_current()

    # ──────────────────────────────────────────
    # 이미지 표시 (여백 포함)
    # ──────────────────────────────────────────
    def _refresh_canvas(self):
        if self.orig_img is None:
            return

        cw = self.canvas.winfo_width()  or 700
        ch = self.canvas.winfo_height() or 550
        h, w = self.orig_img.shape[:2]

        eff_w = max(cw - IMG_MARGIN * 2, 100)
        eff_h = max(ch - IMG_MARGIN * 2, 100)
        scale = min(eff_w / w, eff_h / h, 1.0)
        self.display_scale = scale

        dw, dh = int(w * scale), int(h * scale)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self.canvas_offset = (ox, oy)

        rgb = cv2.cvtColor(self.orig_img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, (dw, dh), interpolation=cv2.INTER_AREA)
        self._tk_main = ImageTk.PhotoImage(Image.fromarray(rgb))

        self.canvas.delete('all')
        self.canvas.create_image(ox, oy, anchor=tk.NW,
                                 image=self._tk_main, tags='img')
        self._draw_corners()

    def _draw_corners(self):
        self.canvas.delete('overlay')
        if not self.corners:
            return

        pts = [self._i2c(p) for p in self.corners]
        n   = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            self.canvas.create_line(x1, y1, x2, y2,
                                    fill=C['accent'], width=2, dash=(7, 3),
                                    tags='overlay')
        r = 9
        for i, (cx, cy) in enumerate(pts):
            col = CORNER_COLORS[i % 4]
            self.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=col, outline='white', width=2,
                                    tags='overlay')
            self.canvas.create_text(cx, cy, text=str(i + 1),
                                    fill='#1e1e2e', font=('Arial', 8, 'bold'),
                                    tags='overlay')

    def _i2c(self, pt):
        ox, oy = self.canvas_offset
        return pt[0] * self.display_scale + ox, pt[1] * self.display_scale + oy

    def _c2i(self, cx, cy):
        ox, oy = self.canvas_offset
        return (cx - ox) / self.display_scale, (cy - oy) / self.display_scale

    # ──────────────────────────────────────────
    # 자동 원근 보정
    # ──────────────────────────────────────────
    def _auto_correct(self):
        if self.orig_img is None:
            return
        h, w   = self.orig_img.shape[:2]
        corners = self._detect_receipt(self.orig_img)
        if corners is None:
            corners = [[0.0, 0.0], [float(w), 0.0],
                       [float(w), float(h)], [0.0, float(h)]]
        self.corners = corners
        self._draw_corners()
        self._apply_correction()

    def _detect_receipt(self, img):
        h, w   = img.shape[:2]
        gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur   = cv2.GaussianBlur(gray, (5, 5), 0)

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

    def _order_pts(self, pts):
        arr = np.array(pts, dtype=np.float32)
        s   = arr.sum(axis=1)
        d   = np.diff(arr, axis=1).flatten()
        out = np.zeros((4, 2), dtype=np.float32)
        out[0] = arr[np.argmin(s)]
        out[1] = arr[np.argmin(d)]
        out[2] = arr[np.argmax(s)]
        out[3] = arr[np.argmax(d)]
        return out.tolist()

    # ──────────────────────────────────────────
    # 수동 조정
    # ──────────────────────────────────────────
    def _toggle_manual(self):
        if self.orig_img is None:
            return
        if self.mode == 'manual':
            self.mode = 'view'
            self.mode_lbl.configure(text="")
        else:
            self.mode = 'manual'
            self.mode_lbl.configure(
                text="[수동] 꼭짓점 드래그로 조정  ·  4개 미만이면 빈 곳 클릭으로 추가")

    # ──────────────────────────────────────────
    # 원근 변환 적용
    # ──────────────────────────────────────────
    def _apply_correction(self):
        if self.orig_img is None or len(self.corners) != 4:
            return

        def _dims(s):
            w = max(int(max(np.linalg.norm(s[1]-s[0]),
                            np.linalg.norm(s[2]-s[3]))), 1)
            h = max(int(max(np.linalg.norm(s[3]-s[0]),
                            np.linalg.norm(s[2]-s[1]))), 1)
            return w, h

        src   = np.float32(self.corners)
        out_w, out_h = _dims(src)

        oh, ow = self.orig_img.shape[:2]
        orig_portrait  = oh > ow * 1.2
        orig_landscape = ow > oh * 1.2
        if (orig_portrait  and out_w > out_h) or \
           (orig_landscape and out_h > out_w):
            src = np.float32([src[0], src[3], src[2], src[1]])
            out_w, out_h = _dims(src)

        dst    = np.float32([[0, 0], [out_w-1, 0],
                             [out_w-1, out_h-1], [0, out_h-1]])
        M      = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(self.orig_img, M, (out_w, out_h))

        self.warped_img = warped
        self._update_preview(warped)

    def _update_preview(self, img):
        self.prev_canvas.update_idletasks()
        pw = self.prev_canvas.winfo_width()  or 346
        ph = self.prev_canvas.winfo_height() or 193
        h, w   = img.shape[:2]
        scale  = min(pw / w, ph / h)
        nw, nh = max(int(w * scale), 1), max(int(h * scale), 1)
        rgb    = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb    = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
        self._tk_prev = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.prev_canvas.delete('all')
        self.prev_canvas.create_image(
            (pw - nw) // 2, (ph - nh) // 2,
            anchor=tk.NW, image=self._tk_prev)

    # ── 마우스 ────────────────────────────────
    def _on_click(self, event):
        if self.orig_img is None:
            return

        self.drag_idx = None
        for i, pt in enumerate(self.corners):
            cx, cy = self._i2c(pt)
            if ((event.x - cx) ** 2 + (event.y - cy) ** 2) ** 0.5 < 18:
                self.drag_idx = i
                return

        if self.mode == 'manual':
            ix, iy = self._c2i(event.x, event.y)
            h, w   = self.orig_img.shape[:2]
            if 0 <= ix <= w and 0 <= iy <= h and len(self.corners) < 4:
                self.corners.append([ix, iy])
                self._draw_corners()
                if len(self.corners) == 4:
                    self._apply_correction()

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
    def _run_ocr_batch(self):
        """목록에서 선택된 항목 전체에 OCR 실행 (백그라운드 병렬)."""
        selected = self.receipt_tree.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "목록에서 OCR 할 항목을 선택하세요.\n(Ctrl+클릭으로 다중 선택)")
            return

        indices = [int(iid) for iid in selected]
        total   = len(indices)
        self._batch_remaining = total
        self.batch_ocr_btn.configure(state=tk.DISABLED,
                                     text=f"OCR 실행 중… (0/{total})")

        for idx in indices:
            # 진행 중 표시
            self._mark_list_processing(idx)
            threading.Thread(
                target=self._batch_worker,
                args=(idx, total),
                daemon=True,
            ).start()

    def _mark_list_processing(self, idx: int):
        try:
            vals = list(self.receipt_tree.item(str(idx), 'values'))
            vals[1] = '처리중…'
            self.receipt_tree.item(str(idx), values=vals)
        except tk.TclError:
            pass

    def _batch_worker(self, idx: int, total: int):
        """백그라운드 스레드: 파일 로드 → 자동 보정 → OCR."""
        try:
            path = self.file_queue[idx]
            raw  = np.fromfile(path, dtype=np.uint8)
            img  = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError
            # 자동 꼭짓점 검출 + 원근 보정
            corners = self._detect_receipt(img)
            if corners and len(corners) == 4:
                src = np.float32(corners)
                def _dims(s):
                    w = max(int(max(np.linalg.norm(s[1]-s[0]),
                                    np.linalg.norm(s[2]-s[3]))), 1)
                    h = max(int(max(np.linalg.norm(s[3]-s[0]),
                                    np.linalg.norm(s[2]-s[1]))), 1)
                    return w, h
                out_w, out_h = _dims(src)
                oh, ow = img.shape[:2]
                if (oh > ow * 1.2 and out_w > out_h) or \
                   (ow > oh * 1.2 and out_h > out_w):
                    src = np.float32([src[0], src[3], src[2], src[1]])
                    out_w, out_h = _dims(src)
                dst    = np.float32([[0, 0], [out_w-1, 0],
                                     [out_w-1, out_h-1], [0, out_h-1]])
                M      = cv2.getPerspectiveTransform(src, dst)
                target = cv2.warpPerspective(img, M, (out_w, out_h))
            else:
                target = img
            text = self._do_ocr(target)
        except Exception:
            text = ''
        self.root.after(0, lambda: self._batch_done(idx, text, total))

    def _batch_done(self, idx: int, text: str, total: int):
        """메인 스레드: OCR 결과 반영 후 카운터 갱신."""
        date = amount = ''
        if text:
            date   = self._parse_date(text)
            amount = self._parse_amount(text)

        if 0 <= idx < len(self.receipt_data):
            rd = self.receipt_data[idx]
            rd['ocr_done'] = True
            if date:   rd['date']   = date
            if amount: rd['amount'] = amount
            self._update_list_row(idx)

        # 현재 화면에 표시 중인 파일이면 우측 패널도 갱신
        if idx == self.queue_idx and text:
            self.ocr_text.delete('1.0', tk.END)
            self.ocr_text.insert(tk.END, text)
            self.ocr_status_var.set(f"OCR 완료  ({len(text)}자 인식)")
            self._loading = True
            if date:   self.date_var.set(date)
            if amount: self.amount_var.set(amount)
            self._loading = False

        self._batch_remaining -= 1
        done = total - self._batch_remaining
        if self._batch_remaining > 0:
            self.batch_ocr_btn.configure(
                text=f"OCR 실행 중… ({done}/{total})")
        else:
            self.batch_ocr_btn.configure(
                state=tk.NORMAL, text="선택 항목 OCR 실행")

    def _run_ocr(self):
        target = self.warped_img if self.warped_img is not None else self.orig_img
        if target is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return

        target_idx = self.queue_idx
        img_copy   = target.copy()

        self.ocr_btn.configure(state=tk.DISABLED)
        self.ocr_status_var.set("OCR 실행 중…")
        self.ocr_progress.grid()
        self.ocr_progress.start(10)
        self.root.update()

        def worker():
            text = self._do_ocr(img_copy)
            self.root.after(0, lambda: self._ocr_done(text, target_idx))

        threading.Thread(target=worker, daemon=True).start()

    def _ocr_done(self, text: str, target_idx: int):
        date = amount = ''
        if text:
            date   = self._parse_date(text)
            amount = self._parse_amount(text)

        # receipt_data 항상 업데이트 (화면과 무관)
        if 0 <= target_idx < len(self.receipt_data):
            rd = self.receipt_data[target_idx]
            rd['ocr_done'] = True
            if date:   rd['date']   = date
            if amount: rd['amount'] = amount
            self._update_list_row(target_idx)

        # UI는 현재 보고 있는 파일일 때만 갱신
        if target_idx != self.queue_idx:
            return

        self.ocr_progress.stop()
        self.ocr_progress.grid_remove()
        self.ocr_btn.configure(state=tk.NORMAL)

        if not text:
            self.ocr_status_var.set("OCR 실패 — 직접 입력해 주세요")
            return

        self.ocr_status_var.set(f"OCR 완료  ({len(text)}자 인식)")
        self.ocr_text.delete('1.0', tk.END)
        self.ocr_text.insert(tk.END, text)

        self._loading = True
        if date:   self.date_var.set(date)
        if amount: self.amount_var.set(amount)
        self._loading = False

    # ── Tesseract 경로 탐색 ───────────────────
    def _init_tesseract(self):
        import shutil

        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
            exe  = os.path.join(base, 'tesseract.exe')
            data = os.path.join(base, 'tessdata')
            if os.path.exists(exe):
                self._tess_cmd  = exe
                self._tess_data = data if os.path.exists(data) else None
                return

        exe_dir   = Path(sys.argv[0]).resolve().parent
        local_exe = exe_dir / 'tesseract' / 'tesseract.exe'
        if local_exe.exists():
            self._tess_cmd  = str(local_exe)
            local_data = exe_dir / 'tesseract' / 'tessdata'
            self._tess_data = str(local_data) if local_data.exists() else None
            return

        tess = shutil.which('tesseract')
        if tess:
            self._tess_cmd  = tess
            data = os.path.join(os.path.dirname(tess), 'tessdata')
            self._tess_data = data if os.path.exists(data) else None
            return

        for cand in [
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
        ]:
            if os.path.exists(cand):
                self._tess_cmd  = cand
                data = os.path.join(os.path.dirname(cand), 'tessdata')
                self._tess_data = data if os.path.exists(data) else None
                return

    # ── OCR 전처리 ────────────────────────────
    def _preprocess_for_ocr(self, img: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) \
               if len(img.shape) == 3 else img.copy()

        h, w = gray.shape
        if h < 1800:
            scale = 1800 / h
            gray = cv2.resize(gray, None, fx=scale, fy=scale,
                              interpolation=cv2.INTER_CUBIC)

        gray = cv2.fastNlMeansDenoising(gray, h=15,
                                        templateWindowSize=7,
                                        searchWindowSize=21)

        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray  = clahe.apply(gray)

        kernel = np.array([[-1, -1, -1],
                           [-1,  9, -1],
                           [-1, -1, -1]])
        gray = cv2.filter2D(gray, -1, kernel)
        gray = np.clip(gray, 0, 255).astype(np.uint8)

        return cv2.adaptiveThreshold(
            gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 13, 4,
        )

    # ── OCR 엔진 선택 ─────────────────────────
    def _do_ocr(self, img: np.ndarray) -> str:
        if HAS_TESSERACT and self._tess_cmd:
            text = self._tesseract_ocr(img)
            if text.strip():
                return text

        fd, tmp = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        try:
            ok, buf = cv2.imencode('.png', img)
            if ok:
                buf.tofile(tmp)
            return self._windows_ocr(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ── Tesseract OCR ─────────────────────────
    def _tesseract_ocr(self, img: np.ndarray) -> str:
        try:
            pytesseract.pytesseract.tesseract_cmd = self._tess_cmd

            processed = self._preprocess_for_ocr(img)

            config = '--oem 1 --psm 4'
            if self._tess_data:
                config += f' --tessdata-dir "{self._tess_data}"'

            lang = 'kor+eng'
            if self._tess_data and \
               not os.path.exists(os.path.join(self._tess_data, 'kor.traineddata')):
                lang = 'eng'

            return pytesseract.image_to_string(
                Image.fromarray(processed), lang=lang, config=config
            ).strip()
        except Exception:
            return ''

    # ── Windows 내장 OCR (폴백) ───────────────
    def _windows_ocr(self, img_path: str) -> str:
        abs_img  = os.path.abspath(img_path).replace('/', '\\')

        fd, txt_path = tempfile.mkstemp(suffix='.txt')
        os.close(fd)
        abs_txt = txt_path.replace('/', '\\')

        ps = (
            "$ErrorActionPreference='Stop'\n"
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime\n"
            "$m=[System.WindowsRuntimeSystemExtensions].GetMethods()\n"
            "$ag=($m|Where-Object{"
            "  $_.Name -eq 'AsTask' -and"
            "  $_.GetParameters().Count -eq 1 -and"
            "  $_.GetParameters()[0].ParameterType.Name -match 'IAsyncOperation'"
            "})[0]\n"
            "function Await($wrt,$t){"
            "  $task=$ag.MakeGenericMethod($t).Invoke($null,@($wrt));"
            "  $task.Wait(-1)|Out-Null; $task.Result}\n"
            "[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]|Out-Null\n"
            "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null\n"
            "[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics,ContentType=WindowsRuntime]|Out-Null\n"
            f"$imgPath='{abs_img}'\n"
            f"$txtPath='{abs_txt}'\n"
            "$file=Await([Windows.Storage.StorageFile]::GetFileFromPathAsync($imgPath))"
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
            "if(-not $engine){exit 1}\n"
            "$r=Await($engine.RecognizeAsync($bitmap))([Windows.Media.Ocr.OcrResult])\n"
            "[System.IO.File]::WriteAllText($txtPath,$r.Text,"
            "  [System.Text.UTF8Encoding]::new($false))\n"
        )

        try:
            proc = subprocess.run(
                ['powershell', '-ExecutionPolicy', 'Bypass',
                 '-NoProfile', '-NonInteractive', '-Command', ps],
                capture_output=True, timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if proc.returncode == 0 and os.path.getsize(txt_path) > 0:
                with open(txt_path, 'r', encoding='utf-8') as f:
                    return f.read().strip()
            return ''
        except Exception:
            return ''
        finally:
            try:
                os.unlink(txt_path)
            except OSError:
                pass

    # ── 날짜 파싱 ─────────────────────────────
    def _parse_date(self, text: str) -> str:
        for pat in [
            r'(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})',
            r'\b(\d{4})(\d{2})(\d{2})\b',
        ]:
            for m in re.finditer(pat, text):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}{mo:02d}{d:02d}"
        return ''

    # ── 금액 파싱 ─────────────────────────────
    def _parse_amount(self, text: str) -> str:
        currency = []
        for m in re.finditer(r'\d{1,3}(?:,\d{3})+', text):
            v = int(m.group().replace(',', ''))
            if 100 <= v <= 50_000_000:
                currency.append(v)
        if currency:
            return str(max(currency))

        plain = []
        for m in re.finditer(r'\b(\d{3,7})\b', text):
            v = int(m.group())
            if 100 <= v <= 9_999_999:
                plain.append(v)
        return str(max(plain)) if plain else ''

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
        try:
            from datetime import datetime
            datetime.strptime(date, '%Y%m%d')
        except ValueError:
            messagebox.showwarning("입력 오류", "올바른 날짜가 아닙니다.")
            return
        if not amount or not (1 <= int(amount) <= 100_000_000):
            messagebox.showwarning("입력 오류", "유효한 금액을 입력해 주세요.")
            return

        ext = Path(self.orig_path).suffix.lower() if self.orig_path else '.jpg'
        if ext not in {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}:
            ext = '.jpg'
        encode_ext = '.jpg' if ext in {'.jpg', '.jpeg'} else \
                     '.png' if ext in {'.tiff', '.tif'} else ext

        exe_dir  = Path(sys.argv[0]).resolve().parent
        out_dir  = exe_dir / '스캔된_영수증'
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

        # 목록에 저장 완료 표시
        if self.queue_idx < len(self.receipt_data):
            self.receipt_data[self.queue_idx]['saved'] = True
            self._update_list_row(self.queue_idx)

        remaining = len(self.file_queue) - self.queue_idx - 1
        msg = f"저장 완료!\n\n파일명: {out_path.name}"
        if remaining > 0:
            msg += f"\n\n남은 파일: {remaining}장 → 자동으로 다음 파일을 불러옵니다."
        messagebox.showinfo("저장 완료", msg)

        if remaining > 0:
            self._next_file()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    app = ReceiptApp()
    app.run()
