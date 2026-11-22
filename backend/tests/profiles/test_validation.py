"""Tests for UI Profile and Value Set validation (Phase 0 Step 0.12)."""

import copy
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from lae.profiles.validation import ValidationReport, load_yaml, validate_profiles_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "profiles"
AAT = "http://vocab.getty.edu/aat/"

#: Value Sets for every id HumanMadeObject.yaml references, plus bindings.
VOCAB: dict[str, Any] = {
    "value_sets": [
        {
            "id": "vocab:work_title_types",
            "version": 1,
            "label": {"en": "Title types", "fr": "Types de titre"},
            "scope_note": {"en": "Kinds of title for works."},
            "mode": "enumerated",
            "enforcement": "extensible",
            "terms": [
                {"uri": f"{AAT}300404670", "label": {"en": "Primary Name"}, "is_default": True},
                {"uri": f"{AAT}300417227", "deprecated": True},
            ],
        },
        {
            "id": "vocab:identifier_types",
            "version": 2,
            "label": {"en": "Identifier types"},
            "mode": "enumerated",
            "enforcement": "strict",
            "terms": [{"uri": f"{AAT}300312355", "flags": {"unique": True}}],
        },
        {
            "id": "vocab:statement_types",
            "version": 1,
            "label": {"en": "Statement types"},
            "mode": "enumerated",
            "enforcement": "extensible",
            "terms": [{"uri": f"{AAT}300435416", "is_default": True, "scope_note": {"en": "x"}}],
        },
        {
            "id": "vocab:dimension_types",
            "version": 1,
            "label": {"en": "Dimension types"},
            "mode": "enumerated",
            "enforcement": "extensible",
            "terms": [{"uri": f"{AAT}300055644"}, {"uri": f"{AAT}300055647"}],
        },
        {
            "id": "vocab:type_of_work",
            "version": 1,
            "label": {"en": "Type of work"},
            "mode": "rule",
            "enforcement": "advisory",
            "rule": {
                "metatype_uris": [f"{AAT}300435443"],
                "broader_roots": [],
                "exclusions": [],
                "classes": [],
            },
            "terms": [{"uri": f"{AAT}300033618"}],
        },
        {
            "id": "vocab:material",
            "version": 1,
            "label": {"en": "Materials"},
            "mode": "rule",
            "enforcement": "extensible",
            "rule": {
                "metatype_uris": [],
                "broader_roots": [],
                "exclusions": [],
                "classes": ["Material"],
            },
        },
    ],
    "bindings": [
        {
            "class": "HumanMadeObject",
            "path": "identified_by[Name].classified_as",
            "value_set": "vocab:work_title_types",
        },
        {
            "class": "HumanMadeObject",
            "path": "classified_as",
            "value_set": "vocab:type_of_work",
            "enforcement": "strict",
        },
        {"class": "HumanMadeObject", "path": "made_of", "value_set": "vocab:material"},
    ],
}

#: A JSON-LD context holding every term HumanMadeObject.yaml uses, one of them scoped.
CONTEXT: dict[str, Any] = {
    "@context": {
        "@version": 1.1,
        "crm": "http://www.cidoc-crm.org/cidoc-crm/",
        "identified_by": {"@id": "crm:P1_is_identified_by", "@container": "@set"},
        "classified_as": {"@id": "crm:P2_has_type", "@container": "@set"},
        "carried_out_by": {"@id": "crm:P14_carried_out_by"},
        "timespan": {"@id": "crm:P4_has_time-span"},
        "took_place_at": {"@id": "crm:P7_took_place_at"},
        "made_of": {"@id": "crm:P45_consists_of"},
        "dimension": {"@id": "crm:P43_has_dimension"},
        "referred_to_by": {"@id": "crm:P67i_is_referred_to_by"},
        "Name": "crm:E33_E41_Linguistic_Appellation",
        "Identifier": "crm:E42_Identifier",
        "LinguisticObject": "crm:E33_Linguistic_Object",
        "HumanMadeObject": {
            "@id": "crm:E22_Human-Made_Object",
            "@context": {"produced_by": {"@id": "crm:P108i_was_produced_by"}},
        },
    }
}


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    """An isolated profiles/ tree: both schemas and HumanMadeObject.yaml; no vocab, no context."""
    target = tmp_path / "profiles"
    for name in ("ui/_schema.json", "ui/HumanMadeObject.yaml", "vocab/_schema.json"):
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PROFILES / name, target / name)
    (target / "context").mkdir()
    return target


def read(path: Path) -> Any:
    return load_yaml(path)


