from bots5.rendering import render_synthesis_user_message, render_worker_user_message


def test_worker_rendering_exact():
    assert render_worker_user_message([("a", "X\n"), ("b", "Y")]) == (
        "=== INPUT: a ===\nX\n\n=== END INPUT: a ===\n"
        "=== INPUT: b ===\nY\n=== END INPUT: b ==="
    )


def test_synthesis_rendering_exact():
    assert render_synthesis_user_message([("w1", "one"), ("w2", "two\n")]) == (
        "=== WORKER OUTPUT: w1 ===\none\n=== END WORKER OUTPUT: w1 ===\n"
        "=== WORKER OUTPUT: w2 ===\ntwo\n\n=== END WORKER OUTPUT: w2 ==="
    )
