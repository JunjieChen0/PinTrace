from pintrace.ties import topological_winner, winner_tree_state


def test_equal_timestamp_uses_descendant_instead_of_lexical_commit_order():
    assert topological_winner(["z-parent", "a-child"], lambda a, b: (a, b) == ("z-parent", "a-child")) == "a-child"


def test_incomparable_branches_remain_unresolved():
    assert topological_winner(["left", "right"], lambda a, b: False) is None


def test_equal_commit_rows_need_tree_evidence_but_not_arbitrary_tie_break():
    assert topological_winner(["same", "same"], lambda a, b: False) == "same"


def test_later_deletion_marker_is_resolved_by_actual_path_absence():
    files = [{"source_marker": "deletion", "resolved_path": ".github/workflows/ci.yml", "tree_path_present": False}]
    assert winner_tree_state(files) == "deleted"
    assert winner_tree_state([dict(files[0], tree_path_present=True)]) is None
    assert winner_tree_state([dict(files[0], resolved_path="")]) is None