def write(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def hmo(profiles: Path) -> dict[str, Any]:
    data: dict[str, Any] = read(profiles / "ui" / "HumanMadeObject.yaml")
    return data


def save_hmo(profiles: Path, data: dict[str, Any]) -> None:
    write(profiles / "ui" / "HumanMadeObject.yaml", data)


def slot(data: dict[str, Any], slot_id: str) -> dict[str, Any]:
    for section in data["sections"]:
        for candidate in section["slots"]:
            if candidate["id"] == slot_id:
                found: dict[str, Any] = candidate
                return found
    raise KeyError(slot_id)


def errors_of(profiles: Path) -> list[str]:
    report = validate_profiles_dir(profiles)
    assert not report.ok, "expected validation errors"
    return report.errors


def assert_error(profiles: Path, *fragments: str) -> None:
    errors = errors_of(profiles)
    assert any(all(f in error for f in fragments) for error in errors), errors


# --- the repository's own profiles -------------------------------------------------------------


def test_repository_profiles_are_valid() -> None:
    report = validate_profiles_dir(PROFILES)
    assert report.errors == []
    assert report.files_checked >= 1


def test_human_made_object_has_the_core_slots() -> None:
    data = hmo(PROFILES)
    assert data["class"] == "HumanMadeObject"
    paths = {s["id"]: s["path"] for section in data["sections"] for s in section["slots"]}
    assert paths == {
        "title": "identified_by[Name]",
        "identifiers": "identified_by[Identifier]",
        "type_of_work": "classified_as",
        "creator": "produced_by.carried_out_by",
        "production_date": "produced_by.timespan",
        "production_place": "produced_by.took_place_at",
        "materials": "made_of",
        "dimensions": "dimension",
        "description": "referred_to_by[LinguisticObject]",
    }


def test_skipped_checks_are_reported_as_notes(profiles: Path) -> None:
    report = validate_profiles_dir(profiles)
    assert report.ok, report.errors
    assert any("value_set reference check" in note for note in report.notes)
    assert any("path term check" in note for note in report.notes)


# --- LangMap ------------------------------------------------------------------------------------


def test_bare_string_label_fails(profiles: Path) -> None:
    """The guide's Verify: breaking a label into a bare string makes validation fail."""
    data = hmo(profiles)
    slot(data, "title")["label"] = "Title"
    save_hmo(profiles, data)
    assert_error(profiles, "$.sections[0].slots[0].label", "'Title' is not of type 'object'")


def test_bare_string_label_fails_through_the_script(profiles: Path) -> None:
    script = load_script()
    assert script.main(["--profiles-dir", str(profiles)]) == 0
    text = (profiles / "ui" / "HumanMadeObject.yaml").read_text(encoding="utf-8")
    broken = text.replace("    label:\n      en: Production\n", "    label: Production\n", 1)
    assert broken != text
    (profiles / "ui" / "HumanMadeObject.yaml").write_text(broken, encoding="utf-8")
    assert script.main(["--profiles-dir", str(profiles)]) == 1


def test_langmap_requires_en(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "title")["label"] = {"fr": "Titre"}
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].label", "'en' is a required property")


@pytest.mark.parametrize(
    "tag",
    ["en-GB", "fr", "zh-Hant-TW", "sr-Latn", "es-419", "de-CH-1901", "und", "gsw", "en-US-x-twain"],
)
def test_langmap_accepts_bcp47_tags(profiles: Path, tag: str) -> None:
    data = hmo(profiles)
    slot(data, "title")["label"][tag] = "x"
    save_hmo(profiles, data)
    assert validate_profiles_dir(profiles).ok


@pytest.mark.parametrize("tag", ["EN", "en_GB", "en-gb", "english", "zh-hant", "e", "en-", ""])
def test_langmap_rejects_malformed_tags(profiles: Path, tag: str) -> None:
    data = hmo(profiles)
    slot(data, "title")["label"][tag] = "x"
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].label", "is not a BCP-47 language tag")


@pytest.mark.parametrize("value", ["", "   ", 3, None])
def test_langmap_rejects_blank_or_non_string_values(profiles: Path, value: Any) -> None:
    data = hmo(profiles)
    slot(data, "title")["help_text"] = {"en": value}
    save_hmo(profiles, data)
    errors_of(profiles)


def test_unquoted_norwegian_key_is_rejected(profiles: Path) -> None:
    path = profiles / "ui" / "HumanMadeObject.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("en: Title\n", "en: Title\n          no: Tittel\n", 1))
    assert_error(profiles, "invalid YAML", "is not a string")
    path.write_text(text.replace("en: Title\n", "en: Title\n          'no': Tittel\n", 1))
    assert validate_profiles_dir(profiles).ok


