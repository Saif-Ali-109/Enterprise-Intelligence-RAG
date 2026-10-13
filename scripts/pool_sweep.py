"""How large must the candidate pool be for the gold set's targets to be reachable?

Measured 2026-10-13. With the pool at 12, seven of the 26 graded questions could not
reach their expected section at all — the target was not in the pool, so no reranker
could have rescued it. This sweeps the pool size and reports, per size, how many
targets enter it.

Stops before reranking, because the organisation's rerank quota is exhausted and
reranking is not what this measures: this is an upper bound on what reranking could
achieve at each pool size.

Usage:  PYTHONPATH=. python scripts/pool_sweep.py [12 24 48 96]
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
from app.retrieval.retriever import retrieve  # noqa: E402
from app.retrieval.vector_store import get_vector_store  # noqa: E402


def covers(candidate: object, expected: object) -> bool:
    return unit_covers_target(
        product=candidate.product,  # type: ignore[attr-defined]
        category=candidate.category,  # type: ignore[attr-defined]
        heading_path=list(candidate.heading_path),  # type: ignore[attr-defined]
        expected_product=expected.product,
        expected_category=expected.category,
        expected_heading_path=list(expected.heading_path),
    )


async def main() -> None:
    sizes = [int(a) for a in sys.argv[1:]] or [12, 24, 48, 96]
    dataset, _ = load_dataset()
    store = get_vector_store()
    factory = get_session_factory()
    questions = [q for q in dataset.questions if not q.expected.must_refuse and q.expected.heading_path]
    print(f"{len(questions)} graded questions; corpus pool sizes: {sizes}\n")
    print(f"{'pool':>5} {'reachable':>10} {'absent':>8}   first rank of each miss (worst last)")
    print("-" * 84)

    async with factory() as session:
        for size in sizes:
            reached = 0
            misses: list[tuple[int, str]] = []
            for q in questions:
                candidates = await retrieve(
                    q.question, session=session, store=store, analysis=None, top_k=size
                )
                rank = next((i + 1 for i, c in enumerate(candidates) if covers(c, q.expected)), None)
                if rank is None:
                    misses.append((0, q.id))
                else:
                    reached += 1
                    if rank > 6:
                        misses.append((rank, q.id))
            misses.sort()
            detail = ", ".join(f"{qid.split('-', 1)[-1]}@{r if r else 'absent'}" for r, qid in misses)
            print(f"{size:>5} {reached:>10} {len(misses):>8}   {detail[:60]}")

    print("\n'reachable' counts targets present anywhere in the pool; the miss list also")
    print("flags targets that arrived below rank 6, which reranking could still lift.")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())