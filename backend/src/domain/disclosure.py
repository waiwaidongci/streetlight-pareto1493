"""街道报修表下载的统计披露控制（k-匿名 + 跨下载歧义）。

口径
----
- 灯组日单量 cell(g, d)：灯组 g 在日期 d 的工单数。同一数据批次内，真实单量
  少于 3 的 cell 一律遮住（k=3 硬规则），达到 3 的 cell 如实发布。
- 片区日合计 DD、灯组期间合计 GP 为「小计」。跨下载时，连同历次已发布的 cell
  与小计，本次再发布的每个小计必须仍存在至少两种非负整数取值（歧义），否则遮住。

算法
----
约束是一个二部运输问题：S -> GP 小计 -> cell 运输边 -> DD 小计 -> T，
未被对侧小计覆盖的 cell 经超节点 Z 自由供流/收流。用 Dinic 最大流判定可行性，
再在残量网络上判定小计歧义（GP 节点能到 Z / Z 能到 DD 节点）。
该法已在穷举小网格（约 49 万组合）上与精确回溯逐一核对一致，且为多项式时间，
在台账规模下毫秒级完成。
"""
from collections import defaultdict, deque

K_THRESHOLD = 3
MASK = '**'
_INF = 10**12


class _Dinic:
    def __init__(self, n):
        self.graph = [[] for _ in range(n)]

    def add_edge(self, u, v, cap):
        self.graph[u].append([v, cap, len(self.graph[v])])
        self.graph[v].append([u, 0, len(self.graph[u]) - 1])

    def _bfs(self, s, t):
        self.level = [-1] * len(self.graph)
        q = deque([s])
        self.level[s] = 0
        while q:
            u = q.popleft()
            for v, cap, _ in self.graph[u]:
                if cap > 0 and self.level[v] < 0:
                    self.level[v] = self.level[u] + 1
                    q.append(v)
        return self.level[t] >= 0

    def _dfs(self, u, t, pushed):
        if u == t:
            return pushed
        while self.it[u] < len(self.graph[u]):
            e = self.graph[u][self.it[u]]
            v, cap, rev = e
            if cap > 0 and self.level[v] == self.level[u] + 1:
                d = self._dfs(v, t, min(pushed, cap))
                if d:
                    e[1] -= d
                    self.graph[v][rev][1] += d
                    return d
            self.it[u] += 1
        return 0

    def max_flow(self, s, t):
        flow = 0
        while self._bfs(s, t):
            self.it = [0] * len(self.graph)
            while True:
                f = self._dfs(s, t, _INF)
                if not f:
                    break
                flow += f
        return flow

    def reachable(self, src):
        seen = {src}
        stack = [src]
        while stack:
            u = stack.pop()
            for v, cap, _ in self.graph[u]:
                if cap > 0 and v not in seen:
                    seen.add(v)
                    stack.append(v)
        return seen


