"""Exercise files (<title>.exercise.json) -> Kolibri Perseus exercises."""
import io
import json
import os
import shutil
import sqlite3
import zipfile

import pytest

from channel_builder import exercise as ex
from channel_builder.build import Builder, file_type, resource_title

HERE = os.path.dirname(os.path.abspath(__file__))
EXDIR = os.path.join(HERE, "exercises")


def ids(i):
    return "%032x" % (i + 1)


def test_file_type_and_title():
    assert file_type("Fractions.exercise.json") == "exercise.json"
    assert file_type("X.Exercise.JSON") == "exercise.json"
    assert file_type("channel.json") is None and file_type("a.json") is None
    assert file_type("a.pdf") == "pdf" and file_type("notes.docx") is None
    assert resource_title("Adding_fractions.exercise.json") == "Adding fractions"


def test_choice_and_number_items():
    e = ex.parse({"questions": [
        {"type": "choice", "question": "Q1", "choices": ["a", "b", "c"], "answer": "b", "hint": "h"},
        {"type": "number", "question": "Q2", "answer": "3/4", "tolerance": 0.01},
        {"question": "Q3", "choices": ["x", "y", "z"], "answer": [0, "z"]},
        {"question": "Q4", "answer": 12}]}, "Default", ids)
    r1 = e["items"][0]["question"]
    assert "[[\u2603 radio 1]]" in r1["content"]
    ch = r1["widgets"]["radio 1"]["options"]["choices"]
    assert [c["correct"] for c in ch] == [False, True, False]
    assert e["items"][0]["hints"][0]["content"] == "h"
    n = e["items"][1]["question"]["widgets"]["numeric-input 1"]["options"]["answers"][0]
    assert n["value"] == 0.75 and n["status"] == "correct" and n["maxError"] == 0.01
    multi = e["items"][2]["question"]["widgets"]["radio 1"]["options"]
    assert multi["multipleSelect"] and [c["correct"] for c in multi["choices"]] == [True, False, True]
    assert e["items"][3]["question"]["widgets"]["numeric-input 1"]["options"]["answers"][0]["value"] == 12
    assert e["title"] == "Default" and e["item_ids"] == [ids(i) for i in range(4)]
    assert e["mastery_model"] == {"type": "m_of_n", "m": 3, "n": 4}


def test_passage_on_every_question():
    e = ex.parse({"passage": "Once upon a time.", "questions": [
        {"question": "A?", "choices": ["1", "2"], "answer": 0},
        {"question": "B?", "choices": ["1", "2"], "answer": 1}]}, "R", ids)
    assert all(i["question"]["content"].startswith("Once upon a time.\n\n") for i in e["items"])
    assert e["mastery_model"] == {"type": "m_of_n", "m": 2, "n": 2}


@pytest.mark.parametrize("spec,want", [
    ("do_all", {"type": "do_all"}),
    ({"m": 3, "n": 5}, {"type": "m_of_n", "m": 3, "n": 5}),
    ({"type": "m_of_n", "m": 1, "n": 1}, {"type": "m_of_n", "m": 1, "n": 1}),
])
def test_mastery(spec, want):
    assert ex.mastery(spec, 5) == want


@pytest.mark.parametrize("data,msg", [
    ([], "top level"),
    ({}, "questions"),
    ({"questions": [{"choices": ["a"], "answer": "a", "question": "q"}]}, "2-8"),
    ({"questions": [{"choices": ["a", "b"], "answer": "c", "question": "q"}]}, "not one of"),
    ({"questions": [{"type": "number", "answer": "lots", "question": "q"}]}, "number"),
    ({"questions": [{"type": "essay", "question": "q"}]}, "unknown type"),
    ({"questions": [{"choices": ["a", "b"], "answer": "a"}]}, "question 1"),
    ({"mastery": {"m": 6, "n": 5}, "questions": [{"question": "q", "answer": 1}]}, "mastery"),
    ({"mastery": "most", "questions": [{"question": "q", "answer": 1}]}, "mastery"),
])
def test_invalid(data, msg):
    with pytest.raises(ex.ExerciseError, match=msg):
        ex.parse(data, "T", ids)


def test_perseus_zip_layout_and_deterministic():
    e = ex.load(os.path.join(EXDIR, "Fractions_quiz.exercise.json"), "F", ids)
    z = zipfile.ZipFile(io.BytesIO(e["perseus"]))
    names = sorted(z.namelist())
    assert names == sorted(["exercise.json"] + ["%s.json" % i for i in e["item_ids"]])
    meta = json.loads(z.read("exercise.json"))
    assert meta["all_assessment_items"] == e["item_ids"]
    assert meta["mastery_model"] == "m_of_n" and (meta["m"], meta["n"]) == (2, 3)
    item = json.loads(z.read("%s.json" % e["item_ids"][0]))
    assert set(item) == {"question", "answerArea", "itemDataVersion", "hints"}
    assert ex.load(os.path.join(EXDIR, "Fractions_quiz.exercise.json"), "F", ids)["perseus"] == e["perseus"]


def rows(db, sql):
    c = sqlite3.connect(db)
    r = c.execute(sql).fetchall()
    c.close()
    return r


def test_builder_writes_exercise_nodes(samples, tmp_path):
    src = tmp_path / "src"
    shutil.copytree(str(samples), str(src))
    shutil.copytree(EXDIR, str(src / "Practice"))
    r = Builder(str(src), str(tmp_path / "out"), "T", "Ms T", "ex-test").build()
    assert r["resources"] == 8 and r["exercises"] == 2
    assert "Practice/Broken.exercise.json" in r["skipped"]
    assert any("Broken.exercise.json" in w and "2-8" in w for w in r["warnings"])
    ex_nodes = rows(r["db"], "select id, title, description, learning_activities from content_contentnode "
                              "where kind='exercise' order by title")
    assert [(t, d) for _, t, d, _ in ex_nodes] == [("Fractions check", "Three quick fraction questions"),
                                                    ("Reading", "")]
    assert {a for *_, a in ex_nodes} == {"VwRCom7G"}  # learning activity: Practice
    am = rows(r["db"], "select contentnode_id, assessment_item_ids, number_of_assessments, mastery_model, "
                       "randomize from content_assessmentmetadata")
    by = {a[0]: a for a in am}
    assert set(by) == {i for i, *_ in ex_nodes}
    frac = by[ex_nodes[0][0]]
    assert len(json.loads(frac[1])) == frac[2] == 3
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in json.loads(frac[1]))
    assert json.loads(frac[3]) == {"type": "m_of_n", "m": 2, "n": 3}
    assert json.loads(by[ex_nodes[1][0]][3]) == {"type": "do_all"}
    files = rows(r["db"], "select f.preset, l.extension from content_file f join content_localfile l "
                          "on l.id=f.local_file_id join content_contentnode n on n.id=f.contentnode_id "
                          "where n.kind='exercise'")
    assert files == [("exercise", "perseus")] * 2
    # stable ids across rebuilds (quizzes refer to item ids)
    r2 = Builder(str(src), str(tmp_path / "out"), "T", "Ms T", "ex-test").build()
    assert rows(r2["db"], "select assessment_item_ids from content_assessmentmetadata order by id") == \
        rows(r["db"], "select assessment_item_ids from content_assessmentmetadata order by id")
