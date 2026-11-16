# -*- coding: utf-8 -*-
"""
Companion Lite · 本地神经向量（bge-small-zh，ONNX，纯 CPU）

- 模型 24MB，首次用自动下载（hf-mirror）
- 向量算一次缓存到 data/models/vectors.npz
- 任何一步失败都静默降级（退回词级 TF-IDF）
"""
import json
import os
import threading

import numpy as np

MODEL_URL = "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main"
MAX_LEN = 128


class NeuralVec:
    def __init__(self, data_dir, con=None):
        self.dir = os.path.join(data_dir, "models")
        os.makedirs(self.dir, exist_ok=True)
        self.model_path = os.path.join(self.dir, "model.onnx")
        self.tok_path = os.path.join(self.dir, "tokenizer.json")
        self.vec_path = os.path.join(self.dir, "vectors.npz")
        self.con = con
        self.session = None
        self.tok = None
        self.ids = None          # np.array of message ids
        self.mat = None          # np.array (N, d) float32 normalized
        self.lock = threading.Lock()
        self.ready = False
        self.error = ""

    # ---------- 模型 ----------
    def ensure_model(self, timeout=600):
        if os.path.exists(self.model_path) and os.path.exists(self.tok_path):
            return True
        import urllib.request

        os.makedirs(self.dir, exist_ok=True)
        for name, url in ((os.path.basename(self.model_path), f"{MODEL_URL}/onnx/model_quantized.onnx"),
                          ("tokenizer.json", f"{MODEL_URL}/tokenizer.json")):
            dst = os.path.join(self.dir, name)
            if os.path.exists(dst):
                continue
            tmp = dst + ".part"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, dst)
        return os.path.exists(self.model_path) and os.path.exists(self.tok_path)

    def load(self):
        """加载 onnx + tokenizer（失败抛异常，由外层降级）"""
        if self.session is not None:
            return
        if not self.ensure_model():
            raise RuntimeError("模型下载失败")
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.log_severity_level = 3
        self.session = ort.InferenceSession(self.model_path, so,
                                            providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(self.tok_path)
        self.tok.enable_truncation(max_length=MAX_LEN)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")

    # ---------- 编码 ----------
    def encode(self, texts):
        self.load()
        encs = self.tok.encode_batch([t[:400] for t in texts])
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        ttype = np.zeros_like(ids)
        inputs = {"input_ids": ids, "attention_mask": mask, "token_type_ids": ttype}
        names = {i.name for i in self.session.get_inputs()}
        inputs = {k: v for k, v in inputs.items() if k in names}
        out = self.session.run(None, inputs)[0]      # (B, T, d)
        # CLS 池化 + L2 归一化
        vec = out[:, 0, :]
        norm = np.linalg.norm(vec, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return (vec / norm).astype(np.float32)

    # ---------- 索引 ----------
    def build(self, force=False):
        with self.lock:
            if self.ready and not force:
                return True
            if self.con is None:
                return False
            rows = self.con.execute("SELECT id, text FROM messages").fetchall()
            if not rows:
                return False
            if os.path.exists(self.vec_path) and not force:
                try:
                    z = np.load(self.vec_path, allow_pickle=False)
                    if len(z["ids"]) == len(rows):
                        self.ids, self.mat = z["ids"], z["mat"]
                        self.load()          # 必须确认模型真能用
                        self.ready = True
                        return True
                except Exception:
                    pass
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            mats = []
            for i in range(0, len(rows), 32):
                batch = [r[1] for r in rows[i:i + 32]]
                mats.append(self.encode(batch))
            mat = np.vstack(mats)
            np.savez_compressed(self.vec_path, ids=ids, mat=mat)
            self.ids, self.mat = ids, mat
            self.ready = True
            return True

    def search(self, query, k=20):
        if not self.ready:
            self.build()
        if self.mat is None or len(self.ids) == 0:
            return []
        q = self.encode([query])[0]
        sims = self.mat @ q
        idx = np.argsort(-sims)[:k]
        return [(int(self.ids[i]), float(sims[i])) for i in idx if sims[i] > 0.25]