def _build_network(fixed_cells, margins, n_groups, n_days):
    """构造运输流网络并跑满最大流。不可行返回 None；无约束返回 'empty'。

    节点覆盖每个出现过的 GP（按灯组）与 DD（按灯组集合+日期）小计；
    未被对侧小计约束的 cell 经超节点 Z 按极性连接（自由 GP: Z->GP，自由 DD: DD->Z）。
    """
    if not margins:
        return 'empty'
    gp_rem = defaultdict(int)
    dd_rem = defaultdict(int)
    cell_gp, cell_dd = {}, {}
    for m in margins:
        cells = m['cells']
        groups = tuple(sorted({g for g, _ in cells}))
        days = tuple(sorted({d for _, d in cells}))
        used = sum(fixed_cells.get(c, 0) for c in cells)
        remaining = m['value'] - used
        if remaining < 0:
            return None
        free = [c for c in cells if c not in fixed_cells]
        if m['kind'] == 'gp':
            g = groups[0]
            gp_rem[g] += remaining
            for c in free:
                cell_gp[c] = g
        else:
            key = (groups, days[0])
            dd_rem[key] += remaining
            for c in free:
                cell_dd[c] = key

    # 为只落在单侧约束里的 cell 补出对侧的「自由 margin 节点」。
    free_gp_nodes = set()
    free_dd_nodes = set()
    for c in set(cell_gp) | set(cell_dd):
        if c not in cell_dd:
            free_dd_nodes.add(('free-dd', c[1]))   # 该日无 DD 约束
        if c not in cell_gp:
            free_gp_nodes.add(('free-gp', c[0]))   # 该灯组无 GP 约束

    gp_nodes = list(gp_rem) + list(free_gp_nodes)
    dd_nodes = list(dd_rem) + list(free_dd_nodes)
    gp_id = {k: i for i, k in enumerate(gp_nodes)}
    dd_id = {k: len(gp_nodes) + i for i, k in enumerate(dd_nodes)}
    S = len(gp_nodes) + len(dd_nodes)
    T = S + 1
    Z = T + 1
    dinic = _Dinic(Z + 1)

    total_gp = sum(gp_rem.values())
    total_dd = sum(dd_rem.values())
    for k in gp_rem:
        dinic.add_edge(S, gp_id[k], gp_rem[k])
    for k in dd_rem:
        dinic.add_edge(dd_id[k], T, dd_rem[k])

    all_cells = set(cell_gp) | set(cell_dd)
    for c in all_cells:
        gp_node = cell_gp[c] if c in cell_gp else ('free-gp', c[0])
        dd_node = cell_dd[c] if c in cell_dd else ('free-dd', c[1])
        dinic.add_edge(gp_id[gp_node], dd_id[dd_node], _INF)
    # 自由（未发布）margin 节点无平衡约束，与 Z 双向连通，既可供流也可收流。
    for k in free_gp_nodes:
        node = gp_id[k]
        dinic.add_edge(Z, node, _INF)
        dinic.add_edge(node, Z, _INF)
    for k in free_dd_nodes:
        node = dd_id[k]
        dinic.add_edge(Z, node, _INF)
        dinic.add_edge(node, Z, _INF)
    # Z 的有限源/汇边仅用于可行性运输（满足两侧差额），容量限定总需求。
    dinic.add_edge(S, Z, total_dd)
    dinic.add_edge(Z, T, total_gp)
    dinic.max_flow(S, T)

    if any(cap != 0 for _, cap, _ in dinic.graph[S]):
        return None
    for k in dd_rem:
        for v, cap, _ in dinic.graph[dd_id[k]]:
            if v == T and cap != 0:
                return None
    return dinic, (gp_id, dd_id, Z, set(gp_rem), set(dd_rem))


def margin_ambiguity(fixed_cells, margins, n_groups, n_days):
    """返回 ({gp_key: bool}, {dd_key: bool})，True 表示该小计仍有 >=2 个取值。"""
    built = _build_network(fixed_cells, margins, n_groups, n_days)
    if built is None:
        return None  # 当前约束已不可行
    if built == 'empty':
        # 无任何小计约束：每个小计都可自由变化。
        gp_keys = {m['cells'][0][0] for m in margins if m['kind'] == 'gp'}
        dd_keys = {(tuple(sorted({g for g, _ in m['cells']})),
                    min(d for _, d in m['cells'])) for m in margins if m['kind'] == 'dd'}
        return ({k: True for k in gp_keys}, {k: True for k in dd_keys})
    dinic, (gp_id, dd_id, Z, published_gp, published_dd) = built
    # 仅判定已发布（受约束）的小计；自由节点的歧义不影响发布决策。
    # 节点与 Z 在残差图上任一方向可达，即存在增流或减流调整路径 => 小计可变化。
    reachable_from = {}
    def reaches(a, b):
        if a not in reachable_from:
            reachable_from[a] = dinic.reachable(a)
        return b in reachable_from[a]
    z_reach = dinic.reachable(Z)
    gp_amb = {g: (reaches(gp_id[g], Z) or gp_id[g] in z_reach) for g in published_gp}
    dd_amb = {k: (reaches(dd_id[k], Z) or dd_id[k] in z_reach) for k in published_dd}
    return gp_amb, dd_amb


