"""alpha/factor_search_gp.py - Genetic Programming 鍥犲瓙鎼滅储 (T15.3 v2, 2026-06-03)

L2 鍥犲瓙鑷姩鍖?v2. 澶嶇敤 alpha/factor_search.py 鐨?AST 鑺傜偣鐢熸垚鍣? 澧炲姞绉嶇兢杩涘寲
(閫夋嫨/浜ゅ弶/鍙樺紓/绮捐嫳), 鏈熸湜姣?random search 鏀舵暃鏇村揩.

涓?v1 random search 鐨勫叧绯?
- random_search: 1000 涓€欓€? 0.4s/expr, 绾殢鏈? 鍛戒腑鐜?< 5%
- GP search:    50 涓缇?脳 20 浠? 閫傚簲搴﹂┍鍔? 鏈熸湜 top-1 score 鎻愬崌 20-50%

璁捐 (鍗曚粨搴?GP, 杞婚噺):
- 閫傚簲搴? ExpressionScore.score (0-100, 缁煎悎 IC + 绋冲畾鎬?
- 閫夋嫨:   tournament selection (k=3)
- 浜ゅ弶:   瀛愭爲浜ゆ崲 (1-point)
- 鍙樺紓:   鏇挎崲闅忔満瀛愭爲 (prob=0.10) / 鎶栧姩甯告暟 (prob=0.05)
- 绮捐嫳:   top 10% 鐩存帴杩涘叆涓嬩竴浠?- 澶辫触/UNKNOWN -> fitness=-1 (涓嶄細姹℃煋绉嶇兢)
"""
from __future__ import annotations

import json
import logging
import random
import time as _time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from alpha.factor_dsl import FactorNode, parse_dsl
from alpha.factor_score_evaluator import FactorScoreEvaluator, ExpressionScore
from alpha.factor_search import (
    random_node, random_leaf, random_unary_op, random_ts_op,
)

logger = logging.getLogger(__name__)


# --- AST utilities ----------------------------------------------------

def clone_ast(node: FactorNode) -> FactorNode:
    """Recursive deep copy. Avoids DSL roundtrip (const nodes cannot reparse)."""
    new_args = []
    for a in node.args:
        if isinstance(a, FactorNode):
            new_args.append(clone_ast(a))
        else:
            new_args.append(a)
    return FactorNode(op=node.op, args=new_args, params=dict(node.params))


def count_nodes(node: FactorNode) -> int:
    """缁熻 AST 鑺傜偣鎬绘暟 (鍚嚜韬?."""
    n = 1
    for a in node.args:
        if isinstance(a, FactorNode):
            n += count_nodes(a)
    return n


def collect_nodes(node: FactorNode) -> list[FactorNode]:
    """杩斿洖鏍戜腑鎵€鏈夎妭鐐圭殑鍒楄〃 (DFS)."""
    out = [node]
    for a in node.args:
        if isinstance(a, FactorNode):
            out.extend(collect_nodes(a))
    return out


def pick_random_subtree(root: FactorNode) -> FactorNode:
    """浠?AST 涓殢鏈洪€変竴涓妭鐐?(鎸?1/size 鍔犳潈, 鍊惧悜閫夊皬)."""
    nodes = collect_nodes(root)
    sizes = np.array([count_nodes(n) for n in nodes], dtype=float)
    weights = 1.0 / sizes
    weights /= weights.sum()
    return nodes[np.random.choice(len(nodes), p=weights)]


def replace_subtree(root: FactorNode, target: FactorNode, replacement: FactorNode) -> FactorNode:
    """鎶?root 涓瓑浜?target 鐨勭涓€涓妭鐐规浛鎹㈡垚 replacement. 杩斿洖鏂?root."""
    if root is target:
        return replacement
    new_args = []
    for a in root.args:
        if isinstance(a, FactorNode):
            new_args.append(replace_subtree(a, target, replacement))
        else:
            new_args.append(a)
    return FactorNode(op=root.op, args=new_args, params=dict(root.params))


# --- GP operators -----------------------------------------------------

