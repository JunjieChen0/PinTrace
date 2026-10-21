import csv
import json

import pytest

from pintrace import validity_goldset as golden


def ref(value, **fields):
    base = dict(value=value, kind="action", ref_type="full_sha" if len(value.rsplit("@", 1)[-1]) == 40 else "mutable_or_unresolved",
                action=value.rsplit("@", 1)[0].lower(), path=".github/workflows/ci.yml", job="build", step_index=0,
                step_id=None, step_name=None, line=10, comment_text=None, lineage_status="matched", configuration_coverage=dict(status="unknown"))
    base.update(fields)
    return base


def test_refs_normalizes_a_single_object_a_list_and_absence():
    assert golden._refs(None) == [] and golden._refs(dict(a=1)) == [dict(a=1)] and golden._refs([dict(a=1)]) == [dict(a=1)]


def test_visible_reference_hides_every_pipeline_judgement():
    visible = golden.visible_reference(ref("a/b@" + "a" * 40, comment_text="v1", step_name="Use"))
    assert set(visible) == {"path", "job", "step_index", "step_id", "step_name", "line", "value", "comment"}
    assert visible["comment"] == "v1" and visible["value"].endswith("a" * 40)
    assert "ref_type" not in visible and "kind" not in visible and "lineage_status" not in visible


@pytest.mark.parametrize("value,kind,ref_type,label", [
    ("a/b@" + "a" * 40, "action", "full_sha", "pinned"),
    ("a/b@v4", "action", "mutable_or_unresolved", "mutable"),
    ("./local", "local", None, "not_direct"),
    ("docker://alpine:3", "docker", None, "not_direct"),
    ("org/repo/.github/workflows/x.yml@v1", "reusable_workflow", None, "not_direct"),
    ("a/b@${{ matrix.ref }}", "dynamic", None, "not_direct"),
    ("a/b@" + "a" * 40, "action", "something_else", "unresolvable"),
])
def test_pipeline_reference_labels(value, kind, ref_type, label):
    assert golden.pipeline_reference_label(dict(value=value, kind=kind, ref_type=ref_type)) == label


@pytest.mark.parametrize("event,label", [
    ("pin", "pin"), ("unpin", "unpin"), ("sha_target_change", "sha_change"), ("mutable_update", "tag_change"),
    ("replace", "replace"), ("remove", "remove"), ("first_observed_action", "entry"), ("copied_reference", "entry"),
    ("reintroduced_action", "entry"), ("ambiguous_transition", "unknown"), ("unknown_boundary", "unknown"),
])
def test_pipeline_transition_labels(event, label):
    assert golden.pipeline_transition_label(event) == label


def test_kappa_matches_hand_computed_values():
    assert golden.kappa(["a", "a", "b", "b"], ["a", "a", "b", "b"]) == 1
    # observed 0.75, expected 0.5 -> kappa 0.5
    assert golden.kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == pytest.approx(0.0)
    assert golden.kappa(["a", "a", "a", "b"], ["a", "a", "b", "b"]) == pytest.approx((0.75 - 0.5) / 0.5)
    assert golden.kappa(["a", "a"], ["a", "a"]) is None, "a constant rater has no kappa"
    assert golden.kappa([], []) is None


def test_agreement_and_bootstrap_are_deterministic():
    first, second = ["pin"] * 6 + ["unknown"] * 4, ["pin"] * 5 + ["no_change"] + ["unknown"] * 3 + ["entry"]
    result = golden.agreement(first, second)
    assert result["items"] == 10 and result["agree"] == 8 and result["rate"] == pytest.approx(0.8)
    left, right = golden.bootstrap_agreement(first, second), golden.bootstrap_agreement(first, second)
    assert left == right and left["rate"][0] <= 0.8 <= left["rate"][1]


def event_row(event_id, event, before, after):
    return dict(event_id=event_id, repository_id="1", commit="c2", previous_commit="c1", payload=json.dumps(dict(event=event, before=before, after=after)))


def write_events(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["event_id", "repository_id", "commit", "previous_commit", "event", "before_lineages", "after_lineages", "payload"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row, before_lineages="[]", after_lineages="[]", event=json.loads(row["payload"])["event"]))


@pytest.fixture()
def project(tmp_path, monkeypatch):
    rows = [
        event_row("e1", "pin", ref("a/b@v4"), ref("a/b@" + "a" * 40)),
        event_row("e2", "unpin", ref("a/b@" + "a" * 40), ref("a/b@v4")),
        event_row("e3", "replace", ref("a/b@v4"), ref("c/d@" + "c" * 40)),
        event_row("e4", "ambiguous_transition", [ref("a/b@v4"), ref("a/b@v5")], [ref("a/b@" + "a" * 40), ref("a/b@" + "b" * 40)]),
        event_row("e5", "first_observed_action", None, ref("e/f@v1")),
        event_row("e6", "mutable_update", ref("a/b@v1", comment_text="v1"), ref("a/b@v2", comment_text="v2")),
    ]
    write_events(tmp_path / golden.EVENTS, rows)
    monkeypatch.setattr(golden, "REFERENCE_STRATA", dict(ambiguous_after=1, ambiguous_before=1, replace_after=1, entry_after=1, commented=1))
    monkeypatch.setattr(golden, "TRANSITION_STRATA", dict(pin=1, unpin=1, sha_target_change=1, mutable_update=1, replace=1, ambiguous_transition=1, entry=1))
    return tmp_path