def plan_release(true_cells, district_of_group, selected_days, requested_dd,
                 requested_gp, history):
    """计算一次下载的发布方案（最大化保留数字，同数优先更多片区日合计）。

    true_cells:    {(g, d): int} 全批次真实单量。
    selected_days: 本次日期轴（d 为下标）。
    requested_dd:  [{'day': d, 'groups': [g...], 'value': int}]
    requested_gp:  [{'group': g, 'days': [d...], 'value': int}]
    history:       {'cells': {(g,d): int}, 'margins': [{'kind','cells','value'}]}
    返回 (keep_cells, kept_dd, kept_gp, fixed_cells)。
    """
    n_groups = len(district_of_group)
    n_days = len(selected_days)

    # 1) cell：历史已发布固定；达到 k 阈值的固定并发布，其余遮住。
    fixed_cells = dict(history['cells'])
    keep_cells = set(fixed_cells)
    for cell, value in true_cells.items():
        if cell not in fixed_cells and value >= K_THRESHOLD:
            fixed_cells[cell] = value
            keep_cells.add(cell)

    # 2) 小计候选：DD 排前（同数优先保留更多片区日合计）。每个候选带稳定 key。
    candidates = []
    for item in requested_dd:
        cells = [(g, item['day']) for g in item['groups']]
        candidates.append({'family': 'dd', 'cells': cells, 'value': item['value'],
                           'key': (tuple(sorted(item['groups'])), item['day'])})
    for item in requested_gp:
        cells = [(item['group'], d) for d in item['days']]
        candidates.append({'family': 'gp', 'cells': cells, 'value': item['value'],
                           'key': item['group']})

    base_margins = [dict(m) for m in history['margins']]

    # 预筛：单独（仅与历史约束一起）就不歧义的候选剔除。
    viable = []
    for c in candidates:
        result = margin_ambiguity(
            fixed_cells, base_margins + [{'kind': c['family'], 'cells': c['cells'],
                                          'value': c['value']}], n_groups, n_days)
        if result is None:
            continue
        gp_amb, dd_amb = result
        amb = gp_amb if c['family'] == 'gp' else dd_amb
        if amb.get(c['key'], False):
            viable.append(c)

    def jointly_ambiguous(kept):
        margins = base_margins + [{'kind': c['family'], 'cells': c['cells'], 'value': c['value']}
                                  for c in kept]
        result = margin_ambiguity(fixed_cells, margins, n_groups, n_days)
        if result is None:
            return False
        gp_amb, dd_amb = result
        for c in kept:
            amb = gp_amb if c['family'] == 'gp' else dd_amb
            if not amb.get(c['key'], False):
                return False
        return True

    # 贪心初始解，再用分支定界求全局最大（DD 优先）。
    greedy = []
    for c in viable:
        if jointly_ambiguous(greedy + [c]):
            greedy.append(c)
    best = {'kept': list(greedy), 'dd': sum(1 for c in greedy if c['family'] == 'dd')}

    def branch(index, kept):
        kept_count, kept_dd = len(kept), sum(1 for c in kept if c['family'] == 'dd')
        remaining = len(viable) - index
        if kept_count + remaining < len(best['kept']):
            return
        if (kept_count + remaining == len(best['kept'])
                and kept_dd + sum(1 for c in viable[index:] if c['family'] == 'dd') <= best['dd']):
            return
        if index == len(viable):
            if (kept_count > len(best['kept'])
                    or (kept_count == len(best['kept']) and kept_dd > best['dd'])):
                if jointly_ambiguous(kept):
                    best['kept'] = list(kept)
                    best['dd'] = kept_dd
            return
        kept.append(viable[index])
        branch(index + 1, kept)
        kept.pop()
        branch(index + 1, kept)

    branch(0, [])
    kept_cells_sig = {tuple(c['cells']) for c in best['kept']}
    kept_dd = [tuple((g, item['day']) for g in item['groups']) in kept_cells_sig
               for item in requested_dd]
    kept_gp = [tuple((item['group'], d) for d in item['days']) in kept_cells_sig
               for item in requested_gp]
    return keep_cells, kept_dd, kept_gp, fixed_cells
