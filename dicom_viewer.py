import os
import glob
import numpy as np
import pydicom
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from PIL import Image, ImageTk

# -----------------------------
# DICOM series loader
# -----------------------------
def load_dicom_series(folder: str):
    files = sorted(glob.glob(os.path.join(folder, "**", "*.dcm"), recursive=True))
    if not files:
        raise FileNotFoundError("指定フォルダ配下に .dcm が見つかりませんでした。")

    dsets = []
    for f in files:
        try:
            ds = pydicom.dcmread(f, force=True)
            if hasattr(ds, "PixelData"):
                dsets.append(ds)
        except Exception:
            pass

    if not dsets:
        raise ValueError("PixelData を含む DICOM が読み込めませんでした。")

    # 並び順：InstanceNumber 優先、だめなら ImagePositionPatient(Z)
    def sort_key(ds):
        if hasattr(ds, "InstanceNumber"):
            try:
                return int(ds.InstanceNumber)
            except Exception:
                return 0
        if hasattr(ds, "ImagePositionPatient"):
            try:
                return float(ds.ImagePositionPatient[2])
            except Exception:
                return 0
        return 0

    dsets.sort(key=sort_key)

    # 画像サイズ（Rows, Columns）
    rows = int(dsets[0].Rows)
    cols = int(dsets[0].Columns)

    # SliceThickness（なければ 0）
    slice_thickness = float(getattr(dsets[0], "SliceThickness", 0.0))

    # 3D Volume を構築 [Z, Y, X]
    vol = []
    for ds in dsets:
        arr = ds.pixel_array.astype(np.int32)

        # RescaleSlope/Intercept（CTでよくある）対応
        slope = float(getattr(ds, "RescaleSlope", 1.0))
        intercept = float(getattr(ds, "RescaleIntercept", 0.0))
        arr = (arr * slope + intercept).astype(np.int32)

        vol.append(arr)

    vol = np.stack(vol, axis=0)  # [Z, Y, X]
    num_slices = vol.shape[0]

    # 表示用：初期WL/WWはDICOMのWindowCenter/WindowWidthがあれば使う
    wc = getattr(dsets[0], "WindowCenter", None)
    ww = getattr(dsets[0], "WindowWidth", None)

    def _as_float(x):
        # MultiValue 対策
        if isinstance(x, (list, tuple)) or (hasattr(x, "__len__") and not isinstance(x, (str, bytes))):
            try:
                return float(x[0])
            except Exception:
                return None
        try:
            return float(x)
        except Exception:
            return None

    init_wl = _as_float(wc)
    init_ww = _as_float(ww)

    # なければ適当にレンジから推定
    vmin, vmax = int(vol.min()), int(vol.max())
    if init_wl is None:
        init_wl = (vmin + vmax) / 2
    if init_ww is None or init_ww <= 0:
        init_ww = max(1, (vmax - vmin) / 2)

    meta = {
        "rows": rows,
        "cols": cols,
        "slice_thickness": slice_thickness,
        "num_slices": num_slices,
        "vmin": vmin,
        "vmax": vmax,
        "init_wl": float(init_wl),
        "init_ww": float(init_ww),
        "file_count": len(files),
    }
    return vol, meta


# -----------------------------
# Window/Level convert to 8bit
# -----------------------------
def apply_window(img_2d: np.ndarray, wl: float, ww: float) -> np.ndarray:
    ww = max(1.0, float(ww))
    wl = float(wl)
    low = wl - ww / 2.0
    high = wl + ww / 2.0

    x = img_2d.astype(np.float32)
    x = np.clip(x, low, high)
    x = (x - low) / (high - low) * 255.0
    return x.astype(np.uint8)