def test_candidates_dedupe_a_reference_that_qualifies_for_two_strata(project):
    found = golden.candidates(project)
    keys = [(item["kind"], item["source"]["event_id"], item["source"]["step"]) for item in found]
    assert len(keys) == len(set(keys)), "a reference kept under two strata would be counted twice"
    assert any(item["stratum"] == "replace_after" for item in found) and not any(item["stratum"] == "commented" and item["source"]["event_id"] == "e3" for item in found)


def test_sampling_is_deterministic_and_reports_a_shortfall(project, monkeypatch):
    found = golden.candidates(project)
    first, shortfall = golden.sample(found)
    again, again_shortfall = golden.sample(list(reversed(found)))
    assert [golden.source_key(i) for i in first] == [golden.source_key(i) for i in again]
    assert shortfall == again_shortfall and shortfall["sha_target_change"] == 1
    monkeypatch.setattr(golden, "TRANSITION_STRATA", dict(pin=5), raising=False)
    _, short = golden.sample(found)
    assert short["pin"] == 4


def test_build_writes_two_orders_of_the_same_unique_items_without_leaking_labels(project, monkeypatch):
    monkeypatch.setattr(golden, "TRANSITION_STRATA", dict(pin=1))
    built = golden.build(project)
    first = json.loads((project / golden.BASE / "packet_round1.json").read_text(encoding="utf-8"))
    second = json.loads((project / golden.BASE / "packet_round2.json").read_text(encoding="utf-8"))
    ids = [item["id"] for item in first["items"]]
    assert len(ids) == len(set(ids)) and set(ids) == {item["id"] for item in second["items"]}
    assert [item["id"] for item in first["items"]] != [item["id"] for item in second["items"]]
    for item in first["items"]:
        for key in ("ref_type", "pipeline", "stratum", "lineage_status", "configuration_coverage"):
            assert key not in json.dumps(item), f"{key} must not appear in the packet"
    key = json.loads((project / golden.BASE / "answer_key.json").read_text(encoding="utf-8"))
    assert set(key) == set(ids) and {key[i]["kind"] for i in ids} <= {"reference", "transition"}


def test_load_labels_rejects_missing_extra_and_out_of_vocabulary_labels(project, monkeypatch):
    monkeypatch.setattr(golden, "TRANSITION_STRATA", dict(pin=1))
    golden.build(project)
    packet = project / golden.BASE / "packet_round1.json"
    items = json.loads(packet.read_text(encoding="utf-8"))["items"]
    ids = [item["id"] for item in items]
    good = {item["id"]: ("pinned" if item["kind"] == "reference" else "pin") for item in items}
    path = project / "labels.json"
    path.write_text(json.dumps(dict(labels=good)), encoding="utf-8")
    assert golden.load_labels(path, packet) == good
    path.write_text(json.dumps(dict(labels=dict(good, extra1="pin"))), encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        golden.load_labels(path, packet)
    broken = dict(good)
    broken[ids[0]] = "definitely_not_a_label"
    path.write_text(json.dumps(dict(labels=broken)), encoding="utf-8")
    with pytest.raises(ValueError, match="vocabulary"):
        golden.load_labels(path, packet)
    path.write_text(json.dumps(dict(labels={k: v for k, v in good.items() if k != ids[-1]})), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        golden.load_labels(path, packet)


def test_directional_separates_fabricated_missed_and_conservative():
    key = {"a": dict(kind="transition", pipeline="pin"), "b": dict(kind="transition", pipeline="pin"),
           "c": dict(kind="transition", pipeline="unknown"), "d": dict(kind="transition", pipeline="unknown"),
           "e": dict(kind="reference", pipeline="pinned")}
    labels = {"a": "pin", "b": "no_change", "c": "pin", "d": "no_change", "e": "mutable"}
    result = golden.directional(key, labels)
    assert result["items"] == 4
    assert result["fabricated"]["count"] == 1 and result["fabricated"]["items"][0]["id"] == "b"
    assert result["missed"]["count"] == 1 and result["missed"]["items"][0]["id"] == "c"
    assert result["conservative"]["count"] == 1 and result["conservative"]["items"][0]["id"] == "d"


def test_score_reports_confusion_per_kind_and_per_stratum():
    key = {"1": dict(kind="reference", pipeline="pinned", stratum="replace_after", source=dict(event_id="e", step="after")),
           "2": dict(kind="transition", pipeline="unknown", stratum="ambiguous_transition", source=dict(event_id="f", step="event"))}
    result = golden.score(key, {"1": "pinned", "2": "no_change"}, {"1": "pinned", "2": "no_change"})
    assert result["overall"]["round1_vs_pipeline"]["rate"] == 0.5
    assert result["by_kind"]["reference"]["round1_vs_pipeline"]["rate"] == 1.0
    assert result["by_stratum"]["ambiguous_transition"]["items"] == 1
    assert result["confusion"] == {"pinned -> pinned": 1, "unknown -> no_change": 1}
    assert result["disagreements"][0]["stratum"] == "ambiguous_transition"
    assert result["directional"]["round1"]["conservative"]["count"] == 1