def test_duplicate_yaml_key_is_rejected(profiles: Path) -> None:
    path = profiles / "ui" / "HumanMadeObject.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("version: 1\n", "version: 1\nversion: 2\n", 1))
    assert_error(profiles, "invalid YAML", "duplicate mapping key 'version'")


# --- UI Profile structure -----------------------------------------------------------------------


def test_unknown_widget_fails(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "title")["widget"] = "richtext"
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].widget", "'richtext' is not one of")


@pytest.mark.parametrize("key", ["multiple", "required", "advanced", "path", "widget", "id"])
def test_slot_requires_its_core_keys(profiles: Path, key: str) -> None:
    data = hmo(profiles)
    del slot(data, "title")[key]
    save_hmo(profiles, data)
    assert_error(profiles, f"'{key}' is a required property")


def test_unknown_slot_key_fails(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "title")["lable"] = {"en": "Title"}
    save_hmo(profiles, data)
    assert_error(profiles, "Additional properties are not allowed ('lable' was unexpected)")


def test_entity_ref_requires_allowed_classes(profiles: Path) -> None:
    data = hmo(profiles)
    del slot(data, "creator")["allowed_classes"]
    save_hmo(profiles, data)
    assert_error(profiles, "'allowed_classes' is a required property")


def test_allowed_classes_only_on_entity_ref(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "title")["allowed_classes"] = ["Person"]
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0]:", "'allowed_classes' is not allowed here")


def test_allowed_classes_must_be_record_classes(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "creator")["allowed_classes"] = ["Person", "Name"]
    save_hmo(profiles, data)
    assert_error(profiles, "allowed_classes[1]", "'Name' is not one of")


def test_classification_requires_value_set(profiles: Path) -> None:
    data = hmo(profiles)
    del slot(data, "type_of_work")["value_set"]
    save_hmo(profiles, data)
    assert_error(profiles, "'value_set' is a required property")


@pytest.mark.parametrize(
    "path", ["identified_by[0].content", "produced_by/timespan", "Identified_by", "a..b"]
)
def test_malformed_slot_path_fails(profiles: Path, path: str) -> None:
    data = hmo(profiles)
    slot(data, "title")["path"] = path
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].path", "is not a profile path")


@pytest.mark.parametrize("path", ["identified_by[Name].type", "produced_by.id"])
def test_path_may_not_name_id_or_type(profiles: Path, path: str) -> None:
    data = hmo(profiles)
    slot(data, "title")["path"] = path
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].path", "cannot appear in a path")


def test_value_set_id_needs_vocab_prefix(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "title")["value_set"] = "work_title_types"
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].value_set", "is not a Value Set id")


def test_class_must_match_file_name(profiles: Path) -> None:
    shutil.copy(profiles / "ui" / "HumanMadeObject.yaml", profiles / "ui" / "Person.yaml")
    assert_error(profiles, "ui/Person.yaml", "must match the file name")


def test_unknown_class_fails(profiles: Path) -> None:
    data = hmo(profiles)
    data["class"] = "Painting"
    save_hmo(profiles, data)
    assert_error(profiles, "$.class", "'Painting' is not one of")


def test_duplicate_slot_and_section_ids_fail(profiles: Path) -> None:
    data = hmo(profiles)
    slot(data, "identifiers")["id"] = "title"
    data["sections"][1]["id"] = data["sections"][0]["id"]
    save_hmo(profiles, data)
    assert_error(profiles, "duplicate slot id 'title'")
    assert_error(profiles, "duplicate section id 'identification'")


def test_grid_column_must_name_a_slot(profiles: Path) -> None:
    data = hmo(profiles)
    data["grid_columns"].append({"slot": "provenance", "label": {"en": "Provenance"}})
    save_hmo(profiles, data)
    assert_error(profiles, "grid_columns[5].slot", "unknown slot 'provenance'")


def test_section_may_be_advanced(profiles: Path) -> None:
    data = hmo(profiles)
    data["sections"][2]["advanced"] = True
    save_hmo(profiles, data)
    assert validate_profiles_dir(profiles).ok


@pytest.mark.parametrize("name", ["Person.json", "Person.yml", "notes.txt"])
def test_unexpected_files_fail(profiles: Path, name: str) -> None:
    (profiles / "ui" / name).write_text("{}\n", encoding="utf-8")
    assert_error(profiles, f"ui/{name}", "unexpected entry")