def tournament_select(population, fitness, k=3):
    """Tournament 閫夋嫨: 浠?k 涓殢鏈轰釜浣撲腑閫?fitness 鏈€楂樼殑."""
    idxs = random.sample(range(len(population)), min(k, len(population)))
    best_i = max(idxs, key=lambda i: fitness[i])
    return clone_ast(population[best_i])


def crossover(p1, p2, max_depth=6):
    """鍗曠偣浜ゅ弶: 鍦?p1 / p2 鍚勯€変竴涓瓙鏍? 浜ゆ崲. 鎺у埗 max_depth 閬垮厤鐖嗙偢."""
    child1 = clone_ast(p1)
    child2 = clone_ast(p2)
    sub1 = pick_random_subtree(child1)
    sub2 = pick_random_subtree(child2)
    new1 = replace_subtree(child1, sub1, clone_ast(sub2))
    new2 = replace_subtree(child2, sub2, clone_ast(sub1))
    if count_nodes(new1) > 80 or _depth(new1) > max_depth:
        new1 = clone_ast(p1)
    if count_nodes(new2) > 80 or _depth(new2) > max_depth:
        new2 = clone_ast(p2)
    return new1, new2


def mutate(node, prob_subtree=0.10, prob_const=0.05, max_depth=4):
    """鍙樺紓: 鏇挎崲闅忔満瀛愭爲 鎴?鎶栧姩甯告暟."""
    new = clone_ast(node)
    if random.random() < prob_subtree:
        sub = pick_random_subtree(new)
        replacement = random_node(max_depth=max_depth)
        new = replace_subtree(new, sub, replacement)
    if random.random() < prob_const:
        consts = [n for n in collect_nodes(new) if n.op == "const"]
        if consts:
            c = random.choice(consts)
            if c.args and isinstance(c.args[0], (int, float)):
                old = float(c.args[0])
                delta = old * random.uniform(-0.5, 0.5)
                new_val = max(0.1, old + delta)
                c.args[0] = round(new_val, 3) if isinstance(c.args[0], float) else int(round(new_val))
    return new


def _depth(node, current=0):
    """AST 鏈€澶ф繁搴?"""
    if not node.args:
        return current
    d = current
    for a in node.args:
        if isinstance(a, FactorNode):
            d = max(d, _depth(a, current + 1))
    return d


# --- Population / Evolution ------------------------------------------

@dataclass
class GPResult:
    """GP 鎼滅储鏈€缁堢粨鏋?"""
    best: list = field(default_factory=list)
    all_top_per_gen: list = field(default_factory=list)
    n_generations: int = 0
    pop_size: int = 0
    total_evaluated: int = 0
    elapsed_sec: float = 0.0
    best_score_history: list = field(default_factory=list)
    avg_score_history: list = field(default_factory=list)

    def to_dict(self):
        return {
            "n_generations": self.n_generations,
            "pop_size": self.pop_size,
            "total_evaluated": self.total_evaluated,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "best_score_history": [round(x, 2) for x in self.best_score_history],
            "avg_score_history": [round(x, 2) for x in self.avg_score_history],
            "best": [s.to_dict() for s in self.best],
        }


