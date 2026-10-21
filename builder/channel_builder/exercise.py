"""Teacher-written exercises -> Kolibri 0.15.5 Perseus exercise files.

A teacher writes `<title>.exercise.json` in a channel folder:

    {
      "title": "Adding fractions",            # optional, default: file name
      "description": "Five quick questions",  # optional
      "passage": "Tendai has a goat...",      # optional text shown above every question
      "mastery": {"m": 3, "n": 5},            # optional, or "do_all"; default below
      "randomize": true,                      # optional, shuffle question order
      "questions": [
        {"type": "choice", "question": "1/2 + 1/4 = ?",
         "choices": ["3/4", "2/6", "1/8"], "answer": "3/4", "hint": "Make the quarters."},
        {"type": "number", "question": "7 x 8 = ?", "answer": 56}
      ]
    }

- `choice`: multiple choice. `answer` is one choice (text or 0-based index) or a
  list of them (then the learner must tick all of them). 2-8 choices.
- `number`: numeric answer (int or decimal; "3/4" fractions are accepted too).
  `tolerance` (optional) allows +- that much.
- Text is Perseus markdown; `$...$` is TeX maths.

Mastery default: `m_of_n` with n = min(5, questions) and m = n - 1 (all of them
for 1-2 questions). Kolibri's mastery is per exercise: the learner keeps getting
questions until `m` of the last `n` are right, then the exercise is completed.

The output is the zip Kolibri's perseus viewer reads: `exercise.json` plus one
`<assessment_item_id>.json` per question (item ids are 32-hex, as Kolibri quizzes
require), with fixed zip timestamps so rebuilds give the same md5.
"""
import io
import json
import zipfile
from fractions import Fraction

MAX_QUESTIONS = 100


class ExerciseError(ValueError):
    pass


def _text(v, what):
    if not isinstance(v, (str, int, float)) or isinstance(v, bool) or str(v).strip() == "":
        raise ExerciseError("%s must be non-empty text" % what)
    return str(v).strip()


def _number(v, what):
    if isinstance(v, bool):
        raise ExerciseError("%s must be a number" % what)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            f = Fraction(v.strip().replace(",", "."))
        except (ValueError, ZeroDivisionError):
            raise ExerciseError("%s must be a number, got %r" % (what, v))
        return int(f) if f.denominator == 1 else float(f)
    raise ExerciseError("%s must be a number" % what)


ANSWER_AREA = {"calculator": False, "chi2Table": False, "periodicTable": False,
               "tTable": False, "zTable": False}


def _hints(q):
    h = q.get("hint")
    hs = h if isinstance(h, list) else ([h] if h else [])
    return [{"content": _text(x, "hint"), "images": {}, "widgets": {}} for x in hs]


def choice_item(q, passage, n):
    choices = q.get("choices")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 8:
        raise ExerciseError("question %d: 'choices' needs 2-8 entries" % n)
    choices = [_text(c, "question %d choice" % n) for c in choices]
    ans = q.get("answer")
    ans = ans if isinstance(ans, list) else [ans]
    correct = set()
    for a in ans:
        if isinstance(a, int) and not isinstance(a, bool) and 0 <= a < len(choices):
            correct.add(a)
        elif isinstance(a, (str, int, float)) and str(a).strip() in choices:
            correct.add(choices.index(str(a).strip()))
        else:
            raise ExerciseError("question %d: answer %r is not one of the choices" % (n, a))
    if not correct:
        raise ExerciseError("question %d: no answer" % n)
    widget = {"type": "radio", "alignment": "default", "static": False, "graded": True,
              "options": {"choices": [{"content": c, "correct": i in correct}
                                      for i, c in enumerate(choices)],
                          "randomize": bool(q.get("shuffle_choices", True)),
                          "multipleSelect": len(correct) > 1, "displayCount": None,
                          "hasNoneOfTheAbove": False, "onePerLine": True,
                          "deselectEnabled": False},
              "version": {"major": 1, "minor": 0}}
    return _item(passage, _text(q.get("question"), "question %d" % n), "radio 1", widget, q)


