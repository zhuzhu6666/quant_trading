"""alpha/factor_search_gp.py - Genetic Programming factor discovery (T15.3 v2, 2026-06-03).









# L2 factor auto-mining v2. Reuses AST node generators and adds population evolution.




# (selection/crossover/mutation/elite), aiming for faster convergence than random search.









# Comparison with v1 random search:




# - random_search: 1000 candidates, 0.4s/expression, purely random, hit rate < 5%




# - GP search:     50 population x 20 generations, fitness-driven, expected top-1 lift 20-50%









# Design (simple steady-state GP, lightweight):




# - Fitness:   ExpressionScore.score (0-100, combined IC + stability)




# - Selection: tournament selection (k=3)




# - Crossover: subtree swap (1-point)




# - Mutation:  replace random subtree (prob=0.10) / perturb constants (prob=0.05)




# - Elite:     top 10% carried to next gen; failure/UNKNOWN -> fitness=-1




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




    """Count AST nodes (including self)."""




    n = 1




    for a in node.args:




        if isinstance(a, FactorNode):




            n += count_nodes(a)




    return n














def collect_nodes(node: FactorNode) -> list[FactorNode]:




    """Return DFS list of all nodes in the tree."""




    out = [node]




    for a in node.args:




        if isinstance(a, FactorNode):




            out.extend(collect_nodes(a))




    return out














def pick_random_subtree(root: FactorNode) -> FactorNode:




    """Pick a random node from the AST (weighted by 1/size, bias toward small)."""




    nodes = collect_nodes(root)




    sizes = np.array([count_nodes(n) for n in nodes], dtype=float)




    weights = 1.0 / sizes




    weights /= weights.sum()




    return nodes[np.random.choice(len(nodes), p=weights)]














def replace_subtree(root: FactorNode, target: FactorNode, replacement: FactorNode) -> FactorNode:




    """Replace the first node in root that equals target with replacement. Return new root."""




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




    """Tournament selection: pick the highest-fitness from k random individuals."""




    idxs = random.sample(range(len(population)), min(k, len(population)))




    best_i = max(idxs, key=lambda i: fitness[i])




    return clone_ast(population[best_i])














def crossover(p1, p2, max_depth=6):




    """Single-point crossover: pick a subtree from p1 and p2, swap. Control max_depth to avoid bloat."""




    child1 = clone_ast(p1)




    child2 = clone_ast(p2)




    sub1 = pick_random_subtree(child1)




    sub2 = pick_random_subtree(child2)




    new1 = replace_subtree(child1, sub1, clone_ast(sub2))




    new2 = replace_subtree(child2, sub2, clone_ast(sub1))




    if count_nodes(new1) > 80 or _depth(new1) > max_depth:




        new1 = mutate(p1)  # BUG-19 fix: apply some mutation instead of pure clone




    if count_nodes(new2) > 80 or _depth(new2) > max_depth:




        new2 = mutate(p2)  # BUG-19 fix




    return new1, new2














def mutate(node, prob_subtree=0.10, prob_const=0.05, max_depth=4):




    """Mutation: replace random subtree or perturb constants."""




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




    """Maximum AST depth."""




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




    """Final GP search result."""




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




    GP factor discovery engine.









    Usage:




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




        max_runtime_sec: float = 600.0,  # FEAT-3: total wall-clock budget




    ) -> GPResult:




        if init_max_depth < 1:
            raise ValueError(f"init_max_depth must be >= 1, got {init_max_depth}")
        if max_runtime_sec <= 0:
            raise ValueError(f"max_runtime_sec must be > 0, got {max_runtime_sec}")
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
            if _time.time() - t0 > max_runtime_sec:
                logger.warning(f"[GP] max_runtime_sec={max_runtime_sec}s reached at gen {gen}, stopping early")
                break




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









        # Dedupe + sort




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




    """Persist GP result to JSON. Filename includes timestamp.""",




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