class FactorSearchGP:
    """
    GP 鍥犲瓙鎼滅储寮曟搸.

    鐢ㄦ硶:
        evaluator = FactorScoreEvaluator(df, forward_period=1)
        gp = FactorSearchGP(evaluator)
        result = gp.run(pop_size=50, n_generations=20, top_k=20, verbose=True)
    """

    def __init__(self, evaluator: FactorScoreEvaluator):
        self.evaluator = evaluator

    def _fitness(self, score: ExpressionScore) -> float:
        if score.status == "UNKNOWN" or score.error:
            return -1.0
        return float(score.score)

    def _make_initial_population(self, n, max_depth=4):
        pop = []
        for _ in range(n):
            try:
                if random.random() < 0.1:
                    pop.append(random_leaf())
                else:
                    pop.append(random_node(max_depth=max_depth))
            except Exception:
                pop.append(random_leaf())
        return pop

    def _step(self, population, fitness, elite_frac, tournament_k, mut_prob):
        n = len(population)
        n_elite = max(1, int(n * elite_frac))
        sorted_idx = sorted(range(n), key=lambda i: fitness[i], reverse=True)
        new_pop = [clone_ast(population[i]) for i in sorted_idx[:n_elite]]
        while len(new_pop) < n:
            p1 = tournament_select(population, fitness, k=tournament_k)
            p2 = tournament_select(population, fitness, k=tournament_k)
            c1, c2 = crossover(p1, p2)
            c1 = mutate(c1, prob_subtree=mut_prob)
            c2 = mutate(c2, prob_subtree=mut_prob)
            new_pop.append(c1)
            if len(new_pop) < n:
                new_pop.append(c2)
        return new_pop

    def _best_score(self, scores):
        valid = [s for s in scores if s.status != "UNKNOWN"]
        if not valid:
            return ExpressionScore(expression="<no_valid>")
        return max(valid, key=lambda s: s.score)

    def run(
        self,
        pop_size: int = 50,
        n_generations: int = 20,
        elite_frac: float = 0.10,
        tournament_k: int = 3,
        mut_prob: float = 0.10,
        top_k: int = 20,
        init_max_depth: int = 4,
        seed: int = 42,
        verbose: bool = True,
    ) -> GPResult:
        random.seed(seed)
        np.random.seed(seed)
        t0 = _time.time()

        population = self._make_initial_population(pop_size, max_depth=init_max_depth)
        if verbose:
            logger.info(f"[GP] init pop_size={pop_size} max_depth={init_max_depth}")

        exprs = [n.to_string() for n in population]
        scores = self.evaluator.score_batch(exprs, verbose=False)
        fitness = [self._fitness(s) for s in scores]
        best_history = [max(fitness)]
        valid_fits = [f for f in fitness if f > 0]
        avg_history = [float(np.mean(valid_fits)) if valid_fits else 0.0]
        all_top = [self._best_score(scores)]

        if verbose:
            logger.info(f"[GP] gen=0  best={best_history[0]:.1f}  avg={avg_history[0]:.1f}  "
                        f"n_valid={sum(1 for f in fitness if f > 0)}/{len(fitness)}")

        for gen in range(1, n_generations + 1):
            population = self._step(population, fitness, elite_frac, tournament_k, mut_prob)
            exprs = [n.to_string() for n in population]
            scores = self.evaluator.score_batch(exprs, verbose=False)
            fitness = [self._fitness(s) for s in scores]
            best_history.append(max(fitness))
            valid_fits = [f for f in fitness if f > 0]
            avg_history.append(float(np.mean(valid_fits)) if valid_fits else 0.0)
            top = self._best_score(scores)
            all_top.append(top)
            if verbose and (gen % 2 == 0 or gen == n_generations):
                logger.info(f"[GP] gen={gen}  best={best_history[-1]:.1f}  avg={avg_history[-1]:.1f}  "
                            f"top1='{top.expression[:50]}' score={top.score:.1f}")

        # 鍘婚噸 + 鎺掑簭
        seen = {}
        for s in all_top + scores:
            if s.expression not in seen or s.score > seen[s.expression].score:
                seen[s.expression] = s
        valid = [s for s in seen.values() if s.status != "UNKNOWN"]
        valid.sort(key=lambda s: s.score, reverse=True)

        elapsed = _time.time() - t0
        result = GPResult(
            best=valid[:top_k],
            all_top_per_gen=all_top,
            n_generations=n_generations,
            pop_size=pop_size,
            total_evaluated=(n_generations + 1) * pop_size,
            elapsed_sec=elapsed,
            best_score_history=best_history,
            avg_score_history=avg_history,
        )
        if verbose:
            logger.info(f"[GP] DONE: gens={n_generations} pop={pop_size} "
                        f"top1_score={valid[0].score if valid else 0:.1f} "
                        f"top1_expr='{valid[0].expression if valid else 'none'}'  "
                        f"elapsed={elapsed:.1f}s")
        return result


# --- Persistence -----------------------------------------------------

def save_gp_result(result: GPResult, output_dir) -> Path:
    """钀界洏 GP 缁撴灉鍒?JSON. 鏂囦欢鍚嶅甫鏃堕棿鎴?"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = _time.strftime("%Y%m%d_%H%M%S")
    out_path = output_dir / f"gp_run_{ts}.json"
    payload = {
        "ts": ts,
        "result": result.to_dict(),
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[GP] saved -> {out_path}")
    return out_path