def number_item(q, passage, n):
    value = _number(q.get("answer"), "question %d answer" % n)
    tol = q.get("tolerance")
    tol = None if tol in (None, 0) else abs(_number(tol, "question %d tolerance" % n))
    widget = {"type": "numeric-input", "alignment": "default", "static": False, "graded": True,
              "options": {"static": False, "coefficient": False, "labelText": "", "size": "normal",
                          "answers": [{"value": value, "status": "correct", "message": "",
                                       "simplify": "optional", "strict": False, "maxError": tol}]},
              "version": {"major": 0, "minor": 0}}
    return _item(passage, _text(q.get("question"), "question %d" % n), "numeric-input 1", widget, q)


def _item(passage, question, wid, widget, q):
    content = (passage + "\n\n" if passage else "") + question + "\n\n[[\u2603 %s]]" % wid
    return {"question": {"content": content, "images": {}, "widgets": {wid: widget}},
            "answerArea": ANSWER_AREA, "itemDataVersion": {"major": 0, "minor": 1},
            "hints": _hints(q)}


KINDS = {"choice": choice_item, "multiple_choice": choice_item,
         "number": number_item, "numeric": number_item}


def mastery(spec, count):
    if spec in (None, ""):
        n = min(5, count)
        m = n if n <= 2 else n - 1
        return {"type": "m_of_n", "m": m, "n": n}
    if spec == "do_all":
        return {"type": "do_all"}
    if isinstance(spec, dict) and set(spec) <= {"type", "m", "n"} and spec.get("type", "m_of_n") == "m_of_n":
        m, n = spec.get("m"), spec.get("n")
        if all(isinstance(x, int) and not isinstance(x, bool) for x in (m, n)) and 1 <= m <= n:
            return {"type": "m_of_n", "m": m, "n": n}
    raise ExerciseError("mastery must be \"do_all\" or {\"m\": M, \"n\": N} with 1 <= M <= N")


def parse(data, default_title, item_id):
    """Validate an exercise dict. Returns dict(title, description, items, item_ids,
    mastery_model, randomize). item_id(i) gives the stable 32-hex id of question i."""
    if not isinstance(data, dict):
        raise ExerciseError("top level must be an object")
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ExerciseError("'questions' must be a non-empty list")
    if len(qs) > MAX_QUESTIONS:
        raise ExerciseError("at most %d questions" % MAX_QUESTIONS)
    passage = data.get("passage") or ""
    if passage:
        passage = _text(passage, "passage")
    items = []
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            raise ExerciseError("question %d must be an object" % i)
        kind = q.get("type", "choice" if "choices" in q else "number")
        if kind not in KINDS:
            raise ExerciseError("question %d: unknown type %r (use choice or number)" % (i, kind))
        items.append(KINDS[kind](q, passage, i))
    ids = [item_id(i) for i in range(len(items))]
    title = data.get("title")
    return dict(title=_text(title, "title") if title not in (None, "") else default_title,
                description=str(data.get("description") or "").strip(),
                items=items, item_ids=ids, mastery_model=mastery(data.get("mastery"), len(items)),
                randomize=bool(data.get("randomize", True)))


def perseus_zip(ex):
    mm = ex["mastery_model"]
    meta = {"all_assessment_items": ex["item_ids"],
            "assessment_mapping": {i: "perseus_question" for i in ex["item_ids"]},
            "mastery_model": mm["type"], "randomize": ex["randomize"]}
    if mm["type"] == "m_of_n":
        meta.update(m=mm["m"], n=mm["n"])
    files = [("exercise.json", meta)] + list(zip(("%s.json" % i for i in ex["item_ids"]), ex["items"]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, obj in files:
            z.writestr(zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0)),
                       json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return buf.getvalue()


def load(path, default_title, item_id):
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError) as e:
        raise ExerciseError("cannot read: %s" % e)
    except ValueError as e:
        raise ExerciseError("bad JSON: %s" % e)
    ex = parse(data, default_title, item_id)
    ex["perseus"] = perseus_zip(ex)
    return ex
