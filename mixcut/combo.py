"""组合算法：编号法去重 + 跨批次历史 + 素材使用次数均衡

把“每个分镜位各选一条素材”的一个组合看成一个混合进制的整数编号：
    编号 = i0*(n1*n2*...)+ i1*(n2*...) + ... + i_last
其中 n_k 是第 k 个文件夹的素材数，i_k 是选中的素材序号。
于是“抽 N 个不重复组合” = “从 0..总数-1 中抽 N 个不重复整数”，一次完成，不会撞车重抽。
"""
import bisect
import math
import random
import sys


def total_combos(counts):
    if not counts or any(c <= 0 for c in counts):
        return 0
    return math.prod(counts)


def code_to_indices(code, counts):
    """编号 -> 每个文件夹的素材序号（混合进制拆分）"""
    idx = [0] * len(counts)
    for k in range(len(counts) - 1, -1, -1):
        code, idx[k] = divmod(code, counts[k])
    return idx


def indices_to_code(indices, counts):
    code = 0
    for i, n in zip(indices, counts):
        code = code * n + i
    return code


def sample_unique(n, k, rng):
    """从 range(n) 中无放回抽 k 个整数。
    常规情况用 random.sample（对 range 是 O(k)，不会展开成列表）；
    组合数超过 sys.maxsize（len(range) 会溢出）时改用 Floyd 算法，同样 O(k)、严格不重复。"""
    if k <= 0:
        return []
    if k > n:
        raise ValueError("k > n")
    if n <= sys.maxsize:
        return rng.sample(range(n), k)
    chosen = set()
    for j in range(n - k, n):
        t = rng.randrange(j + 1)
        chosen.add(j if t in chosen else t)
    out = list(chosen)
    rng.shuffle(out)
    return out


def ranks_to_codes(ranks, used_sorted):
    """把“在剩余可用编号中的名次”映射为真实编号（跳过已用编号）。
    ranks 任意顺序；used_sorted 为升序且无重复的已用编号。O(k log k + |used|)。"""
    order = sorted(range(len(ranks)), key=lambda i: ranks[i])
    out = [0] * len(ranks)
    j = 0
    m = len(used_sorted)
    for i in order:
        r = ranks[i]
        while j < m and used_sorted[j] <= r + j:
            j += 1
        out[i] = r + j
    return out


class ComboPlanner:
    """生成一批组合。

    counts:      各文件夹素材数
    used_codes:  历史上已经生成过的组合编号（跨批次去重）
    usage_init:  各文件夹每个素材历史使用次数（用于均衡），可为 None
    """

    def __init__(self, counts, used_codes=(), usage_init=None, seed=None):
        self.counts = list(counts)
        self.total = total_combos(self.counts)
        self.used_sorted = sorted(set(c for c in used_codes if 0 <= c < self.total))
        self.rng = random.Random(seed)
        if usage_init is None:
            usage_init = [[0] * n for n in self.counts]
        self.usage = [list(u) for u in usage_init]

    @property
    def remaining(self):
        return self.total - len(self.used_sorted)

    # ---- 均衡选择 ----
    def _score(self, code):
        return sum(self.usage[k][i] for k, i in enumerate(code_to_indices(code, self.counts)))

    def _take(self, code):
        for k, i in enumerate(code_to_indices(code, self.counts)):
            self.usage[k][i] += 1

    def _balanced_pick(self, candidates, k):
        """从候选编号中挑 k 个，使各素材使用次数尽量平均（配额法，线性复杂度）。
        每个文件夹的素材都有一个使用上限 = ceil((历史总使用 + 本批数量) / 素材数)，
        按随机顺序扫描候选，会让某个素材超过当前上限的组合先跳过；上限分级逐步放开，
        这样少用的素材会先被选上。"""
        cands = list(candidates)
        self.rng.shuffle(cands)
        if k >= len(cands):
            for c in cands:
                self._take(c)
            return cands
        quotas = [-(-(sum(self.usage[f]) + k) // n) for f, n in enumerate(self.counts)]
        floors = [min(u) for u in self.usage]
        picked = []
        chosen = [False] * len(cands)
        decoded = [code_to_indices(c, self.counts) for c in cands]
        # 分级“灌水”：上限从 当前最少次数+1 分 8 级升到配额，之后每轮再放宽 1
        steps = 8
        level = 1
        while len(picked) < k:
            if level <= steps:
                caps = [fl + max(1, -(-(q - fl) * level // steps)) for fl, q in zip(floors, quotas)]
            else:
                caps = [q + (level - steps) for q in quotas]
            for j, c in enumerate(cands):
                if chosen[j]:
                    continue
                idx = decoded[j]
                if all(self.usage[f][i] < caps[f] for f, i in enumerate(idx)):
                    chosen[j] = True
                    picked.append(c)
                    for f, i in enumerate(idx):
                        self.usage[f][i] += 1
                    if len(picked) >= k:
                        break
            level += 1
        return picked

    def _pool_size(self, need, available):
        # 候选池为需要数量的 4 倍：足够做均衡，又保持速度
        return min(available, max(need * 4, need + 64))

    def plan(self, n, allow_repeat=False):
        """返回编号列表。不允许重复时最多返回 remaining 个。
        允许重复时：先用完所有未用过的组合，再分轮从全部组合中补足（每一轮内部仍不重复）。"""
        if self.total == 0 or n <= 0:
            return []
        result = []
        avail = self.remaining
        first = min(n, avail)
        if first > 0:
            pool = self._pool_size(first, avail)
            ranks = sample_unique(avail, pool, self.rng)
            cands = ranks_to_codes(ranks, self.used_sorted)
            result.extend(self._balanced_pick(cands, first))
        if allow_repeat:
            extra = n - len(result)
            while extra > 0:
                m = min(extra, self.total)
                pool = self._pool_size(m, self.total)
                cands = sample_unique(self.total, pool, self.rng)
                result.extend(self._balanced_pick(cands, m))
                extra -= m
        return result
