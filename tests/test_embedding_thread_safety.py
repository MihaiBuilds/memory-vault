"""The shared embedding model tolerates concurrent use."""

import threading

import pytest

from memory_vault.services import embedding


def test_parallel_encode_calls_do_not_crash():
    """Many threads embedding at once complete without killing the process.

    Regression guard for #148. The model is a single shared object and its
    forward pass is not safe to run from several threads simultaneously — the
    failure mode is a segfault, not an exception, so an unguarded version takes
    the interpreter down instead of failing this assertion. Embedding runs in a
    worker thread on the async paths, which is how ordinary concurrent requests
    reach it in parallel.
    """
    embedding._get_model()  # Load once up front so this measures encode, not load.

    threads = 8
    start = threading.Barrier(threads)
    results: list[list[float]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        try:
            start.wait(timeout=60)
            vector = embedding.embed(f"concurrent embedding call number {n}")
            with lock:
                results.append(vector)
        except BaseException as exc:  # noqa: BLE001 - recorded and re-raised below
            with lock:
                errors.append(exc)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=120)

    assert not [t for t in workers if t.is_alive()], "an embedding thread hung"
    assert errors == [], f"concurrent embedding raised: {errors}"
    assert len(results) == threads
    assert all(len(v) == embedding.settings.embedding_dimensions for v in results)


def test_parallel_batch_and_single_encode_mix():
    """`embed` and `embed_batch` running together stay safe.

    Both reach the same shared model, so the guarantee has to cover them
    jointly rather than one at a time.
    """
    embedding._get_model()

    start = threading.Barrier(6)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def single(n: int) -> None:
        try:
            start.wait(timeout=60)
            embedding.embed(f"single {n}")
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    def batch(n: int) -> None:
        try:
            start.wait(timeout=60)
            embedding.embed_batch([f"batch {n} first", f"batch {n} second"])
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    workers = [threading.Thread(target=single, args=(i,)) for i in range(3)]
    workers += [threading.Thread(target=batch, args=(i,)) for i in range(3)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=120)

    assert not [t for t in workers if t.is_alive()], "an embedding thread hung"
    assert errors == [], f"mixed concurrent embedding raised: {errors}"


def test_concurrent_first_use_loads_one_model(monkeypatch):
    """A cold start under load constructs the model exactly once.

    The lazy load used to check and assign without a lock, so several threads
    arriving before the first load finished would each build their own
    SentenceTransformer. That wastes the load time and memory of every extra
    copy, and leaves whichever instance loses the assignment to be collected
    while callers may still be using it.
    """
    monkeypatch.setattr(embedding, "_model", None)

    constructed: list[str] = []
    real_ctor = embedding.SentenceTransformer

    def counting_ctor(*args, **kwargs):
        constructed.append(args[0] if args else "")
        return real_ctor(*args, **kwargs)

    monkeypatch.setattr(embedding, "SentenceTransformer", counting_ctor)

    threads = 6
    start = threading.Barrier(threads)
    models: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        start.wait(timeout=60)
        model = embedding._get_model()
        with lock:
            models.append(model)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=180)

    assert len(constructed) == 1, f"model was constructed {len(constructed)} times"
    assert len(models) == threads
    assert all(m is models[0] for m in models), "callers received different model objects"


@pytest.mark.parametrize("texts", [[], ["only one"]])
def test_embed_batch_edge_cases_still_work(texts):
    """Locking did not change the empty-input shortcut or ordinary batching."""
    result = embedding.embed_batch(texts)
    assert len(result) == len(texts)
