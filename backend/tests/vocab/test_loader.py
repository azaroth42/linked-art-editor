"""``lae vocab import`` / ``export``: YAML ⇄ registry round trip and upsert rules (Step 2.1)."""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from lae.db.models import (
    Enforcement,
    EntityLabel,
    SlotVocabularyBinding,
    Vocabulary,
    VocabularyMembership,
    VocabularyTerm,
)
from lae.profiles.validation import validate_profiles_dir
from lae.vocab.loader import (
    VOCAB_DIR,
    VocabConfigError,
    export_to_yaml,
    load_from_yaml,
    read_vocab_dir,
    write_vocab_dir,
)
from tests.vocab.conftest import AAT, GENDER, NATIONALITY, OCCUPATION, Labels, fresh_uri

SEED_SETS = {
    "vocab:person_name_parts",
    "vocab:work_title_types",
    "vocab:identifier_types",
    "vocab:statement_types",
    "vocab:dimension_types",
    "vocab:measurement_units",
    "vocab:gender",
    "vocab:occupation",
    "vocab:nationality",
    "vocab:type_of_work",
    "vocab:style",
    "vocab:material",
}


def same_tree(left: Path, right: Path) -> bool:
    """True if both directories hold the same file names with byte-identical contents."""
    names = sorted(p.name for p in left.iterdir())
    if names != sorted(p.name for p in right.iterdir()):
        return False
    _, mismatch, errors = filecmp.cmpfiles(left, right, names, shallow=False)
    return not mismatch and not errors


def copy_seeds(tmp_path: Path, *names: str) -> Path:
    """A directory with the named seed files (all of them if none is named)."""
    target = tmp_path / "vocab"
    target.mkdir()
    for path in VOCAB_DIR.glob("*.yaml"):
        if not names or path.stem in names:
            shutil.copy(path, target / path.name)
    return target


# --- files only ---------------------------------------------------------------------------------


def test_the_seeds_define_every_value_set_the_guide_lists() -> None:
    config, files = read_vocab_dir(VOCAB_DIR)
    assert files == len(SEED_SETS)
    assert {value_set.id for value_set in config.value_sets} == SEED_SETS
    by_id = {value_set.id: value_set for value_set in config.value_sets}
    rules = {
        name: by_id[f"vocab:{name}"].rule
        for name in ("gender", "occupation", "nationality", "type_of_work", "style", "material")
    }
    assert rules["nationality"] and rules["nationality"]["metatype_uris"] == [NATIONALITY]
    assert rules["occupation"] and rules["occupation"]["metatype_uris"] == [OCCUPATION]
    assert rules["gender"] and rules["gender"]["metatype_uris"] == [GENDER]
    assert rules["style"] and rules["style"]["metatype_uris"] == [f"{AAT}300015646"]
    assert rules["type_of_work"] and rules["type_of_work"]["metatype_uris"] == [f"{AAT}300435443"]
    assert rules["material"] and rules["material"]["classes"] == ["Material"]
    for name in ("person_name_parts", "work_title_types", "identifier_types"):
        assert by_id[f"vocab:{name}"].terms


def test_spec_7_2_bindings() -> None:
    config, _ = read_vocab_dir(VOCAB_DIR)
    bound = {(b.entity_class, b.property_path, b.vocabulary_id) for b in config.bindings}
    person = {v for (c, p, v) in bound if (c, p) == ("Person", "classified_as")}
    assert person == {"vocab:gender", "vocab:occupation", "vocab:nationality"}
    assert (
        "Person",
        "identified_by[Name].part[Name].classified_as",
        "vocab:person_name_parts",
    ) in bound
    for cls in ("HumanMadeObject", "VisualItem", "LinguisticObject"):
        works = {v for (c, p, v) in bound if (c, p) == (cls, "classified_as")}
        assert works == {"vocab:type_of_work", "vocab:style"}
        assert (cls, "identified_by[Name].classified_as", "vocab:work_title_types") in bound


def test_every_value_set_the_hmo_profile_names_is_defined() -> None:
    report = validate_profiles_dir(VOCAB_DIR.parent)
    assert report.ok, report.errors
    assert not any("skipped the slot value_set" in note for note in report.notes)


