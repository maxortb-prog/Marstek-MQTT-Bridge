from input_averager import InputAverager


def test_zero_window_passes_through_unchanged():
    avg = InputAverager(window_s=0)
    assert avg.add(100, now=0.0) == 100
    assert avg.add(500, now=1.0) == 500


def test_averages_over_window():
    avg = InputAverager(window_s=10)
    assert avg.add(100, now=0.0) == 100
    assert avg.add(200, now=2.0) == 150  # (100+200)/2
    assert avg.add(300, now=4.0) == 200  # (100+200+300)/3


def test_old_samples_drop_out_of_window():
    avg = InputAverager(window_s=5)
    avg.add(100, now=0.0)
    avg.add(200, now=2.0)
    # bei t=10 sind beide alten Samples (t=0, t=2) laenger als 5s her -> raus
    result = avg.add(900, now=10.0)
    assert result == 900


def test_partial_window_expiry():
    avg = InputAverager(window_s=5)
    avg.add(100, now=0.0)   # faellt bei t=6 raus (6-0=6 > 5)
    avg.add(200, now=3.0)   # bleibt bei t=6 (6-3=3 <= 5)
    result = avg.add(300, now=6.0)
    assert result == 250  # (200+300)/2


def test_set_window_changes_behavior_live():
    avg = InputAverager(window_s=100)
    avg.add(100, now=0.0)
    avg.add(200, now=1.0)
    avg.set_window(0)
    # nach Umschalten auf 0 -> kein Mitteln mehr, Rohwert direkt durchreichen
    assert avg.add(999, now=2.0) == 999


def test_reset_clears_samples():
    avg = InputAverager(window_s=100)
    avg.add(100, now=0.0)
    avg.add(200, now=1.0)
    avg.reset()
    # nach reset: nur noch der neue Wert im Fenster
    assert avg.add(500, now=2.0) == 500


def test_sample_count():
    avg = InputAverager(window_s=10)
    avg.add(1, now=0.0)
    avg.add(2, now=1.0)
    avg.add(3, now=2.0)
    assert avg.sample_count() == 3