def test_missing_schema_fails(profiles: Path) -> None:
    (profiles / "vocab" / "_schema.json").unlink()
    assert_error(profiles, "vocab/_schema.json", "schema file is missing")


def test_shared_definitions_must_not_drift(profiles: Path) -> None:
    schema_path = profiles / "vocab" / "_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["$defs"]["LangMap"]["required"] = []
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    assert_error(profiles, "$defs.LangMap differs")


# --- Value Sets and bindings --------------------------------------------------------------------


def write_vocab(profiles: Path, vocab: dict[str, Any], name: str = "test_sets.yaml") -> None:
    write(profiles / "vocab" / name, vocab)


def test_vocab_file_validates_and_enables_reference_check(profiles: Path) -> None:
    write_vocab(profiles, VOCAB)
    report = validate_profiles_dir(profiles)
    assert report.errors == []
    assert not any("value_set reference check" in note for note in report.notes)


def test_slot_value_set_must_exist_once_vocab_exists(profiles: Path) -> None:
    vocab = copy.deepcopy(VOCAB)
    vocab["value_sets"] = [s for s in vocab["value_sets"] if s["id"] != "vocab:material"]
    vocab["bindings"] = [b for b in vocab["bindings"] if b["value_set"] != "vocab:material"]
    write_vocab(profiles, vocab)
    assert_error(profiles, "HumanMadeObject.yaml", "'vocab:material' is not defined")


def test_invalid_vocab_file_suppresses_reference_check(profiles: Path) -> None:
    write_vocab(profiles, {"value_sets": [{"id": "vocab:x"}]})
    errors = errors_of(profiles)
    assert errors and all("test_sets.yaml" in error for error in errors)


def test_value_sets_and_bindings_may_live_in_separate_files(profiles: Path) -> None:
    write_vocab(profiles, {"value_sets": VOCAB["value_sets"]}, "sets.yaml")
    write_vocab(profiles, {"bindings": VOCAB["bindings"]}, "bindings.yaml")
    assert validate_profiles_dir(profiles).ok


@pytest.mark.parametrize("vocab", [{}, {"value_sets": []}, {"bindings": []}, [], None])
def test_empty_vocab_file_fails(profiles: Path, vocab: Any) -> None:
    write_vocab(profiles, vocab)
    errors_of(profiles)


def mutate_set(profiles: Path, index: int, **changes: Any) -> None:
    vocab = copy.deepcopy(VOCAB)
    for key, value in changes.items():
        if value is None:
            vocab["value_sets"][index].pop(key, None)
        else:
            vocab["value_sets"][index][key] = value
    write_vocab(profiles, vocab)


def test_enumerated_set_requires_terms(profiles: Path) -> None:
    mutate_set(profiles, 0, terms=None)
    assert_error(profiles, "value_sets[0]", "'terms' is a required property")


def test_enumerated_set_rejects_rule(profiles: Path) -> None:
    mutate_set(profiles, 0, rule=VOCAB["value_sets"][4]["rule"])
    assert_error(profiles, "value_sets[0]:", "'rule' is not allowed here")


def test_rule_set_requires_rule(profiles: Path) -> None:
    mutate_set(profiles, 4, rule=None)
    assert_error(profiles, "value_sets[4]", "'rule' is a required property")


def test_rule_needs_a_non_empty_criterion(profiles: Path) -> None:
    rule = {"metatype_uris": [], "broader_roots": [], "exclusions": [f"{AAT}1"], "classes": []}
    mutate_set(profiles, 4, rule=rule)
    assert_error(profiles, "value_sets[4].rule", "is not valid under any of the given schemas")


def test_rule_requires_all_keys(profiles: Path) -> None:
    mutate_set(profiles, 4, rule={"metatype_uris": [f"{AAT}300435443"]})
    assert_error(profiles, "value_sets[4].rule", "'broader_roots' is a required property")


def test_rule_classes_are_concept_classes(profiles: Path) -> None:
    rule = {"metatype_uris": [], "broader_roots": [], "exclusions": [], "classes": ["Person"]}
    mutate_set(profiles, 5, rule=rule)
    assert_error(profiles, "value_sets[5].rule.classes[0]", "'Person' is not one of")


@pytest.mark.parametrize("field", ["version", "label", "mode", "enforcement", "id"])
def test_value_set_requires_its_keys(profiles: Path, field: str) -> None:
    mutate_set(profiles, 1, **{field: None})
    assert_error(profiles, f"'{field}' is a required property")