# -----------------------------
# GUI
# -----------------------------
class DicomViewerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DICOM Viewer (Axial/Sagittal/Coronal + WL/WW)")
        self.geometry("1100x700")

        self.vol = None
        self.meta = None

        # ★★★ 追加：表示サイズを固定（ここが拡大バグ対策の本体）★★★
        self.display_width = 640
        self.display_height = 640

        self.plane = tk.StringVar(value="Axial")  # Axial / Sagittal / Coronal

        self.slice_var = tk.IntVar(value=0)
        self.wl_var = tk.DoubleVar(value=0.0)
        self.ww_var = tk.DoubleVar(value=1.0)

        self._build_ui()

    def _build_ui(self):
        # 左：画像
        left = ttk.Frame(self)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.img_label = ttk.Label(left)
        self.img_label.pack(fill=tk.BOTH, expand=True)

        # 右：操作パネル
        right = ttk.Frame(self)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)

        btn = ttk.Button(right, text="フォルダを選んで読み込み", command=self.on_open_folder)
        btn.pack(fill=tk.X, pady=(0, 10))

        # ヘッダ情報表示
        self.info = tk.Text(right, height=8, width=40)
        self.info.pack(fill=tk.X, pady=(0, 10))
        self.info.configure(state="disabled")

        # Plane selector
        ttk.Label(right, text="スライス面").pack(anchor="w")
        for p in ["Axial", "Sagittal", "Coronal"]:
            ttk.Radiobutton(right, text=p, value=p, variable=self.plane, command=self.on_plane_change).pack(anchor="w")

        ttk.Separator(right).pack(fill=tk.X, pady=10)

        # Slice slider
        ttk.Label(right, text="スライス番号").pack(anchor="w")
        self.slice_scale = ttk.Scale(
            right, from_=0, to=0, orient="horizontal",
            command=self._on_slice_scale
        )
        self.slice_scale.pack(fill=tk.X)
        self.slice_value_label = ttk.Label(right, text="0")
        self.slice_value_label.pack(anchor="e", pady=(0, 10))

        # WL / WW sliders
        ttk.Label(right, text="Window Level (WL)").pack(anchor="w")
        self.wl_scale = ttk.Scale(
            right, from_=0, to=1, orient="horizontal",
            command=self._on_wl_scale
        )
        self.wl_scale.pack(fill=tk.X)
        self.wl_value_label = ttk.Label(right, text="0")
        self.wl_value_label.pack(anchor="e", pady=(0, 10))

        ttk.Label(right, text="Window Width (WW)").pack(anchor="w")
        self.ww_scale = ttk.Scale(
            right, from_=1, to=2, orient="horizontal",
            command=self._on_ww_scale
        )
        self.ww_scale.pack(fill=tk.X)
        self.ww_value_label = ttk.Label(right, text="1")
        self.ww_value_label.pack(anchor="e", pady=(0, 10))

        ttk.Separator(right).pack(fill=tk.X, pady=10)

        ttk.Button(right, text="中央スライスへ", command=self.go_center).pack(fill=tk.X)

    def on_open_folder(self):
        folder = filedialog.askdirectory()
        if not folder:
            return
        try:
            vol, meta = load_dicom_series(folder)
        except Exception as e:
            messagebox.showerror("読み込み失敗", str(e))
            return

        self.vol = vol
        self.meta = meta

        # スライダー範囲セット
        self._reset_controls()

        # ヘッダ情報表示
        self._show_meta()

        self.render()

    def _reset_controls(self):
        vmin, vmax = self.meta["vmin"], self.meta["vmax"]

        # WL/WW 初期
        self.wl_var.set(self.meta["init_wl"])
        self.ww_var.set(self.meta["init_ww"])

        # WL/WW slider range（ざっくり全レンジ）
        self.wl_scale.configure(from_=vmin, to=vmax)
        self.ww_scale.configure(from_=1, to=max(2, vmax - vmin))

        self.wl_scale.set(self.wl_var.get())
        self.ww_scale.set(self.ww_var.get())

        self.wl_value_label.config(text=f"{self.wl_var.get():.1f}")
        self.ww_value_label.config(text=f"{self.ww_var.get():.1f}")

        # plane ごとのスライス範囲
        self.on_plane_change(go_center=True)

    def _show_meta(self):
        m = self.meta
        text = (
            f"--- DICOMヘッダ/系列情報 ---\n"
            f"画像サイズ: {m['rows']} x {m['cols']}\n"
            f"スライス厚: {m['slice_thickness']} mm\n"
            f"スライス数: {m['num_slices']}\n"
            f"画素値範囲: {m['vmin']} .. {m['vmax']}\n"
            f"(探索した.dcm数: {m['file_count']})\n"
            f"表示サイズ: {self.display_width} x {self.display_height}\n"
        )
        self.info.configure(state="normal")
        self.info.delete("1.0", tk.END)
        self.info.insert(tk.END, text)
        self.info.configure(state="disabled")

    def on_plane_change(self, go_center=False):
        if self.vol is None:
            return
        z, y, x = self.vol.shape
        p = self.plane.get()

        if p == "Axial":
            max_idx = z - 1
        elif p == "Coronal":
            max_idx = y - 1
        else:  # Sagittal
            max_idx = x - 1

        self.slice_scale.configure(from_=0, to=max_idx)
        if go_center:
            center = max_idx // 2
            self.slice_scale.set(center)
            self.slice_var.set(center)
            self.slice_value_label.config(text=str(center))

        self.render()

    def go_center(self):
        if self.vol is None:
            return
        p = self.plane.get()
        z, y, x = self.vol.shape
        max_idx = (z - 1) if p == "Axial" else (y - 1) if p == "Coronal" else (x - 1)
        center = max_idx // 2
        self.slice_scale.set(center)
        self.slice_var.set(center)
        self.slice_value_label.config(text=str(center))
        self.render()

    def _on_slice_scale(self, val):
        idx = int(float(val))
        self.slice_var.set(idx)
        self.slice_value_label.config(text=str(idx))
        self.render()

    def _on_wl_scale(self, val):
        self.wl_var.set(float(val))
        self.wl_value_label.config(text=f"{self.wl_var.get():.1f}")
        self.render()

    def _on_ww_scale(self, val):
        self.ww_var.set(float(val))
        self.ww_value_label.config(text=f"{self.ww_var.get():.1f}")
        self.render()

    def render(self):
        if self.vol is None:
            return

        idx = int(self.slice_var.get())
        wl = float(self.wl_var.get())
        ww = float(self.ww_var.get())

        # plane slice
        p = self.plane.get()
        if p == "Axial":
            img2d = self.vol[idx, :, :]
        elif p == "Coronal":
            img2d = self.vol[:, idx, :]   # [Z, X]
        else:  # Sagittal
            img2d = self.vol[:, :, idx]   # [Z, Y]

        # 表示向きをそれっぽく整える（見やすさ優先）
        img2d = np.flipud(img2d)

        img8 = apply_window(img2d, wl, ww)
        pil = Image.fromarray(img8, mode="L")

        # ★★★ 修正：ウィジェットサイズに追随せず、固定サイズにする（拡大バグ防止）★★★
        pil = pil.resize((self.display_width, self.display_height), Image.NEAREST)

        self.tk_img = ImageTk.PhotoImage(pil)
        self.img_label.configure(image=self.tk_img)


if __name__ == "__main__":
    app = DicomViewerApp()
    app.mainloop()