def test_the_seed_files_are_in_canonical_export_form(tmp_path: Path) -> None:
    config, _ = read_vocab_dir(VOCAB_DIR)
    write_vocab_dir(config, tmp_path / "out")
    assert same_tree(VOCAB_DIR, tmp_path / "out")


def test_invalid_files_are_rejected_with_every_problem(tmp_path: Path) -> None:
    directory = copy_seeds(tmp_path, "gender", "occupation")
    (directory / "bad.yaml").write_text(
        "value_sets:\n"
        "  - id: vocab:gender\n"
        "    version: 1\n"
        "    label: {en: Again}\n"
        "    mode: enumerated\n"
        "    enforcement: strict\n"
        "    terms: [{uri: 'http://a.test/1'}, {uri: 'http://a.test/1'}]\n"
        "bindings:\n"
        "  - {class: Person, path: classified_as, value_set: vocab:gender}\n"
    )
    (directory / "worse.yaml").write_text("value_sets:\n  - id: Not An Id\n")
    (directory / "notes.txt").write_text("")
    with pytest.raises(VocabConfigError) as raised:
        read_vocab_dir(directory)
    errors = raised.value.errors
    assert any("notes.txt" in e and "unexpected" in e for e in errors)
    # Files are read in name order, so bad.yaml defines vocab:gender first.
    assert any(e.startswith("gender.yaml") and "already defined in bad.yaml" in e for e in errors)
    assert any(e.startswith("bad.yaml") and "duplicate term uri" in e for e in errors)
    assert any(e.startswith("gender.yaml") and "duplicate binding" in e for e in errors)
    assert any(e.startswith("worse.yaml: $.value_sets[0]") for e in errors)


# --- the database -------------------------------------------------------------------------------


async def test_import_then_export_reproduces_profiles_vocab(
    empty: None, db_session: AsyncSession, tmp_path: Path
) -> None:
    summary = await load_from_yaml(db_session, VOCAB_DIR)
    assert set(summary.created) == SEED_SETS and summary.bindings_created == summary.bindings
    assert summary.membership["vocab:person_name_parts"] == 5
    written = await export_to_yaml(db_session, tmp_path / "out")
    assert len(written) == len(SEED_SETS)
    assert same_tree(VOCAB_DIR, tmp_path / "out")


async def test_a_second_import_changes_nothing(empty: None, db_session: AsyncSession) -> None:
    await load_from_yaml(db_session, VOCAB_DIR)
    before = dict((await db_session.execute(select(Vocabulary.id, Vocabulary.updated_at))).all())
    again = await load_from_yaml(db_session, VOCAB_DIR)
    assert again.as_json()["unchanged"] == len(SEED_SETS)
    assert (again.created, again.updated, again.bindings_created, again.bindings_updated) == (
        [],
        [],
        0,
        0,
    )
    after = dict((await db_session.execute(select(Vocabulary.id, Vocabulary.updated_at))).all())
    assert after == before


async def test_a_changed_value_set_is_replaced_and_its_updated_at_moves(
    empty: None, db_session: AsyncSession, tmp_path: Path
) -> None:
    await load_from_yaml(db_session, VOCAB_DIR)
    stamp = select(Vocabulary.updated_at).where(Vocabulary.id == "vocab:identifier_types")
    first = await db_session.scalar(stamp)
    directory = copy_seeds(tmp_path, "identifier_types")
    path = directory / "identifier_types.yaml"
    text = path.read_text()
    text = text.replace("version: 1", "version: 2").replace("Call Number", "Shelf Mark")
    text = text.replace(
        "      - uri: http://vocab.getty.edu/aat/300456575\n"
        "        label:\n"
        "          en: Sort Value\n"
        "        scope_note:\n"
        "          en: A value used only to sort the entity with others.\n",
        "",
    )
    path.write_text(text)
    summary = await load_from_yaml(db_session, directory)
    assert summary.updated == ["vocab:identifier_types"] and summary.files == 1
    later = await db_session.scalar(stamp)
    assert first is not None and later is not None and later > first
    terms = (
        await db_session.scalars(
            select(VocabularyTerm)
            .where(VocabularyTerm.vocabulary_id == "vocab:identifier_types")
            .order_by(VocabularyTerm.position)
        )
    ).all()
    assert [t.position for t in terms] == [0, 1, 2, 3]
    assert terms[3].label_override == {"en": "Shelf Mark"}
    assert terms[0].flags == {"unique": True} and terms[0].is_default
    members = await db_session.scalar(
        select(func.count()).where(VocabularyMembership.vocabulary_id == "vocab:identifier_types")
    )
    assert members == 4
    # Every other Value Set is kept although the directory held only this one.
    assert await db_session.scalar(select(func.count(Vocabulary.id))) == len(SEED_SETS)


