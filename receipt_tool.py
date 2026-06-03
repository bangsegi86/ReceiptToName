#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
영수증 보정 프로그램 (Receipt Correction Tool)
완전 포터블 - 추가 설치 불필요
"""

import sys
import os
import re
import struct
import ctypes
import subprocess
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import json

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

_PADDLE_IMPORT_ERROR = None
try:
    from paddleocr import PaddleOCR
    HAS_PADDLE = True
except Exception as _e:
    HAS_PADDLE = False
    _PADDLE_IMPORT_ERROR = str(_e)


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

        # 이미지 분할 탭 상태 — _build_ui() 에서 _sp_load_presets() 가
        # self.sp_presets 를 채우므로, 빌드 전에 미리 초기화해 둔다.
        # (빌드 후 초기화하면 로드한 즐겨찾기가 빈 dict 로 덮어써짐)
        self.sp_img:              np.ndarray | None = None
        self.sp_orig_path:        str | None        = None
        self.sp_h_lines:          list              = []
        self.sp_v_lines:          list              = []
        self.sp_selected_regions: set               = set()
        self.sp_last_selected:    tuple | None      = None
        self.sp_drag:             tuple | None      = None
        self.sp_hover:            tuple | None      = None
        self.sp_selected_line:    tuple | None      = None
        self.sp_scale:            float             = 1.0
        self.sp_ox:               int               = 0
        self.sp_oy:               int               = 0
        self.sp_presets:          dict              = {}
        self.sp_pending_preset:   dict | None       = None
        self._sp_tk_img                             = None

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
        self._thr_after_id            = None  # 감지 강도 슬라이더 디바운스

        # 목록 ↔ 우측 패널 동기화 트레이스
        self.date_var.trace_add('write',   self._on_field_change)
        self.amount_var.trace_add('write', self._on_field_change)

        self._tess_cmd:  str | None = None
        self._tess_data: str | None = None
        self._init_tesseract()
        # 엔진 레이블 초기값 (Paddle 초기화 전이라 실제 값은 잠시 후 업데이트됨)
        if not HAS_PADDLE:
            engine_txt = "Tesseract" if self._tess_cmd else "Windows OCR"
            fg = C['yellow'] if self._tess_cmd else C['dim']
            self.root.after(100, lambda t=engine_txt, f=fg:
                            self.ocr_engine_lbl.configure(text=f"OCR 엔진: {t}", fg=f))

        self._paddle:           object | None = None
        self._paddle_ready:     bool          = False
        self._paddle_error:     str | None    = _PADDLE_IMPORT_ERROR
        self._paddle_init_lock: threading.Lock = threading.Lock()
        if HAS_PADDLE:
            threading.Thread(target=self._paddle_init_bg, daemon=True).start()
        elif _PADDLE_IMPORT_ERROR:
            err_short = _PADDLE_IMPORT_ERROR[:100]
            self.root.after(200, lambda: self.ocr_status_var.set(
                f"PaddleOCR 로드 실패 → Tesseract 사용\n오류: {err_short}"))

    # ── 루트 ──────────────────────────────────
    def _build_root(self):
        self.root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
        self.root.title("영수증 스캔 도우미")
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
        style.configure('TNotebook', background=C['bg'], borderwidth=0, tabmargins=0)
        style.configure('TNotebook.Tab',
            background=C['button'], foreground=C['subtext'],
            padding=[16, 8], font=('맑은 고딕', 10))
        style.map('TNotebook.Tab',
            background=[('selected', C['accent']), ('active', C['surface'])],
            foreground=[('selected', '#1e1e2e'), ('active', C['text'])])
        style.configure('Horizontal.TScale',
            background=C['panel'], troughcolor=C['surface'], borderwidth=0)
        style.configure('Vertical.TScrollbar',
            background=C['button'], troughcolor=C['surface'],
            borderwidth=0, arrowcolor=C['subtext'])
        style.map('Vertical.TScrollbar',
            background=[('active', '#7f849c'), ('disabled', C['surface'])])
        style.configure('TCombobox',
            fieldbackground=C['surface'], background=C['button'],
            foreground=C['text'], selectbackground=C['accent'],
            selectforeground='#1e1e2e', arrowcolor=C['subtext'], borderwidth=0)
        style.map('TCombobox',
            fieldbackground=[('readonly', C['surface'])],
            foreground=[('readonly', C['text'])],
            selectbackground=[('readonly', C['accent'])])
        style.configure('Accent.Horizontal.TProgressbar',
            background=C['accent'], troughcolor=C['surface'], borderwidth=0)

        win_w, win_h = 1380, 860
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(1080, 640)
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(
            f"{win_w}x{win_h}+{max((sw-win_w)//2, 0)}+{max((sh-win_h)//2, 0)}")

    # ── UI ────────────────────────────────────
    def _build_ui(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True)

        mt   = tk.Frame(nb, bg=C['bg'])
        tab2 = tk.Frame(nb, bg=C['bg'])
        nb.add(mt,   text='  📋 영수증 보정  ')
        nb.add(tab2, text='  ✂️ 이미지 분할  ')
        self._main_tab = mt

        mt.columnconfigure(0, weight=0, minsize=380)
        mt.columnconfigure(1, weight=3)
        mt.rowconfigure(0, weight=1)

        self._build_list_panel()

        # ─ 이미지 보정 패널 ─
        left = tk.Frame(mt, bg=C['panel'])
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

        self.file_lbl = tk.Label(hdr, text="", bg=C['panel'], fg=C['dim'],
                                 font=('맑은 고딕', 9))
        self.file_lbl.pack(side=tk.LEFT, padx=(8, 0))

        # 구역 감지 강도 슬라이더 (0=자동 스윕, 1~255=수동 역치)
        thr_box = tk.Frame(hdr, bg=C['panel'])
        thr_box.pack(side=tk.RIGHT)
        self.thr_val_lbl = tk.Label(thr_box, text="자동", bg=C['panel'],
                                    fg=C['accent'], font=('맑은 고딕', 9, 'bold'),
                                    width=4, anchor='e')
        self.thr_val_lbl.pack(side=tk.RIGHT, padx=(4, 0))
        self.thr_var = tk.IntVar(value=0)
        self.thr_scale = ttk.Scale(thr_box, from_=0, to=255, length=150,
                                   orient=tk.HORIZONTAL, variable=self.thr_var,
                                   command=self._on_threshold_change)
        self.thr_scale.pack(side=tk.RIGHT)
        tk.Label(thr_box, text="🎚 감지 강도", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 9)).pack(side=tk.RIGHT, padx=(0, 6))

        cf = tk.Frame(left, bg=C['canvas_bg'])
        cf.grid(row=1, column=0, sticky='nsew', padx=10, pady=4)
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(cf, bg=C['canvas_bg'], highlightthickness=0,
                                cursor='arrow')
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.canvas.create_text(400, 300,
            text="이미지를 드래그 앤 드롭하거나\n아래 [파일 열기] 버튼을 클릭하세요",
            fill='#585b70', font=('맑은 고딕', 15), tags='hint', anchor='center')

        bf = tk.Frame(left, bg=C['panel'])
        bf.grid(row=2, column=0, sticky='ew', padx=10, pady=(4, 10))

        def btn(parent, text, cmd, bg=C['surface'], fg=C['text'], bold=False):
            f = ('맑은 고딕', 9, 'bold') if bold else ('맑은 고딕', 9)
            return tk.Button(parent, text=text, command=cmd,
                             bg=bg, fg=fg, relief=tk.FLAT,
                             padx=8, pady=5, font=f, justify=tk.CENTER,
                             activebackground=C['button'], activeforeground=C['text'],
                             cursor='hand2', bd=0)

        # 이전/다음 — 다른 버튼과 같은 2줄 스타일
        self.next_btn = btn(bf, "다음\n▶", self._next_file)
        self.next_btn.pack(side=tk.RIGHT, padx=2)
        self.prev_btn = btn(bf, "◀\n이전", self._prev_file)
        self.prev_btn.pack(side=tk.RIGHT, padx=2)
        tk.Frame(bf, bg=C['panel'], width=8).pack(side=tk.RIGHT)

        # 보정 관련 버튼
        btn(bf, "✨\n자동 보정",   self._auto_correct).pack(side=tk.LEFT, padx=2)
        btn(bf, "✏️\n수동 조정",   self._toggle_manual).pack(side=tk.LEFT, padx=2)
        btn(bf, "🔍\nOCR",         self._run_ocr,
            C['accent'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "💾\n원본 대체",   self._replace_with_warped,
            C['yellow'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "📋\n복사",        self._copy_image_to_clipboard,
            C['green'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        btn(bf, "🗜\n압축 저장",   self._compress_current).pack(side=tk.LEFT, padx=2)
        tk.Frame(bf, bg=C['panel'], width=8).pack(side=tk.LEFT)
        btn(bf, "✅\n최종 저장",   self._save,
            '#40a02b', 'white', bold=True).pack(side=tk.LEFT, padx=2)

        # ─ 히든 프레임: 코드 참조용 위젯 (화면에 표시 안 함) ─
        _hf = tk.Frame(self.root)  # 배치하지 않음
        self.prev_canvas   = tk.Canvas(_hf, bg=C['canvas_bg'], highlightthickness=0)
        self.ocr_btn       = tk.Button(_hf, text="", command=self._run_ocr)
        self.ocr_status_var = tk.StringVar(value="대기 중")
        self.ocr_progress  = ttk.Progressbar(_hf, mode='indeterminate')
        self.ocr_text      = tk.Text(_hf, height=4)
        self.date_var      = tk.StringVar()
        self.amount_var    = tk.StringVar()

        self._build_splitter_tab(tab2)

    # ── 영수증 목록 패널 ──────────────────────
    def _build_list_panel(self):
        lp = tk.Frame(self._main_tab, bg=C['panel'])
        lp.grid(row=0, column=0, sticky='nsew', padx=(8, 4), pady=8)
        lp.rowconfigure(3, weight=1)   # Treeview 행만 늘어남
        lp.columnconfigure(0, weight=1)
        lp.columnconfigure(1, weight=0)

        # 제목 + 파일 열기 버튼
        hdr0 = tk.Frame(lp, bg=C['panel'])
        hdr0.grid(row=0, column=0, columnspan=2, sticky='ew',
                  padx=10, pady=(10, 2))
        tk.Label(hdr0, text="영수증 목록", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 11, 'bold')).pack(side=tk.LEFT)
        tk.Button(hdr0, text="📂 파일 열기", command=self._open_file,
                  bg=C['accent'], fg='#1e1e2e', relief=tk.FLAT,
                  padx=8, pady=3, font=('맑은 고딕', 9, 'bold'),
                  activebackground=C['button'], activeforeground=C['text'],
                  cursor='hand2', bd=0,
                  ).pack(side=tk.RIGHT)

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
        self.receipt_tree.heading('size',   text='파일 크기')
        self.receipt_tree.column('file',   width=100, minwidth=60, stretch=True)
        self.receipt_tree.column('date',   width=74,  minwidth=65, stretch=False, anchor='center')
        self.receipt_tree.column('amount', width=68,  minwidth=55, stretch=False, anchor='e')
        self.receipt_tree.column('size',   width=115, minwidth=90, stretch=False, anchor='center')

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
            lp, text="🔍  선택 항목 OCR",
            command=self._run_ocr_batch,
            bg='#7287fd', fg='white', relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground='#8294ff', activeforeground='white',
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
            activebackground='#52b83f', activeforeground='white',
            cursor='hand2', bd=0,
        )
        self.batch_save_btn.grid(row=5, column=0, columnspan=2,
                                 sticky='ew', padx=8, pady=(2, 2))
        lp.rowconfigure(5, weight=0)

        self.compress_btn = tk.Button(
            lp, text="🗜  선택 항목 압축 저장",
            command=self._compress_selected,
            bg=C['surface'], fg=C['text'], relief=tk.FLAT,
            padx=10, pady=7, font=('맑은 고딕', 10, 'bold'),
            activebackground=C['button'], activeforeground=C['text'],
            cursor='hand2', bd=0,
        )
        self.compress_btn.grid(row=6, column=0, columnspan=2,
                               sticky='ew', padx=8, pady=(2, 4))
        lp.rowconfigure(6, weight=0)

        # OCR 엔진 상태 표시
        self.ocr_engine_lbl = tk.Label(
            lp, text="OCR 엔진 확인 중…",
            bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8),
            anchor='w', padx=10,
        )
        self.ocr_engine_lbl.grid(row=7, column=0, columnspan=2,
                                 sticky='ew', pady=(0, 8))
        lp.rowconfigure(7, weight=0)

        self.receipt_tree.bind('<<TreeviewSelect>>', self._on_list_select)
        self.receipt_tree.bind('<Double-Button-1>',  self._on_tree_double_click)
        self.receipt_tree.bind('<ButtonPress-1>',    self._on_tree_single_click)
        self.receipt_tree.bind('<KeyPress>',         self._on_tree_key)

    # ── 이벤트 바인딩 ─────────────────────────
    def _bind_events(self):
        self.canvas.bind('<ButtonPress-1>',   self._on_click)
        self.canvas.bind('<B1-Motion>',       self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)

        def _on_canvas_resize(e):
            self._refresh_canvas()
            if not self.file_queue:
                self.canvas.coords('hint', e.width // 2, e.height // 2)
        self.canvas.bind('<Configure>', _on_canvas_resize)

        # 전역 단축키
        self.root.bind('<Control-o>', lambda _: self._open_file())
        self.root.bind('<Control-O>', lambda _: self._open_file())
        self.root.bind('<F5>',        lambda _: self._auto_correct())
        self.root.bind('<Control-s>', lambda _: self._save())
        self.root.bind('<Control-S>', lambda _: self._save())

        if HAS_DND:
            for w in (self.canvas, self.root):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self._on_dnd)

    # ══════════════════════════════════════════
    # 이미지 분할 탭
    # ══════════════════════════════════════════
    def _build_splitter_tab(self, parent):
        def sbtn(p, text, cmd, bg=C['surface'], fg=C['text']):
            return tk.Button(p, text=text, command=cmd,
                             bg=bg, fg=fg, relief=tk.FLAT,
                             padx=10, pady=5, font=('맑은 고딕', 9),
                             activebackground=C['button'],
                             activeforeground=C['text'],
                             cursor='hand2', bd=0)

        # ── 툴바 (1줄: 이미지/선 편집) ──
        tb = tk.Frame(parent, bg=C['panel'])
        tb.pack(fill=tk.X, padx=8, pady=(8, 0))

        sbtn(tb, "📂 불러오기", self._sp_load_file).pack(side=tk.LEFT, padx=2)
        sbtn(tb, "📋 붙여넣기 (Ctrl+V)", self._sp_paste_image).pack(side=tk.LEFT, padx=2)

        tk.Frame(tb, bg=C['dim'], width=1).pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=3)

        sbtn(tb, "＋ 가로선", self._sp_add_h_line,
             C['yellow'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        sbtn(tb, "＋ 세로선", self._sp_add_v_line,
             C['accent'], '#1e1e2e').pack(side=tk.LEFT, padx=2)
        sbtn(tb, "선 삭제 (Del)", self._sp_delete_line).pack(side=tk.LEFT, padx=2)
        sbtn(tb, "전체 초기화",  self._sp_clear_lines).pack(side=tk.LEFT, padx=2)

        # ── 툴바 (2줄: 선 유지 + 즐겨찾기) ──
        tb2 = tk.Frame(parent, bg=C['panel'])
        tb2.pack(fill=tk.X, padx=8, pady=(4, 4))

        # 새 이미지를 불러와도 현재 선을 유지 (비슷한 영수증 연속 분할용)
        self.sp_keep_lines = tk.BooleanVar(value=True)
        tk.Checkbutton(
            tb2, text="이미지 바뀌어도 선 유지",
            variable=self.sp_keep_lines,
            bg=C['panel'], fg=C['subtext'], font=('맑은 고딕', 9),
            activebackground=C['panel'], activeforeground=C['text'],
            selectcolor=C['surface'], bd=0, highlightthickness=0,
            cursor='hand2',
        ).pack(side=tk.LEFT, padx=2)

        tk.Frame(tb2, bg=C['dim'], width=1).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=3)

        tk.Label(tb2, text="즐겨찾기:", bg=C['panel'], fg=C['subtext'],
                 font=('맑은 고딕', 9)).pack(side=tk.LEFT, padx=(0, 4))

        self.sp_preset_var = tk.StringVar()
        self.sp_preset_combo = ttk.Combobox(tb2, textvariable=self.sp_preset_var,
                                             width=18, state='readonly',
                                             font=('맑은 고딕', 9))
        self.sp_preset_combo.pack(side=tk.LEFT, padx=2)
        # 콤보에서 고르면 즉시 적용 (버튼을 누르지 않아도 됨)
        self.sp_preset_combo.bind('<<ComboboxSelected>>',
                                  lambda _e: self._sp_apply_preset())

        sbtn(tb2, "💾 저장",    self._sp_save_preset).pack(side=tk.LEFT, padx=2)
        sbtn(tb2, "📂 불러오기", self._sp_apply_preset).pack(side=tk.LEFT, padx=2)
        sbtn(tb2, "🗑 삭제",    self._sp_delete_preset).pack(side=tk.LEFT, padx=2)

        # 안내 힌트
        tk.Label(tb2,
                 text="선을 추가한 뒤 💾저장 → 다음에 콤보에서 고르면 즉시 적용됩니다",
                 bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8)
                 ).pack(side=tk.LEFT, padx=10)

        # ── 본문 (캔버스 + 영역 패널) ──
        content = tk.Frame(parent, bg=C['bg'])
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0)

        cf = tk.Frame(content, bg=C['canvas_bg'])
        cf.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        cf.rowconfigure(0, weight=1)
        cf.columnconfigure(0, weight=1)

        self.sp_canvas = tk.Canvas(cf, bg=C['canvas_bg'],
                                   highlightthickness=0, cursor='crosshair')
        self.sp_canvas.grid(row=0, column=0, sticky='nsew')

        self.sp_info_lbl = tk.Label(cf, text="",
                                    bg=C['canvas_bg'], fg=C['dim'],
                                    font=('맑은 고딕', 8), anchor='w', padx=6)
        self.sp_info_lbl.grid(row=1, column=0, sticky='ew')
        cf.rowconfigure(1, weight=0)

        # ── 우측 영역 패널 ──
        rp = tk.Frame(content, bg=C['panel'], width=250)
        rp.grid(row=0, column=1, sticky='nsew')
        rp.pack_propagate(False)
        rp.rowconfigure(2, weight=1)
        rp.columnconfigure(0, weight=1)

        rp_hdr = tk.Frame(rp, bg=C['panel'])
        rp_hdr.grid(row=0, column=0, sticky='ew', padx=12, pady=(10, 2))
        rp_hdr.columnconfigure(0, weight=1)
        tk.Label(rp_hdr, text="분할 영역", bg=C['panel'], fg=C['text'],
                 font=('맑은 고딕', 11, 'bold')).pack(side=tk.LEFT)
        self.sp_count_lbl = tk.Label(rp_hdr, text="",
                                     bg=C['panel'], fg=C['dim'],
                                     font=('맑은 고딕', 8))
        self.sp_count_lbl.pack(side=tk.RIGHT)
        tk.Label(rp, text="클릭하여 선택/해제",
                 bg=C['panel'], fg=C['dim'], font=('맑은 고딕', 8)
                 ).grid(row=1, column=0, sticky='w', padx=12)

        rf = tk.Frame(rp, bg=C['panel'])
        rf.grid(row=2, column=0, sticky='nsew', padx=8, pady=4)
        rf.rowconfigure(0, weight=1)
        rf.columnconfigure(0, weight=1)

        self.sp_region_tree = ttk.Treeview(
            rf, columns=('sel', 'name', 'size'),
            show='headings', selectmode='none')
        self.sp_region_tree.heading('sel',  text='선택')
        self.sp_region_tree.heading('name', text='영역')
        self.sp_region_tree.heading('size', text='크기(px)')
        self.sp_region_tree.column('sel',  width=38,  minwidth=38,  stretch=False, anchor='center')
        self.sp_region_tree.column('name', width=70,  minwidth=60,  stretch=True,  anchor='center')
        self.sp_region_tree.column('size', width=100, minwidth=80,  stretch=False, anchor='center')
        self.sp_region_tree.tag_configure('sel_on',   foreground=C['green'])
        self.sp_region_tree.tag_configure('sel_last', foreground='#fab387')
        self.sp_region_tree.tag_configure('sel_off',  foreground=C['subtext'])

        rvsb = ttk.Scrollbar(rf, orient='vertical',
                             command=self.sp_region_tree.yview)
        self.sp_region_tree.configure(yscrollcommand=rvsb.set)
        self.sp_region_tree.grid(row=0, column=0, sticky='nsew')
        rvsb.grid(row=0, column=1, sticky='ns')

        bf2 = tk.Frame(rp, bg=C['panel'])
        bf2.grid(row=3, column=0, sticky='ew', padx=8, pady=4)
        bf2.columnconfigure(0, weight=1)
        bf2.columnconfigure(1, weight=1)
        sbtn(bf2, "전체 선택", self._sp_select_all
             ).grid(row=0, column=0, sticky='ew', padx=2, pady=2)
        sbtn(bf2, "선택 해제", self._sp_deselect_all
             ).grid(row=0, column=1, sticky='ew', padx=2, pady=2)

        sbtn(rp, "📋  선택 영역 복사 (Ctrl+C)", self._sp_copy_region,
             C['accent'], 'white').grid(row=4, column=0,
                                       sticky='ew', padx=8, pady=(0, 4))

        sbtn(rp, "💾  선택 영역 저장", self._sp_save_selected,
             '#40a02b', 'white').grid(row=5, column=0,
                                     sticky='ew', padx=8, pady=(0, 12))

        # ── 이벤트 ──
        self.sp_canvas.bind('<Configure>',       lambda _: self._sp_draw())
        self.sp_canvas.bind('<ButtonPress-1>',   self._sp_on_press)
        self.sp_canvas.bind('<B1-Motion>',       self._sp_on_drag)
        self.sp_canvas.bind('<ButtonRelease-1>', self._sp_on_release)
        self.sp_canvas.bind('<Motion>',          self._sp_on_move)
        self.sp_canvas.bind('<Leave>',           lambda _: self._sp_on_leave())
        self.sp_canvas.bind('<Control-c>',       lambda _: self._sp_copy_region())
        self.sp_canvas.bind('<Control-C>',       lambda _: self._sp_copy_region())
        self.sp_canvas.bind('<Control-v>',       lambda _: self._sp_paste_image())
        self.sp_canvas.bind('<Control-V>',       lambda _: self._sp_paste_image())
        self.sp_canvas.bind('<Delete>',          lambda _: self._sp_delete_line())
        self.sp_region_tree.bind('<ButtonPress-1>', self._sp_on_region_tree_click)

        self._sp_load_presets()

    # ── 분할: 이미지 로드 ──────────────────────
    def _sp_load_file(self):
        paths = filedialog.askopenfilenames(
            title="이미지 선택",
            filetypes=[("이미지 파일",
                        "*.jpg *.jpeg *.png *.bmp *.tiff *.tif *.webp"),
                       ("모든 파일", "*.*")])
        if paths:
            raw = np.fromfile(paths[0], dtype=np.uint8)
            img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if img is not None:
                self._sp_set_image(img, paths[0])

    def _sp_paste_image(self, event=None):
        try:
            from PIL import ImageGrab
            pil_img = ImageGrab.grabclipboard()
            if pil_img is None:
                messagebox.showinfo("알림", "클립보드에 이미지가 없습니다.")
                return
            arr = np.array(pil_img.convert('RGB'))
            self._sp_set_image(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), None)
        except Exception as e:
            messagebox.showerror("오류", f"붙여넣기 실패\n{e}")

    def _sp_set_image(self, img, path):
        self.sp_img         = img
        self.sp_orig_path   = path

        # 대기 중인 프리셋이 있으면 그 선을 사용,
        # 없고 "선 유지"가 켜져 있으면 기존 선을 그대로 유지,
        # 그 외엔 초기화
        pending = getattr(self, 'sp_pending_preset', None)
        if pending is not None:
            self.sp_h_lines = list(pending.get('h', []))
            self.sp_v_lines = list(pending.get('v', []))
            self.sp_pending_preset = None
        elif getattr(self, 'sp_keep_lines', None) and self.sp_keep_lines.get():
            pass  # 기존 self.sp_h_lines / self.sp_v_lines 유지
        else:
            self.sp_h_lines = []
            self.sp_v_lines = []

        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self.sp_drag        = None
        self.sp_hover       = None
        self.sp_selected_line = None
        self._sp_update_region_list()
        self._sp_draw()
        if hasattr(self, 'sp_info_lbl') and self.sp_img is not None:
            h, w = self.sp_img.shape[:2]
            name = Path(self.sp_orig_path).name if self.sp_orig_path else "클립보드"
            self.sp_info_lbl.configure(text=f"  {name}  |  {w} × {h} px")

    # ── 분할: 캔버스 그리기 ────────────────────
    def _sp_draw(self):
        c  = self.sp_canvas
        cw = c.winfo_width()
        ch = c.winfo_height()
        c.delete('all')
        if cw <= 1 or ch <= 1:
            return

        if self.sp_img is None:
            c.create_text(cw // 2, ch // 2,
                text="이미지를 불러오거나 Ctrl+V 로 붙여넣으세요",
                fill='#585b70', font=('맑은 고딕', 13), anchor='center')
            return

        ih, iw = self.sp_img.shape[:2]
        scale = min((cw - 20) / iw, (ch - 20) / ih)
        self.sp_scale = scale
        dw = int(iw * scale)
        dh = int(ih * scale)
        self.sp_ox = (cw - dw) // 2
        self.sp_oy = (ch - dh) // 2

        img_rgb = cv2.cvtColor(self.sp_img, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(img_rgb).resize((dw, dh), Image.LANCZOS)
        self._sp_tk_img = ImageTk.PhotoImage(pil_img)
        c.create_image(self.sp_ox, self.sp_oy, anchor='nw',
                       image=self._sp_tk_img)

        h_edges = [0.0] + sorted(self.sp_h_lines) + [1.0]
        v_edges = [0.0] + sorted(self.sp_v_lines) + [1.0]

        for ri in range(len(h_edges) - 1):
            for ci in range(len(v_edges) - 1):
                x0 = self.sp_ox + int(v_edges[ci]   * dw)
                y0 = self.sp_oy + int(h_edges[ri]   * dh)
                x1 = self.sp_ox + int(v_edges[ci+1] * dw)
                y1 = self.sp_oy + int(h_edges[ri+1] * dh)
                if (ri, ci) == self.sp_last_selected:
                    c.create_rectangle(x0, y0, x1, y1,
                                       outline='#fab387', fill='', width=3)
                    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
                    c.create_text(mx, my, text=f"{ri+1}-{ci+1}",
                                  fill='#fab387',
                                  font=('맑은 고딕', 10, 'bold'))
                elif (ri, ci) in self.sp_selected_regions:
                    c.create_rectangle(x0, y0, x1, y1,
                                       outline=C['green'], fill='', width=3)
                    mx, my = (x0 + x1) // 2, (y0 + y1) // 2
                    c.create_text(mx, my, text=f"{ri+1}-{ci+1}",
                                  fill=C['green'],
                                  font=('맑은 고딕', 10, 'bold'))
                else:
                    c.create_rectangle(x0, y0, x1, y1,
                                       fill='#000000', outline='',
                                       stipple='gray25')

        LINE_COL_H = C['yellow']
        LINE_COL_V = C['accent']
        SEL_COL    = '#f38ba8'

        for i, fy in enumerate(self.sp_h_lines):
            y = self.sp_oy + int(fy * dh)
            col = SEL_COL if self.sp_selected_line == ('h', i) \
                  else LINE_COL_H
            w_  = 3 if (self.sp_hover == ('h', i) or
                        self.sp_selected_line == ('h', i)) else 2
            c.create_line(self.sp_ox, y, self.sp_ox + dw, y,
                          fill=col, width=w_, dash=(6, 3))
            hx = self.sp_ox + dw // 2
            c.create_oval(hx-7, y-7, hx+7, y+7, fill=col, outline='')
            c.create_text(hx, y, text='↕', fill='#1e1e2e',
                          font=('맑은 고딕', 8, 'bold'))

        for i, fx in enumerate(self.sp_v_lines):
            x = self.sp_ox + int(fx * dw)
            col = SEL_COL if self.sp_selected_line == ('v', i) \
                  else LINE_COL_V
            w_  = 3 if (self.sp_hover == ('v', i) or
                        self.sp_selected_line == ('v', i)) else 2
            c.create_line(x, self.sp_oy, x, self.sp_oy + dh,
                          fill=col, width=w_, dash=(6, 3))
            hy = self.sp_oy + dh // 2
            c.create_oval(x-7, hy-7, x+7, hy+7, fill=col, outline='')
            c.create_text(x, hy, text='↔', fill='#1e1e2e',
                          font=('맑은 고딕', 8, 'bold'))

    # ── 분할: 마우스 히트 테스트 ───────────────
    def _sp_hit_test(self, x, y):
        if self.sp_img is None:
            return None
        ih, iw = self.sp_img.shape[:2]
        dw = int(iw * self.sp_scale)
        dh = int(ih * self.sp_scale)
        TOL = 8
        for i, fy in enumerate(self.sp_h_lines):
            cy = self.sp_oy + int(fy * dh)
            if abs(y - cy) <= TOL and self.sp_ox <= x <= self.sp_ox + dw:
                return ('h', i)
        for i, fx in enumerate(self.sp_v_lines):
            cx = self.sp_ox + int(fx * dw)
            if abs(x - cx) <= TOL and self.sp_oy <= y <= self.sp_oy + dh:
                return ('v', i)
        return None

    def _sp_region_at(self, x, y):
        if self.sp_img is None:
            return None
        ih, iw = self.sp_img.shape[:2]
        dw = int(iw * self.sp_scale)
        dh = int(ih * self.sp_scale)
        if not (self.sp_ox <= x <= self.sp_ox + dw and
                self.sp_oy <= y <= self.sp_oy + dh):
            return None
        fx = (x - self.sp_ox) / dw
        fy = (y - self.sp_oy) / dh
        h_edges = [0.0] + sorted(self.sp_h_lines) + [1.0]
        v_edges = [0.0] + sorted(self.sp_v_lines) + [1.0]
        row = next((i for i in range(len(h_edges)-1)
                    if h_edges[i] <= fy < h_edges[i+1]),
                   len(h_edges)-2)
        col = next((i for i in range(len(v_edges)-1)
                    if v_edges[i] <= fx < v_edges[i+1]),
                   len(v_edges)-2)
        return (row, col)

    # ── 분할: 마우스 이벤트 ────────────────────
    def _sp_on_press(self, event):
        self.sp_canvas.focus_set()
        hit = self._sp_hit_test(event.x, event.y)
        if hit:
            self.sp_drag = hit
            self.sp_selected_line = hit
            self._sp_draw()
        else:
            region = self._sp_region_at(event.x, event.y)
            if region is not None:
                if region in self.sp_selected_regions:
                    self.sp_selected_regions.discard(region)
                    if self.sp_last_selected == region:
                        self.sp_last_selected = None
                else:
                    self.sp_selected_regions.add(region)
                    self.sp_last_selected = region
                self._sp_update_region_list()
                self._sp_draw()
            self.sp_selected_line = None
            self.sp_drag = None

    def _sp_on_drag(self, event):
        if self.sp_drag is None or self.sp_img is None:
            return
        ih, iw = self.sp_img.shape[:2]
        dw = int(iw * self.sp_scale)
        dh = int(ih * self.sp_scale)
        dtype, idx = self.sp_drag
        if dtype == 'h':
            fy = max(0.005, min(0.995, (event.y - self.sp_oy) / dh))
            self.sp_h_lines[idx] = fy
        else:
            fx = max(0.005, min(0.995, (event.x - self.sp_ox) / dw))
            self.sp_v_lines[idx] = fx
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_on_release(self, event):
        if self.sp_drag:
            self.sp_h_lines.sort()
            self.sp_v_lines.sort()
            self.sp_drag = None
            self._sp_draw()

    def _sp_on_move(self, event):
        hit = self._sp_hit_test(event.x, event.y)
        if hit != self.sp_hover:
            self.sp_hover = hit
            if hit:
                cur = 'sb_v_double_arrow' if hit[0] == 'h' \
                      else 'sb_h_double_arrow'
            else:
                cur = 'crosshair'
            self.sp_canvas.configure(cursor=cur)
            self._sp_draw()

    def _sp_on_leave(self):
        if self.sp_hover:
            self.sp_hover = None
            self.sp_canvas.configure(cursor='crosshair')
            self._sp_draw()

    # ── 분할: 선 추가/삭제 ────────────────────
    def _sp_add_h_line(self):
        if self.sp_img is None:
            messagebox.showinfo("알림", "먼저 이미지를 불러오세요.")
            return
        edges = [0.0] + sorted(self.sp_h_lines) + [1.0]
        gaps  = [(edges[i+1] - edges[i], i) for i in range(len(edges)-1)]
        _, gi = max(gaps)
        self.sp_h_lines.append((edges[gi] + edges[gi+1]) / 2)
        self.sp_h_lines.sort()
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_add_v_line(self):
        if self.sp_img is None:
            messagebox.showinfo("알림", "먼저 이미지를 불러오세요.")
            return
        edges = [0.0] + sorted(self.sp_v_lines) + [1.0]
        gaps  = [(edges[i+1] - edges[i], i) for i in range(len(edges)-1)]
        _, gi = max(gaps)
        self.sp_v_lines.append((edges[gi] + edges[gi+1]) / 2)
        self.sp_v_lines.sort()
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_delete_line(self, event=None):
        if self.sp_selected_line is None:
            messagebox.showinfo("알림", "삭제할 선을 먼저 클릭하여 선택하세요.")
            return
        dtype, idx = self.sp_selected_line
        if dtype == 'h' and 0 <= idx < len(self.sp_h_lines):
            del self.sp_h_lines[idx]
        elif dtype == 'v' and 0 <= idx < len(self.sp_v_lines):
            del self.sp_v_lines[idx]
        self.sp_selected_line = None
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_clear_lines(self):
        self.sp_h_lines  = []
        self.sp_v_lines  = []
        self.sp_selected_regions = set()
        self.sp_last_selected    = None
        self.sp_selected_line    = None
        self._sp_update_region_list()
        self._sp_draw()

    # ── 분할: 영역 목록 ───────────────────────
    def _sp_get_regions(self):
        if self.sp_img is None:
            return []
        ih, iw = self.sp_img.shape[:2]
        h_edges = [0.0] + sorted(self.sp_h_lines) + [1.0]
        v_edges = [0.0] + sorted(self.sp_v_lines) + [1.0]
        result  = []
        for ri in range(len(h_edges)-1):
            for ci in range(len(v_edges)-1):
                y0 = int(h_edges[ri]   * ih)
                y1 = int(h_edges[ri+1] * ih)
                x0 = int(v_edges[ci]   * iw)
                x1 = int(v_edges[ci+1] * iw)
                result.append((ri, ci, x0, y0, x1, y1))
        return result

    def _sp_update_region_list(self):
        t = self.sp_region_tree
        t.delete(*t.get_children())
        regions = list(self._sp_get_regions())
        for ri, ci, x0, y0, x1, y1 in regions:
            if (ri, ci) == self.sp_last_selected:
                sel = '★'
                tag = 'sel_last'
            elif (ri, ci) in self.sp_selected_regions:
                sel = '✓'
                tag = 'sel_on'
            else:
                sel = '☐'
                tag = 'sel_off'
            name = f"{ri+1}행 {ci+1}열"
            size = f"{x1-x0}×{y1-y0}"
            t.insert('', 'end', iid=f"{ri}_{ci}",
                     values=(sel, name, size), tags=(tag,))
        # 영역 수 / 선택 수 레이블 업데이트
        if hasattr(self, 'sp_count_lbl'):
            n_total = len(regions)
            n_sel   = len(self.sp_selected_regions)
            if n_total == 0:
                self.sp_count_lbl.configure(text="")
            else:
                self.sp_count_lbl.configure(
                    text=f"{n_sel}/{n_total}개 선택",
                    fg=C['green'] if n_sel > 0 else C['dim'])

    def _sp_on_region_tree_click(self, event):
        iid = self.sp_region_tree.identify_row(event.y)
        if not iid:
            return
        parts = iid.split('_')
        if len(parts) == 2:
            ri, ci = int(parts[0]), int(parts[1])
            if (ri, ci) in self.sp_selected_regions:
                self.sp_selected_regions.discard((ri, ci))
                if self.sp_last_selected == (ri, ci):
                    self.sp_last_selected = None
            else:
                self.sp_selected_regions.add((ri, ci))
                self.sp_last_selected = (ri, ci)
            self._sp_update_region_list()
            self._sp_draw()
        return 'break'

    def _sp_select_all(self):
        self.sp_selected_regions = {
            (ri, ci) for ri, ci, *_ in self._sp_get_regions()}
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_deselect_all(self):
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    # ── 분할: 복사 ────────────────────────────
    def _sp_copy_region(self):
        if self.sp_img is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return
        region = self.sp_last_selected
        if region is None:
            messagebox.showwarning("경고", "복사할 영역을 선택하세요.\n(마지막으로 클릭한 영역이 복사됩니다)")
            return
        ri, ci = region
        regions = {(r, c): (x0, y0, x1, y1)
                   for r, c, x0, y0, x1, y1 in self._sp_get_regions()}
        if (ri, ci) not in regions:
            return
        x0, y0, x1, y1 = regions[(ri, ci)]
        crop = self.sp_img[y0:y1, x0:x1]
        try:
            self._set_clipboard_image(crop)
            self._show_toast(f"📋 {ri+1}행 {ci+1}열 복사됐습니다")
        except Exception as e:
            messagebox.showerror("오류", f"클립보드 복사 실패\n{e}")

    # ── 분할: 저장 ────────────────────────────
    def _sp_save_selected(self):
        if self.sp_img is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return
        if not self.sp_selected_regions:
            messagebox.showwarning("경고", "저장할 영역을 선택하세요.")
            return
        out_dir = filedialog.askdirectory(title="저장 폴더 선택")
        if not out_dir:
            return
        out_dir   = Path(out_dir)
        base_name = Path(self.sp_orig_path).stem \
                    if self.sp_orig_path else "split"
        saved = 0
        for ri, ci, x0, y0, x1, y1 in self._sp_get_regions():
            if (ri, ci) not in self.sp_selected_regions:
                continue
            crop = self.sp_img[y0:y1, x0:x1]
            out_path = out_dir / f"{base_name}_{ri+1}_{ci+1}.jpg"
            ok, buf  = cv2.imencode('.jpg', crop,
                                    [cv2.IMWRITE_JPEG_QUALITY, 95])
            if ok:
                buf.tofile(str(out_path))
                saved += 1
        self._show_toast(f"💾 {saved}개 영역 저장 완료  →  {Path(out_dir).name}/",
                         color=C['green'])

    # ── 분할: 즐겨찾기 ────────────────────────
    def _sp_preset_path(self):
        if getattr(sys, 'frozen', False):
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).parent
        return base / 'split_presets.json'

    def _sp_load_presets(self):
        p = self._sp_preset_path()
        try:
            if p.exists():
                with open(p, 'r', encoding='utf-8') as f:
                    self.sp_presets = json.load(f)
            else:
                self.sp_presets = {}
        except Exception:
            self.sp_presets = {}
        self._sp_refresh_preset_combo()

    def _sp_save_presets_file(self):
        try:
            with open(self._sp_preset_path(), 'w', encoding='utf-8') as f:
                json.dump(self.sp_presets, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("오류", f"즐겨찾기 저장 실패\n{e}")

    def _sp_refresh_preset_combo(self):
        names = list(self.sp_presets.keys())
        self.sp_preset_combo['values'] = names
        if names:
            self.sp_preset_combo.current(0)

    def _sp_save_preset(self):
        if not self.sp_h_lines and not self.sp_v_lines:
            messagebox.showinfo("알림", "저장할 선이 없습니다.\n먼저 선을 추가하세요.")
            return
        name = simpledialog.askstring(
            "즐겨찾기 저장", "즐겨찾기 이름을 입력하세요:",
            parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        self.sp_presets[name] = {
            'h': sorted(self.sp_h_lines),
            'v': sorted(self.sp_v_lines),
        }
        self._sp_save_presets_file()
        self._sp_refresh_preset_combo()
        self.sp_preset_var.set(name)

    def _sp_apply_preset(self):
        name = (self.sp_preset_var.get() or '').strip()
        if not name or name not in self.sp_presets:
            messagebox.showinfo("알림", "불러올 즐겨찾기를 선택하세요.")
            return
        p = self.sp_presets[name]

        # 아직 이미지가 없으면 다음에 이미지를 불러올 때 적용되도록 보관
        if self.sp_img is None:
            self.sp_pending_preset = {
                'h': list(p.get('h', [])),
                'v': list(p.get('v', [])),
            }
            messagebox.showinfo(
                "알림",
                f'"{name}" 선을 기억했습니다.\n'
                "이미지를 불러오면 자동으로 적용됩니다.")
            return

        self.sp_h_lines = list(p.get('h', []))
        self.sp_v_lines = list(p.get('v', []))
        self.sp_selected_regions = set()
        self.sp_last_selected = None
        self._sp_update_region_list()
        self._sp_draw()

    def _sp_delete_preset(self):
        name = self.sp_preset_var.get()
        if not name or name not in self.sp_presets:
            messagebox.showinfo("알림", "삭제할 즐겨찾기를 선택하세요.")
            return
        if messagebox.askyesno("확인", f'"{name}" 즐겨찾기를 삭제하시겠습니까?'):
            del self.sp_presets[name]
            self._sp_save_presets_file()
            self._sp_refresh_preset_combo()

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
        if len(fname) > 18:
            fname = fname[:15] + '…'
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
        self.canvas.config(cursor='arrow')
        fname = Path(self.orig_path).name if self.orig_path else ""
        self.file_lbl.configure(text=fname)

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
        self._show_toast("✅ 원본 파일을 보정 이미지로 대체했습니다", color=C['green'])

    # ── 토스트 알림 ────────────────────────────
    def _show_toast(self, message, duration=2000, color='#a6e3a1'):
        if hasattr(self, '_toast_win') and self._toast_win:
            try:
                self._toast_win.destroy()
            except Exception:
                pass
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes('-topmost', True)
        toast.configure(bg='#313244')
        tk.Label(toast, text=message, bg='#313244', fg=color,
                 font=('맑은 고딕', 10, 'bold'), padx=18, pady=10).pack()
        toast.update_idletasks()
        tw = toast.winfo_reqwidth()
        th = toast.winfo_reqheight()
        rx = self.root.winfo_x() + self.root.winfo_width()
        ry = self.root.winfo_y() + self.root.winfo_height()
        toast.geometry(f'+{rx - tw - 20}+{ry - th - 40}')
        self._toast_win = toast

        def _dismiss():
            try:
                toast.destroy()
            except Exception:
                pass
        toast.after(duration, _dismiss)

    # ── 클립보드 이미지 복사 (Win32 ctypes, 빠름) ──
    def _set_clipboard_image(self, bgr_img):
        """BGR numpy 이미지를 클립보드에 복사. Win32 API 직접 호출."""
        h, w = bgr_img.shape[:2]
        bgra = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2BGRA)
        bgra_flip = cv2.flip(bgra, 0)  # bottom-up DIB
        pixel_data = bgra_flip.tobytes()

        bi = struct.pack('<IiiHHIIiiII',
                         40, w, h, 1, 32, 0, len(pixel_data), 0, 0, 0, 0)
        dib = bi + pixel_data

        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32

        # 64비트 포인터/핸들 잘림 방지를 위해 restype/argtypes 명시
        k32.GlobalAlloc.restype  = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype   = ctypes.c_void_p
        k32.GlobalLock.argtypes  = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes  = [wintypes.HWND]
        u32.SetClipboardData.restype  = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

        GMEM_MOVEABLE = 0x0002
        CF_DIB = 8

        hMem = k32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
        if not hMem:
            raise RuntimeError("GlobalAlloc 실패")
        pMem = k32.GlobalLock(hMem)
        if not pMem:
            k32.GlobalFree(hMem)
            raise RuntimeError("GlobalLock 실패")
        ctypes.memmove(pMem, dib, len(dib))
        k32.GlobalUnlock(hMem)
        if not u32.OpenClipboard(None):
            k32.GlobalFree(hMem)
            raise RuntimeError("OpenClipboard 실패")
        try:
            u32.EmptyClipboard()
            if not u32.SetClipboardData(CF_DIB, hMem):
                k32.GlobalFree(hMem)
                raise RuntimeError("SetClipboardData 실패")
        finally:
            u32.CloseClipboard()

    # ── 현재 이미지 클립보드 복사
    # ──────────────────────────────────────────
    def _copy_image_to_clipboard(self):
        img = self.warped_img if self.warped_img is not None else self.orig_img
        if img is None:
            messagebox.showwarning("경고", "먼저 이미지를 불러오세요.")
            return
        try:
            self._set_clipboard_image(img)
            self._show_toast("📋 클립보드에 복사됐습니다")
        except Exception as e:
            messagebox.showerror("오류", f"클립보드 복사 실패\n{e}")

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

            size_info = f"  ({_fmt(orig_size)} → {_fmt(new_size)})" if orig_size else ""
            self._show_toast(f"🗜 {out_path.name} 저장 완료{size_info}",
                             color=C['yellow'])
        except Exception as e:
            messagebox.showerror("저장 오류", f"압축 저장 실패\n{e}")

    # ── 자동 원근 보정
    # ──────────────────────────────────────────
    def _auto_correct(self):
        if self.orig_img is None:
            return
        h, w   = self.orig_img.shape[:2]
        thr = self.thr_var.get() if hasattr(self, 'thr_var') else 0
        corners = self._detect_receipt(self.orig_img,
                                       threshold=(None if thr == 0 else thr))
        if corners is None:
            corners = [[0.0, 0.0], [float(w), 0.0],
                       [float(w), float(h)], [0.0, float(h)]]
        self.corners = corners
        self._draw_corners()
        self._apply_correction()

    def _on_threshold_change(self, val):
        v = int(float(val))
        self.thr_val_lbl.config(text="자동" if v == 0 else str(v))
        if self.orig_img is None:
            return
        if self._thr_after_id is not None:
            self.root.after_cancel(self._thr_after_id)
        self._thr_after_id = self.root.after(120, self._auto_correct)

    def _detect_receipt(self, img, threshold=None):
        # threshold=None  → 자동: 모든 역치(240~20) 스윕 후 최대 윤곽
        # threshold=정수  → 수동: 해당 역치만 사용 (슬라이더 조절값)
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        min_area = w * h * 0.05
        max_area = w * h * 0.96

        def cnt_to_corners(cnt):
            peri = cv2.arcLength(cnt, True)
            for eps in (0.02, 0.04, 0.06, 0.08, 0.12):
                ap = cv2.approxPolyDP(cnt, eps * peri, True)
                if len(ap) == 4:
                    return self._order_pts(
                        ap.reshape(4, 2).astype(float).tolist())
            rect = cv2.minAreaRect(cnt)
            return self._order_pts(cv2.boxPoints(rect).astype(float).tolist())

        ksize = min(101, (min(h, w) // 8) | 1)
        blr   = cv2.GaussianBlur(gray, (ksize, ksize), 0)
        close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))

        best_cnt  = None
        best_area = 0

        tv_values = range(240, 20, -10) if threshold is None else [int(threshold)]
        for tv in tv_values:
            for flag in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
                _, mask = cv2.threshold(blr, tv, 255, flag)
                mask    = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_k)
                cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
                if not cnts:
                    continue
                cnt  = max(cnts, key=cv2.contourArea)
                area = cv2.contourArea(cnt)
                if min_area <= area <= max_area and area > best_area:
                    best_cnt  = cnt
                    best_area = area

        if threshold is None:
            # 자동 모드: 40% 미만 윤곽은 내부 텍스트로 보고 전체 프레임 처리
            if best_cnt is not None and best_area >= w * h * 0.40:
                return cnt_to_corners(best_cnt)
            return None
        # 수동 모드: 해당 역치로 찾은 윤곽을 그대로 반환
        if best_cnt is not None:
            return cnt_to_corners(best_cnt)
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
            self.canvas.config(cursor='arrow')
        else:
            self.mode = 'manual'
            self.mode_lbl.configure(text="✏️ 수동 조정  —  꼭짓점 드래그 / 빈 곳 클릭으로 추가")
            self.canvas.config(cursor='crosshair')

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
        """선택 항목 병렬 전처리 → 순차 OCR."""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        selected = self.receipt_tree.selection()
        if not selected:
            messagebox.showwarning("선택 없음",
                                   "목록에서 OCR 할 항목을 선택하세요.\n(Ctrl+클릭으로 다중 선택)")
            return

        indices = [int(iid) for iid in selected]
        total   = len(indices)
        for idx in indices:
            self._mark_list_processing(idx)
        self.batch_ocr_btn.configure(state=tk.DISABLED,
                                     text=f"전처리 중… (0/{total})")

        def worker():
            # 1단계: 이미지 로드+보정 병렬 처리 (I/O 바운드)
            preloaded = [None] * total
            n_pre = [0]
            with ThreadPoolExecutor(max_workers=min(4, total)) as ex:
                fut_map = {ex.submit(self._load_and_warp_safe, indices[i]): i
                           for i in range(total)}
                for fut in as_completed(fut_map):
                    i = fut_map[fut]
                    try:
                        preloaded[i] = fut.result()
                    except Exception:
                        preloaded[i] = None
                    n_pre[0] += 1
                    k = n_pre[0]
                    self.root.after(0, lambda v=k: self.batch_ocr_btn.configure(
                        text=f"전처리 중… ({v}/{total})"))

            # 2단계: OCR 순차 처리 (PaddleOCR 스레드 비안전)
            for k, idx in enumerate(indices):
                img = preloaded[k]
                try:
                    text = self._do_ocr(img) if img is not None else ''
                except Exception:
                    text = ''
                done = k + 1
                self.root.after(0, lambda i=idx, t=text, d=done:
                                self._ocr_batch_item_done(i, t, d, total))

            self.root.after(0, lambda: self.batch_ocr_btn.configure(
                state=tk.NORMAL, text="선택 항목 OCR 실행"))

        threading.Thread(target=worker, daemon=True).start()

    def _load_and_warp_safe(self, idx: int):
        try:
            return self._load_and_warp(idx)
        except Exception:
            return None

    def _mark_list_processing(self, idx: int):
        try:
            vals = list(self.receipt_tree.item(str(idx), 'values'))
            vals[1] = '처리중…'
            self.receipt_tree.item(str(idx), values=vals)
        except tk.TclError:
            pass

    def _ocr_batch_item_done(self, idx: int, text: str, done: int, total: int):
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

        if idx == self.queue_idx:
            self._loading = True
            if date:   self.date_var.set(date)
            if amount: self.amount_var.set(amount)
            self._loading = False

        self._update_summary()
        self.batch_ocr_btn.configure(text=f"OCR 실행 중… ({done}/{total})")

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
            self._show_toast(f"✅ {saved}개 파일 저장 완료", color=C['green'])

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
            n_ok = self._compress_total - len(self._compress_errors)
            if self._compress_errors:
                msg = f"{n_ok}개 압축 완료\n\n실패:\n" + "\n".join(self._compress_errors)
                messagebox.showwarning("압축 완료 (일부 실패)", msg)
            else:
                self._show_toast(f"🗜 {n_ok}개 압축 완료  (resize_ 접두어로 저장)",
                                 color=C['yellow'])
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
        if HAS_PADDLE and not self._paddle_ready:
            self.ocr_status_var.set("PaddleOCR 초기화 중… (첫 실행 시 모델 다운로드)")
        else:
            engine = "PaddleOCR" if (HAS_PADDLE and self._paddle_ready) else \
                     ("Tesseract" if (HAS_TESSERACT and self._tess_cmd) else "Windows OCR")
            self.ocr_status_var.set(f"OCR 실행 중… ({engine})")
        self.ocr_progress.grid()
        self.ocr_progress.start(10)
        self.root.update()

        def worker():
            # PaddleOCR가 아직 초기화 중이면 완료까지 대기
            if HAS_PADDLE and not self._paddle_ready:
                self._init_paddle()
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

        engine = "PaddleOCR" if (HAS_PADDLE and self._paddle_ready) else \
                 ("Tesseract" if (HAS_TESSERACT and self._tess_cmd) else "Windows OCR")
        self.ocr_status_var.set(f"OCR 완료  ({len(text)}자 인식, {engine})")
        self.ocr_text.delete('1.0', tk.END)
        self.ocr_text.insert(tk.END, text)
        self.ocr_engine_lbl.configure(text=f"OCR 엔진: {engine}  ✓", fg=C['green'])

        self._loading = True
        if date:   self.date_var.set(date)
        if amount: self.amount_var.set(amount)
        self._loading = False

    # ── PaddleOCR 초기화 (백그라운드 래퍼) ────────
    def _paddle_init_bg(self):
        self._init_paddle()
        if self._paddle_ready:
            self.root.after(0, lambda: (
                self.ocr_status_var.set("PaddleOCR 준비 완료"),
                self.ocr_engine_lbl.configure(
                    text="OCR 엔진: PaddleOCR  ✓", fg=C['green'])
            ))
        else:
            err = self._paddle_error or "알 수 없는 오류"
            fallback = "Tesseract" if (HAS_TESSERACT and hasattr(self, '_tess_cmd') and self._tess_cmd) \
                       else "Windows OCR"
            self.root.after(0, lambda: (
                self.ocr_status_var.set(
                    f"PaddleOCR 초기화 실패 → {fallback} 사용\n오류: {err[:80]}"),
                self.ocr_engine_lbl.configure(
                    text=f"OCR 엔진: {fallback}", fg=C['yellow'])
            ))

    # ── PaddleOCR 초기화 ─────────────────────────
    def _init_paddle(self):
        with self._paddle_init_lock:
            if self._paddle_ready:
                return
            # 서버급 인식 모델 우선 시도, 파라미터 미지원 시 모바일 모델로 폴백
            for kwargs in [
                {'use_textline_orientation': True, 'lang': 'korean',
                 'rec_model_name': 'korean_PP-OCRv5_server_rec'},
                {'use_textline_orientation': True, 'lang': 'korean'},
            ]:
                try:
                    self._paddle = PaddleOCR(**kwargs)
                    self._paddle_ready = True
                    self._paddle_error = None
                    return
                except (ValueError, TypeError):
                    continue
                except Exception as e:
                    self._paddle       = None
                    self._paddle_ready = False
                    self._paddle_error = str(e)
                    return

    # ── Tesseract 경로 탐색 ───────────────────

    # ── PaddleOCR 전처리 ──────────────────────
    def _preprocess_for_paddle(self, img: np.ndarray) -> np.ndarray:
        # [1단계] 사방 50px 흰색 패딩 — 테두리 글자 누락 방지
        img = cv2.copyMakeBorder(img, 50, 50, 50, 50,
                                 cv2.BORDER_CONSTANT, value=(255, 255, 255))

        # [3단계] 해상도 정규화 (높이 1800~2000px, 종횡비 유지)
        h, w = img.shape[:2]
        if h < 1800:
            scale = 1800 / h
            img = cv2.resize(img, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)
        elif h > 2000:
            scale = 2000 / h
            img = cv2.resize(img, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_CUBIC)

        # [4단계] 그림자 제거 및 조명 균일화 — BGR 3채널로 반환
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        gray  = clahe.apply(gray)
        img   = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

        # [5단계] 미세 노이즈 제거 (3x3 Gaussian blur)
        img = cv2.GaussianBlur(img, (3, 3), 0)

        return img

    # ── PaddleOCR 실행 ────────────────────────
    def _paddle_ocr(self, img: np.ndarray) -> str:
        try:
            processed = self._preprocess_for_paddle(img)
            result = self._paddle.ocr(processed, cls=True)
            lines = []
            if result and result[0]:
                for line in result[0]:
                    if line and len(line) >= 2:
                        text_info = line[1]
                        if text_info and text_info[0]:
                            lines.append(text_info[0])
            return '\n'.join(lines)
        except Exception:
            return ''

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
        # 1순위: PaddleOCR (원본 컬러 이미지)
        if HAS_PADDLE and self._paddle_ready and self._paddle is not None:
            text = self._paddle_ocr(img)
            if text.strip():
                return text

        # 2순위: Tesseract (전처리 후)
        if HAS_TESSERACT and self._tess_cmd:
            text = self._tesseract_ocr(img)
            if text.strip():
                return text

        # 3순위: Windows 내장 OCR (폴백)
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
        # 4자리 연도 — 정상 구분자
        for pat in [
            r'(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})',
            r'\b(\d{4})(\d{2})(\d{2})\b',
        ]:
            for m in re.finditer(pat, text):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}{mo:02d}{d:02d}"

        # OCR 구분자 깨짐 대응 퍼지 패턴
        # 예: "2026••()5서4(띡)" → 2026 / 5 / 4
        # 숫자 사이 1~8개 비숫자 문자를 구분자로 허용
        for m in re.finditer(r'\b(20\d{2})\D{1,8}?(\d{1,2})\D{1,8}?(\d{1,2})\b', text):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}{mo:02d}{d:02d}"

        # 2자리 연도 패턴 (예: 25-02-28)
        for pat in [
            r'\b(\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\b',
        ]:
            for m in re.finditer(pat, text):
                yy, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                y = 2000 + yy
                if 2000 <= y <= 2099 and 1 <= mo <= 12 and 1 <= d <= 31:
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
        suffix = f"  ({remaining}장 남음)" if remaining > 0 else ""
        self._show_toast(f"✅ {out_path.name} 저장 완료{suffix}", color=C['green'])

        if remaining > 0:
            self._next_file()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    app = ReceiptApp()
    app.run()
