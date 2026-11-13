from rsi.schedule import budget_table, edit_budget


def test_budget_table_matches_hand_computation():
    # T=4, b_min=1, b_max=3: t=0 -> 3; t=1 -> ceil(2.7071)=3;
    # t=2 -> 2; t=3 -> ceil(1.2929)=2.
    assert budget_table(4, 1, 3) == [3, 3, 2, 2]


def test_budget_is_nonincreasing_and_bounded():
    table = budget_table(20, 1, 4)
    assert table[0] == 4 and table[-1] == 2  # t only runs 0..T-1; t=T would be 1
    assert all(table[i] >= table[i + 1] for i in range(len(table) - 1))
    assert all(1 <= b <= 4 for b in table)


def test_zero_rounds_returns_b_max():
    assert edit_budget(0, 0, 1, 4) == 4
