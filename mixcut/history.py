"""跨批次历史记录。

按“分镜文件夹列表（含顺序）”区分项目，每个项目一个 .jsonl 文件，每行是一个已生成组合
（按文件名记录，而不是按序号——这样文件夹里增删素材后，旧记录仍然能正确对应）。
"""
import json
import os
import threading

from .utils import data_root, norm_path, short_hash


def project_key(folders):
    return short_hash("\n".join(norm_path(f) for f in folders), 20)


class History:
    def __init__(self, folders, root=None):
        self.folders = list(folders)
        self.dir = os.path.join(root or data_root(), "history")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, project_key(self.folders) + ".jsonl")
        self._lock = threading.Lock()

    def load_names(self):
        """返回历史组合列表，每项是各文件夹的文件名列表"""
        combos = []
        if not os.path.exists(self.path):
            return combos
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 断电等导致的半行，忽略
                names = rec.get("names")
                if isinstance(names, list) and len(names) == len(self.folders):
                    combos.append(names)
        return combos

    def load_codes(self, materials):
        """把历史组合换算为当前素材列表下的编号，并统计每个素材的历史使用次数。
        materials: 每个文件夹的素材路径列表（与生成时同样的顺序）"""
        from .combo import indices_to_code

        counts = [len(m) for m in materials]
        name_maps = [{os.path.basename(p): i for i, p in enumerate(m)} for m in materials]
        usage = [[0] * n for n in counts]
        codes = set()
        for names in self.load_names():
            idx = []
            for k, nm in enumerate(names):
                i = name_maps[k].get(nm)
                if i is None:
                    break
                idx.append(i)
            else:
                code = indices_to_code(idx, counts)
                if code not in codes:
                    codes.add(code)
                    for k, i in enumerate(idx):
                        usage[k][i] += 1
        return codes, usage

    def append(self, names, output_file):
        rec = {"names": list(names), "output": output_file}
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()

    def clear(self):
        with self._lock:
            if os.path.exists(self.path):
                os.remove(self.path)

    def count(self):
        return len(self.load_names())
