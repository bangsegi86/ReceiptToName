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
        self._active_col:  int        = 1   # 1=결제일자, 2=합계금액

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
        self.root.columnconfigure(0, weight=0, minsize=380)  # 목록 패널
        self.root.columnconfigure(1, weight=3)               # 이미지 캔버스
        self.root.columnconfigure(2, weight=0, minsize=310)  # 우측 패널
        self.root.rowconfigure(0, weight=1)

        self._build_list_panel()

        # ─ 이미지 보정 패널 ─
        left = tk.Frame(self.root, bg=C['panel'])
        left.grid(row=0, column=1, sticky='nsew', padx=4, pady=8)
        left.rowconfigure(1, weight=1)
        left.rowconfigure(2, weight=0, minsize=50)  # 버튼 행 항상 표시
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
        btn(bf, "💾  보정본으로 원본 대체", self._replace_with_warped,
            C['yellow'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "📋  현재 이미지 복사", self._copy_image_to_clipboard,
            C['green'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "🗜  현재 이미지 압축 저장", self._compress_current,
            C['surface']).pack(side=tk.LEFT, padx=2)

        tk.Frame(bf, bg=C['panel'], width=12).pack(side=tk.LEFT)
        self.prev_btn = btn(bf, "◀ 이전", self._prev_file)
        self.prev_btn.pack(side=tk.LEFT, padx=2)
        self.next_btn = btn(bf, "다음 ▶", self._next_file)
        self.next_btn.pack(side=tk.LEFT, padx=2)

        # ─ 우측 패널 ─
        right = tk.Frame(self.root, bg=C['panel'], width=310)
        right.grid(row=0, column=2, sticky='nsew', padx=(4, 8), pady=8)
        right.pack_propagate(False)
        right.columnconfigure(0, weight=1)

        row = 0

        tk.Label(right, text="보정 미리보기", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 10)).grid(row=row, column=0,
                                              sticky='w', padx=12, pady=(10, 2))
        row += 1

        pf = tk.Frame(right, bg=C['canvas_bg'], height=260)
        pf.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 6))
        pf.pack_propagate(False)
        self.prev_canvas = tk.Canvas(pf, bg=C['canvas_bg'], highlightthickness=0)
        self.prev_canvas.pack(fill=tk.BOTH, expand=True)
        row += 1

        ttk.Separator(right).grid(row=row, column=0, sticky='ew', padx=12, pady=6)
        row += 1

        self.ocr_btn = tk.Button(
            right, text="🔍  OCR 실행 (현재 파일)",
            command=self._run_ocr,
            bg=C['surface'], fg=C['text'], relief=tk.FLAT,
            padx=10, pady=8, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['button'], activeforeground=C['text'],
            cursor='hand2', bd=0,
        )
        self.ocr_btn.grid(row=row, column=0, sticky='ew', padx=12, pady=(0, 4))
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
        self.ocr_text = tk.Text(tf, height=4, bg=C['canvas_bg'], fg=C['subtext'],
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
        lp.rowconfigure(3, weight=1)   # Treeview 행만 늘어남
        lp.columnconfigure(0, weight=1)
        lp.columnconfigure(1, weight=0)

        # 제목
        tk.Label(lp, text="영수증 목록", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 11, 'bold')
                 ).grid(row=0, column=0, columnspan=2, sticky='w',
                        padx=10, pady=(10, 2))

        # 요약 바 (건수 + 합계금액)
        summary_frame = tk.Frame(lp, bg=C['surface'])
        summary_frame.grid(row=1, column=0, columnspan=2, sticky='ew',
                           padx=8, pady=(0, 4))
        summary_frame.columnconfigure(0, weight=1)
        summary_frame.columnconfigure(1, weight=1)

        self.summary_count_var  = tk.StringVar(value="총  0건")
        self.summary_amount_var = tk.StringVar(value="합계  0원")
        tk.Label(summary_frame, textvariable=self.summary_count_var,
                 bg=C['surface'], fg=C['accent'],
                 font=('맑은 고딕', 9, 'bold'), anchor='w', padx=8, pady=4
                 ).grid(row=0, column=0, sticky='ew')
        tk.Label(summary_frame, textvariable=self.summary_amount_var,
                 bg=C['surface'], fg=C['yellow'],
                 font=('맑은 고딕', 9, 'bold'), anchor='e', padx=8, pady=4
                 ).grid(row=0, column=1, sticky='ew')

        # 색상 범례
        legend = tk.Frame(lp, bg=C['panel'])
        legend.grid(row=2, column=0, columnspan=2, sticky='w', padx=10, pady=(2, 4))
        for color, label in [
            (C['green'],  '날짜+금액'),
            (C['yellow'], '하나만'),
            (C['red'],    '미인식'),
            (C['dim'],    '저장완료'),
        ]:
            tk.Label(legend, text='●', bg=C['panel'], fg=color,
                     font=('맑은 고딕', 9)).pack(side=tk.LEFT)
            tk.Label(legend, text=label, bg=C['panel'], fg=C['dim'],
                     font=('맑은 고딕', 8)).pack(side=tk.LEFT, padx=(1, 8))

        cols = ('file', 'date', 'amount', 'size')
        self.receipt_tree = ttk.Treeview(lp, columns=cols, show='headings',
                                          selectmode='extended')
        self.receipt_tree.heading('file',   text='파일명')
        self.receipt_tree.heading('date',   text='결제일자')
        self.receipt_tree.heading('amount', text='합계금액')
        self.receipt_tree.heading('size',   text='크기 (현재→압축후)')
        self.receipt_tree.column('file',   width=85,  minwidth=55, stretch=True)
        self.receipt_tree.column('date',   width=72,  minwidth=65, stretch=False, anchor='center')
        self.receipt_tree.column('amount', width=65,  minwidth=55, stretch=False, anchor='e')
        self.receipt_tree.column('size',   width=130, minwidth=100, stretch=False, anchor='center')

        # 인식 상태별 색상
        self.receipt_tree.tag_configure('done',    foreground=C['green'])   # 날짜+금액 모두
        self.receipt_tree.tag_configure('partial', foreground=C['yellow'])  # 하나만
        self.receipt_tree.tag_configure('none',    foreground=C['red'])     # 미인식
        self.receipt_tree.tag_configure('saved',   foreground=C['dim'])     # 저장 완료

        vsb = ttk.Scrollbar(lp, orient='vertical', command=self.receipt_tree.yview)
        self.receipt_tree.configure(yscrollcommand=vsb.set)

        self.receipt_tree.grid(row=3, column=0, sticky='nsew',
                               padx=(8, 0), pady=(0, 0))
        vsb.grid(row=3, column=1, sticky='ns', padx=(0, 8), pady=(0, 0))

        # 선택 항목 OCR 실행 버튼
        self.batch_ocr_btn = tk.Button(
            lp, text="선택 항목 OCR 실행",
            command=self._run_ocr_batch,
            bg=C['accent'], fg='#1e1e2e', relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['button'], activeforeground=C['text'],
            cursor='hand2', bd=0,
        )
        self.batch_ocr_btn.grid(row=4, column=0, columnspan=2,
                                sticky='ew', padx=8, pady=(6, 2))
        lp.rowconfigure(4, weight=0)

        self.batch_save_btn = tk.Button(
            lp, text="✅  선택 항목 저장",
            command=self._save_selected,
            bg='#40a02b', fg='white', relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['green'], activeforeground='#1e1e2e',
            cursor='hand2', bd=0,
        )
        self.batch_save_btn.grid(row=5, column=0, columnspan=2,
                                 sticky='ew', padx=8, pady=(2, 2))
        lp.rowconfigure(5, weight=0)

        self.compress_btn = tk.Button(
            lp, text="🗜  선택 항목 압축 저장",
            command=self._compress_selected,
            bg=C['button'], fg=C['text'], relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['surface'], activeforeground=C['text'],
            cursor='hand2', bd=0,
        )
        self.compress_btn.grid(row=6, column=0, columnspan=2,
                               sticky='ew', padx=8, pady=(2, 8))
        lp.rowconfigure(6, weight=0)

        self.receipt_tree.bind('<<TreeviewSelect>>', self._on_list_select)
        self.receipt_tree.bind('<Double-Button-1>',  self._on_tree_double_click)
        self.receipt_tree.bind('<ButtonPress-1>',    self._on_tree_single_click)
        self.receipt_tree.bind('<KeyPress>',         self._on_tree_key)

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
    # 목록 요약 및 인라인 편집
    # ──────────────────────────────────────────
    def _update_summary(self):
        total = len(self.file_queue)
        amount_sum = 0
        for rd in self.receipt_data:
            try:
                amount_sum += int(rd['amount']) if rd['amount'] else 0
            except ValueError:
                pass
        self.summary_count_var.set(f"총  {total}건")
        self.summary_amount_var.set(
            f"합계  {amount_sum:,}원" if amount_sum else "합계  0원")

    def _on_tree_single_click(self, event):
        """단일 클릭으로 활성 컬럼 추적."""
        col = self.receipt_tree.identify_column(event.x)
        if col:
            col_idx = int(col[1:]) - 1
            if col_idx in (1, 2):
                self._active_col = col_idx

    def _on_tree_double_click(self, event):
        region = self.receipt_tree.identify_region(event.x, event.y)
        if region != 'cell':
            return
        col     = self.receipt_tree.identify_column(event.x)
        item    = self.receipt_tree.identify_row(event.y)
        if not item:
            return
        col_idx = int(col[1:]) - 1
        if col_idx not in (1, 2):   # 결제일자·합계금액만 편집 가능
            return
        self._active_col = col_idx
        self._start_cell_edit(item, col_idx)

    def _on_tree_key(self, event):
        """키보드로 셀 편집 시작 (엑셀 스타일)."""
        item = self.receipt_tree.focus()
        if not item:
            return

        ks = event.keysym

        # F2 → 기존 값 유지하며 편집
        if ks == 'F2':
            self._edit_focused_cell()
            return 'break'

        # Enter → 편집 시작
        if ks == 'Return':
            self._edit_focused_cell()
            return 'break'

        # Tab → 활성 컬럼 전환 (편집 시작 없이)
        if ks == 'Tab':
            self._active_col = 2 if self._active_col == 1 else 1
            return 'break'

        # Delete / BackSpace → 셀 내용 지우기
        if ks in ('Delete', 'BackSpace'):
            data_idx = int(item)
            if data_idx < len(self.receipt_data):
                field = 'date' if self._active_col == 1 else 'amount'
                self.receipt_data[data_idx][field] = ''
                self._update_list_row(data_idx)
                self._update_summary()
                if data_idx == self.queue_idx:
                    self._loading = True
                    if field == 'date':   self.date_var.set('')
                    else:                 self.amount_var.set('')
                    self._loading = False
            return 'break'

        # 인쇄 가능한 문자 → 내용 교체하며 편집 시작
        if event.char and event.char.isprintable() and ks not in (
            'Up', 'Down', 'Left', 'Right', 'Prior', 'Next',
            'Home', 'End', 'Escape',
        ):
            self._edit_focused_cell(replace=True, initial=event.char)
            return 'break'

    def _edit_focused_cell(self, replace: bool = False, initial: str = ''):
        item = self.receipt_tree.focus()
        if not item:
            return
        self._start_cell_edit(item, self._active_col, replace=replace, initial=initial)

    def _start_cell_edit(self, item: str, col_idx: int,
                         replace: bool = False, initial: str = ''):
        col  = f'#{col_idx + 1}'
        bbox = self.receipt_tree.bbox(item, col)
        if not bbox:
            return
        x, y, w, h = bbox
        data_idx = int(item)
        if data_idx >= len(self.receipt_data):
            return
        rd    = self.receipt_data[data_idx]
        field = 'date' if col_idx == 1 else 'amount'

        init_val = initial if replace else rd[field]
        var      = tk.StringVar(value=init_val)
        entry    = tk.Entry(self.receipt_tree, textvariable=var,
                            bg=C['accent'], fg='#1e1e2e',
                            font=('맑은 고딕', 9), relief=tk.FLAT, bd=2,
                            insertbackground='#1e1e2e', justify='center')
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()
        entry.icursor(tk.END)
        if not replace:
            entry.select_range(0, tk.END)

        committed = [False]

        def _do_commit():
            if committed[0]:
                return
            committed[0] = True
            new_val = re.sub(r'[^\d]', '', var.get().strip())
            rd[field]      = new_val
            rd['ocr_done'] = True
            self._update_list_row(data_idx)
            self._update_summary()
            if data_idx == self.queue_idx:
                self._loading = True
                if field == 'date':   self.date_var.set(new_val)
                if field == 'amount': self.amount_var.set(new_val)
                self._loading = False
            try:
                entry.destroy()
            except tk.TclError:
                pass

        def on_enter(_=None):
            _do_commit()
            # 다음 행 같은 컬럼으로 이동
            nxt = self.receipt_tree.next(item)
            if nxt:
                self.receipt_tree.selection_set(nxt)
                self.receipt_tree.focus(nxt)
                self.receipt_tree.see(nxt)
                idx = int(nxt)
                if idx != self.queue_idx:
                    self.queue_idx = idx
                    self._load_current()
                self.root.after(20, self._edit_focused_cell)
            return 'break'

        def on_tab(_=None):
            _do_commit()
            # 같은 행 반대 컬럼으로 이동
            self._active_col = 2 if col_idx == 1 else 1
            self.root.after(20, self._edit_focused_cell)
            return 'break'

        def on_up(_=None):
            _do_commit()
            prv = self.receipt_tree.prev(item)
            if prv:
                self.receipt_tree.selection_set(prv)
                self.receipt_tree.focus(prv)
                self.receipt_tree.see(prv)
                idx = int(prv)
                if idx != self.queue_idx:
                    self.queue_idx = idx
                    self._load_current()
                self.root.after(20, self._edit_focused_cell)
            return 'break'

        def on_down(_=None):
            _do_commit()
            nxt = self.receipt_tree.next(item)
            if nxt:
                self.receipt_tree.selection_set(nxt)
                self.receipt_tree.focus(nxt)
                self.receipt_tree.see(nxt)
                idx = int(nxt)
                if idx != self.queue_idx:
                    self.queue_idx = idx
                    self._load_current()
                self.root.after(20, self._edit_focused_cell)
            return 'break'

        entry.bind('<Return>',    on_enter)
        entry.bind('<Tab>',       on_tab)
        entry.bind('<Up>',        on_up)
        entry.bind('<Down>',      on_down)
        entry.bind('<FocusOut>',  lambda _: _do_commit())
        entry.bind('<Escape>',    lambda _: entry.destroy())

    # ──────────────────────────────────────────
    # 목록 데이터 관리
    # ──────────────────────────────────────────
    def _init_receipt_data(self, paths: list):
        self.receipt_data = [
            {'date': '', 'amount': '', 'ocr_done': False, 'saved': False,
             'file_size': 0, 'compressed_size': -1}
            for _ in paths
        ]
        self._refresh_list()
        self._update_summary()
        # 파일 크기 순차 계산
        self._size_queue = list(range(len(paths)))
        self._size_next()

    def _refresh_list(self):
        self.receipt_tree.delete(*self.receipt_tree.get_children())
        for i, (path, data) in enumerate(zip(self.file_queue, self.receipt_data)):
            self.receipt_tree.insert('', 'end', iid=str(i),
                                     values=self._row_values(i, path, data),
                                     tags=(self._row_tag(data),))
        self._select_list_row(self.queue_idx)

    def _update_list_row(self, idx: int):
        if idx >= len(self.receipt_data) or idx >= len(self.file_queue):
            return
        data = self.receipt_data[idx]
        try:
            self.receipt_tree.item(str(idx),
                                   values=self._row_values(idx, self.file_queue[idx], data),
                                   tags=(self._row_tag(data),))
        except tk.TclError:
            pass

    def _row_values(self, idx: int, path: str, data: dict) -> tuple:
        fname = Path(path).name
        if len(fname) > 13:
            fname = fname[:10] + '...'
        date_disp   = data['date'] if data['date'] else '-'
        amount_disp = self._fmt_amount(data['amount'])
        size_disp   = self._fmt_size_col(data)
        return (fname, date_disp, amount_disp, size_disp)

    def _fmt_size_col(self, data: dict) -> str:
        fs = data.get('file_size', 0)
        cs = data.get('compressed_size', -1)
        if fs <= 0:
            return '계산중…' if cs == -1 else '-'
        cur = self._fmt_size(fs)
        if cs == -1:
            return f"{cur} → ?"
        if cs >= fs:
            return f"{cur} (최소)"
        return f"{cur} → {self._fmt_size(cs)}"

    @staticmethod
    def _fmt_size(nbytes: int) -> str:
        if nbytes >= 1_048_576:
            return f"{nbytes / 1_048_576:.1f}MB"
        if nbytes >= 1_024:
            return f"{nbytes / 1_024:.0f}KB"
        return f"{nbytes}B"

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
        self._update_summary()

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
    # 보정본으로 원본 파일 대체
    # ──────────────────────────────────────────
    def _replace_with_warped(self):
        if self.warped_img is None:
            messagebox.showwarning("경고", "먼저 보정을 적용하세요.")
            return
        if not self.orig_path:
            messagebox.showwarning("경고", "원본 파일 경로를 알 수 없습니다.")
            return

        if not messagebox.askyesno(
            "원본 파일 대체",
            f"보정된 이미지로 원본 파일을 덮어씁니다.\n\n"
            f"  {Path(self.orig_path).name}\n\n"
            "이 작업은 되돌릴 수 없습니다. 계속하시겠습니까?",
        ):
            return

        ext = Path(self.orig_path).suffix.lower()
        encode_ext = '.jpg' if ext in {'.jpg', '.jpeg'} else \
                     '.png' if ext in {'.tiff', '.tif'} else ext
        if encode_ext not in {'.jpg', '.png', '.bmp', '.webp'}:
            encode_ext = '.jpg'

        try:
            ok, buf = cv2.imencode(encode_ext, self.warped_img)
            if not ok:
                raise RuntimeError("인코딩 실패")
            buf.tofile(self.orig_path)
        except Exception as e:
            messagebox.showerror("저장 오류", f"파일 저장 실패\n{e}")
            return

        # 보정본을 새 원본으로 교체 → 다시 보정 불필요 상태로 리셋
        self.orig_img   = self.warped_img.copy()
        self.warped_img = None
        self.corners    = []
        self.mode       = 'view'
        self.mode_lbl.configure(text="")
        h, w = self.orig_img.shape[:2]
        self.corners = [[0.0, 0.0], [float(w), 0.0],
                        [float(w), float(h)], [0.0, float(h)]]
        self._refresh_canvas()
        self._apply_correction()
        messagebox.showinfo("완료", "원본 파일을 보정된 이미지로 대체했습니다.")

    # ── 현재 이미지 클립보드 복사
    # ──────────────────────────────────────────
    def _copy_image_to_clipboard(self):
        img = self.warped_img if self.warped_img is not None else self.orig_img
        if img is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return
        try:
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f:
                tmp_path = f.name
            ok, buf = cv2.imencode('.png', img)
            if not ok:
                raise RuntimeError("인코딩 실패")
            buf.tofile(tmp_path)

            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "Add-Type -AssemblyName System.Drawing;"
                f"$img = [System.Drawing.Image]::FromFile('{tmp_path}');"
                "[System.Windows.Forms.Clipboard]::SetImage($img);"
                "$img.Dispose();"
            )
            subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_script],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=15,
            )
            messagebox.showinfo("복사 완료", "현재 이미지를 클립보드에 복사했습니다.")
        except Exception as e:
            messagebox.showerror("오류", f"클립보드 복사 실패\n{e}")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ── 현재 이미지 압축 저장
    # ──────────────────────────────────────────
    def _compress_current(self):
        img = self.warped_img if self.warped_img is not None else self.orig_img
        if img is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return
        if not self.orig_path:
            messagebox.showwarning("경고", "원본 파일 경로를 알 수 없습니다.")
            return

        path = Path(self.orig_path)
        ext  = path.suffix.lower()
        if ext == '.png':
            encode_ext = '.png'
            params     = [cv2.IMWRITE_PNG_COMPRESSION, 9]
        else:
            encode_ext = '.jpg'
            params     = [cv2.IMWRITE_JPEG_QUALITY, 85]

        stem     = path.stem
        out_stem = stem if stem.startswith('resize_') else f'resize_{stem}'
        out_path = path.parent / f'{out_stem}{encode_ext}'

        try:
            ok, buf = cv2.imencode(encode_ext, img, params)
            if not ok:
                raise RuntimeError("인코딩 실패")
            buf.tofile(str(out_path))
            new_size  = len(buf)
            orig_size = path.stat().st_size if path.exists() else 0

            def _fmt(b):
                return f"{b/1024/1024:.1f}MB" if b >= 1024*1024 else f"{b/1024:.0f}KB"

            msg = f"저장 완료: {out_path.name}"
            if orig_size:
                msg += f"\n{_fmt(orig_size)} → {_fmt(new_size)}"
            messagebox.showinfo("압축 저장 완료", msg)
        except Exception as e:
            messagebox.showerror("저장 오류", f"압축 저장 실패\n{e}")

    # ── 자동 원근 보정
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
        """목록에서 선택된 항목을 순차적으로 OCR 실행."""
        selected = self.receipt_tree.selection()
        if not selected:
            messagebox.showwarning("선택 없음",
                                   "목록에서 OCR 할 항목을 선택하세요.\n(Ctrl+클릭으로 다중 선택)")
            return

        self._ocr_queue = [int(iid) for iid in selected]
        self._ocr_total  = len(self._ocr_queue)
        self._ocr_done_n = 0
        self.batch_ocr_btn.configure(state=tk.DISABLED,
                                     text=f"OCR 실행 중… (0/{self._ocr_total})")
        self._ocr_next()

    def _ocr_next(self):
        if not self._ocr_queue:
            self.batch_ocr_btn.configure(state=tk.NORMAL, text="선택 항목 OCR 실행")
            return
        idx = self._ocr_queue.pop(0)
        self._mark_list_processing(idx)
        threading.Thread(target=self._ocr_seq_worker, args=(idx,), daemon=True).start()

    def _mark_list_processing(self, idx: int):
        try:
            vals = list(self.receipt_tree.item(str(idx), 'values'))
            vals[1] = '처리중…'
            self.receipt_tree.item(str(idx), values=vals)
        except tk.TclError:
            pass

    def _ocr_seq_worker(self, idx: int):
        try:
            target = self._load_and_warp(idx)
            text   = self._do_ocr(target)
        except Exception:
            text = ''
        self.root.after(0, lambda: self._ocr_seq_done(idx, text))

    def _ocr_seq_done(self, idx: int, text: str):
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

        if idx == self.queue_idx and text:
            self.ocr_text.delete('1.0', tk.END)
            self.ocr_text.insert(tk.END, text)
            self.ocr_status_var.set(f"OCR 완료  ({len(text)}자 인식)")
            self._loading = True
            if date:   self.date_var.set(date)
            if amount: self.amount_var.set(amount)
            self._loading = False

        self._update_summary()
        self._ocr_done_n += 1
        self.batch_ocr_btn.configure(
            text=f"OCR 실행 중… ({self._ocr_done_n}/{self._ocr_total})")
        self._ocr_next()

    # ──────────────────────────────────────────
    # 선택 항목 일괄 저장
    # ──────────────────────────────────────────
    def _save_selected(self):
        selected = self.receipt_tree.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "저장할 항목을 목록에서 선택하세요.")
            return

        indices = [int(iid) for iid in selected]

        # 누락 값 검사 — 하나라도 없으면 전체 차단
        incomplete = []
        for idx in indices:
            rd = self.receipt_data[idx]
            missing = []
            if not rd['date']:   missing.append("결제일자")
            if not rd['amount']: missing.append("합계금액")
            if missing:
                fname = Path(self.file_queue[idx]).name
                incomplete.append(f"  • {fname}  [{', '.join(missing)} 없음]")

        if incomplete:
            msg = ("아래 항목에 값이 없어 저장할 수 없습니다.\n"
                   "OCR 실행 또는 직접 입력 후 다시 시도하세요.\n\n"
                   + "\n".join(incomplete))
            messagebox.showwarning("저장 불가", msg)
            return

        self.batch_save_btn.configure(state=tk.DISABLED, text="저장 중…")
        threading.Thread(
            target=self._batch_save_worker,
            args=(indices,),
            daemon=True,
        ).start()

    def _batch_save_worker(self, indices: list):
        exe_dir = Path(sys.argv[0]).resolve().parent
        out_dir = exe_dir / '스캔된_영수증'
        out_dir.mkdir(exist_ok=True)

        saved, errors = 0, []

        for idx in indices:
            rd     = self.receipt_data[idx]
            path   = self.file_queue[idx]
            date   = rd['date']
            amount = re.sub(r'[^\d]', '', rd['amount'])

            try:
                # 현재 화면의 파일은 보정본 재사용, 나머지는 자동 보정
                if idx == self.queue_idx and self.warped_img is not None:
                    target = self.warped_img.copy()
                else:
                    target = self._load_and_warp(idx)

                ext = Path(path).suffix.lower()
                if ext not in {'.jpg', '.jpeg', '.png', '.bmp',
                               '.tiff', '.tif', '.webp'}:
                    ext = '.jpg'
                encode_ext = ('.jpg' if ext in {'.jpg', '.jpeg'} else
                              '.png' if ext in {'.tiff', '.tif'} else ext)

                base     = f"{date}_{amount}"
                out_path = out_dir / f"{base}{ext}"
                counter  = 1
                while out_path.exists():
                    out_path = out_dir / f"{base}_{counter}{ext}"
                    counter += 1

                ok, buf = cv2.imencode(encode_ext, target)
                if not ok:
                    raise RuntimeError("인코딩 실패")
                buf.tofile(str(out_path))
                saved += 1
                self.root.after(0, lambda i=idx: self._mark_saved_in_list(i))

            except Exception as e:
                errors.append(f"{Path(path).name}: {e}")

        self.root.after(0, lambda: self._batch_save_done(saved, errors))

    def _load_and_warp(self, idx: int) -> np.ndarray:
        """파일 로드 → 자동 꼭짓점 검출 → 원근 보정 이미지 반환."""
        path = self.file_queue[idx]
        raw  = np.fromfile(path, dtype=np.uint8)
        img  = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"이미지 로드 실패: {path}")
        corners = self._detect_receipt(img)
        if not corners or len(corners) != 4:
            return img

        def _dims(s):
            w = max(int(max(np.linalg.norm(s[1]-s[0]),
                            np.linalg.norm(s[2]-s[3]))), 1)
            h = max(int(max(np.linalg.norm(s[3]-s[0]),
                            np.linalg.norm(s[2]-s[1]))), 1)
            return w, h

        src = np.float32(corners)
        out_w, out_h = _dims(src)
        oh, ow = img.shape[:2]
        if (oh > ow * 1.2 and out_w > out_h) or \
           (ow > oh * 1.2 and out_h > out_w):
            src = np.float32([src[0], src[3], src[2], src[1]])
            out_w, out_h = _dims(src)
        dst = np.float32([[0, 0], [out_w-1, 0],
                          [out_w-1, out_h-1], [0, out_h-1]])
        M   = cv2.getPerspectiveTransform(src, dst)
        return cv2.warpPerspective(img, M, (out_w, out_h))

    def _mark_saved_in_list(self, idx: int):
        if 0 <= idx < len(self.receipt_data):
            self.receipt_data[idx]['saved'] = True
            self._update_list_row(idx)
            self._update_summary()

    def _batch_save_done(self, saved: int, errors: list):
        self.batch_save_btn.configure(state=tk.NORMAL, text="✅  선택 항목 저장")
        if errors:
            msg = f"{saved}개 저장 완료\n\n실패 목록:\n" + "\n".join(errors)
            messagebox.showwarning("일부 저장 실패", msg)
        else:
            messagebox.showinfo("저장 완료", f"{saved}개 파일을 저장했습니다.")

    # ──────────────────────────────────────────
    # 파일 크기 계산 (순차 백그라운드)
    # ──────────────────────────────────────────
    def _size_next(self):
        if not hasattr(self, '_size_queue') or not self._size_queue:
            return
        idx = self._size_queue.pop(0)
        threading.Thread(target=self._size_worker, args=(idx,), daemon=True).start()

    def _size_worker(self, idx: int):
        try:
            path      = self.file_queue[idx]
            file_size = os.path.getsize(path)
            raw  = np.fromfile(path, dtype=np.uint8)
            img  = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError
            ext = Path(path).suffix.lower()
            compressed_size = self._encode_compressed_buf(img, ext)
        except Exception:
            file_size = compressed_size = 0
        self.root.after(0, lambda: self._size_done(idx, file_size, compressed_size))

    def _encode_compressed_buf(self, img: np.ndarray, ext: str) -> int:
        if ext == '.png':
            ok, buf = cv2.imencode('.png', img,
                                   [cv2.IMWRITE_PNG_COMPRESSION, 9])
        else:
            ok, buf = cv2.imencode('.jpg', img,
                                   [cv2.IMWRITE_JPEG_QUALITY, 85])
        return len(buf) if ok else 0

    def _size_done(self, idx: int, file_size: int, compressed_size: int):
        if idx >= len(self.receipt_data):
            return
        rd = self.receipt_data[idx]
        rd['file_size']       = file_size
        rd['compressed_size'] = compressed_size
        self._update_list_row(idx)
        self._size_next()   # 다음 파일 계산

    # ──────────────────────────────────────────
    # 선택 항목 압축 저장
    # ──────────────────────────────────────────
    def _compress_selected(self):
        selected = self.receipt_tree.selection()
        if not selected:
            messagebox.showwarning("선택 없음", "압축할 항목을 선택하세요.")
            return
        self._compress_queue  = [int(iid) for iid in selected]
        self._compress_total  = len(self._compress_queue)
        self._compress_done_n = 0
        self._compress_errors = []
        self.compress_btn.configure(state=tk.DISABLED,
                                    text=f"압축 중… (0/{self._compress_total})")
        self._compress_next_item()

    def _compress_next_item(self):
        if not self._compress_queue:
            self.compress_btn.configure(state=tk.NORMAL, text="🗜  선택 항목 압축 저장")
            msg = f"{self._compress_total - len(self._compress_errors)}개 압축 완료"
            if self._compress_errors:
                msg += "\n\n실패:\n" + "\n".join(self._compress_errors)
                messagebox.showwarning("압축 완료 (일부 실패)", msg)
            else:
                messagebox.showinfo("압축 완료", msg +
                                    "\n파일명 앞에 'resize_' 가 붙어 원본 폴더에 저장됐습니다.")
            return
        idx = self._compress_queue.pop(0)
        threading.Thread(target=self._compress_worker, args=(idx,), daemon=True).start()

    def _compress_worker(self, idx: int):
        try:
            path   = self.file_queue[idx]
            raw    = np.fromfile(path, dtype=np.uint8)
            img    = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("이미지 로드 실패")

            ext      = Path(path).suffix.lower()
            parent   = Path(path).parent
            stem     = Path(path).stem

            if ext == '.png':
                encode_ext = '.png'
                params     = [cv2.IMWRITE_PNG_COMPRESSION, 9]
            else:
                encode_ext = '.jpg'
                params     = [cv2.IMWRITE_JPEG_QUALITY, 85]

            # 이미 resize_ 접두어가 있으면 그대로
            out_stem = stem if stem.startswith('resize_') else f'resize_{stem}'
            out_path = parent / f'{out_stem}{encode_ext}'

            ok, buf = cv2.imencode(encode_ext, img, params)
            if not ok:
                raise RuntimeError("인코딩 실패")
            buf.tofile(str(out_path))
            new_size = len(buf)
            self.root.after(0, lambda: self._compress_item_done(idx, new_size, None))
        except Exception as e:
            err = f"{Path(self.file_queue[idx]).name}: {e}"
            self.root.after(0, lambda: self._compress_item_done(idx, 0, err))

    def _compress_item_done(self, idx: int, new_size: int, error: str | None):
        if error:
            self._compress_errors.append(error)
        else:
            if 0 <= idx < len(self.receipt_data):
                # 압축 후 크기를 compressed_size에 반영
                self.receipt_data[idx]['compressed_size'] = new_size
                self._update_list_row(idx)
        self._compress_done_n += 1
        self.compress_btn.configure(
            text=f"압축 중… ({self._compress_done_n}/{self._compress_total})")
        self._compress_next_item()

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
