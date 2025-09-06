def test_stop_never_decreases():
    last_high = 100.0
    trail = 2.0
    stop = last_high - trail  # 98
    last_high = 103.0
    stop_new = max(stop, last_high - trail)  # 101
    assert stop_new >= stop
    price = 100.0
    stop_new2 = max(stop_new, price - trail)
    assert stop_new2 == stop_new