async def test_a_binding_change_moves_its_value_sets_updated_at(
    empty: None, db_session: AsyncSession, tmp_path: Path
) -> None:
    await load_from_yaml(db_session, VOCAB_DIR)
    stamp = select(Vocabulary.updated_at).where(Vocabulary.id == "vocab:gender")
    first = await db_session.scalar(stamp)
    directory = copy_seeds(tmp_path, "gender")
    path = directory / "gender.yaml"
    path.write_text(
        path.read_text().replace(
            "    value_set: vocab:gender\n",
            "    value_set: vocab:gender\n    enforcement: strict\n",
        )
        + "  - class: Group\n    path: classified_as\n    value_set: vocab:gender\n"
    )
    summary = await load_from_yaml(db_session, directory)
    assert (summary.updated, summary.bindings_created, summary.bindings_updated) == ([], 1, 1)
    later = await db_session.scalar(stamp)
    assert first is not None and later is not None and later > first
    bindings = dict(
        (
            await db_session.execute(
                select(SlotVocabularyBinding.entity_class, SlotVocabularyBinding.enforcement).where(
                    SlotVocabularyBinding.vocabulary_id == "vocab:gender"
                )
            )
        ).all()
    )
    assert bindings == {"Person": Enforcement.STRICT, "Group": None}


async def test_a_binding_may_name_a_value_set_only_in_the_database(
    empty: None, db_session: AsyncSession, tmp_path: Path
) -> None:
    await load_from_yaml(db_session, VOCAB_DIR)
    directory = tmp_path / "bindings"
    directory.mkdir()
    (directory / "extra.yaml").write_text(
        "bindings:\n  - class: Group\n    path: classified_as\n    value_set: vocab:nationality\n"
    )
    summary = await load_from_yaml(db_session, directory)
    assert (summary.value_sets, summary.bindings_created) == (0, 1)
    (directory / "extra.yaml").write_text(
        "bindings:\n  - class: Group\n    path: classified_as\n    value_set: vocab:nowhere\n"
    )
    with pytest.raises(VocabConfigError, match="vocab:nowhere"):
        await load_from_yaml(db_session, directory)


async def test_enumerated_terms_become_members_and_vocab_ids(
    db_engine: AsyncEngine, empty: None, labels: Labels
) -> None:
    accession = await labels.put(f"{AAT}300312355", "accession numbers")
    async with AsyncSession(db_engine) as session:
        await load_from_yaml(session, VOCAB_DIR)
        await session.commit()
    row = await labels.get(accession)
    assert row is not None and row.vocab_ids == ["vocab:identifier_types"]
    assert row.updated_at is not None


async def test_a_value_set_can_change_mode(
    empty: None, db_session: AsyncSession, tmp_path: Path
) -> None:
    await load_from_yaml(db_session, VOCAB_DIR)
    typed = fresh_uri()
    db_session.add(EntityLabel(uri=typed, type="Type", default_label="x", metatype_uris=[GENDER]))
    directory = copy_seeds(tmp_path, "gender")
    path = directory / "gender.yaml"
    text = path.read_text().replace("mode: rule", "mode: enumerated")
    start = text.index("    rule:\n")
    end = text.index("    terms:\n")
    path.write_text(text[:start] + text[end:])
    summary = await load_from_yaml(db_session, directory)
    assert summary.updated == ["vocab:gender"] and summary.membership == {"vocab:gender": 2}
    members = set(
        await db_session.scalars(
            select(VocabularyMembership.uri).where(
                VocabularyMembership.vocabulary_id == "vocab:gender"
            )
        )
    )
    assert members == {f"{AAT}300189557", f"{AAT}300189559"}