@pytest.mark.parametrize(
    "uri", ["aat:300404670", "300404670", "urn:x", "http://vocab.getty.edu/aat/3 00"]
)
def test_term_uri_must_be_absolute_http(profiles: Path, uri: str) -> None:
    mutate_set(profiles, 2, terms=[{"uri": uri}])
    assert_error(profiles, "value_sets[2].terms[0].uri", "is not an absolute http(s) URI")


def test_deprecated_term_cannot_be_default(profiles: Path) -> None:
    mutate_set(profiles, 3, terms=[{"uri": f"{AAT}1", "deprecated": True, "is_default": True}])
    assert_error(profiles, "value_sets[3].terms[0].is_default")


def test_unknown_term_flag_fails(profiles: Path) -> None:
    mutate_set(profiles, 1, terms=[{"uri": f"{AAT}1", "flags": {"uniq": True}}])
    assert_error(profiles, "value_sets[1].terms[0].flags", "'uniq' was unexpected")


def test_duplicate_term_uri_fails(profiles: Path) -> None:
    mutate_set(profiles, 3, terms=[{"uri": f"{AAT}1"}, {"uri": f"{AAT}1"}])
    assert_error(profiles, "value_sets[3].terms", "duplicate term uri")


def test_duplicate_value_set_id_across_files_fails(profiles: Path) -> None:
    write_vocab(profiles, VOCAB)
    write_vocab(profiles, {"value_sets": [VOCAB["value_sets"][0]]}, "again.yaml")
    assert_error(profiles, "'vocab:work_title_types' is already defined")


def test_binding_must_name_a_defined_value_set(profiles: Path) -> None:
    vocab = copy.deepcopy(VOCAB)
    vocab["bindings"].append({"class": "Person", "path": "classified_as", "value_set": "vocab:x"})
    write_vocab(profiles, vocab)
    assert_error(profiles, "bindings[3].value_set", "unknown Value Set 'vocab:x'")


def test_duplicate_binding_fails(profiles: Path) -> None:
    vocab = copy.deepcopy(VOCAB)
    vocab["bindings"].append(dict(vocab["bindings"][0]))
    write_vocab(profiles, vocab)
    assert_error(profiles, "bindings[3]", "duplicate binding")


def test_several_value_sets_may_bind_to_one_path(profiles: Path) -> None:
    """Spec section 7.2: Gender, Occupation, and Nationality all bind to Person classified_as."""
    vocab = copy.deepcopy(VOCAB)
    for set_id in ("vocab:type_of_work", "vocab:material"):
        vocab["bindings"].append({"class": "Person", "path": "classified_as", "value_set": set_id})
    write_vocab(profiles, vocab)
    assert validate_profiles_dir(profiles).ok


def test_binding_enforcement_override_is_checked(profiles: Path) -> None:
    vocab = copy.deepcopy(VOCAB)
    vocab["bindings"][0]["enforcement"] = "mandatory"
    write_vocab(profiles, vocab)
    assert_error(profiles, "bindings[0].enforcement", "'mandatory' is not one of")


# --- @context term check ------------------------------------------------------------------------


def write_context(profiles: Path) -> None:
    (profiles / "context" / "v1.json").write_text(json.dumps(CONTEXT), encoding="utf-8")


def test_paths_use_context_terms(profiles: Path) -> None:
    write_context(profiles)
    report = validate_profiles_dir(profiles)
    assert report.errors == []
    assert not any("path term check" in note for note in report.notes)


def test_unknown_path_term_fails(profiles: Path) -> None:
    write_context(profiles)
    data = hmo(profiles)
    slot(data, "materials")["path"] = "made_off"
    slot(data, "description")["path"] = "referred_to_by[LinguisticObjekt]"
    save_hmo(profiles, data)
    assert_error(profiles, "slots[0].path", "'made_off' is not a Linked Art term")
    assert_error(profiles, "slots[0].path", "'LinguisticObjekt' is not a Linked Art class")


def test_unknown_binding_path_term_fails(profiles: Path) -> None:
    write_context(profiles)
    vocab = copy.deepcopy(VOCAB)
    vocab["bindings"][2]["path"] = "consists_of"
    write_vocab(profiles, vocab)
    assert_error(profiles, "bindings[2].path", "'consists_of' is not a Linked Art term")


# --- helpers ------------------------------------------------------------------------------------


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location(
        "validate_profiles", REPO_ROOT / "scripts" / "validate_profiles.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_reports_ok_on_repository_profiles(capsys: pytest.CaptureFixture[str]) -> None:
    assert load_script().main([]) == 0
    assert "OK:" in capsys.readouterr().out


def test_report_ok_property() -> None:
    assert ValidationReport().ok
    assert not ValidationReport(errors=["x"]).ok
