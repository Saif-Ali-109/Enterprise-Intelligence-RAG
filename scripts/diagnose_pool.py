"""Diagnose the candidate pool per gold-set question, without reranking.

Throwaway diagnostic, run against the live stack with real credentials. It
deliberately stops before the rerank stage, because the organisation's rerank
quota is exhausted and reranking is not what this measures.

For each graded question it reports:
  * how many candidates retrieval returned before rerank (the pool),
  * whether the gold set's expected heading path is present in that pool at all,
  * the first rank at which it appears.

The three outcomes are different problems and need different fixes:
  target absent from the pool      -> corpus or embedding problem
  target present but below rank 5  -> ranking problem
  pool smaller than the budget     -> starvation problem
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.session import dispose_engine, get_session_factory  # noqa: E402
from app.evaluation.dataset import load_dataset  # noqa: E402
from app.evaluation.metrics import unit_covers_target  # noqa: E402
from app.generation.provider import get_language_model  # noqa: E402
from app.retrieval.query_analyzer import analyze_question  # noqa: E402
from app.retrieval.retriever import build_metadata_filter, retrieve  # noqa: E402
from app.retrieval.vector_store import get_vector_store  # noqa: E402


async def main() -> None:
    settings = get_settings()
    dataset, _ = load_dataset()
    store = get_vector_store()
    provider = get_language_model()
    factory = get_session_factory()

    print(f"pool budget = {settings.retrieval_candidate_pool}, "
          f"evidence min = {settings.evidence_min_units}\n")
    print(f"{'question':34} {'pool':>4} {'target?':>8} {'rank':>5}  filter")
    print("-" * 92)

    absent: list[str] = []
    thin: list[str] = []

    async with factory() as session:
        for q in dataset.questions:
            expected = q.expected
            if expected.must_refuse or not expected.heading_path:
                continue

            try:
                analysis = await analyze_question(q.question, provider=provider)
            except Exception as exc:  # noqa: BLE001
                print(f"{q.id:34} analysis failed: {type(exc).__name__}")
                continue

            plan = build_metadata_filter(analysis)
            candidates = await retrieve(q.question, session=session, store=store, analysis=analysis)
            widened = False
            if plan.filter is not None and len(candidates) < settings.evidence_min_units:
                widened = True
                plan = build_metadata_filter(None)
                candidates = await retrieve(
                    q.question, session=session, store=store, analysis=None
                )

            rank = next(
                (
                    i + 1
                    for i, c in enumerate(candidates)
                    if unit_covers_target(
                        product=c.product,
                        category=c.category,
                        heading_path=list(c.heading_path),
                        expected_product=expected.product,
                        expected_category=expected.category,
                        expected_heading_path=list(expected.heading_path),
                    )
                ),
                None,
            )
            status = f"rank {rank}" if rank else "ABSENT"
            if rank is None:
                absent.append(q.id)
            elif rank > 5:
                thin.append(q.id)
            if len(candidates) < settings.retrieval_candidate_pool:
                thin.append(q.id)

            filters = ",".join(f"{k}={v}" for k, v in (plan.filter or {}).items()) or "none"
            if widened:
                filters += " (widened)"
            print(f"{q.id:34} {len(candidates):>4} {status:>8} {rank or '-':>5}  {filters}")

    print("\n--- summary ---")
    print(f"target absent from the pool: {len(absent)}")
    for qid in absent:
        print(f"    {qid}")
    print(f"pool thinner than {settings.retrieval_candidate_pool} or target beyond rank 5: {len(set(thin))}")
    for qid in sorted(set(thin)):
        print(f"    {qid}")

    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())